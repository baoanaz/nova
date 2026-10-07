"""service 侧 MCP 端点（TASK-040；R38 裁定：M2a 由 service 直出 Streamable HTTP）。

为什么 MCP 在 service 里（而不是 Rust client）：Module/05 原设计把 MCP 放在 client，前提是
**服务在远端**（client 要扫描本地代码、算 hash、上传 blob）。本地单用户模式下 service 与代码
同机同文件系统，同步环节整体消失（TASK-034 改为服务端懒重扫），client 的厚同步层没有存在理由；
Rust client 保留给 M2c 的远端场景。

本模块只做三件事（薄壳，D-34）：

1. **协议装配**：``MCPServer``（``mcp.server.mcpserver``；2.x 起 FastMCP 更名为此）+
   两个工具（CF-06 冻结）+ 挂到 FastAPI 的 ``/mcp``（见 :func:`mount`）；
2. **参数与错误映射**：Schema 校验由 CF-06 的 inputSchema 表达；预期内的失败一律
   :class:`ToolError`（``isError=true`` + 人类可读文本，Module/05 §2.2），不让裸异常变成
   协议层 crash；
3. **阻塞调用隔离**：core 的检索是同步阻塞的（~0.5s，大仓库更久），一律经
   :func:`starlette.concurrency.run_in_threadpool`，**不在事件循环里跑**。

检索/组装/渲染**一律复用下游**：检索走 ``EngineManager.search``（= core），meta 走 TASK-032 的
:func:`nova_service.packmeta.pack_meta`，Markdown 走 core 的 ``render_markdown``。
本文件不出现任何检索、排序、装填或渲染逻辑（render_markdown 的规则与合同强耦合，只此一份）。

与 HTTP 面的关系：``/api/query/*`` 是 REST 面，``/mcp`` 是 MCP 面，两者共用同一套 core 调用与
meta；区别只在返回形态（REST 返回 ``{markdown, meta}``，MCP 返回文本）与错误形态（CF-05 信封
vs ``isError``）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar, Token
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Protocol

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool
from nova_core.contextpack import render_markdown
from nova_core.engine import (
    EngineError,
    repo_identity,
)
from nova_core.engine import (
    project_id_for as engine_project_id_for,
)
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from nova_service.answer import AnswerError, answer_question
from nova_service.auth import local_user
from nova_service.config import Settings
from nova_service.deps import project_not_found_message, require_ownership_of
from nova_service.errors import (
    PROVIDER_UNAVAILABLE_HINT,
    ApiError,
    map_engine_error,
)
from nova_service.llmconfig import provider_for_request
from nova_service.logging import get_logger, redact_text
from nova_service.metadb import MetaDB
from nova_service.packmeta import pack_meta
from nova_service.quota import append_warning, warning_for
from nova_service.routers.query import (
    ASK_MAX_TOKENS,
    DEFAULT_MAX_TOKENS,
    DEGRADED_NOTICE,
    INSUFFICIENT_NOTICE,
    LLM_FAILED_NOTICE,
    MAX_MAX_TOKENS,
    MAX_QUERY_CHARS,
)
from nova_service.runtime import EngineManager

__all__ = [
    "MCP_MOUNT_PATH",
    "MCP_SERVER_NAME",
    "ASK_TOOL",
    "SEARCH_TOOL",
    "TOOL_NAMES",
    "bind_user",
    "build_mcp",
    "current_user",
    "manager_for_app",
    "mount",
    "reset_user",
    "session_lifespan",
]

logger = get_logger("nova_service.mcp")

#: 挂载点（编辑器 URL 为 ``http://<host>:<port>/mcp``）。
MCP_MOUNT_PATH = "/mcp"
#: ``initialize`` 里的 server 名（编辑器侧显示用）。
MCP_SERVER_NAME = "nova"
#: ``search_context`` 的 ``max_tokens`` 上限（**CF-06 冻结值**：16000）。
MAX_TOKENS_LIMIT = 16_000
#: 对 AI **隐藏**的工具参数（TASK-MCP-BUDGET）：仍可接收，但不进 ``inputSchema``。
#:
#: 为什么隐藏而不是删除：预算是**服务端按证据密度决定的**，不是调用方该猜的；
#: 实测 AI 会传一个偏小的值（如 8000/10000）把长函数截断，而这恰恰毁掉它自己要的链路完整性。
#: 保留入参是为了向后兼容（老编辑器/脚本仍在发），删掉声明则是“不让它选”。
HIDDEN_PARAMS = frozenset({"max_tokens"})

SEARCH_TOOL = "search_context"
ASK_TOOL = "ask_project"
#: CF-06 冻结的两个工具名（测试断言"恰好这两个"）。
TOOL_NAMES: frozenset[str] = frozenset({SEARCH_TOOL, ASK_TOOL})

#: 工具 description（CF-06 的文案：description 是"行为控制"，含与 grep/read 的分工边界）。
#:
#: TASK-102：两个工具的分工按**答案形态**划分（不再是"深/浅"），并按真实失败模式写清"什么时候
#: 该用哪个、拿到结果后该怎么走"。文案改动不改工具名/参数/类型（description 属可打磨面）。
#:
#: TASK-107：面向"Agent 自主选择工具"重写。三处关键改动：
#: ① 把"**什么时候不要用我**"提到显眼位置（Agent 的决策成本主要在排除，不在理解功能）；
#: ② 修正与实现不符的字段名——MCP 返回的是 `[nova] answerable=...` 状态行，**没有** `status`
#:   字段（那是 HTTP router 的 D-24 短路包才有）；
#: ③ 补救路径从"再问一次"改为可执行动作（换指纹 / 改问法 / 换工具），并写清引用编号契约。
#: CF-06 契约文件（**description 的单一来源**，TASK-MCP-BUDGET）。
#:
#: 为什么改成读契约：此前 client 与 service **各写一份** description，实测两者长度差 4 倍
#: （client 298 字符 / service 1309 字符），而 AI 在 stdio 面看到的是 **client** 那份——
#: 即模块文档要求的"查询写法/分工边界/导航"实际上到不了 AI 眼前。改成读同一份契约后
#: 两端不可能再漂移（且有测试逐字守住）。
_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "mcp-tools.json"


@lru_cache(maxsize=1)
def _cf06_descriptions() -> dict[str, str]:
    """``{工具名: description}``，取自 CF-06 契约（缓存：进程内只读一次）。

    契约缺失/损坏时**不静默**：抛 ``RuntimeError`` 并在消息里给出路径与补救办法——
    description 是行为控制，缺失会让 Agent 完全不会用工具，比启动失败更糟。
    """
    try:
        payload = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:  # pragma: no cover - 部署缺少契约文件时才会触发
        raise RuntimeError(
            f"缺少 CF-06 契约文件 {_CONTRACT_PATH}；它是工具 description 的单一来源。"
            "请确认部署时带上了仓库的 docs/contracts/ 目录。"
        ) from exc
    except json.JSONDecodeError as exc:  # pragma: no cover
        raise RuntimeError(f"CF-06 契约文件不是合法 JSON：{_CONTRACT_PATH}（{exc}）") from exc
    return {tool["name"]: tool["description"] for tool in payload["tools"]}


#: 懒构造 EngineManager 的互斥（MCP 工具没有 ``Request``，不能直接用 ``deps.get_engine_manager``）。
_manager_lock = Lock()


# --------------------------------------------------------------------------- 身份通道


class _Identified(Protocol):
    """身份的最小形状（TASK-089 §A）：**只要求一个 ``id``**。

    绑定进来的是 ``app.py`` 写入 ``request.state.nova_user`` 的那个对象，即 ``auth.Principal.user``
    （``auth.User``）——不是 ``Principal`` 本身。用结构化 Protocol 表达"我需要的是能取到 id 的
    东西"，既不用为一个字段多一条 ``mcp`` → ``auth`` 的耦合边，也不强绑到 ``User`` 这个具体类型
    （测试可注入任何带 ``id`` 的对象）。
    """

    id: str


#: 当前请求/会话的**已认证用户**（``None`` = 无账户口径，本地模式 R34）。
#:
#: **为什么用 contextvars**（卡内 §A 方案 A）：它与 :mod:`nova_service.logging` 的 ``requestId``
#: **同构**，且 MCP 的 Streamable HTTP 会话模型下**实测**能正确传播——SDK 在写入消息时用
#: ``ContextSendStream`` 快照 ``contextvars.copy_context()``，再以
#: ``sender_ctx.run(start_soon, fn)`` 恢复，因此工具函数执行时看到的正是"那个 HTTP 请求"的
#: 上下文（见任务卡执行记录的验证证据）。
_user: ContextVar[Any | None] = ContextVar("nova_mcp_user", default=None)


def bind_user(user: _Identified | None) -> Token[Any | None]:
    """绑定当前上下文的用户（返回值交给 :func:`reset_user` 还原）。

    ``user`` 是 ``auth.User | None``；``None`` 表示无账户（本地模式）。
    """
    return _user.set(user)


def reset_user(token: Token[Any | None]) -> None:
    _user.reset(token)


def current_user() -> _Identified | None:
    """当前上下文里的已认证用户（``None`` = 本地模式/未绑定）。"""
    return _user.get()


class _IdentityBinding:
    """ASGI 包装（TASK-089 §A/B 的**接入点**）：把请求已认证用户存进 contextvar 再放行。

    身份来源是 ``scope["state"]["nova_user"]``——即 ``app.py::_install_auth`` 的鉴权中间件在
    每个 HTTP 请求上写入、并经 ``Mount`` 传给子应用的那一份（注意它存的是 ``Principal.user``，
    不是 ``Principal``）。实测（本卡执行记录）在 MCP 挂载点与 ``/mcp`` 别名路由上都能读到，
    因此**不改 ``app.py``** 即可完成接线。

    这条"复用已认证结果"的窄路比两种替代都稳：

    - 不比"在 MCP 子应用里手写 Bearer/Cookie 解析"更失败不安全（身份是同一个中间件算出来的）；
    - 不会把未认证请求变成 401（鉴权中间件已经拦过：云端无凭据根本到不了这里）——
      本包装的职责只是**把已算好的身份送进去**，不是第二道鉴权。

    未认证传入（本地模式，或未挂鉴权中间件的裸 ``FastAPI``）→ 绑定 ``None``，工具侧按无账户处理。

    纪律：``state`` 必须是映射，否则绑定 ``None``，绝不因缺键而异常（子应用要能处理裸 ASGI 请求）。
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - MCP 面只走 HTTP
            await self._app(scope, receive, send)
            return
        state = scope.get("state")
        user = state.get("nova_user") if hasattr(state, "get") else None
        token = bind_user(user)
        try:
            await self._app(scope, receive, send)
        finally:
            reset_user(token)


