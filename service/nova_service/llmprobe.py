"""LLM 连接自检（TASK-113）：探测 ``/v1/models`` 并按需发一次最小请求。

**为什么需要它**（真实故障驱动，2026-09-16）：用户把配置粘进设置页 → 保存返回 200 →
`ask_project` 每次 503。保存端点的校验只看了"字符串非空、URL 形状"，
于是**key 错、模型名拼错、协议不匹配**这三种致命的配置错误都要等到真正提问才暴露，
而那时的现象（"总结模型暂时不可用"）完全没有指向性。本模块把这三件事变成一次显式的、秒级的检查。

两级设计（用户 2026-09-16 拍板）：

| 级 | 做什么 | 成本 |
|---|---|---|
| L1 | ``GET {base}/v1/models``：key 是否有效、模型名是否精确存在、模型声明支持哪些协议 | 零 token |
| L2 | 用最小 prompt 真发一次请求（走用户当前选的协议） | 少量 token |

L1 能查出"模型名拼错"与"协议不匹配"（后者靠上游的 ``supported_protocols`` 声明），
但**测不出**"模型列表正常而推理 503"（正是本次故障的形态之一）——所以 L2 是独立的一级，
而不是 L1 的加强版。

纪律：

1. **key 不出网**：本模块的任何返回值都不得包含 key（连长度都不给）；
2. **不抛异常给调用方**：一切失败都变成结构化结果（``ok=False`` + 可操作文案），
   由路由层包成 200 响应——"测试连接失败"是一个**正常结果**，不是服务器错误；
3. **上游报错摘要脱敏后再回**：走 ``logging.redact_text`` 并替换本模块登记的明文 key。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from nova_service.llmprotocol import (
    DEFAULT_PROTOCOL,
    PROTOCOL_LABELS,
    detect_protocol,
    protocol_endpoint,
)
from nova_service.logging import get_logger, redact_text, register_secret

__all__ = [
    "PROBE_MAX_TOKENS",
    "PROBE_PROMPT",
    "MODELS_TIMEOUT_S",
    "ConnectionTestResult",
    "models_endpoint",
    "probe_models",
    "run_connection_test",
    "test_completion",
]

logger = get_logger("nova_service.llmprobe")

#: ``/v1/models`` 的超时（探测是交互式动作，不该让用户等 60 秒）。
MODELS_TIMEOUT_S = 15.0
#: L2 的最小 prompt 与输出上限。
PROBE_PROMPT = "回复两个字：可用"
#: L2 的输出上限。
#:
#: **不能给太小**（实测 2026-09-16）：推理类模型的 `max_tokens` 是**推理 token + 正文**的总预算，
#: 给 16 时 reasoning 就把它吃光（`finish_reason=length`、`content` 为空、
#: `usage.completion_tokens_details.reasoning_tokens=16`），于是自检会报一个
#: **与配置无关的假失败**。512 足够这类模型推理完并输出两个字，同时仍属可忽略的量级。
PROBE_MAX_TOKENS = 512


def models_endpoint(base_url: str) -> str:
    """``{base}/v1/models``（``base`` 已带 ``/v1`` 时不重复）。

    复用 :func:`protocol_endpoint` 的剥后缀逻辑：用户把 ``/v1/chat/completions`` 粘进来时，
    这里也要能得到正确的 models 端点（而不是 404）。
    """
    from nova_service.llmprotocol import ENDPOINT_SUFFIXES  # 局部导入避免 __all__ 膨胀

    base = base_url.strip().rstrip("/")
    if not base:
        raise ValueError("base URL 不能为空")
    for suffix in ENDPOINT_SUFFIXES:
        if base.endswith(suffix):
            stripped = base[: -len(suffix)].rstrip("/")
            if stripped:
                base = stripped
                break
    # 复用 protocol_endpoint 的 /v1 判定，但路径换成 models。
    with_models = protocol_endpoint(base, DEFAULT_PROTOCOL)  # .../v1/chat/completions
    prefix = with_models[: -len("/chat/completions")]
    return f"{prefix}/models"


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    """一次连接自检的结果（**绝不含 key**；``ok`` 是唯一的总判定）。"""

    ok: bool
    #: 失败的可操作说明（中文，面向用户；不含 key、不含上游原始报文）。
    message: str
    #: 本次检查用的协议（用户选择或服务端默认）。
    protocol: str
    #: 实际请求的端点（不含凭据；便于用户核对 URL 是否写错）。
    endpoint: str
    #: 该模型是否在上游的模型列表里被精确找到（``None`` = 没做/做不成 L1）。
    model_found: bool | None = None
    #: 上游为该模型声明的协议（已归一为本服务的取值；认不出来的被忽略）。
    supported_protocols: tuple[str, ...] = ()
    #: 按上游声明推断的**建议协议**（``None`` = 无法推断）。
    suggested_protocol: str | None = None
    #: 用户选择的协议与上游声明不一致（前端据此提示"该模型不支持你选的协议"）。
    protocol_mismatch: bool = False
    #: L2 是否跑过、是否通过（``None`` = 没跑）。
    completion_ok: bool | None = None
    #: 上游可读到的报错摘要（已脱敏、已截断）。
    detail: str | None = None
    #: 本次实际检查了哪些级（``("models",)`` / ``("models","completion")``）。
    checks: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        """对外形态（``key`` 与上游原始报文都不在其中）。"""
        return {
            "ok": self.ok,
            "message": self.message,
            "protocol": self.protocol,
            "protocolLabel": PROTOCOL_LABELS.get(self.protocol, self.protocol),
            "endpoint": self.endpoint,
            "modelFound": self.model_found,
            "supportedProtocols": list(self.supported_protocols),
            "suggestedProtocol": self.suggested_protocol,
            "protocolMismatch": self.protocol_mismatch,
            "completionOk": self.completion_ok,
            "detail": self.detail,
            "checks": list(self.checks),
        }


def _summarize(text: str, api_key: str, limit: int = 200) -> str:
    """上游文本摘要：先替换明文 key，再走通用脱敏并截断（**不回显完整报文**）。"""
    scrubbed = text.replace(api_key, "***") if api_key else text
    collapsed = " ".join(redact_text(scrubbed).split())
    return collapsed[:limit]


def _short_status_error(response: httpx.Response, api_key: str) -> str:
    """``HTTP 4xx/5xx`` 的可读摘要（含状态码 + 脱敏后的 body 片段）。"""
    body = _summarize(response.text, api_key)
    return f"HTTP {response.status_code}" + (f"：{body}" if body else "")


def _normalize_supported(raw: Any) -> tuple[str, ...]:
    """上游 ``supported_protocols`` → 本服务认识的协议元组（去重，保持优先级顺序）。

    认不出来的名字被忽略（不猜）；顺序按 :data:`llmprotocol.detect_protocol` 的优先级，
    让前端的"支持：a > b"读起来与"建议"一致。
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    names = [str(item) for item in raw if isinstance(item, str)]
    found = detect_protocol(names)  # 只是借用它的归一：逐个判定更直观
    from nova_service.llmprotocol import SUPPORTED_PROTOCOLS, UPSTREAM_PROTOCOL_NAMES

    known = {
        UPSTREAM_PROTOCOL_NAMES[name.strip().lower()]
        for name in names
        if name.strip().lower() in UPSTREAM_PROTOCOL_NAMES
    }
    ordered = tuple(protocol for protocol in SUPPORTED_PROTOCOLS if protocol in known)
    if found is not None and found not in ordered:  # pragma: no cover - 防御性
        ordered = (*ordered, found)
    return ordered


