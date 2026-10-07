"""TASK-113 验收：LLM 连接自检（``POST /api/auth/llm-config/test`` + ``llmprobe``）。

**纪律：全程不联网**（``httpx.MockTransport`` 注入假上游），断言里出现的 key 都是假串，
且每个用例都断言 **key 不出现在响应里**。

覆盖的分支（逐条对应一种真实配置错误）：

| 用例 | 守住什么 |
|---|---|
| 模型存在 + 声明含当前协议 | ``ok=true``，``supportedProtocols`` 如实回报 |
| 模型存在但只支持别的协议 | ``ok=false`` + ``protocolMismatch=true`` + 建议值（**本次实测根因**） |
| 模型名拼错 | ``ok=false`` + 列出可用模型名（帮用户改正） |
| key 无效（401） | ``ok=false`` + 可操作文案；**不 500** |
| 地址写错（404） | ``ok=false`` + 提示核对 base URL |
| 上游没声明协议 | ``ok=true``（不是所有网关都实现该字段，判失败会让它们永远存不上） |
| deep=true 真实请求成功 | ``ok=true``、``completionOk=true``、回显上游产出 |
| deep=true 真实请求 503 | ``ok=false``（**模型列表正常但推理失败**——本次实测的另一种形态） |
| 未给 key 且库里没有 | 400 ``invalid_llm_config``（不能拿空 key 探测） |
| 非法协议 | 400 ``invalid_llm_protocol``（不静默回落） |
| 测试不写库 | 调用后库里配置与调用前逐字相同 |
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from nova_service.app import create_app
from nova_service.config import Settings
from nova_service.llmprobe import models_endpoint
from nova_service.llmprotocol import (
    ANTHROPIC_VERSION,
    PROTOCOL_ANTHROPIC,
    PROTOCOL_OPENAI,
    PROTOCOL_RESPONSES,
)
from nova_service.metadb import MetaDB

from tests.conftest import make_client

FAKE_KEY = "sk-fake-probe-key-SHOULD-NOT-LEAK-1234"

#: 实测（2026-09-16）cviauto 网关的模型列表形状（截取与本卡相关的三条）。
CVIAUTO_MODELS: dict[str, Any] = {
    "object": "list",
    "data": [
        {
            "id": "deepseek-v4-flash",
            "object": "model",
            "supported_protocols": ["ANTHROPIC", "RESPONSES"],
        },
        {
            "id": "glm-5.3-flash",
            "object": "model",
            "supported_protocols": ["ANTHROPIC", "OPENAI", "RESPONSES"],
        },
        {"id": "deepseek-v4-pro", "object": "model", "supported_protocols": ["ANTHROPIC"]},
    ],
}


def _make(tmp_path, **overrides: object) -> SimpleNamespace:
    """本地模式 app + 测试客户端（本地模式允许用户级 LLM 配置，见 auth 模块 docstring）。"""
    defaults: dict[str, object] = {
        "data_root": tmp_path / "data",
        "local_mode": True,
        "local_rescan_interval_s": 0.0,
    }
    settings = Settings(**{**defaults, **overrides})  # type: ignore[arg-type]
    app = create_app(settings)
    # 本地模式 ``create_app`` 不建库（R34）；用户级 LLM 配置需要元数据库，
    # 因此像既有测试（``test_answer._make``）那样手动接上。
    app.state.meta_db = MetaDB.open(settings.meta_db_path)
    client = make_client(app)
    return SimpleNamespace(app=app, client=client, settings=settings, tmp_path=tmp_path)


@pytest.fixture
def env(tmp_path) -> Iterator[SimpleNamespace]:
    ns = _make(tmp_path)
    with ns.client:
        yield ns


@pytest.fixture
def routed(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch):
    """把 ``llmprobe`` 的 HTTP 客户端换成假上游（按 handler 决定响应）。"""

    def install(handler) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        transport = httpx.MockTransport(wrapped)
        real_client = httpx.Client

        def fake_client(*args: Any, **kwargs: Any) -> httpx.Client:
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr("nova_service.llmprobe.httpx.Client", fake_client)
        return seen

    return install


def _save(ns: SimpleNamespace, **body: Any) -> httpx.Response:
    payload = {"model": "deepseek-v4-flash", "baseUrl": "https://gw/ai/transit", "apiKey": FAKE_KEY}
    payload.update(body)
    return ns.client.put("/api/auth/llm-config", json=payload)


def _test(ns: SimpleNamespace, **body: Any) -> dict[str, Any]:
    """跑一次自检（默认带上本次填的 key；与设置页"在屏幕上正填的那份"同一语义）。"""
    payload = {
        "model": "deepseek-v4-flash",
        "baseUrl": "https://gw/ai/transit",
        "apiKey": FAKE_KEY,
    }
    payload.update(body)
    response = ns.client.post("/api/auth/llm-config/test", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- §A 端点拼接


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://gw/ai/transit", "https://gw/ai/transit/v1/models"),
        ("https://gw/ai/transit/v1", "https://gw/ai/transit/v1/models"),
        ("https://gw/ai/transit/", "https://gw/ai/transit/v1/models"),
        ("https://gw/ai/transit/v1/chat/completions", "https://gw/ai/transit/v1/models"),
    ],
)
def test_models_endpoint_is_idempotent(base: str, expected: str) -> None:
    """``/v1/models`` 的拼接与协议端点同纪律（含误粘完整端点的纠正）。"""
    assert models_endpoint(base) == expected


# --------------------------------------------------------------------------- §B L1 探测


def test_model_found_and_protocol_supported(env: SimpleNamespace, routed) -> None:
    """模型存在且声明支持当前协议 → ``ok=true``，并如实回报声明列表。"""
    routed(lambda request: httpx.Response(200, json=CVIAUTO_MODELS))

    body = _test(env, model="glm-5.3-flash", protocol=PROTOCOL_OPENAI)

    assert body["ok"] is True
    assert body["modelFound"] is True
    assert body["protocol"] == PROTOCOL_OPENAI
    assert set(body["supportedProtocols"]) == {
        PROTOCOL_OPENAI,
        PROTOCOL_RESPONSES,
        PROTOCOL_ANTHROPIC,
    }
    assert body["protocolMismatch"] is False
    assert body["checks"] == ["models"]
    assert FAKE_KEY not in str(body), "key 不得出现在响应任何字段"


def test_protocol_mismatch_is_reported_with_suggestion(env: SimpleNamespace, routed) -> None:
    """**本次实测根因**：``deepseek-v4-flash`` 只支持 ANTHROPIC/RESPONSES，选 openai 必须报出来。"""
    routed(lambda request: httpx.Response(200, json=CVIAUTO_MODELS))

    body = _test(env, model="deepseek-v4-flash", protocol=PROTOCOL_OPENAI)

    assert body["ok"] is False
    assert body["protocolMismatch"] is True
    assert body["suggestedProtocol"] == PROTOCOL_RESPONSES, "建议值优先既有实现之外的首选"
    assert "openai" in body["message"] and "responses" in body["message"]
    assert FAKE_KEY not in str(body)


def test_responses_protocol_matches_deepseek_flash(env: SimpleNamespace, routed) -> None:
    """同一个模型切到 ``responses`` 后 L1 即可通过（证明"换个协议就能用"）。"""
    routed(lambda request: httpx.Response(200, json=CVIAUTO_MODELS))

    body = _test(env, model="deepseek-v4-flash", protocol=PROTOCOL_RESPONSES)

    assert body["ok"] is True
    assert body["protocolMismatch"] is False


def test_anthropic_only_model_suggests_anthropic(env: SimpleNamespace, routed) -> None:
    """``deepseek-v4-pro`` 仅支持 ANTHROPIC → 建议值就是 anthroic，且 openai 判失败。"""
    routed(lambda request: httpx.Response(200, json=CVIAUTO_MODELS))

    body = _test(env, model="deepseek-v4-pro", protocol=PROTOCOL_OPENAI)

    assert body["ok"] is False
    assert body["supportedProtocols"] == [PROTOCOL_ANTHROPIC]
    assert body["suggestedProtocol"] == PROTOCOL_ANTHROPIC


def test_unknown_model_lists_available_names(env: SimpleNamespace, routed) -> None:
    """模型名拼错 → ``modelFound=false`` + 列出该网关可用模型名（用户能照抄改正）。"""
    routed(lambda request: httpx.Response(200, json=CVIAUTO_MODELS))

    body = _test(env, model="deepseek-v4-falsh")  # 拼错：flase

    assert body["ok"] is False
    assert body["modelFound"] is False
    assert "deepseek-v4-flash" in body["message"]
    assert FAKE_KEY not in str(body)


def test_bad_key_reports_actionable_message(env: SimpleNamespace, routed) -> None:
    """key 无效（401）→ ``ok=false`` + 可操作文案；**不 500**。"""
    routed(lambda request: httpx.Response(401, json={"error": "invalid api key"}))

    body = _test(env)

    assert body["ok"] is False
    assert "凭据" in body["message"]
    assert FAKE_KEY not in str(body)


def test_wrong_base_url_reports_404_hint(env: SimpleNamespace, routed) -> None:
    """地址写错（404）→ 提示核对 base URL（与 401 的动作不同，必须分开说）。"""
    routed(lambda request: httpx.Response(404, text="not found"))

    body = _test(env, baseUrl="https://gw/wrong-path")

    assert body["ok"] is False
    assert "base URL" in body["message"] or "接口地址" in body["message"]
    assert FAKE_KEY not in str(body)


def test_unreachable_upstream_is_a_result_not_an_error(env: SimpleNamespace, routed) -> None:
    """网络不通 → 仍是 200 + ``ok=false``（自检失败是结果，不是服务器错误）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    routed(handler)
    body = _test(env)

    assert body["ok"] is False
    assert body["message"], "必须给可操作说明"
    assert FAKE_KEY not in str(body)