# --------------------------------------------------------------------------- 装配


def build_mcp(
    engine_manager: EngineManager | Callable[[], EngineManager],
    *,
    settings: Settings | None = None,
    app: FastAPI | None = None,
) -> MCPServer:
    """装配 MCPServer + 两个工具（冻结入口：``build_mcp(engine_manager) -> MCPServer``）。

    ``engine_manager`` 可以是 :class:`~nova_service.runtime.EngineManager` 实例，也可以是**零参
    可调用对象**（懒解析）：``create_app`` 不知道引擎何时建（TASK-030 的"起服务不加载模型 / 懒构造"
    纪律），因此应用侧传 ``lambda: manager_for_app(app)``，工具被调用时才真正拿管理器。

    ``settings`` 只用于本地模式的懒重扫间隔（TASK-034 §C）与 LLM 配置（TASK-088）；缺省从环境变量
    解析，便于单测直接用冻结的一参形式。
    ``app``（TASK-088）：传入时 ``ask_project`` 复用应用级 LLM provider（缓存 + 与 REST 面同一个
    配置来源）；不传（例如直接单测本函数）时按 ``settings`` 现建一个不带缓存的 provider。
    """
    def _load_settings() -> Settings:
        """取当前生效的配置（MCP 会话可能活很久，不吃启动那一刻的快照）。

        显式传入 ``settings`` 时用它（测试与内嵌用法）；否则每次调用重读环境变量
        （TASK-088：LLM 配置要能"改 env 即改行为"，包括运行中的进程）。
        """
        return settings if settings is not None else Settings.from_env()
    resolve = engine_manager if callable(engine_manager) else (lambda: engine_manager)

    async def search_context(
        query: Annotated[
            str, Field(min_length=1, description="自然语言或符号混合查询，中英均可")
        ],
        project_root: Annotated[
            str, Field(min_length=1, description="项目根绝对路径，正斜杠")
        ],
        max_tokens: Annotated[int, Field(ge=1, le=MAX_TOKENS_LIMIT)] = DEFAULT_MAX_TOKENS,
    ) -> str:
        """CF-06 的 ``search_context``（Fast 模式：检索 + 组装 + 服务端渲染）。

        TASK-MCP-BUDGET：``max_tokens`` 已从 CF-06 的 ``inputSchema`` 中**移除**
        （工具面向 AI 不再暴露预算参数），此处保留入参只是为了兼容仍然发送该字段的旧编辑器；
        不传时用服务端默认值（14K）。
        """
        _require_query(query)
        manager = resolve()
        project_id = _project_id_for(manager, project_root)
        _require_index(manager, project_id)
        current = _load_settings()
        return await run_in_threadpool(
            _search_text, manager, current, project_id, query.strip(), max_tokens
        )

    async def ask_project(
        question: Annotated[
            str, Field(min_length=1, description="需要项目级回答的问题，中英均可")
        ],
        project_root: Annotated[
            str, Field(min_length=1, description="项目根绝对路径，正斜杠")
        ],
        max_tokens: Annotated[
            int, Field(ge=1, description="answer 输出上限（token）")
        ] = ASK_MAX_TOKENS,
    ) -> str:
        """CF-06 的 ``ask_project``（Deep 模式：grounded LLM 总结；未配置/失败 → 降级包）。

        TASK-MCP-BUDGET：同 ``search_context``，``max_tokens`` 不再出现在 ``inputSchema`` 中；
        入参默认值改为 Deep 档（16K），与 HTTP ``ask`` 路由一致。
        """
        _require_query(question, field="question")
        _require_service_max_tokens(max_tokens)
        manager = resolve()
        project_id = _project_id_for(manager, project_root)
        _require_index(manager, project_id)
        current = _load_settings()
        return await run_in_threadpool(
            _ask_text,
            manager,
            current,
            project_id,
            question.strip(),
            max_tokens,
            _provider_resolver(app, current),
        )

    descriptions = _cf06_descriptions()
    return MCPServer(
        name=MCP_SERVER_NAME,
        tools=[
            _cf06_tool(
                search_context, name=SEARCH_TOOL, description=descriptions[SEARCH_TOOL]
            ),
            _cf06_tool(ask_project, name=ASK_TOOL, description=descriptions[ASK_TOOL]),
        ],
    )