def probe_models(
    *,
    base_url: str,
    api_key: str,
    model: str,
    client: httpx.Client,
) -> tuple[bool, Any]:
    """``GET {base}/v1/models``；返回 ``(是否成功, 解析后的 body 或错误说明)``。

    不抛异常：调用方（:func:`run_connection_test`）需要区分"网络不通"与"key 无效"
    并给出不同文案，因此把失败也当数据返回。
    """
    register_secret(api_key)  # 第二道防线：即便异常文本带 key，日志也会脱敏
    endpoint = models_endpoint(base_url)
    try:
        response = client.get(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        # 网络类失败也是**结果**：自检要区分"地址不可达"与"key 无效"，两者动作不同。
        return False, f"无法连接（{type(exc).__name__}）：请检查接口地址与网络"
    if response.status_code >= 400:
        return False, _short_status_error(response, api_key)
    try:
        body = response.json()
    except ValueError:
        return False, "上游 /v1/models 返回的不是 JSON"
    return True, body


def _find_model(body: Any, model: str) -> Mapping[str, Any] | None:
    """在模型列表里精确找 ``model``（也认 ``data[].id`` 的常见变体）。"""
    if not isinstance(body, Mapping):
        return None
    data = body.get("data")
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        return None
    for item in data:
        if isinstance(item, Mapping) and str(item.get("id", "")) == model:
            return item
    return None


def test_completion(
    *,
    protocol: str,
    base_url: str,
    api_key: str,
    model: str,
    client: httpx.Client,
    timeout_s: float = 30.0,
) -> tuple[bool, str | None, str | None]:
    """L2：用最小 prompt 真发一次请求；返回 ``(是否成功, 失败说明, 成功回显)``。

    走 :class:`nova_service.answer.HttpJsonProvider` 的**同一实现**（不另写一套 HTTP 逻辑）：
    重试/超时/响应抽取/脱敏全部复用，因此"测试通过"与"ask 能用"是同一个判据。

    成功时的回显（``echo``）是上游真实产出的前若干字符——让用户看到"确实出内容了"，
    而不是只看到一个绿灯。
    """
    from nova_service.answer import AnswerError, HttpJsonProvider

    provider = HttpJsonProvider(
        base_url=base_url,
        api_key=api_key,
        model=model,
        protocol=protocol,
        timeout_s=timeout_s,
        max_retries=0,  # 自检不需要重试：失败要立刻如实回报，不拖 2 秒
        client=client,
    )
    try:
        text = provider.complete(
            system="你是连通性自检助手，只按要求回复极短内容。",
            user=PROBE_PROMPT,
            max_tokens=PROBE_MAX_TOKENS,
            temperature=0.0,
        )
    except AnswerError as exc:
        return False, _completion_failure_hint(_summarize(str(exc), api_key)), None
    except Exception as exc:  # noqa: BLE001 - 自检绝不把异常抛给路由（失败即结果）
        logger.warning("连接自检异常：%s", redact_text(f"{type(exc).__name__}: {exc}"))
        return False, f"{type(exc).__name__}（详见服务端日志）", None
    return True, None, text[:80]


def run_connection_test(
    *,
    protocol: str,
    base_url: str,
    api_key: str,
    model: str,
    deep: bool = False,
    client: httpx.Client | None = None,
    timeout_s: float = 60.0,
) -> ConnectionTestResult:
    """跑一次连接自检（L1，``deep=True`` 时叠加 L2）。

    **判定语义**（每条都对应一种真实的配置错误）：

    | 情况 | ``ok`` | ``message`` 说什么 |
    |---|---|---|
    | L1 失败（网络/401） | ``False`` | 端点不可达 / key 无效（可操作） |
    | 模型名不在列表 | ``False`` | 列出该网关上**存在**的模型名（帮用户改正拼写） |
    | 协议不在该模型声明里 | ``False`` | 声明支持哪些协议 + 建议值 |
    | 声明里有当前协议 | ``True`` | 通过；``protocol_mismatch=False`` |
    | 上游没声明协议信息 | ``True`` | 通过（无法判定，如实说明"未声明"） |
    | L2 失败 | ``False`` | 上游报错摘要（已脱敏） |

    为什么"上游没声明"算通过：不是所有网关都实现 ``supported_protocols``；
    把"没信息"判成失败会让这些网关永远无法保存配置。此时由 L2 兜底。
    """
    owns = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10.0))
    register_secret(api_key)
    try:
        try:
            endpoint = protocol_endpoint(base_url, protocol)
        except ValueError as exc:
            return ConnectionTestResult(
                ok=False, message=str(exc), protocol=protocol, endpoint="", checks=(),
            )

        checks: list[str] = ["models"]
        models_ok, payload = probe_models(
            base_url=base_url, api_key=api_key, model=model, client=http
        )
        if not models_ok:
            detail = str(payload)
            message = _models_failure_message(detail)
            return ConnectionTestResult(
                ok=False,
                message=message,
                protocol=protocol,
                endpoint=endpoint,
                detail=detail,
                checks=tuple(checks),
            )

        found = _find_model(payload, model)
        if found is None:
            available = _available_models(payload)
            return ConnectionTestResult(
                ok=False,
                message=(
                    f"上游模型列表里没有 {model!r}（请核对模型名拼写）"
                    + (f"；该网关当前可用：{', '.join(available[:10])}" if available else "")
                ),
                protocol=protocol,
                endpoint=endpoint,
                model_found=False,
                checks=tuple(checks),
            )

        supported = _normalize_supported(found.get("supported_protocols"))
        suggested = detect_protocol([p.upper() for p in supported]) if supported else None
        mismatch = bool(supported) and protocol not in supported

        base_kwargs: dict[str, Any] = {
            "protocol": protocol,
            "endpoint": endpoint,
            "model_found": True,
            "supported_protocols": supported,
            "suggested_protocol": suggested,
            "protocol_mismatch": mismatch,
        }

        if mismatch:
            return ConnectionTestResult(
                ok=False,
                message=(
                    f"模型 {model} 在上游只声明支持 "
                    f"{'、'.join(supported)}，与你选的 {protocol} 不匹配"
                    + (f"；建议改为 {suggested}" if suggested else "")
                ),
                checks=tuple(checks),
                **base_kwargs,
            )

        if deep:
            checks.append("completion")
            completion_ok, failure, echo = test_completion(
                protocol=protocol,
                base_url=base_url,
                api_key=api_key,
                model=model,
                client=http,
                timeout_s=timeout_s,
            )
            if not completion_ok:
                return ConnectionTestResult(
                    ok=False,
                    message=f"模型列表可见，但真实请求失败：{failure}（协议 {protocol}）",
                    completion_ok=False,
                    detail=failure,
                    checks=tuple(checks),
                    **base_kwargs,
                )
            return ConnectionTestResult(
                ok=True,
                message=f"连通（真实请求成功）：{echo}" if echo else "连通（真实请求成功）",
                completion_ok=True,
                checks=tuple(checks),
                **base_kwargs,
            )

        # L1 通过：声明里有当前协议（或上游没声明协议信息）。
        message = (
            "连通（/v1/models 可访问，模型存在）"
            if supported
            else "连通（/v1/models 可访问；上游未声明协议，建议再跑一次真实请求确认）"
        )
        return ConnectionTestResult(ok=True, message=message, checks=tuple(checks), **base_kwargs)
    finally:
        if owns:
            http.close()


