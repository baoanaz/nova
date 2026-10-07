"""TASK-040 验收：service 侧 MCP 端点（协议冒烟 / CF-06 schema / 错误面 / Origin 防护）。

纪律：

- 全部走**真实 ASGI 应用**（``create_app`` + ``TestClient``）：不联网、不加载真实模型
  （embedding 用 ``tests.conftest`` 的确定性假 provider）；
- ``base_url`` 必须带端口（``http://127.0.0.1:8787``）：SDK 默认的 DNS-rebinding 白名单只有
  ``127.0.0.1:*`` / ``localhost:*`` / ``[::1]:*``，``testserver`` 会被 421 拒掉；
- 断言的是**冻结合同**（``docs/contracts/mcp-tools.json``，CF-06）而不是实现细节：工具名、
  参数名、类型、默认值、上限、required、additionalProperties 逐字段比对。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nova_core.engine import EngineError
from nova_service import mcp as mcp_module
from nova_service.app import create_app
from nova_service.cli_hint import editor_config_snippets, format_snippets, mcp_url
from nova_service.config import Settings
from nova_service.mcp import (
    ASK_TOOL,
    MCP_MOUNT_PATH,
    SEARCH_TOOL,
    TOOL_NAMES,
    build_mcp,
    mount,
    session_lifespan,
)
from nova_service.runtime import EngineManager

from tests.conftest import (
    REPO_ROOT,
    SAMPLE_DOC_PATH,
    SAMPLE_FILES,
    SAMPLE_MODULE_PATH,
    TARGET_SYMBOL,
    make_manager,
    upload_files,
)
from tests.conftest import (
    test_settings as make_settings,
)

#: CF-06 合同文件（工具 schema 的唯一事实来源）。
MCP_CONTRACT = REPO_ROOT / "docs" / "contracts" / "mcp-tools.json"

#: TestClient 的 base_url：**必须**与 SDK 的默认 host 白名单（``127.0.0.1:*``）相容。
BASE_URL = "http://127.0.0.1:8787"
#: MCP Streamable HTTP 的请求头（缺 ``Accept`` 会被协议层拒）。
RPC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
#: 协议版本（SDK 2.2.0 协商到的版本）。
PROTOCOL_VERSION = "2025-06-18"
#: 恶意 Origin（DNS-rebinding 防护的回归用例）。
BAD_ORIGIN = "http://evil.example"
#: 命中样例代码的查询（与 REST 侧 E2E 同一句）。
TARGET_QUERY = "令牌过期后在哪里刷新"


# --------------------------------------------------------------------------- 夹具与工具


@pytest.fixture(scope="session")
def contract() -> dict[str, Any]:
    """CF-06：``{工具名: 工具定义}``。"""
    data = json.loads(MCP_CONTRACT.read_text(encoding="utf-8"))
    return {tool["name"]: tool for tool in data["tools"]}


def _build_env(settings: Settings, repo: Path) -> SimpleNamespace:
    """建一个"真实索引过"的环境：app + 假 provider 的 manager + 已灌数据的项目。"""
    app = create_app(settings)
    manager = make_manager(settings.data_root)
    app.state.engine_manager = manager
    repo.mkdir(parents=True, exist_ok=True)
    attached = manager.attach_local(repo, index=False)
    upload_files(manager, attached.project_id, SAMPLE_FILES)
    client = TestClient(app, base_url=BASE_URL, raise_server_exceptions=False)
    client.__enter__()  # 打开 lifespan（MCP 的 session manager 必须在里面）
    return SimpleNamespace(
        app=app,
        manager=manager,
        client=client,
        repo=repo,
        project_id=attached.project_id,
        project_root=str(attached.root),
    )


@pytest.fixture
def make_env(tmp_path: Path) -> Iterator[Callable[..., SimpleNamespace]]:
    """环境工厂：``make_env(local_rescan_interval_s=0.0)``（每个环境独立目录，用完全部关闭）。"""
    made: list[SimpleNamespace] = []
    counter = iter(range(32))

    def _make(**overrides: Any) -> SimpleNamespace:
        index = next(counter)
        root = tmp_path / f"env{index}"
        settings = make_settings(root / "data", **overrides)
        env = _build_env(settings, root / "repo")
        made.append(env)
        return env

    yield _make
    for env in made:
        env.client.__exit__(None, None, None)
        env.manager.close()


@pytest.fixture
def mcp_env(make_env: Callable[..., SimpleNamespace]) -> SimpleNamespace:
    """默认环境（本地模式、懒重扫间隔用默认值）。"""
    return make_env()


def _rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
    msg_id: int = 1,
):
    """发一个 JSON-RPC 请求到 ``/mcp``（带必需的 Accept/Content-Type 与 session id）。"""
    headers = dict(RPC_HEADERS)
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    if extra_headers:
        headers.update(extra_headers)
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(MCP_MOUNT_PATH, json=body, headers=headers)


def _sse_payload(response) -> dict[str, Any]:
    """从 SSE 响应体里取 JSON-RPC 帧（SDK 默认形态：``event: message`` + ``data: {...}``）。"""
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: ") :])
    raise AssertionError(f"响应里没有 data 帧：{response.text[:200]!r}")


def _payload(response) -> dict[str, Any]:
    """按响应形态取 JSON-RPC 帧（默认 SSE；``json_response=True`` 时是纯 JSON）。"""
    if response.headers["content-type"].startswith("application/json"):
        return response.json()
    return _sse_payload(response)


def _initialize(client: TestClient):
    return _rpc(
        client,
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "nova-tests", "version": "0"},
        },
    )


def _connected(client: TestClient) -> str:
    """``initialize`` + ``notifications/initialized``（协议要求的握手顺序）→ session id。"""
    response = _initialize(client)
    assert response.status_code == 200, response.text
    session_id = response.headers.get("mcp-session-id")
    assert session_id, "initialize 必须返回 mcp-session-id（否则会话建不起来，见 lifespan 纪律）"
    assert _payload(response)["result"]["serverInfo"]["name"] == "nova"

    notified = client.post(
        MCP_MOUNT_PATH,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**RPC_HEADERS, "mcp-session-id": session_id},
    )
    assert notified.status_code == 202, notified.text
    return session_id


def _list_tools(client: TestClient, session_id: str) -> list[dict[str, Any]]:
    response = _rpc(client, "tools/list", {}, session_id=session_id, msg_id=2)
    assert response.status_code == 200, response.text
    return _payload(response)["result"]["tools"]


def _call_tool(
    client: TestClient,
    session_id: str,
    name: str,
    arguments: dict[str, Any],
    *,
    msg_id: int = 3,
) -> dict[str, Any]:
    """``tools/call`` → 结果对象（``{content, isError?}``）。"""
    response = _rpc(
        client,
        "tools/call",
        {"name": name, "arguments": arguments},
        session_id=session_id,
        msg_id=msg_id,
    )
    assert response.status_code == 200, response.text
    return _payload(response)["result"]


def _text(result: dict[str, Any]) -> str:
    """工具结果的纯文本（CF-06 的工具只返回文本内容）。"""
    return "\n".join(block["text"] for block in result["content"] if block["type"] == "text")


def _search(env: SimpleNamespace, query: str, **extra: Any) -> dict[str, Any]:
    """在当前 session 上跑一次 ``search_context``（每个用例自己建握手）。"""
    session_id = _connected(env.client)
    arguments: dict[str, Any] = {"query": query, "project_root": env.project_root, **extra}
    return _call_tool(env.client, session_id, SEARCH_TOOL, arguments)


# --------------------------------------------------------------------------- CF-06 schema


def _schema_diff(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    """逐字段比对 inputSchema；返回差异列表（空 = 与 CF-06 一致）。

    比对范围 = 合同里出现的键（``type`` / ``minLength`` / ``minimum`` / ``maximum`` / ``default``
    / ``required`` / ``additionalProperties`` / 参数集合）。SDK 会额外加 ``title``（JSON Schema
    元数据），合同里没有该键，故不参与比对。
    """
    diffs: list[str] = []
    for key in ("type", "additionalProperties"):
        if key in expected and actual.get(key) != expected[key]:
            diffs.append(f"{key}: 期望 {expected[key]!r}，实际 {actual.get(key)!r}")
    if set(actual.get("required", [])) != set(expected.get("required", [])):
        diffs.append(
            f"required: 期望 {sorted(expected.get('required', []))}，"
            f"实际 {sorted(actual.get('required', []))}"
        )
    expected_props = expected.get("properties", {})
    actual_props = actual.get("properties", {})
    if set(actual_props) != set(expected_props):
        diffs.append(f"参数集合: 期望 {sorted(expected_props)}，实际 {sorted(actual_props)}")
    for name, expected_prop in expected_props.items():
        actual_prop = actual_props.get(name, {})
        for key, value in expected_prop.items():
            if actual_prop.get(key) != value:
                diffs.append(f"{name}.{key}: 期望 {value!r}，实际 {actual_prop.get(key)!r}")
    return diffs


def test_tools_list_matches_cf06_field_by_field(
    mcp_env: SimpleNamespace, contract: dict[str, Any]
) -> None:
    """``tools/list``：恰好两个工具，schema 与 CF-06 **逐字段一致**（DoD 第 1 条）。"""
    tools = _list_tools(mcp_env.client, _connected(mcp_env.client))

    assert {tool["name"] for tool in tools} == TOOL_NAMES == {SEARCH_TOOL, ASK_TOOL}
    for tool in tools:
        expected = contract[tool["name"]]
        assert _schema_diff(tool["inputSchema"], expected["inputSchema"]) == []
        assert tool["description"], "description 是行为控制，不能为空"
        # TASK-MCP-BUDGET：description 以 CF-06 契约为**单一来源**（client 与 service 同源）。
        # 此前两份各写一份、长度差 4 倍，而 AI 在 stdio 面看到的是 client 那份——
        # 模块文档要求的“查询写法/分工边界/导航”实际到不了 AI 眼前。
        assert tool["description"] == expected["description"], (
            f"{tool['name']} 的 description 与 CF-06 契约不一致（两端同源不能漂移）"
        )
    # 关键字段单独再确认一次（防上面的比对被整体改坏而静默通过）
    search = next(tool for tool in tools if tool["name"] == SEARCH_TOOL)
    assert search["inputSchema"]["required"] == ["query", "project_root"]
    # TASK-MCP-BUDGET：``max_tokens`` 不再出现在 inputSchema（对 AI 隐藏，仍可接收）
    assert set(search["inputSchema"]["properties"]) == {"query", "project_root"}
    assert search["inputSchema"]["additionalProperties"] is False

    ask = next(tool for tool in tools if tool["name"] == ASK_TOOL)
    assert ask["inputSchema"]["required"] == ["question", "project_root"]
    assert set(ask["inputSchema"]["properties"]) == {"question", "project_root"}


def test_mcp_mount_is_not_part_of_cf05_paths(mcp_env: SimpleNamespace) -> None:
    """``/mcp`` 是 MCP 面而不是 CF-05 的 REST 路径（``openapi()`` 不得多出它）。"""
    assert MCP_MOUNT_PATH not in mcp_env.app.openapi()["paths"]
    assert any(
        getattr(route, "path", None) == MCP_MOUNT_PATH for route in mcp_env.app.routes
    ), "MCP 必须真的挂在 /mcp 上"


@pytest.mark.parametrize("path", [MCP_MOUNT_PATH, f"{MCP_MOUNT_PATH}/"])
def test_advertised_url_works_without_redirect(mcp_env: SimpleNamespace, path: str) -> None:
    """对外给的 URL ``/mcp`` **不靠 307 重定向**就能用（``follow_redirects=False`` 下仍 200）。

    Starlette 的 ``Mount`` 只匹配 ``/mcp/...``；若只挂载不补别名路由，``POST /mcp`` 会先返回 307，
    而是否跟随重定向取决于各编辑器的 HTTP 客户端。两个 URL 都必须直接可用。
    """
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "nova-tests", "version": "0"},
        },
    }
    response = mcp_env.client.post(
        path, json=body, headers=RPC_HEADERS, follow_redirects=False
    )
    assert response.status_code == 200, response.text
    assert response.headers.get("mcp-session-id")


# --------------------------------------------------------------------------- 正常路径


def test_search_context_returns_rendered_evidence(mcp_env: SimpleNamespace) -> None:
    """``tools/call search_context`` → 文本含 ``render_markdown`` 的证据行（``路径:行号``）。"""
    result = _search(mcp_env, TARGET_SYMBOL)

    assert result.get("isError") is not True, _text(result)
    text = _text(result)
    assert f"{SAMPLE_MODULE_PATH}:" in text, "证据行必须是渲染后的 路径:行号"
    assert text.startswith("[nova] answerable="), "首行是 nova 状态（answerable/confidence）"
    assert "confidence=" in text.splitlines()[0]
    assert "## Relevant Context" in text


def test_search_context_uses_doc_evidence_too(mcp_env: SimpleNamespace) -> None:
    """文档证据同样进包（非 git 目录 + D-29 绝对路径身份的路径能反解 projectId）。"""
    result = _search(mcp_env, "缓存未命中")
    assert result.get("isError") is not True, _text(result)
    assert SAMPLE_DOC_PATH in _text(result)


def test_ask_project_is_explicitly_degraded(mcp_env: SimpleNamespace) -> None:
    """``ask_project`` **未配置 LLM**：如实写明原因，不得让 agent 误以为拿到了总结。

    TASK-088 起：配置了 ``ANSWER_*`` 就走真 LLM；未配置时降级包必须说清"未配置"与
    "缺哪个环境变量"（旧文案"Phase 3 尚未接入"在接入后就成了假话，已改）。
    """
    session_id = _connected(mcp_env.client)
    result = _call_tool(
        mcp_env.client,
        session_id,
        ASK_TOOL,
        {"question": TARGET_QUERY, "project_root": mcp_env.project_root},
    )

    assert result.get("isError") is not True, _text(result)
    text = _text(result)
    assert text.startswith("未配置总结模型")
    assert "ANSWER_BASE_URL" in text, "要告诉管理员缺什么，不要只说没有"
    assert "## Relevant Context" in text
    assert "[nova] answerable=true" in text, "降级包仍带状态行（TASK-032 的既有口径）"


def test_ask_project_returns_llm_answer_when_configured(mcp_env: SimpleNamespace) -> None:
    """配置了 provider 时，``ask_project`` 返回 **LLM 答案**（不再是降级包）。"""
    class _FakeProvider:
        model = "fake-model"

        def complete(self, *, system: str, user: str, max_tokens: int, temperature: float) -> str:
            assert "<evidence>" in user, "证据必须进 prompt（Module/04 §4）"
            return "## Answer\n刷新由 refresh_token 负责 [E1]。\n"

    mcp_env.app.state.settings = Settings(
        data_root=mcp_env.app.state.settings.data_root,
        local_mode=True,
        local_rescan_interval_s=0.0,
        answer_base_url="http://llm.invalid/v1",
        answer_api_key="nova_fake",
        answer_model="fake-model",
    )
    mcp_env.app.state.answer_provider = _FakeProvider()
    mcp_env.app.state.answer_provider_settings = mcp_env.app.state.settings

    session_id = _connected(mcp_env.client)
    result = _call_tool(
        mcp_env.client,
        session_id,
        ASK_TOOL,
        {"question": TARGET_QUERY, "project_root": mcp_env.project_root},
    )

    text = _text(result)
    assert result.get("isError") is not True, text
    assert text.startswith("## Answer"), "有 LLM 时首个内容就是答案"
    assert "degraded=false" in text, "状态行如实报不再降级"


def test_ask_project_short_circuits_when_evidence_insufficient(
    mcp_env: SimpleNamespace,
) -> None:
    """``answerable=false`` 时**不调 LLM**，直接返回尽力而为的上下文（D-24，TASK-107）。

    与 HTTP ``ask`` 路由同语义。MCP 侧此前漏了这一步：证据不足时仍会消耗一次 LLM 调用，
    且返回的回答没有证据支撑。这里用一个仓库里**不存在**的概念提问，逼出 ``answerable=false``。
    """
    calls: list[str] = []

    class _RecordingProvider:
        model = "fake-model"

        def complete(self, *, system: str, user: str, max_tokens: int, temperature: float) -> str:
            calls.append(user)
            return "## Answer\n不该被调用 [E1]。\n"

    mcp_env.app.state.settings = Settings(
        data_root=mcp_env.app.state.settings.data_root,
        local_mode=True,
        local_rescan_interval_s=0.0,
        answer_base_url="http://llm.invalid/v1",
        answer_api_key="nova_fake",
        answer_model="fake-model",
    )
    mcp_env.app.state.answer_provider = _RecordingProvider()
    mcp_env.app.state.answer_provider_settings = mcp_env.app.state.settings

    session_id = _connected(mcp_env.client)
    result = _call_tool(
        mcp_env.client,
        session_id,
        ASK_TOOL,
        {
            "question": "本项目里 Qdrant 向量库的量子态压缩实现位于哪个文件？",
            "project_root": mcp_env.project_root,
        },
    )

    text = _text(result)
    assert result.get("isError") is not True, text
    assert calls == [], "证据不足时不得调用 LLM（D-24 短路）"
    assert "证据不足" in text, "要如实说明为何没给结论"
    assert "## Relevant Context" in text, "仍要给出可直接使用的上下文包"
    assert "[nova] answerable=false" in text


# --------------------------------------------------------------------------- 并发与新鲜度


def test_blocking_core_call_runs_in_threadpool(
    mcp_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """core 调用必须经线程池（``run_in_threadpool``），不得在事件循环里直接跑（卡内纪律）。"""
    threaded: list[str] = []
    real = mcp_module.run_in_threadpool

    async def spy(func: Any, *args: Any, **kwargs: Any) -> Any:
        threaded.append(getattr(func, "__name__", type(func).__name__))
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(mcp_module, "run_in_threadpool", spy)
    _search(mcp_env, TARGET_SYMBOL)
    assert threaded == ["_search_text"]


def test_rescan_hook_is_used_when_interval_enabled(
    make_env: Callable[..., SimpleNamespace], monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地模式下 MCP 检索前会调 TASK-034 的懒重扫（编辑器改代码后仍新鲜，§C 的第二入口）。"""
    calls: list[tuple[str, float]] = []
    original = EngineManager.rescan_if_due

    def spy(self: EngineManager, project_id: str, *, min_interval_s: float) -> bool:
        calls.append((project_id, min_interval_s))
        return original(self, project_id, min_interval_s=min_interval_s)

    monkeypatch.setattr(EngineManager, "rescan_if_due", spy)
    env = make_env(local_rescan_interval_s=2.0)
    _search(env, TARGET_SYMBOL)

    assert calls and calls[0][0] == env.project_id
    assert calls[0][1] == 2.0, "间隔取自 Settings.local_rescan_interval_s"