def mount(app: FastAPI, mcp: MCPServer, *, json_response: bool = False) -> None:
    """把 MCP 挂到 ``app`` 的 ``/mcp``（由 ``create_app`` 调用一次）。

    **必须**传 ``streamable_http_path="/"``：``streamable_http_app`` 的内层路径默认也是 ``/mcp``，
    与挂载前缀叠加会变成 ``/mcp/mcp`` → 所有 MCP 请求 404。

    ``json_response``：默认 ``False``（SDK 的 SSE 形态，``Content-Type: text/event-stream``）；
    ``True`` 时返回纯 JSON（``application/json``）。生产装配保持默认——编排者在 2.2.0 上实测的
    就是 SSE 形态（见任务卡"已验证的实现要点"），不额外引入与编辑器兼容性相关的未知量。

    Origin / DNS-rebinding 防护**保持 SDK 默认**（``host="127.0.0.1"`` → 自动放行
    ``127.0.0.1:*`` / ``localhost:*`` / ``[::1]:*``；其它 Origin 403）：本地单用户模式下这是
    唯一挡住"浏览器里的任意网页访问本机服务"的机制，**不得为了跑通顺手关掉**。

    **另加一条无重定向的别名路由**（实测发现）：Starlette 的 ``Mount`` 只匹配 ``/mcp/...``，
    请求 ``POST /mcp`` 会先被 307 重定向到 ``/mcp/``。我们对外给的 URL 恰好是 ``/mcp``，
    跟随重定向与否取决于各编辑器的 HTTP 客户端，因此这里把 SDK 生成的内层 ASGI 端点直接注册到
    ``/mcp``（与挂载同一个 session manager / 同一个 Starlette 应用），使**两个 URL 都直接可用、
    行为一致**。

    **身份接入点（TASK-089 §A/B）**：挂载的内层 app 与 ``/mcp`` 别名端点**外层各包一层
    :class:`_IdentityBinding`**，把请求已认证身份（``scope["state"]["nova_user"]``，由 ``app.py``
    的鉴权中间件写入）存进 contextvar。两条路径各自包装（而不是包一次）是因为它们持有的是**同一个
    ASGI 应用的两个引用**：客户端走哪个 URL 都能拿到身份，不依赖是否经过 ``Mount``。
    """
    starlette_app = mcp.streamable_http_app(streamable_http_path="/", json_response=json_response)
    app.mount(MCP_MOUNT_PATH, _IdentityBinding(starlette_app))
    app.router.routes.append(
        Route(MCP_MOUNT_PATH, endpoint=_IdentityBinding(starlette_app.routes[0].endpoint))
    )


