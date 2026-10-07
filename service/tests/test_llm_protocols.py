"""TASK-113 验收：LLM 多协议适配（线格式 + 协议无关的重试循环）。

设计依据：``docs/tasks/TASK-113-LLM多协议适配与连接自检.md``、``docs/design/INDEX.md`` D-47、
``docs/design/Module/04-AI总结.md`` §2。

**纪律：全程不联网**（``httpx.MockTransport`` 或纯函数断言），且断言里出现的 key 都是假串。

| 组 | 守什么 |
|---|---|
| §A 归一 | 别名/大小写/空值/非法值（非法值**必须报错**，不静默回落） |
| §B 端点 | 三类 base（无 `/v1`、带 `/v1`、误粘完整端点）幂等；三种协议路径正确 |
| §C 请求体 | 三种协议各自的字段形状（system 位置、max token 字段名、认证头） |
| §D 响应抽取 | 三种形状 + reasoning-only（空回答）→ 明确报错，不把空串当答案 |
| §E 重试循环 | 三种协议共用同一套重试/退避；401 不重试；503 带协议名与脱敏摘要 |
| §F 回归 | ``HttpAnswerProvider`` 的公开行为与 TASK-088 逐字相同 |
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from nova_service.answer import (
    AnswerAuthError,
    AnswerNotConfiguredError,
    AnswerResponseError,
    AnswerUnavailableError,
    HttpAnswerProvider,
    HttpJsonProvider,
    chat_completions_endpoint,
)
from nova_service.llmprotocol import (
    ANTHROPIC_VERSION,
    DEFAULT_PROTOCOL,
    PROTOCOL_ANTHROPIC,
    PROTOCOL_OPENAI,
    PROTOCOL_RESPONSES,
    SUPPORTED_PROTOCOLS,
    ProtocolShapeError,
    build_request,
    detect_protocol,
    extract_text,
    normalize_protocol,
    protocol_endpoint,
)

FAKE_BASE_URL = "http://127.0.0.1:9"
FAKE_KEY = "sk-fake-task-113-key-SHOULD-NOT-LEAK"


# --------------------------------------------------------------------------- §A 归一


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, PROTOCOL_OPENAI),
        ("", PROTOCOL_OPENAI),
        ("   ", PROTOCOL_OPENAI),
        ("openai", PROTOCOL_OPENAI),
        ("OpenAI", PROTOCOL_OPENAI),
        ("OPENAI-CHAT", PROTOCOL_OPENAI),
        ("chat-completions", PROTOCOL_OPENAI),
        ("openai_compatible", PROTOCOL_OPENAI),
        ("responses", PROTOCOL_RESPONSES),
        ("OpenAI-Responses", PROTOCOL_RESPONSES),
        ("anthropic", PROTOCOL_ANTHROPIC),
        ("messages", PROTOCOL_ANTHROPIC),
        ("Claude", PROTOCOL_ANTHROPIC),
    ],
)
def test_normalize_protocol_aliases(raw: str | None, expected: str) -> None:
    """别名与大小写归一；空值 → 默认（``openai``，升级前行为不变）。"""
    assert normalize_protocol(raw) == expected


def test_normalize_protocol_rejects_unknown_value() -> None:
    """非法协议**显式报错**（不静默回落——回落的后果正是本卡要消灭的静默失灵）。"""
    with pytest.raises(ValueError, match="不支持的协议"):
        normalize_protocol("openai-response")  # 少了 s：真实易错拼写


def test_default_protocol_is_openai_and_is_first_in_supported() -> None:
    """默认协议 = ``openai``（TASK-088 的既有行为）；下拉框顺序以默认项开头。"""
    assert DEFAULT_PROTOCOL == PROTOCOL_OPENAI
    assert SUPPORTED_PROTOCOLS[0] == PROTOCOL_OPENAI
    assert set(SUPPORTED_PROTOCOLS) == {PROTOCOL_OPENAI, PROTOCOL_RESPONSES, PROTOCOL_ANTHROPIC}


# --------------------------------------------------------------------------- §B 端点


@pytest.mark.parametrize(
    "base,expected",
    [
        ("http://host:8080", "http://host:8080/v1/chat/completions"),
        ("http://host:8080/v1", "http://host:8080/v1/chat/completions"),
        ("http://host:8080/", "http://host:8080/v1/chat/completions"),
        ("http://host:8080/v1/", "http://host:8080/v1/chat/completions"),
        # 用户按旧文案（"OpenAI 兼容的 /chat/completions 端点"）误粘完整端点：
        # 先剥后缀再拼，否则会拼出 .../v1/chat/completions/v1/chat/completions。
        (
            "http://host:8080/v1/chat/completions",
            "http://host:8080/v1/chat/completions",
        ),
        ("http://host:8080/chat/completions", "http://host:8080/v1/chat/completions"),
    ],
)
def test_openai_endpoint_join_is_idempotent(base: str, expected: str) -> None:
    """``openai`` 端点幂等（含 TASK-088 的三条既有用例 + 误粘后缀的新用例）。"""
    assert protocol_endpoint(base, PROTOCOL_OPENAI) == expected
    assert chat_completions_endpoint(base) == expected, "旧入口必须与之一致"


@pytest.mark.parametrize(
    "protocol,base,expected",
    [
        (PROTOCOL_RESPONSES, "https://ai.cviauto.cn/ai/transit", "https://ai.cviauto.cn/ai/transit/v1/responses"),
        (PROTOCOL_RESPONSES, "https://gw/v1", "https://gw/v1/responses"),
        (PROTOCOL_RESPONSES, "https://gw/v1/responses", "https://gw/v1/responses"),
        (PROTOCOL_ANTHROPIC, "https://ai.cviauto.cn/ai/transit", "https://ai.cviauto.cn/ai/transit/v1/messages"),
        (PROTOCOL_ANTHROPIC, "https://gw/v1", "https://gw/v1/messages"),
        (PROTOCOL_ANTHROPIC, "https://gw/v1/messages", "https://gw/v1/messages"),
    ],
)
def test_endpoint_paths_per_protocol(protocol: str, base: str, expected: str) -> None:
    """Responses 走 ``/v1/responses``、Anthropic 走 ``/v1/messages``（含误粘后缀纠正）。"""
    assert protocol_endpoint(base, protocol) == expected


def test_endpoint_rejects_empty_base() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        protocol_endpoint("   ", PROTOCOL_OPENAI)


# --------------------------------------------------------------------------- §C 请求体


def test_openai_request_shape_matches_task_088() -> None:
    """``openai`` 请求体与 TASK-088 逐字相同（回归护栏）。"""
    payload, headers = build_request(
        PROTOCOL_OPENAI,
        model="m",
        api_key=FAKE_KEY,
        system="SYS",
        user="USR",
        max_tokens=16,
        temperature=0.2,
    )
    assert payload == {
        "model": "m",
        "messages": [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "USR"},
        ],
        "max_tokens": 16,
        "temperature": 0.2,
    }
    assert headers == {
        "Authorization": f"Bearer {FAKE_KEY}",
        "Content-Type": "application/json",
    }


def test_responses_request_shape_uses_instructions_and_input() -> None:
    """Responses：system 走 ``instructions``、user 走 ``input``、上限走 ``max_output_tokens``。"""
    payload, headers = build_request(
        PROTOCOL_RESPONSES,
        model="deepseek-v4-flash",
        api_key=FAKE_KEY,
        system="SYS",
        user="USR",
        max_tokens=3072,
        temperature=0.2,
    )
    assert payload == {
        "model": "deepseek-v4-flash",
        "instructions": "SYS",
        "input": "USR",
        "max_output_tokens": 3072,
        "temperature": 0.2,
    }
    assert "max_tokens" not in payload, "Responses 不用 max_tokens 字段名"
    assert "messages" not in payload
    assert headers["Authorization"] == f"Bearer {FAKE_KEY}"


def test_anthropic_request_shape_uses_top_level_system_and_api_key_header() -> None:
    """Anthropic：``system`` 是顶层字符串、认证走 ``x-api-key`` + 版本头、``max_tokens`` 必填。"""
    payload, headers = build_request(
        PROTOCOL_ANTHROPIC,
        model="deepseek-v4-pro",
        api_key=FAKE_KEY,
        system="SYS",
        user="USR",
        max_tokens=3072,
        temperature=0.2,
    )
    assert payload == {
        "model": "deepseek-v4-pro",
        "system": "SYS",
        "messages": [{"role": "user", "content": "USR"}],
        "max_tokens": 3072,
        "temperature": 0.2,
    }
    assert headers == {
        "x-api-key": FAKE_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    assert "Authorization" not in headers, "Anthropic 不用 Bearer"


def test_build_request_rejects_unknown_protocol() -> None:
    with pytest.raises(ValueError, match="不支持的协议"):
        build_request(
            "gemini",
            model="m",
            api_key="k",
            system="s",
            user="u",
            max_tokens=1,
            temperature=0.0,
        )


# --------------------------------------------------------------------------- §D 响应抽取


def test_extract_openai_text() -> None:
    body = {"choices": [{"message": {"content": "hello"}}]}
    assert extract_text(PROTOCOL_OPENAI, body) == "hello"


def test_extract_responses_from_output_blocks() -> None:
    """官方结构：``output[].content[].text``，只取 ``output_text`` 类型块。"""
    body = {
        "output": [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "内部推理"}]},
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "## Answer\n结论 [E1]"}],
            },
        ]
    }
    assert extract_text(PROTOCOL_RESPONSES, body) == "## Answer\n结论 [E1]"


def test_extract_responses_prefers_output_text_convenience_field() -> None:
    """网关常直接给 ``output_text``（真实形态）：优先用它。"""
    body = {"output_text": "便捷字段", "output": []}
    assert extract_text(PROTOCOL_RESPONSES, body) == "便捷字段"


def test_extract_responses_reasoning_only_is_a_shape_error() -> None:
    """只产出 reasoning（无正文）→ 报错由上层降级，**绝不把空串当答案**。"""
    body = {"output": [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": "x"}]}]}
    with pytest.raises(ProtocolShapeError, match="空回答"):
        extract_text(PROTOCOL_RESPONSES, body)


def test_extract_anthropic_skips_thinking_blocks() -> None:
    """``content[]`` 里 ``thinking`` 块必须跳过（它不是答案）。"""
    body = {
        "content": [
            {"type": "thinking", "thinking": "内部推理"},
            {"type": "text", "text": "结论"},
            {"type": "text", "text": "第二段"},
        ]
    }
    assert extract_text(PROTOCOL_ANTHROPIC, body) == "结论\n第二段"


@pytest.mark.parametrize(
    "protocol,body",
    [
        (PROTOCOL_OPENAI, {"choices": []}),
        (PROTOCOL_OPENAI, {"choices": [{"message": {"content": "   "}}]}),
        (PROTOCOL_RESPONSES, {}),
        (PROTOCOL_ANTHROPIC, {"content": []}),
        (PROTOCOL_ANTHROPIC, "not-a-dict"),
    ],
)
def test_extract_text_rejects_malformed_bodies(protocol: str, body: Any) -> None:
    """形状不对/内容空白 → :class:`ProtocolShapeError`（上层转降级包，绝不 500）。"""
    with pytest.raises(ProtocolShapeError):
        extract_text(protocol, body)


# --------------------------------------------------------------------------- §E 重试循环


def _provider(handler, protocol: str, **kwargs: Any) -> HttpJsonProvider:
    return HttpJsonProvider(
        base_url=FAKE_BASE_URL,
        api_key=FAKE_KEY,
        model="m",
        protocol=protocol,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=kwargs.pop("sleep", lambda _: None),
        **kwargs,
    )


@pytest.mark.parametrize(
    "protocol,success_body",
    [
        (PROTOCOL_OPENAI, {"choices": [{"message": {"content": "ok"}}]}),
        (PROTOCOL_RESPONSES, {"output_text": "ok"}),
        (PROTOCOL_ANTHROPIC, {"content": [{"type": "text", "text": "ok"}]}),
    ],
)
def test_every_protocol_shares_the_same_retry_loop(
    protocol: str, success_body: dict[str, Any]
) -> None:
    """三种协议都走同一条重试循环：前两次 503、第三次 200 → 成功（证明未复制实现）。"""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json=success_body)

    provider = _provider(handler, protocol)
    assert provider.complete(system="s", user="u", max_tokens=8, temperature=0.0) == "ok"
    assert len(attempts) == 3


@pytest.mark.parametrize(
    "protocol", [PROTOCOL_OPENAI, PROTOCOL_RESPONSES, PROTOCOL_ANTHROPIC]
)
def test_503_error_names_the_protocol_and_redacts_key(protocol: str) -> None:
    """503 降级原因带协议名 + 脱敏后的上游摘要（本次实测的根因可见性修复）。

    实测背景（2026-09-16）：协议不匹配时上游回 503，而旧实现只留状态码，
    用户只看到"总结模型暂时不可用"，无从得知"协议不对"。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=f"protocol mismatch, key={FAKE_KEY}")

    provider = _provider(handler, protocol)
    with pytest.raises(AnswerUnavailableError) as excinfo:
        provider.complete(system="s", user="u", max_tokens=8, temperature=0.0)
    message = str(excinfo.value)
    assert protocol in message, "必须写明是哪个协议失败"
    assert FAKE_KEY not in message, "上游响应里的 key 必须脱敏"
    assert "***" in message


