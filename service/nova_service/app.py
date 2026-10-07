"""FastAPI 应用装配（TASK-030 §交付物；后续卡只在此挂实现，不改装配面）。

冻结入口：``create_app(settings: Settings | None = None) -> FastAPI``——测试、``__main__``
与将来的 ASGI server 都走它（不提供模块级 ``app`` 全局单例，避免配置与进程状态纠缠）。

装配内容：

1. ``Settings`` 解析（缺省读环境变量）并写入 ``app.state.settings``；
2. JSON 日志（stderr）+ ``RedactingFilter``（见 ``nova_service.logging``）；
3. requestId middleware：每请求一个 id（尊重调用方传的 ``X-Request-Id``），
   响应头回写 ``X-Request-Id``，访问日志只记 method/path/status/耗时（不碰 headers/body）；
4. CF-05 错误信封处理器（见 ``nova_service.errors``）；
5. 全部路由（路径集合冻结，见 ``nova_service.routers``）；
6. **MCP 端点**（TASK-040）：``/mcp`` 挂 MCP Streamable HTTP（见 ``nova_service.mcp``）。
   MCP 不是 CF-05 的 REST 路径（挂载不进 ``openapi()["paths"]``），因此路径快照测试不受影响。

``app.state.engine_manager`` 在本卡恒为 ``None``（TASK-030 不消费 core）；TASK-031 起由
``nova_service.deps.get_engine_manager`` 懒构造或由测试注入；TASK-040 的 MCP 工具走
``nova_service.mcp.manager_for_app``（同一口径，只是入口没有 ``Request``）。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from nova_service.auth import authenticate
from nova_service.config import Settings
from nova_service.errors import error_envelope, install_error_handlers, internal_error_response
from nova_service.logging import bind_request_id, configure_logging, get_logger, reset_request_id
from nova_service.mcp import build_mcp, manager_for_app, mount, session_lifespan
from nova_service.metadb import MetaDB
from nova_service.requestlog import capture_request
from nova_service.routers import all_routers

__all__ = ["PUBLIC_PATHS", "REQUEST_ID_HEADER", "create_app"]

#: requestId 的传递头（请求可自带，响应必回写）。
REQUEST_ID_HEADER = "X-Request-Id"

logger = get_logger("nova_service.app")


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造应用（唯一入口；同进程可创建多个实例，测试用临时 data_root 互不干扰）。"""
    resolved = settings if settings is not None else Settings.from_env()
    # TASK-090 §A：除了 stderr，再落一份轮转 JSONL（服务重启后仍可查）。
    # 测试用 ``log_max_bytes=0`` 可关掉轮转，但文件本身照写（"落盘而非内存"是要被验证的）。
    configure_logging(
        resolved.log_level,
        log_path=resolved.request_log_path,
        max_bytes=resolved.log_max_bytes,
        backup_count=resolved.log_backup_count,
        retention_days=resolved.log_retention_days,
    )

    app = FastAPI(
        title="nova-service",
        version=resolved.version,
        description="nova 服务化外壳（Module/06）；检索/组装逻辑全部来自 nova-core。",
    )
    app.state.settings = resolved
    app.state.engine_manager = None  # TASK-031 懒构造/测试注入
    # TASK-060：云瑞形态才需要元数据库；本地模式不建库（R34：无账户体系）。
    app.state.meta_db = MetaDB.open(resolved.meta_db_path) if resolved.auth_required else None
    _promote_admin(app.state.meta_db, resolved)

    install_error_handlers(app)
    # 中间件**由内向外**装配：``@app.middleware`` 是「后注册者在外层」，因此下面两个调用的
    # 顺序决定包裹关系。``_install_request_context`` 必须最后注册 = **最外层**（TASK-090 §B）：
    # 否则鉴权中间件短路返回的 401 会绕过它——响应拿不到 ``X-Request-Id``、日志里也没有那次
    # 失败请求（而"报错后按 trace id 查"正是本卡存在的理由；实测云瑞形态下 401 的
    # ``X-Request-Id`` 曾为 ``None``）。
    _install_auth(app, resolved)
    _install_request_context(app, resolved)
    for router in all_routers():
        app.include_router(router)
    _install_mcp(app, resolved)
    return app