@asynccontextmanager
async def _session_manager_running(mcp: MCPServer) -> Iterator[None]:
    async with mcp.session_manager.run():
        yield


def session_lifespan(mcp: MCPServer) -> Callable[[Any], AbstractAsyncContextManager[None]]:
    """Starlette/FastAPI 形状的 lifespan（``Callable[[app], AsyncContextManager]``）。

    跑 session manager 的 lifespan（**必须**，否则 ``initialize`` 建不了会话）：
    ``session_manager`` 由 :func:`mount`（即 ``streamable_http_app``）创建，因此本 lifespan 只能
    在挂载之后启动；TASK-030 的 app 没有自己的 lifespan（起服务不加载模型），所以直接接管
    ``app.router.lifespan_context``——将来若出现别的 lifespan，在 ``async with`` 里嵌套组合，
    不要覆盖。
    """

    def _lifespan(_app: Any) -> AbstractAsyncContextManager[None]:
        return _session_manager_running(mcp)

    return _lifespan


def _cf06_tool(fn: Callable[..., Any], *, name: str, description: str) -> Tool:
    """按函数签名建工具，并把 inputSchema 对齐到 CF-06。

    SDK 从签名生成 schema（``Tool.from_function``），但**不会**生成 ``additionalProperties``，
    而 CF-06 声明了 ``additionalProperties: false``——这里补上这一个键，使 ``tools/list`` 与冻结
    合同逐字段一致（纯声明性，不改变运行时行为：多余参数本来就被 pydantic 忽略）。

    ``HIDDEN_PARAMS``（TASK-MCP-BUDGET）：从 ``inputSchema`` 里删掉这些字段，**但函数签名
    与运行时入参保留**——这样仍能接受旧编辑器发来的字段（不报错、不改变行为），
    而 AI 在 ``tools/list`` 里看不到它们，也就不会去调预算那样一个它无法判断的参数。

    ``structured_output=False``：工具只返回**文本**（ContextPack 的 Markdown + 一行 nova 状态），
    与 Module/05 §5"客户端只透传 Markdown"一致，不给编辑器再塞一份 JSON。
    """
    tool = Tool.from_function(fn, name=name, description=description, structured_output=False)
    tool.parameters["additionalProperties"] = False
    properties = tool.parameters.get("properties")
    if isinstance(properties, dict):
        for hidden in HIDDEN_PARAMS:
            properties.pop(hidden, None)
        # ``required`` 里若残留被隐藏字段会生成非法 schema（字段既必填又不存在）。
        required = tool.parameters.get("required")
        if isinstance(required, list):
            tool.parameters["required"] = [item for item in required if item not in HIDDEN_PARAMS]
    return tool