@pytest.mark.parametrize(
    "protocol", [PROTOCOL_OPENAI, PROTOCOL_RESPONSES, PROTOCOL_ANTHROPIC]
)
def test_401_not_retried_and_mentions_protocol(protocol: str) -> None:
    """401 只发一次；错误文案要点出"协议是否匹配"（改 key 与换协议是两种修法）。"""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, text="bad key")

    provider = _provider(handler, protocol)
    with pytest.raises(AnswerAuthError) as excinfo:
        provider.complete(system="s", user="u", max_tokens=8, temperature=0.0)
    assert len(attempts) == 1
    assert protocol in str(excinfo.value)


@pytest.mark.parametrize(
    "protocol,body",
    [
        (PROTOCOL_OPENAI, {"choices": [{"message": {"content": ""}}]}),
        (PROTOCOL_RESPONSES, {"output": []}),
        (PROTOCOL_ANTHROPIC, {"content": []}),
    ],
)
def test_empty_answer_maps_to_answer_response_error(protocol: str, body: dict[str, Any]) -> None:
    """空回答 → ``AnswerResponseError``（上层降级），并带上协议名便于定位。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    provider = _provider(handler, protocol)
    with pytest.raises(AnswerResponseError) as excinfo:
        provider.complete(system="s", user="u", max_tokens=8, temperature=0.0)
    assert protocol in str(excinfo.value)


def test_provider_protocol_property_and_endpoint() -> None:
    """``protocol`` / ``endpoint`` 可读（``/api/meta`` 与历史页据此展示"实际生效值"）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output_text": "ok"})

    provider = _provider(handler, PROTOCOL_RESPONSES)
    assert provider.protocol == PROTOCOL_RESPONSES
    assert provider.endpoint.endswith("/v1/responses")


