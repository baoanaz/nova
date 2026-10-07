"""上游 LLM 协议的**线格式**层（TASK-113，D-47）。

本模块只做三件事，且**只有 stdlib 依赖**（不 import ``answer``，避免循环）：

1. 协议枚举与归一（含别名与非法值报错）；
2. 端点拼接（含"用户误把完整端点当 base URL 粘贴"的容错）；
3. 请求体构造与响应文本抽取（三种协议各自的形状）。

**为什么独立成模块**：``answer.HttpAnswerProvider`` 的超时/重试/退避/脱敏/注册 secret
那套纪律必须只有一份实现，协议差异只应体现在"发什么包、怎么读回复"。
把这两件事分开，协议部分就能在**不发 HTTP** 的前提下被穷举测试（见
``service/tests/test_llm_protocols.py``）。

三种协议的真实差异（2026-09-16 实测 cviauto 网关确认）：

| 协议 | 端点 | 认证 | 输出上限字段 | 回复位置 |
|---|---|---|---|---|
| openai | ``/v1/chat/completions`` | ``Bearer`` | ``max_tokens`` | ``choices[].message.content`` |
| responses | ``/v1/responses`` | ``Bearer`` | ``max_output_tokens`` | ``output[].content[].text`` |
| anthropic | ``/v1/messages`` | ``x-api-key`` | ``max_tokens`` | ``content[].text`` |

认证列是简述：openai/responses 走 ``Authorization: Bearer <key>``，
anthropic 走 ``x-api-key: <key>`` 加一个 API 版本头（``anthropic-version``，必填）。

Responses 的回复也可能出现在便捷字段 ``output_text``（网关常见实现），两种读法都支持；
Anthropic 的 ``content[]`` 里 ``thinking`` 块会被跳过（它不是答案）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "ANTHROPIC_VERSION",
    "DEFAULT_PROTOCOL",
    "ENDPOINT_SUFFIXES",
    "PROTOCOL_ALIASES",
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_LABELS",
    "PROTOCOL_OPENAI",
    "PROTOCOL_RESPONSES",
    "SUPPORTED_PROTOCOLS",
    "UPSTREAM_PROTOCOL_NAMES",
    "ProtocolShapeError",
    "build_request",
    "detect_protocol",
    "extract_text",
    "normalize_protocol",
    "protocol_endpoint",
    "protocol_label",
]

#: OpenAI Chat Completions（TASK-088 的既有实现；默认协议，行为与今天逐字相同）。
PROTOCOL_OPENAI = "openai"
#: OpenAI Responses API（``deepseek-v4-flash`` / ``glm-5.3`` 在 cviauto 网关声明支持它）。
PROTOCOL_RESPONSES = "responses"
#: Anthropic Messages API（``deepseek-v4-pro`` 在 cviauto 网关**仅**声明支持它）。
PROTOCOL_ANTHROPIC = "anthropic"

#: 支持的协议（顺序即设置页下拉框顺序：默认项在最前）。
SUPPORTED_PROTOCOLS: tuple[str, ...] = (
    PROTOCOL_OPENAI,
    PROTOCOL_RESPONSES,
    PROTOCOL_ANTHROPIC,
)

#: 未显式配置时的协议（与 TASK-088 的行为完全一致——升级不改变既有部署的行为）。
DEFAULT_PROTOCOL = PROTOCOL_OPENAI

#: 设置页展示名（用户看到的是"协议名 + 端点"，不是内部枚举值）。
PROTOCOL_LABELS: dict[str, str] = {
    PROTOCOL_OPENAI: "OpenAI Chat Completions（/v1/chat/completions）",
    PROTOCOL_RESPONSES: "OpenAI Responses（/v1/responses）",
    PROTOCOL_ANTHROPIC: "Anthropic Messages（/v1/messages）",
}

#: Anthropic 必需的 API 版本头（不带它上游直接 400/404）。
ANTHROPIC_VERSION = "2023-06-01"

#: 协议别名 → 规范值（用户手填/环境变量里可能出现的各种写法）。
#:
#: 归一是**显式行为**：命中即转换，未命中即报错（不静默取默认——配错了要让用户看见，
#: 与 ``config._as_bool`` 对 ``local_mode`` 的既有纪律一致）。
PROTOCOL_ALIASES: dict[str, str] = {
    "openai": PROTOCOL_OPENAI,
    "openai-chat": PROTOCOL_OPENAI,
    "openai_chat": PROTOCOL_OPENAI,
    "chat": PROTOCOL_OPENAI,
    "chat-completions": PROTOCOL_OPENAI,
    "chat_completions": PROTOCOL_OPENAI,
    "openai-compatible": PROTOCOL_OPENAI,
    "openai_compatible": PROTOCOL_OPENAI,
    "responses": PROTOCOL_RESPONSES,
    "response": PROTOCOL_RESPONSES,
    "openai-responses": PROTOCOL_RESPONSES,
    "openai_responses": PROTOCOL_RESPONSES,
    "anthropic": PROTOCOL_ANTHROPIC,
    "anthropic-messages": PROTOCOL_ANTHROPIC,
    "anthropic_messages": PROTOCOL_ANTHROPIC,
    "messages": PROTOCOL_ANTHROPIC,
    "claude": PROTOCOL_ANTHROPIC,
}

#: 上游 ``/v1/models`` 里 ``supported_protocols`` 的取值 → 本模块的协议值。
#:
#: 上游用大写厂商名（``ANTHROPIC`` / ``RESPONSES`` / ``OPENAI``），实测样例：
#: ``{"id":"deepseek-v4-flash","supported_protocols":["ANTHROPIC","RESPONSES"]}``。
#: 认不出来的名字**忽略**（不猜），未知协议不属于我们的责任面。
UPSTREAM_PROTOCOL_NAMES: dict[str, str] = {
    "openai": PROTOCOL_OPENAI,
    "openai_chat": PROTOCOL_OPENAI,
    "openai-chat": PROTOCOL_OPENAI,
    "chat_completions": PROTOCOL_OPENAI,
    "chat-completions": PROTOCOL_OPENAI,
    "responses": PROTOCOL_RESPONSES,
    "openai_responses": PROTOCOL_RESPONSES,
    "openai-responses": PROTOCOL_RESPONSES,
    "anthropic": PROTOCOL_ANTHROPIC,
    "anthropic_messages": PROTOCOL_ANTHROPIC,
    "anthropic-messages": PROTOCOL_ANTHROPIC,
    "messages": PROTOCOL_ANTHROPIC,
    "claude": PROTOCOL_ANTHROPIC,
}

#: 端点后缀（用户可能把**完整端点**粘进"接口地址"框；先剥掉再拼，避免
#: ``.../v1/chat/completions/v1/chat/completions`` 这类 404）。
#:
#: 按**长度降序**匹配（``/v1/chat/completions`` 必须先于 ``/chat/completions`` 命中）。
ENDPOINT_SUFFIXES: tuple[str, ...] = (
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/messages",
    "/chat/completions",
    "/responses",
    "/messages",
)

#: 建议协议的优先级：**既有实现优先**（减少升级带来的行为变化），其次 Responses，最后 Anthropic。
_PROTOCOL_PRIORITY: tuple[str, ...] = (
    PROTOCOL_OPENAI,
    PROTOCOL_RESPONSES,
    PROTOCOL_ANTHROPIC,
)


class ProtocolShapeError(ValueError):
    """响应形状不符合该协议（**不含 secret**；由调用方映射成 :class:`AnswerResponseError`）。

    单独一个异常类型的原因：本模块不 import ``answer``（避免循环依赖），
    因此无法直接抛 ``AnswerResponseError``；由 ``answer.HttpJsonProvider`` 统一转换，
    使"协议层"与"降级层"的边界清晰。
    """


def normalize_protocol(raw: str | None) -> str:
    """归一协议名；``None``/空串 → :data:`DEFAULT_PROTOCOL`；非法值 → ``ValueError``。

    为什么非法值报错而不是回落默认：用户手填 ``openai-responses`` 拼错成 ``openai-response``
    时，静默回落 ``openai`` 会让他看到"保存成功但 ask 仍 503"——正是本卡要消灭的静默失灵。
    """
    if raw is None or not raw.strip():
        return DEFAULT_PROTOCOL
    key = raw.strip().lower()
    protocol = PROTOCOL_ALIASES.get(key)
    if protocol is None:
        raise ValueError(
            f"不支持的协议 {raw!r}：可选 {sorted(set(PROTOCOL_ALIASES))}"
        )
    return protocol


def protocol_label(protocol: str) -> str:
    """展示名（未知协议回原值，不编造）。"""
    return PROTOCOL_LABELS.get(protocol, protocol)


def _strip_endpoint_suffix(base: str) -> str:
    """剥掉用户误粘的端点后缀（只剥一次，且必须剩下的部分非空）。"""
    for suffix in ENDPOINT_SUFFIXES:
        if base.endswith(suffix):
            stripped = base[: -len(suffix)].rstrip("/")
            return stripped if stripped else base
    return base


def protocol_endpoint(base_url: str, protocol: str = DEFAULT_PROTOCOL) -> str:
    """按协议拼端点（**幂等**：base 带不带 ``/v1``、误粘端点、带尾斜杠都能得到同一结果）。

    与 TASK-088 的 ``chat_completions_endpoint`` 同一形状；本函数是其超集：
    对 ``openai`` 协议，两者的返回值**逐字相同**（既有测试是回归护栏）。

    为什么先剥后缀再判 ``/v1``：用户按设置页旧文案（"OpenAI 兼容的 /chat/completions 端点"）
    填了完整端点，若直接拼接会得到 ``.../v1/chat/completions/v1/chat/completions``。
    """
    base = base_url.strip().rstrip("/")
    if not base:
        raise ValueError("base URL 不能为空")
    base = _strip_endpoint_suffix(base)
    if not base:
        raise ValueError("base URL 不能为空")
    path = {
        PROTOCOL_OPENAI: "chat/completions",
        PROTOCOL_RESPONSES: "responses",
        PROTOCOL_ANTHROPIC: "messages",
    }[protocol]
    prefix = "" if base.endswith("/v1") else "/v1"
    return f"{base}{prefix}/{path}"


def detect_protocol(supported: Sequence[str] | None) -> str | None:
    """上游声明的协议名列表 → 本服务**最优先尝试**的协议；认不出来 → ``None``。

    用途是"测试连接"的**建议值**（不自动保存、不自动切换）：用户看到"该模型支持 Responses，
    建议切换"后自己决定。自动改配置属于静默行为，本卡不做。
    """
    if not supported:
        return None
    found = {
        UPSTREAM_PROTOCOL_NAMES[name.strip().lower()]
        for name in supported
        if isinstance(name, str) and name.strip().lower() in UPSTREAM_PROTOCOL_NAMES
    }
    for protocol in _PROTOCOL_PRIORITY:
        if protocol in found:
            return protocol
    return None


# --------------------------------------------------------------------------- 请求体构造


def build_request(
    protocol: str,
    *,
    model: str,
    api_key: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
) -> tuple[dict[str, Any], dict[str, str]]:
    """构造 ``(payload, headers)``（**含凭据头**；调用方负责不把它写进日志）。

    三种协议的字段名差异集中在这里——这是本模块存在的理由：``answer`` 的重试循环
    不该知道 ``max_tokens`` 与 ``max_output_tokens`` 的区别。
    """
    if protocol == PROTOCOL_OPENAI:
        # 与 TASK-088 逐字相同（回归护栏：既有部署的行为不变）。
        return (
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": int(max_tokens),
                "temperature": float(temperature),
            },
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
    if protocol == PROTOCOL_RESPONSES:
        # Responses API：system 走 instructions、user 走 input；输出上限字段名不同。
        # temperature 仍传（网关可按自身能力忽略）。
        return (
            {
                "model": model,
                "instructions": system,
                "input": user,
                "max_output_tokens": int(max_tokens),
                "temperature": float(temperature),
            },
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
    if protocol == PROTOCOL_ANTHROPIC:
        # Messages API：system 是**顶层字符串**（不是 messages 里的一条），
        # max_tokens 必填；认证走 x-api-key + anthropic-version。
        return (
            {
                "model": model,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "max_tokens": int(max_tokens),
                "temperature": float(temperature),
            },
            {
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "Content-Type": "application/json",
            },
        )
    raise ValueError(f"不支持的协议：{protocol!r}")


# --------------------------------------------------------------------------- 响应抽取


def extract_text(protocol: str, body: Any) -> str:
    """从响应体抽答案文本；形状不对/内容空白 → :class:`ProtocolShapeError`。

    **绝不返回空串当答案**（reasoning 模型可能只产出 reasoning、``content`` 为空）：
    宁可让上层降级，也不把"空回答"伪装成成功（TASK-088 的既有纪律）。
    """
    if not isinstance(body, Mapping):
        raise ProtocolShapeError("响应不是 JSON 对象")
    if protocol == PROTOCOL_OPENAI:
        text = _openai_text(body)
    elif protocol == PROTOCOL_RESPONSES:
        text = _responses_text(body)
    elif protocol == PROTOCOL_ANTHROPIC:
        text = _anthropic_text(body)
    else:
        raise ValueError(f"不支持的协议：{protocol!r}")
    if not text or not text.strip():
        raise ProtocolShapeError("总结模型返回了空回答")
    return text.strip()


def _openai_text(body: Mapping[str, Any]) -> str:
    """``choices[0].message.content``（与 TASK-088 逐字相同）。"""
    choices = body.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise ProtocolShapeError("响应缺少 choices[0].message.content")
    first = choices[0]
    message = first.get("message") if isinstance(first, Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str):
        raise ProtocolShapeError("响应缺少 choices[0].message.content")
    return content


def _responses_text(body: Mapping[str, Any]) -> str:
    """Responses API 的文本。

    两条读取路径（**都是真实形态**，不是猜测）：

    - ``output_text``：网关常提供的便捷字段（字符串）；
    - ``output[].content[].text``：官方结构，只取 ``type`` 为 ``output_text`` / ``text``
      的块（``reasoning`` 块必须跳过——它不是答案）。

    只产出 reasoning 时两条路径都取不到文本 → 报"空回答"由上层降级。
    """
    convenience = body.get("output_text")
    if isinstance(convenience, str) and convenience.strip():
        return convenience
    output = body.get("output")
    if not isinstance(output, Sequence) or isinstance(output, (str, bytes)):
        raise ProtocolShapeError("响应缺少 output[]")
    parts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if str(block.get("type", "")) not in ("output_text", "text"):
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
    return "\n".join(parts)


def _anthropic_text(body: Mapping[str, Any]) -> str:
    """``content[]`` 里 ``type=text`` 的块拼起来（``thinking`` 块跳过）。"""
    content = body.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        raise ProtocolShapeError("响应缺少 content[]")
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if str(block.get("type", "")) not in ("text", ""):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)