def manager_for_app(app: FastAPI) -> EngineManager:
    """取应用级 EngineManager（懒构造；与 ``deps.get_engine_manager`` 同口径）。

    为什么不在 ``deps`` 里复用：那个入口要一个 ``Request``，而 MCP 工具没有 HTTP 请求对象。
    这里复刻"双重检查 + 加锁"的几行；出现第三个入口时抽成公共函数。
    """
    manager = getattr(app.state, "engine_manager", None)
    if isinstance(manager, EngineManager):
        return manager
    with _manager_lock:
        manager = getattr(app.state, "engine_manager", None)
        if not isinstance(manager, EngineManager):
            manager = EngineManager.open(app.state.settings.data_root)
            # TASK-085：与 ``deps.get_engine_manager`` 同口径——懒构造的 manager 必须接上
            # app 级元数据库，否则 MCP 面触发的上传同样不落索引 run（统计恒为 0）。
            db = getattr(app.state, "meta_db", None)
            manager.attach_meta_db(db if isinstance(db, MetaDB) else None)
            app.state.engine_manager = manager
    return manager


# --------------------------------------------------------------------------- 工具实现（线程池）


def _search_text(
    manager: EngineManager,
    settings: Settings,
    project_id: str,
    query: str,
    max_tokens: int,
) -> str:
    """``search_context`` 的正体（同步；由调用方放进线程池）。"""
    _rescan_if_due(manager, settings, project_id)
    trace = _call_engine(lambda: manager.search(project_id, query, max_tokens))
    meta = pack_meta(
        trace.pack,
        project_id=project_id,
        channels=trace.channels_used,
        degraded=trace.degraded,
        # core 的降级原因可能带 provider 原始报错（TASK-035 §A 的同一纪律：secret 不进响应）。
        reason=redact_text(trace.degraded_reason) if trace.degraded_reason else None,
        candidate_count=trace.candidate_count,
    )
    return append_warning(
        f"{_status_line(meta)}\n\n{render_markdown(trace.pack)}",
        _storage_warning(manager, settings, project_id),
    )


def _provider_resolver(
    app: FastAPI | None, settings: Settings
) -> Callable[[], Any]:
    """``ask_project`` 的 provider 解析器（TASK-099 §C-4：**用户配置优先**）。

    为什么做成零参可调用而不是直接传 provider：MCP 工具可能在**很久以后**才被调用，
    而 provider 的构造要读届时生效的配置（用户配置会变）＋届时生效的身份——延迟到调用时
    解析就不会用到过期的配置快照。

    有 ``app`` 时复用 :func:`llmconfig.provider_for_request` 的缓存（与 REST 面**同一套**
    解析与缓存口径，两面不会漂移）；不传（单测直接调 ``build_mcp``）时按 settings 现建。

    身份取自 :func:`current_user`（与 ``_require_owned_project`` / ``_storage_warning``
    同一通道）：配额按用户算，而**用户自定义的 LLM 也按用户算**——同一个 contextvar，
    同一个口径。无身份（本地模式）→ 服务端默认。
    """
    def resolve() -> Any:
        user_id = _llm_user_id(settings)
        if app is not None:
            return provider_for_request(
                app, user_id=user_id, db=_meta_db_for_llm(app)
            )
        return build_answer_provider(settings, user_id=user_id)

    return resolve


def _llm_user_id(settings: Settings) -> str | None:
    """MCP 面的 LLM 归属人（与 REST 面的 :func:`auth.llm_owner` **同口径**）。

    ``current_user()`` 在云端来自 ``_IdentityBinding`` 注入的已认证用户；而**本地模式**下
    ``app.py`` 的鉴权中间件把 ``request.state.nova_user`` 置为 ``None``（R34：无账户体系），
    因此这里回落到隐式账户 ``local``——否则用户在设置页配好了 LLM，MCP 的 ``ask_project``
    却仍然用服务端默认，而页面显示"已配置"（静默失灵，正是本卡要消灭的问题）。

    云端未认证时返回 ``None``（该路径实际上被鉴权中间件拦住了，这里只是不给出错的可能）。
    """
    user_id = getattr(current_user(), "id", None)
    if user_id:
        return str(user_id)
    return local_user().id if settings.local_mode else None


def _meta_db_for_llm(app: FastAPI) -> MetaDB | None:
    """app 级 ``MetaDB``（读用户 LLM 配置用；缺库 → ``None``，解析层自动回落默认）。"""
    db = getattr(getattr(app, "state", None), "meta_db", None)
    return db if isinstance(db, MetaDB) else None