def test_unknown_protocol_fails_at_construction_not_first_call() -> None:
    """非法协议在**构造期**报错（启动/保存时就暴露，而不是首次 ask 才失败）。"""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不会发请求
        raise AssertionError("不该发请求")

    with pytest.raises(AnswerNotConfiguredError, match="不支持的协议"):
        _provider(handler, "gemini")


# --------------------------------------------------------------------------- §F 回归


def test_http_answer_provider_defaults_to_openai_and_keeps_endpoint() -> None:
    """``HttpAnswerProvider`` 默认仍是 OpenAI Chat Completions（升级不改既有部署行为）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = HttpAnswerProvider(
        base_url=FAKE_BASE_URL,
        api_key=FAKE_KEY,
        model="m",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert provider.protocol == PROTOCOL_OPENAI
    assert provider.endpoint.endswith("/v1/chat/completions")
    assert provider.complete(system="s", user="u", max_tokens=8, temperature=0.0) == "ok"


def test_openai_provider_sends_bearer_header() -> None:
    """回归：openai 协议的认证头仍是 ``Authorization: Bearer``。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = _provider(handler, PROTOCOL_OPENAI)
    provider.complete(system="s", user="u", max_tokens=8, temperature=0.0)
    assert seen["auth"] == f"Bearer {FAKE_KEY}"