#: 免鉴权路径（前缀匹配）：探活、部署形态、登录/注册/初始化/登出。
PUBLIC_PATHS = (
    "/healthz",
    "/api/meta",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/bootstrap",
    "/api/auth/logout",
)


def _promote_admin(db: MetaDB | None, settings: Settings) -> None:
    """启动时把 ``Settings.admin_name`` 指定的账户提为管理员（TASK-110 §7.1）。

    为什么在**启动**做而不是只放在迁移里：目标账户可能在首次启动时还不存在
    （全新部署先把库建出来、再用已属管理员的 bootstrap 建首个账户），也可能被删了重建。
    每次启动幂等地补一刀，比"迁移跑一次、之后改名就再也没人提得上"可靠。

    为什么按名字而不是"最早创建者"：真实部署里先建的未必是负责人（先来试手的同事、
    迁移前的临时账号）。定名字可预测（卡内 §7.1 用户拍板）。

    找不到该名字时**只记一行日志**，不报错：空库时它本来就还不存在
    （那个路径由 ``POST /api/auth/bootstrap`` 直接授予管理员）。
    """
    if db is None or not settings.admin_name:
        return
    promoted = db.promote_first_admin(settings.admin_name)
    if promoted is None:
        logger.info("未找到指定的管理员账户（可能尚未注册）：name=%s", settings.admin_name)
    elif promoted.role == "admin":
        logger.info("管理员账户已就绪：name=%s", promoted.name)