def test_gateway_without_protocol_declarations_passes(env: SimpleNamespace, routed) -> None:
    """上游不实现 ``supported_protocols`` → 不能判失败（否则这类网关永远存不上配置）。"""
    routed(lambda request: httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]}))

    body = _test(env, model="deepseek-v4-flash", protocol=PROTOCOL_OPENAI)

    assert body["ok"] is True
    assert body["supportedProtocols"] == []
    assert body["protocolMismatch"] is False
    assert "未声明协议" in body["message"]


# --------------------------------------------------------------------------- §C L2 真实请求


def _two_phase(deep_body: Any, status: int = 200):
    """handler：``/v1/models`` 回完整列表，其它端点回 ``deep_body``。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CVIAUTO_MODELS)
        return httpx.Response(status, json=deep_body)

    return handler


def test_deep_probe_succeeds_and_echoes_output(env: SimpleNamespace, routed) -> None:
    """``deep=true`` 且真实请求成功 → ``ok=true``、``completionOk=true``、回显产出。"""
    routed(
        _two_phase(
            {
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "可用"}]}
                ]
            }
        )
    )

    body = _test(env, model="deepseek-v4-flash", protocol=PROTOCOL_RESPONSES, deep=True)

    assert body["ok"] is True
    assert body["completionOk"] is True
    assert body["checks"] == ["models", "completion"]
    assert "可用" in body["message"]


def test_deep_probe_catches_inference_failure(env: SimpleNamespace, routed) -> None:
    """**模型列表正常但推理失败**（本次实测的另一种形态）→ ``ok=false`` + 脱敏摘要。"""
    routed(_two_phase({"error": {"message": f"upstream down, key={FAKE_KEY}"}}, status=503))

    body = _test(env, model="glm-5.3-flash", protocol=PROTOCOL_OPENAI, deep=True)

    assert body["ok"] is False
    assert body["completionOk"] is False
    assert body["modelFound"] is True, "L1 仍应如实回报模型可见"
    assert "HTTP 503" in body["message"]
    assert FAKE_KEY not in str(body), "上游报错里的 key 必须被抹掉"


def test_deep_probe_reports_empty_answer(env: SimpleNamespace, routed) -> None:
    """reasoning-only 响应（正文为空）→ ``ok=false``（绝不把空回答当成功）。"""
    routed(_two_phase({"output": [{"type": "reasoning", "content": []}]}))

    body = _test(env, model="deepseek-v4-flash", protocol=PROTOCOL_RESPONSES, deep=True)

    assert body["ok"] is False
    assert FAKE_KEY not in str(body)


def test_empty_answer_hints_at_reasoning_token_budget(env: SimpleNamespace, routed) -> None:
    """空回答时补一条**本地成因**推测（实测：推理模型的输出上限被 reasoning 吃光）。

    该场景上游回 HTTP 200 + 空 content，报错文本里毫无线索，用户会以为是配置错了。
    """
    routed(_two_phase({"choices": [{"message": {"content": ""}}]}))

    body = _test(env, model="glm-5.3-flash", protocol=PROTOCOL_OPENAI, deep=True)

    assert body["ok"] is False
    assert "推理" in body["message"]
    assert "ANSWER_MAX_TOKENS" in body["message"], "要给出可操作的下一步"


def test_probe_max_tokens_is_large_enough_for_reasoning_models() -> None:
    """L2 的输出上限必须给推理模型留出空间（实测 16 会被 reasoning 吃光）。"""
    from nova_service.llmprobe import PROBE_MAX_TOKENS

    assert PROBE_MAX_TOKENS >= 256, (
        "实测：max_tokens=16 时 reasoning token 就把它用尽（finish_reason=length、content 为空），"
        "自检会报一个与配置无关的假失败"
    )


def test_deep_uses_anthropic_headers_for_anthropic_protocol(env: SimpleNamespace, routed) -> None:
    """L2 用 anthropic 协议时，认证走 ``x-api-key`` + 版本头（不是 Bearer）。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CVIAUTO_MODELS)
        seen["api_key"] = request.headers.get("x-api-key", "")
        seen["version"] = request.headers.get("anthropic-version", "")
        assert request.headers.get("authorization") is None, "Anthropic 不用 Bearer"
        return httpx.Response(200, json={"content": [{"type": "text", "text": "可用"}]})

    routed(handler)
    body = _test(env, model="deepseek-v4-pro", protocol=PROTOCOL_ANTHROPIC, deep=True)

    assert body["ok"] is True
    assert seen["api_key"] == FAKE_KEY
    assert seen["version"] == ANTHROPIC_VERSION