def test_rescan_disabled_when_interval_is_zero(
    make_env: Callable[..., SimpleNamespace], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``local_rescan_interval_s=0`` → 不发扫描（禁用语义与 TASK-034 §C 一致）。"""
    calls: list[str] = []
    original = EngineManager.rescan_if_due

    def spy(self: EngineManager, project_id: str, *, min_interval_s: float) -> bool:
        calls.append(project_id)
        return original(self, project_id, min_interval_s=min_interval_s)

    monkeypatch.setattr(EngineManager, "rescan_if_due", spy)
    env = make_env(local_rescan_interval_s=0.0)
    _search(env, TARGET_SYMBOL)

    assert calls == [env.project_id], "rescan_if_due 仍被调（由它自己判间隔），但没有触发扫描"


def test_rescan_failure_does_not_break_the_tool_call(
    make_env: Callable[..., SimpleNamespace], monkeypatch: pytest.MonkeyPatch
) -> None:
    """重扫失败不得让检索失败：照常返回结果（TASK-034 §C 纪律在 MCP 面同样成立）。"""
    env = make_env(local_rescan_interval_s=2.0)

    def boom(*_args: Any, **_kwargs: Any) -> bool:
        raise EngineError("磁盘炸了（模拟）")

    monkeypatch.setattr(env.manager, "rescan_if_due", boom)
    result = _search(env, TARGET_SYMBOL)

    assert result.get("isError") is not True, _text(result)
    assert SAMPLE_MODULE_PATH in _text(result)


# --------------------------------------------------------------------------- 错误面


@pytest.mark.parametrize("blank", ["   ", "\n\t"])
def test_whitespace_query_is_rejected_readably(mcp_env: SimpleNamespace, blank: str) -> None:
    """纯空白 query → ``isError=true`` + 可读文本（不抛协议层异常，Module/05 §2.2）。"""
    result = _search(mcp_env, blank)
    assert result["isError"] is True
    assert "不能为空或纯空白" in _text(result)


def test_empty_query_is_rejected(mcp_env: SimpleNamespace) -> None:
    """空字符串 query 由 CF-06 的 ``minLength: 1`` 先拦下（仍是 isError + 指出参数名）。"""
    result = _search(mcp_env, "")
    assert result["isError"] is True
    assert "query" in _text(result)


def test_overlong_query_is_rejected(mcp_env: SimpleNamespace) -> None:
    """超长 query（> ``MAX_QUERY_CHARS``，与 REST 面同一上限）→ isError。

    TASK-MCP-BUDGET：上限 2000→8000（此前比 client 侧的 8000 更严，客户端放行的
    2000～8000 字符查询会在服务端莫名其妙 400）。这里用 8001 字符卡边界。
    """
    result = _search(mcp_env, "令牌" * 4001)
    assert result["isError"] is True
    assert "过长" in _text(result)


def test_windows_style_project_root_is_rejected_readably(mcp_env: SimpleNamespace) -> None:
    """反斜杠路径（Windows 形态）→ 可读的 ``isError``（Module/05 §2.2 的参数错误示例）。"""
    result = _search(mcp_env, "令牌", project_root=r"C:\work\repo")
    assert result["isError"] is True
    assert "正斜杠" in _text(result)


def test_unknown_project_root_is_actionable(mcp_env: SimpleNamespace, tmp_path: Path) -> None:
    """未知 ``project_root``（没索引过的目录）→ isError + "下一步做什么"。"""
    unknown = tmp_path / "not-indexed-yet"
    unknown.mkdir()
    result = _search(mcp_env, "令牌", project_root=str(unknown))

    assert result["isError"] is True
    text = _text(result)
    assert "未知项目" in text
    assert "nova-service local --repo" in text
    assert str(unknown) in text


def test_empty_index_returns_progress_and_retry_hint(
    make_env: Callable[..., SimpleNamespace],
) -> None:
    """空索引（chunks=0）→ isError + **进度信息 + 重试提示**（Module/05 §3.5 语义）。"""
    env = make_env()
    # 换一个只 attach、不上传任何文件的项目（chunks 恒 0）
    empty_repo = env.repo.parent / "empty-repo"
    empty_repo.mkdir()
    attached = env.manager.attach_local(empty_repo, index=False)

    session_id = _connected(env.client)
    result = _call_tool(
        env.client,
        session_id,
        SEARCH_TOOL,
        {"query": "令牌", "project_root": str(attached.root)},
    )

    assert result["isError"] is True
    text = _text(result)
    assert attached.project_id in text
    assert "chunks=0" in text
    assert "索引状态" in text, "必须报告当前索引进度状态（不许假装）"
    assert "稍后重试本查询" in text
    assert f"/api/projects/{attached.project_id}" in text


def test_provider_failure_is_reported_as_root_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空索引 + provider 不可用 → 报根因（不要说"稍后重试"，否则永远好不了）。"""
    monkeypatch.setenv("EMBED_MODE", "api")
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)
    settings = make_settings(tmp_path / "data")
    app = create_app(settings)
    manager = EngineManager.open(settings.data_root)  # 真工厂：配置非法，构造即失败
    app.state.engine_manager = manager
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    try:
        with TestClient(app, base_url=BASE_URL, raise_server_exceptions=False) as client:
            attached = manager.attach_local(repo, index=False)
            session_id = _connected(client)
            result = _call_tool(
                client,
                session_id,
                SEARCH_TOOL,
                {"query": "令牌", "project_root": str(attached.root)},
            )

            assert result["isError"] is True
            text = _text(result)
            assert "provider 当前不可用" in text
            assert "这不是「索引还没跑完」" in text
            assert "EMBED_MODE" in text, "可操作指引来自 TASK-035 的同一份文案"
    finally:
        manager.close()