def _install_auth(app: FastAPI, settings: Settings) -> None:
    """鉴权中间件（TASK-060；Module/06 §2.2）。

    为什么用中间件而不是逐个路由的 ``Depends``：**鉴权是全局不变量**，逐个挂靠人工维护，
    漏一个就是越权面（TASK-051 A1 的 `auth: enabled` 自述已经在骗人）；中间件只判"能不能进"，
    业务侧的归属校验（TASK-061）仍在依赖里。

    显式 ``nova-service local`` 模式**完全放行**；普通 ``serve`` 始终鉴权。

    ``request.state.nova_user`` 写入已认证用户：下游（归属校验、概览聚合、审计归属）读它，
    避免重复解析凭据。
    """

    @app.middleware("http")
    async def _authenticate(  # noqa: ANN202 - FastAPI middleware 的返回类型由框架决定
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if settings.local_mode:
            request.state.nova_user = None  # 本地模式无账户；下游按 None 走全量口径
            return await call_next(request)

        path = request.url.path
        if path.startswith(PUBLIC_PATHS):
            return await call_next(request)

        principal = authenticate(request)
        if principal is None:
            # 401 不区分细节（无效/已撤销/过期同一文案）：不给探测面。
            return JSONResponse(
                status_code=401,
                content=error_envelope(
                    "unauthorized",
                    "缺少或无效的凭据：请在 Authorization: Bearer 头带上 API Key，或先登录",
                ),
            )
        request.state.nova_user = principal.user
        return await call_next(request)


def _install_mcp(app: FastAPI, settings: Settings) -> None:
    """挂载 MCP 端点并启用 session manager 的 lifespan（TASK-040）。

    ``build_mcp`` 收的是**懒解析函数**：/healthz 与占位路由不得因为挂了 MCP 就去构造引擎
    （TASK-030 的"起服务不加载模型"纪律，`test_healthz_without_touching_core` 守着这一点）。

    lifespan 的接管方式：TASK-030 的 app 没有自己的 lifespan（无既有逻辑可被覆盖），因此这里
    直接设 ``router.lifespan_context``；将来若有了别的 lifespan，改到 :func:`session_lifespan`
    里嵌套组合。
    """
    server = build_mcp(lambda: manager_for_app(app), settings=settings, app=app)
    mount(app, server)
    app.router.lifespan_context = session_lifespan(server)


def _install_request_context(app: FastAPI, settings: Settings) -> None:
    """requestId + 请求日志落盘 + 兜底 500（保证异常不会逃出应用，响应始终是 CF-05 信封）。

    TASK-090 §B：在原有的 method/path/status/耗时之上，再补 ``userId`` / ``projectId`` /
    ``errorCode`` / ``errorMessage`` / ``traceback``，使"按 trace id 查日志"真的能定位问题。
    字段来源全部是**服务端已有的真实值**，不读 headers/body（脱敏纪律见 requestlog 模块）。
    """

    @app.middleware("http")
    async def _request_context(  # noqa: ANN202 - FastAPI middleware 的返回类型由框架决定
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        token = bind_request_id(request_id)
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception as exc:  # 兜底：日志留全量堆栈，响应只给通用文案（errors.py 同口径）
                # 堆栈进 ``traceback`` 字段（§B：5xx 排除的核心价值）；响应里仍无堆栈。
                _record(
                    request,
                    settings,
                    request_id=request_id,
                    status=500,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    error_code="internal_error",
                    exc_info=exc,
                )
                response = internal_error_response()
                response.headers[REQUEST_ID_HEADER] = request_id
                return response
            duration_ms = (time.perf_counter() - started) * 1000
            response, error_code, error_message = await _read_error(response, request_id)
            _record(
                request,
                settings,
                request_id=request_id,
                status=response.status_code,
                duration_ms=duration_ms,
                error_code=error_code,
                error_message=error_message,
            )
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            reset_request_id(token)


async def _read_error(
    response: Response, request_id: str
) -> tuple[Response, str | None, str | None]:
    """CF-05 信封的 ``error.code`` / ``error.message`` → 日志字段（§B）。

    为什么要读响应体：异常处理器（``install_error_handlers``）在**本中间件内层**执行，因此
    ``ApiError``（如 401 ``unauthorized``、404 ``project_not_found``）到这里已经是**普通响应**，
    不能靠 ``except`` 抓到。只对 JSON 的 4xx/5xx 读体（错误体恒为小 JSON），读后原样重建响应，
    对外行为逐字不变。非 JSON / 2xx / 3xx 一概不碰（不缓冲 SSE 与大响应）。
    """
    status = response.status_code
    if status < 400 or not response.headers.get("content-type", "").startswith("application/json"):
        return response, None, None
    iterator = getattr(response, "body_iterator", None)
    if iterator is None:  # pragma: no cover - JSONResponse 恒有 body_iterator
        return response, None, None
    try:
        body = getattr(response, "body", None)
        if body is None:
            chunks = [chunk async for chunk in iterator]
            body = b"".join(chunks)
        payload = json.loads(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        code = str(error.get("code")) if isinstance(error, dict) and error.get("code") else None
        message = (
            str(error.get("message")) if isinstance(error, dict) and error.get("message") else None
        )
        rebuilt = Response(content=body, status_code=status)
        rebuilt.raw_headers = response.raw_headers
        return rebuilt, code, message
    except Exception:  # noqa: BLE001 - 取错误码是旁路：解析失败也不能改变响应
        return response, None, None


def _record(
    request: Request,
    settings: Settings,
    *,
    request_id: str,
    status: int,
    duration_ms: float,
    error_code: str | None,
    error_message: str | None = None,
    exc_info: BaseException | None = None,
) -> None:
    """写一条请求日志（字段装配；落盘/脱敏在 :func:`nova_service.requestlog.capture_request`）。"""
    user = getattr(request.state, "nova_user", None)
    capture_request(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        status=status,
        duration_ms=duration_ms,
        user_id=getattr(user, "id", None),
        project_id=_project_id_from_path(request),
        error_code=error_code,
        error_message=error_message,
        exc_info=exc_info,
    )


def _project_id_from_path(request: Request) -> str | None:
    """从路径参数取 projectId（有则记，§B）。

    只读 URL 路径参数（``{id}`` / ``{projectId}``）：它是**结构化**且不涉及读 body——
    POST 体里的 projectId 不在日志面（日志不读请求体，见脱敏纪律）。
    """
    params = getattr(request, "path_params", None) or {}
    for key in ("projectId", "project_id", "id"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
    return None