def _completion_failure_hint(detail: str) -> str:
    """给空回答失败补一条**本地成因**推测（不改上游报错本身）。

    实测 2026-09-16：推理模型的 `max_tokens` 被 reasoning token 吃光时，上游回的是
    HTTP 200 + 空 `content`（`finish_reason=length`），报错文本里毫无线索，
    用户会以为是配置错了。这里把可能成因说清，并把动作指向可验证的下一步。
    """
    if "空回答" not in detail:
        return detail
    return (
        f"{detail}；若上游是推理类模型，通常是输出上限被推理过程耗尽"
        "（可提高 ANSWER_MAX_TOKENS 或换用非推理模型）"
    )


def _available_models(payload: Any) -> list[str]:
    """模型列表里的 id（错误文案里列出可用值，帮用户改正拼写）。"""
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        return []
    return [str(item.get("id")) for item in data if isinstance(item, Mapping) and item.get("id")]


def _models_failure_message(detail: str) -> str:
    """把 ``/v1/models`` 的失败摘要翻译成用户能照做的下一步。

    区分 401/403（key 或地址不对）与其它（网络/网关）——两者的动作完全不同。
    """
    if "HTTP 401" in detail or "HTTP 403" in detail:
        return f"接口地址可达，但凭据被拒绝（{detail}）：请核对 API Key 与协议"
    if "HTTP 404" in detail:
        return f"该地址下没有 /v1/models（{detail}）：请确认接口地址（base URL）是否写错"
    if detail.startswith("无法连接"):
        return detail
    return f"无法访问上游模型列表（{detail}）：请检查接口地址与网络"