def _ask_text(
    manager: EngineManager,
    settings: Settings,
    project_id: str,
    question: str,
    max_tokens: int,
    provider_resolver: Callable[[], Any],
) -> str:
    """``ask_project`` 的正体：grounded LLM 总结；未配置/失败一律降级（D-26，绝不空手）。

    **证据优先（D-24，TASK-107）**：``answerable=false`` 时不调 LLM，直接返回尽力而为的
    上下文包 + 缺口说明。HTTP ``ask`` 路由一直如此，MCP 侧此前漏了这一步（会在证据不足时
    仍然消耗一次 LLM 调用，且返回的回答没有任何证据支撑）。现在两侧行为一致。

    TASK-MCP-BUDGET：``max_tokens`` 是**装填预算**，与回答长度无关。
    此前 MCP 的 ``ask_project`` 把工具参数的默认值（10K）直接当装填预算用，
    而 HTTP ``ask`` 走 16K——两个面不一致；现在两边都由调用方传 ``ASK_MAX_TOKENS``。
    """
    _rescan_if_due(manager, settings, project_id)
    trace = _call_engine(lambda: manager.search(project_id, question, max_tokens))
    warning = _storage_warning(manager, settings, project_id)
    insufficient = not trace.pack.answerable
    meta = pack_meta(
        trace.pack,
        project_id=project_id,
        channels=trace.channels_used,
        degraded=True,
        reason=INSUFFICIENT_NOTICE if insufficient else DEGRADED_NOTICE,
        candidate_count=trace.candidate_count,
    )
    status_line = _status_line(meta)
    if insufficient:
        # 与 HTTP 路由的 D-24 短路同语义：先说清为何不调 LLM，再给可直接使用的上下文与补证据建议。
        return append_warning(
            f"{INSUFFICIENT_NOTICE}\n\n{status_line}\n\n{render_markdown(trace.pack)}", warning
        )
    provider = provider_resolver()
    if provider is None:
        return append_warning(
            f"{DEGRADED_NOTICE}\n\n{status_line}\n\n{render_markdown(trace.pack)}", warning
        )
    try:
        outcome = answer_question(
            provider=provider, settings=settings, pack=trace.pack, question=question
        )
    except AnswerError as exc:
        logger.warning(
            "ask_project 降级（LLM %s）：%s → %s",
            exc.kind,
            project_id,
            redact_text(f"{type(exc).__name__}: {exc}"),
        )
        return append_warning(
            f"{LLM_FAILED_NOTICE}\n\n{status_line}\n\n{render_markdown(trace.pack)}", warning
        )
    degraded_line = _status_line({**meta, "degraded": False})
    return append_warning(f"{outcome.answer}\n\n{degraded_line}", warning)


def _storage_warning(manager: EngineManager, settings: Settings, project_id: str) -> str | None:
    """存储告警节（TASK-094 §B3；**旁路**：失败返回 ``None``，绝不影响检索）。

    身份取自 :func:`current_user`（与 :func:`_require_owned_project` 同一通道）：配额是
    **按用户**算的，拿不到身份就不能把别人的项目算进额度。

    TASK-110：上限按身份取（``roles.QUOTA_BY_ROLE``）；本地模式（无 `nova_user`）回落
    ``Settings`` 兜底，与 REST 面 :func:`nova_service.auth.quota_identity` 同一口径。
    """
    user = current_user()
    role = getattr(user, "role", None) if not settings.local_mode else None
    override = getattr(user, "quota_bytes", None) if not settings.local_mode else None
    return warning_for(
        manager,
        settings,
        project_id=project_id,
        db=manager.meta_db,
        user_id=getattr(user, "id", None),
        role=role,
        override=override,
    )


def build_answer_provider(settings: Settings, *, user_id: str | None = None) -> Any:
    """按配置现建 provider（未配置 → ``None``）；供 MCP 单测与内嵌用法。

    TASK-099 §C-4：``user_id`` 非空时按**该用户**的配置建（没有库可读，因此仅在调用方
    已经知道"该用户没有单独配置"或走的是无 app 的测试路径时使用）；缺省回落服务端默认。
    """
    from nova_service.llmconfig import resolve_llm_config

    resolved = resolve_llm_config(settings, user_id=user_id, db=None)
    if not resolved.configured:
        return None
    from nova_service.answer import HttpJsonProvider

    return HttpJsonProvider(
        base_url=str(resolved.base_url),
        api_key=str(resolved.api_key),
        model=str(resolved.model),
        protocol=resolved.protocol,
        timeout_s=settings.answer_timeout_s,
    )


