"""``ask`` 的 LLM 总结层：AnswerProvider + Grounded Prompt + Citation 回验（TASK-088）。

设计依据：``docs/design/Module/04-AI总结.md`` §2（接口与参数表）、§4（Grounded Prompt）、
§5（Citation 回验，D-25）、§6（never-empty-handed 降级，D-26）、§8（审计字段）。

本模块的边界（薄壳纪律，D-34）：

- **不做检索/装填/渲染**：证据来自 core 的 ``ContextPack``，prompt 里的证据正文复用 core 的
  ``render_evidence_for_prompt``（D-21 的同一 formatter），本模块只做"分组标签 + 提问"；
- **不做 HTTP 错误→HTTP 响应的映射**：本模块只抛 :class:`AnswerError`（消息已脱敏），
  由 ``routers/query.py`` / ``mcp.py`` 转成 D-26 降级包（**绝不 500**）；
- **不引入新依赖**：只用已在依赖里的 ``httpx``，不加 openai SDK。

三条硬纪律：

1. **key 不进日志/响应**：构造 provider 时 ``register_secret(api_key)``（全仓日志脱敏的
   第二道防线），并且本模块自己再对异常文本做一次字面量替换——即使有人手滑把 key 拼进
   日志 message，也不会落盘；
2. **未配置 = 未配置**：三个必填项任一为空 → :func:`build_provider` 返回 ``None``，
   ``ask`` 走降级包（L5：开源产品冷启动体验），**不在启动期报错**；
3. **回验是确定性的零成本操作**：只做正则抽取 + 集合比对，不删整句、不改写措辞
   （Module/04 §5）；``citationCoverage`` 只用于观察分布，不用于惩罚输出。
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx
from nova_core.contextpack import render_evidence_for_prompt
from nova_core.types import ContextPack, EvidenceItem, Flow

from nova_service.config import Settings
from nova_service.llmprotocol import (
    DEFAULT_PROTOCOL,
    ProtocolShapeError,
    normalize_protocol,
    protocol_endpoint,
)
from nova_service.llmprotocol import (
    build_request as build_protocol_request,
)
from nova_service.llmprotocol import (
    extract_text as extract_protocol_text,
)
from nova_service.logging import get_logger, redact_text, register_secret

__all__ = [
    "DEFAULT_PROTOCOL",
    "ANSWER_SECTIONS",
    "CONNECT_TIMEOUT_S",
    "DEFAULT_BACKOFF_BASE",
    "DEFAULT_MAX_RETRIES",
    "MAX_RETRY_AFTER",
    "SYSTEM_PROMPT",
    "SYSTEM_PROMPT_RULES",
    "AnswerAuthError",
    "AnswerError",
    "AnswerNotConfiguredError",
    "AnswerOutcome",
    "AnswerProvider",
    "AnswerResponseError",
    "AnswerUnavailableError",
    "CitationCheck",
    "HttpAnswerProvider",
    "HttpJsonProvider",
    "answer_question",
    "build_provider",
    "build_user_prompt",
    "chat_completions_endpoint",
    "estimate_tokens",
    "provider_for_app",
    "verify_citations",
]

logger = get_logger("nova_service.answer")

#: 连接超时（Module/04 §2 参数表：连接 10s，整体由 ``ANSWER_TIMEOUT_S`` 决定）。
CONNECT_TIMEOUT_S = 10.0
#: 失败重试上限（Module/04 §6：≤2 次）。
DEFAULT_MAX_RETRIES = 2
#: 指数退避基数（秒）：0.5 / 1.0。
DEFAULT_BACKOFF_BASE = 0.5
#: 尊重服务端 ``Retry-After``，但不允许它把请求挂死。
MAX_RETRY_AFTER = 30.0
#: 值得重试的 HTTP 状态（超时/限流/网关类；4xx 业务错误重试没有意义）。
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# --------------------------------------------------------------------------- Grounded Prompt（§4）


#: 七条规则（Module/04 §4 **原文**）。逐条成元组，测试据此断言一条都不少。
SYSTEM_PROMPT_RULES: tuple[str, ...] = (
    "1. 只能依据 Evidence 回答；不得使用外部知识推测仓库内容",
    "2. 引用格式：[E1] 代码/文档证据，[F1] 调用链证据；每个重要结论必须附引用",
    "3. Code 证据代表当前实现，Doc 证据代表设计意图；两者冲突时必须显式指出"
    '"实现与设计可能不一致"，并分别列出双方证据，不得只取其一',
    "4. 调用关系只信 [F*] 与证据内容；禁止虚构任何调用关系",
    '5. 证据不足时明确回答"证据不足"并说明缺什么，不要猜测',
    "6. 直接回答问题；不要复述全部证据",
    "7. 用与 <question> 相同的语言回答",
)

#: 回答模板的五个节（空节自动省略；§4）。
ANSWER_SECTIONS: tuple[str, ...] = (
    "## Answer",
    "## Code Flow",
    "## Key Evidence",
    "## Spec vs Implementation",
    "## Missing Evidence",
)

#: System prompt（§4 的 seven-rule 正式版 + 回答模板）。
SYSTEM_PROMPT = "\n".join(
    (
        "你是 nova 项目调查助手。只依据 <evidence> 标签内的内容回答 <question>。",
        "",
        "规则：",
        *SYSTEM_PROMPT_RULES,
        "",
        "回答模板（**空节自动省略**）：",
        *ANSWER_SECTIONS,
    )
)

#: prompt 里证据的分组标签（§4 的 ``[Docs]`` / ``[Code]`` / ``[Flows]``）。
_GROUP_LABELS: dict[str, str] = {"### Code": "[Code]", "### Docs": "[Docs]"}
#: ``render_evidence_for_prompt`` 的分节标题（core 改动时这里会显式失败而不是静默丢内容）。
_SECTION_RE = re.compile(r"^### ", re.MULTILINE)

_CITATION_RE = re.compile(r"\[([EF])(\d+)\]")
_MISSING_HEADING = "## Missing Evidence"
_CODE_FENCE_RE = re.compile(r"^\s*(```|~~~)")


class AnswerError(Exception):
    """LLM 调用失败（**消息已脱敏**）。

    ``query.py`` / ``mcp.py`` 捕获它并转成 D-26 降级包：``ask`` 绝不因为 LLM 出问题而 500
    或返回空手（Module/04 §6）。
    """

    #: 给用户看的粗粒度分类（写进日志与降级说明，不回显 provider 的原始响应体）。
    kind = "answer_error"


class AnswerNotConfiguredError(AnswerError):
    """未配置 ``ANSWER_*``（三个必填项不全）。"""

    kind = "not_configured"


class AnswerUnavailableError(AnswerError):
    """超时 / 网络失败 / 5xx 重试用尽（Module/04 §6 的"总结模型暂时不可用"）。"""

    kind = "unavailable"


class AnswerAuthError(AnswerError):
    """401 / 403：密钥或模型名无效（§6：走同一条降级路径）。"""

    kind = "unauthorized"


class AnswerResponseError(AnswerError):
    """HTTP 200 但响应形状不对或内容为空（provider 返回了非 OpenAI-compatible 结构）。"""

    kind = "bad_response"


@runtime_checkable
class AnswerProvider(Protocol):
    """CF-09（``core/nova_core/interfaces.py``）的同名协议；实现放在 service 侧（Module/04 §2）。"""

    def complete(self, *, system: str, user: str, max_tokens: int, temperature: float) -> str: ...


# --------------------------------------------------------------------------- HTTP 实现（§2/§6）


def chat_completions_endpoint(base_url: str) -> str:
    """拼 chat completions endpoint；``base_url`` 已带 ``/v1`` 时不重复拼接。

    与 ``core.embedding.api.embeddings_endpoint`` 同一形状：用户既可能填
    ``http://host:8080/v1``（本卡 ``.env`` 的写法）也可能填 ``http://host:8080``。

    TASK-113 起改为 :func:`nova_service.llmprotocol.protocol_endpoint` 的 ``openai`` 特例
    （同一实现，新增的能力是"误粘完整端点也能纠正"）——返回值与 TASK-088 **逐字相同**。
    """
    try:
        return protocol_endpoint(base_url, DEFAULT_PROTOCOL)
    except ValueError as exc:
        raise AnswerNotConfiguredError(str(exc)) from None


class HttpJsonProvider:
    """HTTP JSON provider 的**共通部分**（TASK-113）：超时 / 重试 / 退避 / 脱敏只有一份。

    协议差异只有两处，由子类（或 ``protocol`` 参数）给出："发什么包"
    （``llmprotocol.build_request``）与"怎么读回复"（``llmprotocol.extract_text``）。
    这样新增协议不必复制重试循环——而重试/退避/脱敏恰恰是最容易在复制中漂移的部分。

    - 超时：整体 ``timeout_s`` / 连接 ``CONNECT_TIMEOUT_S``；
    - 重试：网络异常与 :data:`RETRY_STATUS` ≤ ``max_retries`` 次，指数退避，尊重 ``Retry-After``；
    - 脱敏：构造时 :func:`register_secret` 登记明文 key，异常文本再过一次字面量替换。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        protocol: str = DEFAULT_PROTOCOL,
        timeout_s: float = 60.0,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url.strip():
            raise AnswerNotConfiguredError("ANSWER_BASE_URL 不能为空")
        if not api_key.strip():
            raise AnswerNotConfiguredError("ANSWER_API_KEY 不能为空")
        if not model.strip():
            raise AnswerNotConfiguredError("ANSWER_MODEL 不能为空")
        # 协议非法 → 构造期即报错（配置错误要在"保存/启动"时暴露，而不是首次 ask 才失败）。
        try:
            self._protocol = normalize_protocol(protocol)
            self._endpoint = protocol_endpoint(base_url, self._protocol)
        except ValueError as exc:
            raise AnswerNotConfiguredError(str(exc)) from None
        self._api_key = api_key
        self._model = model
        # 登记明文 key：全仓 JSON 日志的 RedactingFilter 会把它抹成 ***（第二道防线）。
        register_secret(api_key)
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s)
        )
        self._owns_client = client is None
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.0, float(backoff_base))
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self._model

    @property
    def protocol(self) -> str:
        """本次实际使用的协议（D-47：写进 ``/api/meta`` 与日志，供用户核对）。"""
        return self._protocol

    @property
    def endpoint(self) -> str:
        """请求地址（**不含 key**；可用于日志与设置页自检）。"""
        return self._endpoint

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete(self, *, system: str, user: str, max_tokens: int, temperature: float) -> str:
        """一次 grounded 调用（Module/04 §3：恰好一次调用，重试只针对传输失败）。"""
        payload, headers = build_protocol_request(
            self._protocol,
            model=self._model,
            api_key=self._api_key,
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        last_error: AnswerError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(self._endpoint, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                detail = _redact(type(exc).__name__, self._api_key)
                last_error = AnswerUnavailableError(
                    f"总结模型响应超时（{self._endpoint}）：{detail}"
                )
            except httpx.HTTPError as exc:
                detail = _redact(type(exc).__name__, self._api_key)
                last_error = AnswerUnavailableError(
                    f"总结模型不可达（{self._endpoint}）：{detail}"
                )
            else:
                if response.status_code in (401, 403):
                    # 凭据/模型名无效：重试没有意义（Module/04 §6 归入同一条降级路径）。
                    # TASK-113：带上协议名——"密钥无效"与"协议不匹配"在用户侧都是 401/403，
                    # 而修法完全不同（换 key vs 换协议），不写清就只能靠猜。
                    raise AnswerAuthError(
                        f"总结模型拒绝凭据（HTTP {response.status_code}，协议 {self._protocol}）："
                        "请检查 ANSWER_API_KEY / ANSWER_MODEL / 协议是否匹配该模型"
                    )
                if response.status_code in RETRY_STATUS:
                    # TASK-113：把上游响应体摘要（脱敏、截断）带进降级原因——实测 503 时不带
                    # 摘要会让"协议不匹配"这种根因完全不可见（用户只看到"暂时不可用"）。
                    # 摘要**不回显给调用方**（routers 层只记日志），因此不破 D-26 的脱敏纪律。
                    detail = _short(response.text)
                    suffix = f"：{_redact(detail, self._api_key)}" if detail else ""
                    last_error = AnswerUnavailableError(
                        f"总结模型返回 HTTP {response.status_code}"
                        f"（协议 {self._protocol}）{suffix}"
                    )
                    self._sleep_backoff(attempt, response)
                    continue
                if response.status_code >= 400:
                    raise AnswerResponseError(
                        f"总结模型返回 HTTP {response.status_code}"
                        f"（{_redact(_short(response.text), self._api_key)}）"
                    )
                return self._extract_content(response)
            self._sleep_backoff(attempt, None)
        raise last_error or AnswerUnavailableError("总结模型不可用（原因未知）")

    def _sleep_backoff(self, attempt: int, response: httpx.Response | None) -> None:
        """指数退避 + ``Retry-After``（有上限；最后一次失败后不再睡）。"""
        if attempt >= self._max_retries:
            return
        delay = self._backoff_base * (2**attempt)
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    delay = max(delay, min(float(retry_after), MAX_RETRY_AFTER))
                except ValueError:
                    pass
        if delay > 0:
            self._sleep(delay)

    def _extract_content(self, response: httpx.Response) -> str:
        """按协议抽答案文本；形状不对/空白 → :class:`AnswerResponseError`。

        协议差异委托给 :func:`nova_service.llmprotocol.extract_text`（三种形状的读取规则
        在那里被穷举测试）。此处只做"协议异常 → 降级异常"的转换与脱敏。

        reasoning 模型可能只产出 reasoning、正文为空：如实报"空回答"，由上层降级
        （绝不把空串当答案返回给用户）。
        """
        try:
            body: Any = response.json()
        except ValueError as exc:
            detail = _redact(type(exc).__name__, self._api_key)
            raise AnswerResponseError(f"总结模型响应不是 JSON：{detail}") from None
        try:
            return extract_protocol_text(self._protocol, body)
        except ProtocolShapeError as exc:
            raise AnswerResponseError(
                f"总结模型返回了空回答（协议 {self._protocol}）：{exc}"
            ) from None
        except ValueError as exc:  # 不支持的协议（构造期已拦，防御性）
            raise AnswerResponseError(str(exc)) from None


class HttpAnswerProvider(HttpJsonProvider):
    """OpenAI-compatible ``/chat/completions`` provider（TASK-088 的默认协议）。

    TASK-113 起它只是 :class:`HttpJsonProvider` 的 ``openai`` 特例：公开行为（端点、请求体、
    响应抽取、重试、脱敏）与升级前**逐字相同**，既有测试是回归护栏。需要其它协议时直接用
    基类传 ``protocol``（见 ``llmconfig.build_provider_for``），不必新增子类。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        protocol: str = DEFAULT_PROTOCOL,
        timeout_s: float = 60.0,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            protocol=protocol,
            timeout_s=timeout_s,
            connect_timeout_s=connect_timeout_s,
            max_retries=max_retries,
            backoff_base=backoff_base,
            client=client,
            sleep=sleep,
        )


def _redact(text: str, secret: str) -> str:
    """字面量替换 + 通用脱敏（**双保险**：即使 ``register_secret`` 未生效也不落明文）。"""
    scrubbed = text.replace(secret, "***") if secret else text
    return redact_text(scrubbed)


def _short(text: str, limit: int = 300) -> str:
    """截断响应体再脱敏（避免把整段 provider 报错贴进日志/异常）。"""
    collapsed = " ".join(text.split())
    return collapsed[:limit]


# --------------------------------------------------------------------------- 构造与缓存


def build_provider(
    settings: Settings, *, client: httpx.Client | None = None
) -> AnswerProvider | None:
    """按配置构造 provider；**未配置 → ``None``**（调用方走 D-26 降级包）。

    三个必填项来自 ``ANSWER_BASE_URL`` / ``ANSWER_API_KEY`` / ``ANSWER_MODEL``；
    ``ANSWER_TIMEOUT_S`` / ``ANSWER_MAX_TOKENS`` / ``ANSWER_TEMPERATURE`` 有内置默认值
    （改 env 即改行为，代码里没有第二套常量）；``ANSWER_PROTOCOL``（TASK-113，D-47）
    选择上游协议，未配置时等于升级前的行为（``openai``）。
    """
    if not settings.answer_configured:
        return None
    return HttpAnswerProvider(
        base_url=str(settings.answer_base_url),
        api_key=str(settings.answer_api_key),
        model=str(settings.answer_model),
        protocol=normalize_protocol(settings.answer_protocol),
        timeout_s=settings.answer_timeout_s,
        client=client,
    )


_provider_lock = threading.Lock()


def provider_for_app(app: Any) -> AnswerProvider | None:
    """按 ``app.state.settings`` 缓存 provider（同一配置只建一个 HTTP 客户端）。

    顺序：**先看注入的 provider**（测试/未来替换实现用同一个接缝），再判"配置是否齐备"
    （未配置 → ``None``，调用方走降级包），最后才自建。

    为什么缓存：``ask`` 是热路径，每个请求新建 ``httpx.Client`` 会丢掉连接复用；
    又因为 ``Settings`` 是不可变对象、每个 app 一个实例，用**身份比较**判断配置有没有变，
    不比较（也不落）任何 secret 内容。
    """
    settings = getattr(getattr(app, "state", None), "settings", None)
    if not isinstance(settings, Settings):
        return None
    cached = getattr(app.state, "answer_provider", None)
    if isinstance(cached, AnswerProvider) and (
        getattr(app.state, "answer_provider_settings", None) is settings
    ):
        return cached
    if not settings.answer_configured:
        return None
    with _provider_lock:
        cached = getattr(app.state, "answer_provider", None)
        if isinstance(cached, AnswerProvider) and (
            getattr(app.state, "answer_provider_settings", None) is settings
        ):
            return cached
        provider = build_provider(settings)
        app.state.answer_provider = provider
        app.state.answer_provider_settings = settings
        return provider


# --------------------------------------------------------------------------- prompt 组装（§4）


def build_user_prompt(pack: ContextPack, question: str) -> str:
    """``<question>`` + ``<context_meta>`` + ``<evidence>``（Module/04 §4 的结构）。"""
    return "\n".join(
        (
            f"<question>{question}</question>",
            "<context_meta>",
            f"confidence: {pack.confidence}",
            f"index: {index_status(pack)}",
            "</context_meta>",
            "<evidence>",
            build_evidence_block(pack),
            "</evidence>",
        )
    )


def index_status(pack: ContextPack) -> str:
    """``fresh`` / ``stale(N files)`` / ``indexing(N files)`` / ``unknown``（§4 的 meta 行）。

    与 ``core`` 渲染的 ``### Meta`` 行同一语义（``indexing`` 优先于 ``stale``），
    但这里是**给 LLM 的短标记**，不带 ``fresh (x min ago)`` 这类相对时间。
    """
    freshness = pack.freshness
    if freshness.indexing_files:
        return f"indexing({len(freshness.indexing_files)} files)"
    if freshness.stale_files:
        return f"stale({len(freshness.stale_files)} files)"
    return "fresh" if freshness.indexed_at is not None else "unknown"


def build_evidence_block(pack: ContextPack) -> str:
    """证据分块（``[Docs]`` / ``[Code]`` / ``[Flows]``）。

    Code/Docs 的**逐条正文复用 core 的** ``render_evidence_for_prompt``（D-21 的同一 formatter：
    证据 id、``文件:行号``、reason 与行号化正文都只有一份实现）；本函数只做两件事：

    1. 把它的分节标题 ``### Code`` / ``### Docs`` 换成 §4 要求的分组标签，并按
       ``Docs`` → ``Code`` → 其他 的顺序排列（未知新分节原样保留，不静默丢内容）；
    2. 追加 ``[Flows]`` 一节（core 的 prompt formatter 不含 flows；这里是给 LLM 的调用链标记）。
    """
    sections = _split_sections(render_evidence_for_prompt(pack))
    blocks: list[str] = []
    for label in ("[Docs]", "[Code]"):
        block = sections.pop(label, None)
        if block:
            blocks.append(block)
    blocks.extend(sections.values())
    flows = _flows_block(pack.flows)
    if flows:
        blocks.append(flows)
    return "\n".join(blocks)


def _split_sections(text: str) -> dict[str, str]:
    """按 ``### `` 标题切成 ``{标签: 块}``（标题行换成 §4 的 ``[Docs]`` / ``[Code]``）。

    块里**包含标签行本身**（否则分组标签会丢，LLM 看不到代码/文档的区分）；
    未知分节保留原标题行（不静默丢内容）。
    """
    if not text.strip():
        return {}
    sections: dict[str, str] = {}
    heading: str | None = None
    buffer: list[str] = []

    def _store(key: str | None, body: list[str]) -> None:
        if key is not None:
            sections[key] = "\n".join([key, *body]).strip()

    for line in text.splitlines():
        if _SECTION_RE.match(line):
            _store(heading, buffer)
            heading = _GROUP_LABELS.get(line.strip(), line.strip())
            buffer = []
            continue
        buffer.append(line)
    _store(heading, buffer)
    return sections


def _flows_block(flows: Sequence[Flow]) -> str:
    """``[Flows]`` 节（``[F1] A → B → C``；无调用链则不出现该节）。"""
    if not flows:
        return ""
    lines = ["[Flows]"]
    for flow in flows:
        chain = " → ".join(node.symbol for node in flow.nodes)
        lines.append(f"[{flow.id}] {chain}{'（已截断）' if flow.truncated else ''}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- 主流程


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    """一次成功的 LLM 总结（回验后）＋审计用的三个观测值（Module/04 §8）。"""

    answer: str
    latency_ms: float
    answer_tokens: int
    valid_citations: int
    invalid_citations: int
    citation_coverage: float | None


def answer_question(
    *,
    provider: AnswerProvider,
    settings: Settings,
    pack: ContextPack,
    question: str,
) -> AnswerOutcome:
    """调用 LLM 并做 Citation 回验（Module/04 §4 §5）。

    ``max_tokens`` / ``temperature`` **一律取自配置**（``Settings.answer_*``）——用户改
    ``ANSWER_MAX_TOKENS`` / ``ANSWER_TEMPERATURE`` 即改行为，本函数里没有硬编码的策略值。

    失败时抛 :class:`AnswerError`（消息已脱敏），由调用方转成 D-26 降级包。
    """
    user = build_user_prompt(pack, question)
    started = time.perf_counter()
    raw = provider.complete(
        system=SYSTEM_PROMPT,
        user=user,
        max_tokens=settings.answer_max_tokens,
        temperature=settings.answer_temperature,
    )
    latency_ms = (time.perf_counter() - started) * 1000.0
    check = verify_citations(raw, pack)
    return AnswerOutcome(
        answer=check.answer,
        latency_ms=latency_ms,
        answer_tokens=estimate_tokens(check.answer),
        valid_citations=len(check.valid),
        invalid_citations=len(check.invalid),
        citation_coverage=check.coverage,
    )


def estimate_tokens(text: str) -> int:
    """答案长度的 token 估算（写审计 ``answerTokens``）。

    **是估算不是计数**：本服务不引 tokenizer（无新依赖），按"约 2 个字符 1 token"折算——
    中文约 1.5 字符/token、英文约 4 字符/token，取中间值。用途是观察分布（§8），
    不是计费；真要精确计数时由 provider 侧返回 ``usage``（V1 不依赖它）。
    """
    return max(1, math.ceil(len(text) / 2)) if text else 0


# --------------------------------------------------------------------------- Citation 回验（§5）


@dataclass(frozen=True, slots=True)
class CitationCheck:
    """回验结果：``answer`` 是**已删无效标记**的答案（原文其余部分逐字保留）。"""

    answer: str
    valid: tuple[str, ...]
    invalid: tuple[str, ...]
    coverage: float | None
    paragraphs: int


def verify_citations(answer: str, pack: ContextPack) -> CitationCheck:
    """确定性回验（D-25）：无效引用**只删标记不改写**，并在 Missing Evidence 追加注记。

    - ``[E*]`` 的有效集合 = ``pack.evidence`` + ``pack.docs`` 的 id（共用 E 编号空间，D-21）；
    - ``[F*]`` 的有效集合 = ``pack.flows`` 的 id；
    - ``citationCoverage = 有效引用数 / 结论段落数``（上限 1.0）：
      * 有效引用数按**出现次数**计（同一 id 引用两次算两处）；
      * 结论段落数 = 非空、非标题、且不在代码块内的行数（模板的空节不产生行）。
      **不惩罚输出**：覆盖率只入审计，不改变返回给用户的答案（§5）。
    """
    allowed = _allowed_ids(pack)
    valid: list[str] = []
    invalid: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        cid = f"{match.group(1)}{match.group(2)}"
        if cid in allowed:
            valid.append(cid)
            return match.group(0)
        invalid.append(cid)
        return ""

    stripped = _CITATION_RE.sub(_replace, answer)
    if invalid:
        stripped = _append_missing_note(stripped, len(invalid))
    cleaned = _collapse_gaps(stripped)
    paragraphs = _count_paragraphs(cleaned)
    coverage = (
        round(min(1.0, len(valid) / paragraphs), 4) if paragraphs > 0 else None
    )
    return CitationCheck(
        answer=cleaned,
        valid=tuple(valid),
        invalid=tuple(invalid),
        coverage=coverage,
        paragraphs=paragraphs,
    )


def _allowed_ids(pack: ContextPack) -> frozenset[str]:
    """ContextPack 里存在的引用 id（E 编号 + F 编号）。"""
    items: Sequence[EvidenceItem] = [*pack.evidence, *pack.docs]
    return frozenset({item.id for item in items} | {flow.id for flow in pack.flows})


def _append_missing_note(answer: str, count: int) -> str:
    """在 ``## Missing Evidence`` 节末尾追加「回答中 N 处引用无效已移除」。

    没有该节就补一节（§5 要求注记可见）；有该节就追加成同节末尾的一行，**不动其他节的任何字符**。
    """
    note = f"- 回答中 {count} 处引用无效已移除"
    lines = answer.splitlines()
    index = next(
        (
            i
            for i, line in enumerate(lines)
            if line.strip().lower().replace(" ", "") == _MISSING_HEADING.lower().replace(" ", "")
        ),
        None,
    )
    if index is None:
        return f"{answer.rstrip()}\n\n{_MISSING_HEADING}\n{note}"
    end = next(
        (j for j in range(index + 1, len(lines)) if lines[j].startswith("## ")), len(lines)
    )
    body = list(lines[index + 1 : end])
    while body and not body[-1].strip():
        body.pop()
    merged = [*lines[: index + 1], *body, note, *lines[end:]]
    return "\n".join(merged)


def _collapse_gaps(answer: str) -> str:
    """删掉标记后留下的连续空格压成一个（**代码块内不动**，避免破坏缩进/对齐）。

    只处理"删标记造成的空隙"这一类空白，句子本身逐字不动（§5 的"不改写"）。
    """
    out: list[str] = []
    in_fence = False
    for line in answer.splitlines():
        if _CODE_FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        out.append(line if in_fence else re.sub(r"[ \t]{2,}", " ", line))
    return "\n".join(out)


def _count_paragraphs(answer: str) -> int:
    """结论段落数：非空、非标题、且不在代码块内的行。"""
    count = 0
    in_fence = False
    for line in answer.splitlines():
        if _CODE_FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        stripped = line.strip()
        if in_fence or not stripped or stripped.startswith("#"):
            continue
        count += 1
    return count