@pytest.mark.parametrize(
    ("tool", "max_tokens"),
    [(SEARCH_TOOL, 16_001), (ASK_TOOL, 20_001)],
)
def test_max_tokens_beyond_limits_is_rejected(
    mcp_env: SimpleNamespace, tool: str, max_tokens: int
) -> None:
    """超限 ``max_tokens`` → isError（search 的 schema 上限来自 CF-06；ask 由服务端兜底）。"""
    session_id = _connected(mcp_env.client)
    field = "query" if tool == SEARCH_TOOL else "question"
    result = _call_tool(
        mcp_env.client,
        session_id,
        tool,
        {field: "令牌", "project_root": mcp_env.project_root, "max_tokens": max_tokens},
    )
    assert result["isError"] is True
    assert "max_tokens" in _text(result)


def test_engine_error_from_core_is_readable(
    mcp_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """core 抛 ``EngineError`` → isError + 可读文本（不让裸异常冒到协议层）。"""
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise EngineError("非法 project_id：'../etc'")

    monkeypatch.setattr(mcp_env.manager, "search", boom)
    result = _search(mcp_env, "令牌")
    assert result["isError"] is True
    assert "检索失败：非法 project_id" in _text(result)


def test_unexpected_exception_does_not_leak_stack(
    mcp_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未预期异常：客户端只看到一行摘要（堆栈只进日志，Module/06 §3 secret 纪律）。"""

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("内部实现细节：/home/someone/secret/path")

    monkeypatch.setattr(mcp_env.manager, "search", boom)
    result = _search(mcp_env, "令牌")

    assert result["isError"] is True
    assert "服务端日志含完整堆栈" in _text(result)


# --------------------------------------------------------------------------- Origin 防护


def test_malicious_origin_is_rejected(mcp_env: SimpleNamespace) -> None:
    """恶意 ``Origin`` → 403（**回归断言**：不得为了"跑通"关掉 DNS-rebinding 防护）。"""
    session_id = _connected(mcp_env.client)
    evil = _rpc(
        mcp_env.client,
        "tools/list",
        {},
        session_id=session_id,
        extra_headers={"Origin": BAD_ORIGIN},
    )
    assert evil.status_code == 403
    assert "Invalid Origin header" in evil.text

    # 同一请求去掉 Origin（curl / 多数本地客户端）必须正常：防护不能误伤本地使用
    ok = _rpc(mcp_env.client, "tools/list", {}, session_id=session_id)
    assert ok.status_code == 200

    # 本机 Origin（编辑器里的 http://127.0.0.1:<port>）在默认白名单内
    local = _rpc(
        mcp_env.client,
        "tools/list",
        {},
        session_id=session_id,
        extra_headers={"Origin": BASE_URL},
    )
    assert local.status_code == 200, local.text


# --------------------------------------------------------------------------- 响应形态


def test_default_response_is_sse(mcp_env: SimpleNamespace) -> None:
    """默认装配（``create_app``）→ SSE 形态（``text/event-stream``）。"""
    response = _initialize(mcp_env.client)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("event: message")


def test_json_response_mode_is_available(make_env: Callable[..., SimpleNamespace]) -> None:
    """``mount(..., json_response=True)`` → 纯 JSON 形态（编辑器兼容性出问题时的备用开关）。"""
    env = make_env()
    app = FastAPI()
    app.state.settings = env.app.state.settings
    app.state.engine_manager = env.manager
    server = build_mcp(env.manager, settings=env.app.state.settings)
    mount(app, server, json_response=True)
    app.router.lifespan_context = session_lifespan(server)

    with TestClient(app, base_url=BASE_URL, raise_server_exceptions=False) as client:
        response = _initialize(client)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["result"]["serverInfo"]["name"] == "nova"
        session_id = response.headers["mcp-session-id"]
        assert _list_tools(client, session_id)  # 同一 session 上工具仍可用


# --------------------------------------------------------------------------- 装配与 CLI


def test_engine_manager_is_not_built_before_first_call(tmp_path: Path) -> None:
    """挂上 MCP 不得让"起服务即构造引擎"复活（TASK-030 的懒构造纪律）。"""
    app = create_app(make_settings(tmp_path / "data"))
    assert app.state.engine_manager is None
    with TestClient(app, base_url=BASE_URL, raise_server_exceptions=False) as client:
        assert client.get("/healthz").json()["projects"] == []
        assert app.state.engine_manager is None


def test_cli_hint_snippets_contain_the_endpoint_and_no_secret() -> None:
    """编辑器配置片段：含 URL、无 token、明确 stdio 归 M2c（TASK-040 §"编辑器配置输出"）。"""
    url = mcp_url(8787)
    assert url == f"http://127.0.0.1:8787{MCP_MOUNT_PATH}"

    snippets = editor_config_snippets(8787)
    assert len(snippets) == 2
    for snippet in snippets.values():
        assert url in snippet
        assert "token" not in snippet.lower()
    assert '"nova"' in next(iter(snippets.values()))

    text = format_snippets(8787)
    assert url in text
    assert "Rust client" in text


def test_mcp_config_cli_prints_snippets_without_server(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``nova-service mcp-config --port 8787`` 只打片段，不起服务。"""
    from nova_service.__main__ import main

    assert main(["mcp-config", "--port", "8787"]) == 0
    printed = capsys.readouterr().out
    assert "http://127.0.0.1:8787/mcp" in printed
    assert "mcpServers" in printed