def _status_line(meta: dict[str, Any]) -> str:
    """一行 nova 状态（``answerable`` / ``confidence`` / 证据计数）。

    **为什么要有这一行**：``render_markdown`` 只渲染 ``confidence``（``### Meta`` 段），不含
    ``answerable``；而 Module/03 §4.4 要求工具返回值如实反映 answerable 的语义，编辑器也要据此
    决定"要不要说证据不足"。故在 Markdown **之前**加这一行——正文仍逐字来自
    ``render_markdown(pack)``，本行只是 TASK-032 ``pack_meta`` 输出的一行摘要（不重新推导任何值）。
    """
    answerable = "true" if meta["answerable"] else "false"
    degraded = "true" if meta["degraded"] else "false"
    return (
        f"[nova] answerable={answerable} · confidence={meta['confidence']} · "
        f"evidence={meta['evidenceCount']} · docs={meta['docsCount']} · "
        f"mode={meta['mode']} · channels={','.join(meta['channelsUsed']) or '-'} · "
        f"degraded={degraded}"
    )


def _rescan_if_due(manager: EngineManager, settings: Settings, project_id: str) -> None:
    """本地模式：检索前的增量懒重扫（TASK-034 §C 的冻结接口，**MCP 侧入口**）。

    HTTP 面（``routers/query.py``）是 §C 指定的触发点；编辑器（本模块）是同一语义的第二个入口——
    本地模式下用户改完代码直接在编辑器里提问，没有这一步就会一直看到"启动那一刻"的索引。

    纪律与 §C 一致：**重扫失败不得让检索失败**，只记日志（HTTP 面额外把它写进
    ``meta.freshness.rescanError``；MCP 面不往给编辑器的 Markdown 里塞这个信号）。
    """
    try:
        manager.rescan_if_due(project_id, min_interval_s=settings.local_rescan_interval_s)
    except Exception as exc:
        logger.warning(
            "懒重扫失败（检索照常）：%s → %s",
            project_id,
            redact_text(f"{type(exc).__name__}: {exc}"),
        )


# --------------------------------------------------------------------------- 参数与错误面


def _require_query(raw: str, *, field: str = "query") -> None:
    """空/纯空白/超长查询 → 可读的 ``isError`` 文本（Module/05 §2.2 的参数错误面）。"""
    if not raw.strip():
        raise ToolError(f"{field} 不能为空或纯空白：请给出自然语言或符号混合的问题（中英均可）。")
    if len(raw) > MAX_QUERY_CHARS:
        raise ToolError(
            f"{field} 过长（{len(raw)} 字符，上限 {MAX_QUERY_CHARS}）：请把问题聚焦成一句话。"
        )


def _require_service_max_tokens(max_tokens: int) -> None:
    """``ask_project`` 的 ``max_tokens`` 上限。

    CF-06 只给 ``ask_project.max_tokens`` 声明 ``minimum: 1``（无上限），但套餐预算直接把
    ``max_tokens`` 当硬上限（``Engine._budget``），所以服务端必须有兜底：与 HTTP 面
    ``routers/query.py`` 同值（20000）。这是**服务端限制**，不是 schema 变更。
    """
    if max_tokens > MAX_MAX_TOKENS:
        raise ToolError(
            f"max_tokens={max_tokens} 超出服务端上限 {MAX_MAX_TOKENS}：请调小"
            f"（search_context 的 schema 上限更低，为 {MAX_TOKENS_LIMIT}）。"
        )


def _project_id_for(manager: EngineManager, project_root: str) -> str:
    """``project_root`` → projectId（D-29 身份），并确认项目**存在且归属当前身份**。

    CF-06 的参数是 ``project_root``（绝对路径），因此 MCP 面用**同一套身份规则**反解 projectId：
    与 ``POST /api/projects/resolve``/``attach`` 完全一致，不新增映射表、不落额外状态。

    **归属校验（TASK-089）**：反解出 projectId 后过 :func:`deps.require_ownership_of`（与 REST 面
    **同一份实现**）。未归属当前身份 → 与"项目不存在"**同一句文案**的 ``isError``（REST 面是
    404 ``project_not_found``）：MCP 协议下工具执行错误走正常响应 + ``isError=true``
    （Module/05 §2.2），故这里用文本而不是 HTTP 状态码承载 404 语义——但"不给探测面"的目的
    一致：越权者与查错 id 者拿到的文本逐字相同。

    ``nova_user`` 为 ``None``（本地模式，R34）时放行；云端元数据库缺失时 fail closed。
    """
    raw = project_root.strip()
    if "\\" in raw:
        raise ToolError(
            "project_root 含反斜杠，必须是**正斜杠**的绝对路径（本地模式下服务跑在 WSL/Linux，"
            f"Windows 路径如 C:\\... 在这里不存在）：收到 {project_root!r}"
        )
    try:
        identity = repo_identity(Path(raw))
    except Exception as exc:  # 路径本身非法（如含空字节）：如实报参数问题
        raise ToolError(f"project_root 无法解析为路径：{redact_text(str(exc))}") from exc
    project_id = engine_project_id_for(identity.identity_key)
    _require_owned_project(manager, project_id, raw)
    return project_id