# --------------------------------------------------------------------------- §D 请求校验与副作用


def test_first_test_without_key_is_400(env: SimpleNamespace, routed) -> None:
    """库里没有旧 key 且请求也没给 → 400（拿空 key 探测只会得到一个误导的 401）。"""
    routed(lambda request: httpx.Response(200, json=CVIAUTO_MODELS))

    response = env.client.post(
        "/api/auth/llm-config/test",
        json={"model": "m", "baseUrl": "https://gw/v1"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_llm_config"


def test_saved_key_is_reused_when_request_omits_it(env: SimpleNamespace, routed) -> None:
    """已保存过 key → 请求可留空沿用（key 从不回显，因此用户无法重填）。"""
    assert _save(env).status_code == 200
    routed(lambda request: httpx.Response(200, json=CVIAUTO_MODELS))

    body = _test(env, model="glm-5.3-flash", protocol=PROTOCOL_OPENAI)

    assert body["ok"] is True


def test_invalid_protocol_is_400_not_silent_default(env: SimpleNamespace, routed) -> None:
    """非法协议 → 400 ``invalid_llm_protocol``（不静默回落 openai）。"""
    routed(lambda request: httpx.Response(200, json=CVIAUTO_MODELS))

    response = env.client.post(
        "/api/auth/llm-config/test",
        json={"model": "m", "baseUrl": "https://gw/v1", "apiKey": FAKE_KEY, "protocol": "gemini"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_llm_protocol"


def test_invalid_protocol_is_400_on_save_too(env: SimpleNamespace) -> None:
    """保存端点同样拒绝非法协议（两处口径一致，否则会出现"保存不了但能测"的怪状态）。"""
    response = _save(env, protocol="gemini")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_llm_protocol"


def test_save_drops_complete_endpoint_suffix_is_not_needed_but_save_keeps_protocol(
    env: SimpleNamespace,
) -> None:
    """保存协议后 ``/api/meta`` 回填该协议（设置页据此显示"当前生效"）。"""
    assert _save(env, protocol=PROTOCOL_RESPONSES).status_code == 200

    llm = env.client.get("/api/meta").json()["config"]["llm"]
    assert llm["source"] == "user"
    assert llm["protocol"] == PROTOCOL_RESPONSES
    assert PROTOCOL_OPENAI in llm["supportedProtocols"]


def test_saving_without_protocol_keeps_the_previous_one(env: SimpleNamespace) -> None:
    """只改模型名（不传协议）→ 保留用户之前选的协议，不被抹成默认。"""
    assert _save(env, protocol=PROTOCOL_ANTHROPIC).status_code == 200
    second = _save(env, model="deepseek-v4-pro", protocol="")
    assert second.status_code == 200
    assert second.json()["protocol"] == PROTOCOL_ANTHROPIC, "协议应保持不变"

    llm = env.client.get("/api/meta").json()["config"]["llm"]
    assert llm["protocol"] == PROTOCOL_ANTHROPIC


def test_test_endpoint_does_not_write_config(env: SimpleNamespace, routed) -> None:
    """自检**不写库**：调用前后库里配置逐字相同（"先测试再保存"必须安全）。"""
    assert _save(env, protocol=PROTOCOL_ANTHROPIC).status_code == 200
    db = MetaDB.open(env.settings.meta_db_path)
    before = db.get_llm_config("local")
    assert before is not None

    routed(lambda request: httpx.Response(200, json=CVIAUTO_MODELS))
    _test(env, model="glm-5.3-flash", protocol=PROTOCOL_OPENAI, deep=False)

    after = db.get_llm_config("local")
    assert after is not None
    assert (after.model, after.protocol, after.api_key) == (
        before.model,
        before.protocol,
        before.api_key,
    ), "自检不得改动已保存的配置"
    db.close()


def test_test_uses_saved_protocol_when_request_omits_it(env: SimpleNamespace, routed) -> None:
    """请求不带协议 → 用**已保存的**协议（测的就是 ask 会用的那一条）。"""
    assert _save(env, protocol=PROTOCOL_ANTHROPIC).status_code == 200
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=CVIAUTO_MODELS)

    routed(handler)
    body = _test(env, model="deepseek-v4-pro", protocol="")

    assert body["protocol"] == PROTOCOL_ANTHROPIC
    assert body["endpoint"].endswith("/v1/messages")


# --------------------------------------------------------------------------- §E 服务端默认


def test_server_default_protocol_is_used_when_nothing_saved(
    tmp_path, routed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """库为空且请求不带协议 → 用 ``settings.answer_protocol``（本次 ask 会用的那份）。"""
    ns = _make(tmp_path, answer_protocol=PROTOCOL_RESPONSES)
    with ns.client:
        routed(lambda request: httpx.Response(200, json=CVIAUTO_MODELS))
        body = _test(ns, model="deepseek-v4-flash", protocol="")
    assert body["protocol"] == PROTOCOL_RESPONSES
    assert body["endpoint"].endswith("/v1/responses")


def test_env_protocol_alias_is_normalized(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``ANSWER_PROTOCOL`` 接受别名（``openai-responses``）并在启动时归一。"""
    settings = Settings.from_env({"ANSWER_PROTOCOL": "OpenAI-Responses"})
    assert settings.answer_protocol == PROTOCOL_RESPONSES


def test_env_protocol_invalid_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ANSWER_PROTOCOL`` 拼错时**启动即报错**，不留到首次 ask 才失败。"""
    with pytest.raises(ValueError, match="不支持的协议"):
        Settings.from_env({"ANSWER_PROTOCOL": "gemini"})


def test_env_protocol_absent_means_default() -> None:
    """未设 ``ANSWER_PROTOCOL`` → ``None``（构造 provider 时回落 openai，行为同升级前）。"""
    assert Settings.from_env({}).answer_protocol is None