def test_anthropic_provider_sends_api_key_header() -> None:
    """Anthropic 协议实发请求时用 ``x-api-key``（而不是 Bearer）。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["api_key"] = request.headers.get("x-api-key", "")
        seen["version"] = request.headers.get("anthropic-version", "")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    provider = _provider(handler, PROTOCOL_ANTHROPIC)
    provider.complete(system="s", user="u", max_tokens=8, temperature=0.0)
    assert seen["api_key"] == FAKE_KEY
    assert seen["version"] == ANTHROPIC_VERSION


# --------------------------------------------------------------------------- §G 协议推断


@pytest.mark.parametrize(
    "supported,expected",
    [
        (["OPENAI", "ANTHROPIC", "RESPONSES"], PROTOCOL_OPENAI),  # 既有实现优先
        (["ANTHROPIC", "RESPONSES"], PROTOCOL_RESPONSES),  # 实测 deepseek-v4-flash
        (["ANTHROPIC"], PROTOCOL_ANTHROPIC),  # 实测 deepseek-v4-pro
        (["RESPONSES"], PROTOCOL_RESPONSES),
        (["SOMETHING_ELSE"], None),  # 认不出来不猜
        ([], None),
        (None, None),
    ],
)
def test_detect_protocol_picks_the_best_supported(
    supported: list[str] | None, expected: str | None
) -> None:
    """推断"建议协议"：既有实现优先 > Responses > Anthropic；认不出来返回 ``None``。"""
    assert detect_protocol(supported) == expected