def _require_owned_project(manager: EngineManager, project_id: str, raw: str) -> None:
    """存在 + 归属（TASK-089 §B 的**唯一接入点**）。

    分两种形态，这是本卡"不给探测面"的关键设计：

    - **无账户（本地模式，R34）**：只判存在，失败给 TASK-040 已验收的**可操作**文案
      （``未知项目：… 请用 nova-service local --repo …``）——本地单用户模式没有"别人的项目"
      这个概念，保留该提示是 R34"与今天逐字一致"的要求；
    - **有账户（云端）**：存在性与归属**合并为同一句拒绝**。若先报"未知项目"再报"项目不存在"，
      攻击者就能用两个不同的文本区分"这个 projectId 存在"与"不存在"，归属校验就白做了。
      因此两者都走 :func:`deps.project_not_found_message`（与 REST 的 404 同一句）。

    代价（已知且接受）：云端首次同步前查询自己尚未创建的项目，得到的是"项目不存在"而不是
    "怎么建"的提示。客户端流程总是先 resolve/上传再查询，故不影响正常路径；
    见任务卡执行记录的"与设计偏差"。
    """
    user_id = getattr(current_user(), "id", None)
    if user_id is None or user_id == "":
        # 本地模式/无账户口径（R34）：保留原文案，逐字不变。
        if not manager.project_exists(project_id):
            raise ToolError(_unknown_project_message(raw, project_id))
        return
    # 云端："不存在"与"不是你的"必须是同一种响应，否则越权者能据此探测存在性。
    if not manager.project_exists(project_id):
        raise ToolError(project_not_found_message(project_id))
    try:
        require_ownership_of(user_id, manager.meta_db, project_id)
    except ApiError:
        raise ToolError(project_not_found_message(project_id)) from None


def _unknown_project_message(raw: str, project_id: str) -> str:
    """本地模式的"未知项目"可操作提示（TASK-040 冻结文案，本卡未改一字）。"""
    return (
        f"未知项目：{raw} 对应的 projectId {project_id} 在本服务里没有索引记录（D-29 身份）。"
        f"本地模式请用 `nova-service local --repo {raw}` 起服务，"
        "或先 POST /api/projects/attach；远端模式请先同步（POST /api/sync/batch-upload）。"
    )


def _require_index(manager: EngineManager, project_id: str) -> None:
    """空索引 → 可操作的 ``isError`` 文本（含 TASK-034 的进度 + 重试提示）。

    三岔口（与 ``routers/query.py`` 的 TASK-035 §B 同口径，**根因优先**）：

    1. provider 不可用 → 直接说根因（否则用户会一直"稍后重试"而永远好不了）；
    2. 索引中/从未跑过 → 报当前状态与已处理文件数（**不伪造百分比**，D-30）；
    3. 给出"下一步做什么"（重试 / 看 indexProgress / 触发 rescan）。
    """
    if manager.sync_status(project_id)["chunks"] > 0:
        return

    ok, reason = manager.provider_health()
    if not ok:
        raise ToolError(
            f"项目 {project_id} 的索引为空，且 embedding provider 当前不可用（{reason}）："
            f"这不是「索引还没跑完」，重试不会好。{PROVIDER_UNAVAILABLE_HINT}"
        )

    progress = manager.index_progress(project_id)
    state = (
        f"{progress.state}（已处理 {progress.processed_files}/{progress.total_files} 个文件）"
        if progress.state != "idle"
        else "idle（本进程还没为它跑过索引）"
    )
    raise ToolError(
        f"项目 {project_id} 暂无可用索引（chunks=0），当前索引状态：{state}。\n"
        "如果刚启动本地服务，后台索引可能还在跑：**稍后重试本查询**，"
        f"或用 GET /api/projects/{project_id} 查看 indexProgress；\n"
        "如果一直是空，请确认仓库路径正确并重新 `nova-service local --repo <根目录>`"
        "（或 POST /api/projects/{id}/rescan 手动触发增量重扫）。"
    )


def _call_engine(call: Callable[[], Any]) -> Any:
    """执行 core 调用并把失败映射成 ``isError``（**不让裸异常冒到协议层**）。

    - provider / 存储类故障 → 复用 TASK-035 的 :func:`map_engine_error`（同一份可操作指引文案；
      MCP 面没有 HTTP 状态码，只取 message）；
    - ``EngineError``（core 的通用错误）→ 脱敏后原样告知；
    - 其它异常 → 全量堆栈进日志，对外只给一行摘要（secret 不进响应）。
    """
    try:
        return call()
    except Exception as exc:
        mapped = map_engine_error(exc)
        if mapped is not None:
            logger.warning("MCP 工具调用失败（依赖类）：%s", mapped.code)
            raise ToolError(mapped.message) from exc
        if isinstance(exc, EngineError):
            raise ToolError(redact_text(f"检索失败：{exc}")) from exc
        logger.exception("MCP 工具调用出现未预期异常")
        raise ToolError(
            redact_text(f"检索失败（{type(exc).__name__}）：{exc}；服务端日志含完整堆栈")
        ) from exc
