"""OpenAI-compatible embedding API 实现（TASK-008 §C，可选路径）。

隐私语义：与 local.py 相反，**文本会发往第三方服务**（API 提供方）。本模块不写日志、
不落盘，异常信息统一走 ``_redact`` 脱敏，保证 API key 不出现在日志/异常/``repr`` 里。
设置页的隐私告知文案由 Phase 4 负责（本卡只在 docstring 中注明语义）。

协议：``POST {base_url}/v1/embeddings``，请求体 ``{"model": ..., "input": [...]}``，
响应 ``{"data": [{"index": i, "embedding": [...]}]}``（index 用于恢复输入顺序）。

截断与分批（TASK-046 §D，**与 local.py 语义一致**）：

1. **按 token 截断**：每个输入在发送前截到 ``spec.max_input_tokens``（模型实际能力）。
   有自己的 tokenizer 时（``spec.tokenizer_repo_id``）做**精确截断并用同一 tokenizer 回验**；
   拿不到 tokenizer 时回落到**按 UTF-8 字节数**截断（每 token ≥ 1 字节 ⇒ 字节数 ≥ token 数，
   因此是安全上界，代价是英文场景会欠填）。**绝不把超长输入直接发给 API**：
   实测超限请求会被 provider 拒绝（``400 code=20015``）。
2. **按 token 预算分批**：批的切分依据是**累计 token 预算**（默认 ``DEFAULT_BATCH_TOKEN_BUDGET``）
   与条数上限两者取先到者。单条超预算的输入仍单独成批（不拆输入），保证可用性。
   本地 provider 不受此影响（它自己按 token 截断，且批语义由 ONNX 侧决定）。

为什么这么做：卡内实测 ``hello-agents`` 全库（9971 chunks）因 (a) 超长输入 400 与
(b) 批次 token 尖峰触发 TPM 429 而**全量索引失败**（vectors=0）。
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import httpx
import numpy as np

from nova_core.embedding.base import (
    ApiAuthError,
    ApiNetworkError,
    ApiRateLimitError,
    ApiResponseError,
    EmbeddingConfigError,
    EmbeddingDimMismatchError,
    Side,
    l2_normalize,
    with_prefix,
)
from nova_core.embedding.registry import ApiModelSpec, ApiTransportSpec, get_transport
from nova_core.interfaces import EmbeddingProfile

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期需要
    from tokenizers import Tokenizer

#: API 批大小（卡内 §A）。
DEFAULT_BATCH_SIZE = 64

#: 单批的累计 token 预算（TASK-046 §D）：与 ``bge-m3`` 的单条上限同值，
#: 使 64 条短 chunk 的批（~9.4K token）会被拆成两批，而单条长输入仍能独占一批。
DEFAULT_BATCH_TOKEN_BUDGET = 8192

#: 超时：连接 10s / 整体 60s（卡内 §C）。
DEFAULT_TIMEOUT_CONNECT = 10.0
DEFAULT_TIMEOUT_TOTAL = 60.0

#: 失败重试上限（卡内 §C：≤2 次，指数退避）。
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE = 0.5

#: Retry-After 上限，避免服务端给出超长等待把索引 job 挂死。
MAX_RETRY_AFTER = 30.0

_AUTH_STATUS = frozenset({401, 403})
_BEARER_PATTERN = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}")


def build_default_client(
    *,
    timeout_total: float = DEFAULT_TIMEOUT_TOTAL,
    timeout_connect: float = DEFAULT_TIMEOUT_CONNECT,
) -> httpx.Client:
    """构造默认 HTTP 客户端（连接 10s / 整体 60s）。"""
    return httpx.Client(timeout=httpx.Timeout(timeout_total, connect=timeout_connect))


def embeddings_endpoint(base_url: str) -> str:
    """拼 embedding endpoint；``base_url`` 已带 ``/v1`` 时不重复拼接。"""
    base = base_url.strip().rstrip("/")
    if not base:
        raise EmbeddingConfigError("base_url 不能为空（如 https://api.openai.com）")
    return f"{base}/embeddings" if base.endswith("/v1") else f"{base}/v1/embeddings"


class OpenAiCompatibleEmbeddingProvider:
    """OpenAI-compatible API provider（可选实现；API key 全程脱敏）。

    失败语义（供上层决定是否降级，TASK-010）：
    ``ApiAuthError`` / ``ApiRateLimitError`` / ``ApiNetworkError`` / ``ApiResponseError``
    / ``EmbeddingDimMismatchError``，均为 ``EmbeddingError`` 子类。
    """

    def __init__(
        self,
        spec: ApiModelSpec,
        *,
        base_url: str,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        sleep: Callable[[float], None] = time.sleep,
        timeout_total: float = DEFAULT_TIMEOUT_TOTAL,
        timeout_connect: float = DEFAULT_TIMEOUT_CONNECT,
        extra_headers: Mapping[str, str] | None = None,
        tokenizer: Tokenizer | None = None,
        batch_token_budget: int = DEFAULT_BATCH_TOKEN_BUDGET,
        transport: ApiTransportSpec | None = None,
        concurrency: int = 1,
    ) -> None:
        if batch_size < 1:
            raise EmbeddingConfigError(f"batch_size 必须 ≥ 1，收到 {batch_size}")
        if batch_token_budget < 1:
            raise EmbeddingConfigError(f"batch_token_budget 必须 ≥ 1，收到 {batch_token_budget}")
        if concurrency < 1:
            raise EmbeddingConfigError(f"concurrency 必须 ≥ 1，收到 {concurrency}")
        if max_retries < 0:
            raise EmbeddingConfigError(f"max_retries 不能为负，收到 {max_retries}")
        self._spec = spec
        # TASK-049：厂商传输特征（未注入时回落到 generic 的保守默认）。
        self._transport = transport or get_transport("generic")
        if batch_size > self._transport.max_batch_items:
            raise EmbeddingConfigError(
                f"batch_size={batch_size} 超过 {self._transport.provider} 的条数上限 "
                f"{self._transport.max_batch_items}（超限会被 API 拒绝，TASK-049 §8）"
            )
        if batch_token_budget > self._transport.max_batch_tokens:
            raise EmbeddingConfigError(
                f"batch_token_budget={batch_token_budget} 超过 {self._transport.provider} 的 "
                f"token 上限 {self._transport.max_batch_tokens}（TASK-049 §8）"
            )
        self._concurrency = concurrency
        self._endpoint = embeddings_endpoint(base_url)
        #: 只对厂商官方 endpoint 请求 base64：经 base_url 指向的中转/代理不一定认这个参数。
        self._base64 = self._transport.supports_base64 and (
            self._transport.base_url is not None
            and self._endpoint == embeddings_endpoint(self._transport.base_url)
        )
        self._api_key = api_key or None
        self._owns_client = client is None
        self._client = client or build_default_client(
            timeout_total=timeout_total, timeout_connect=timeout_connect
        )
        self._batch_size = batch_size
        self._batch_token_budget = batch_token_budget
        self._tokenizer = tokenizer
        self._tokenizer_load_failed = False
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._extra_headers = dict(extra_headers or {})

    # -- 只读状态 ---------------------------------------------------------

    @property
    def spec(self) -> ApiModelSpec:
        return self._spec

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def profile(self) -> EmbeddingProfile:
        """CF-09 指纹：变更即触发 D-07 二级失效（写 ``index_config``）。"""
        return EmbeddingProfile(
            model_id=self._spec.model_id,
            dim=self._spec.dim,
            max_input_tokens=self._spec.max_input_tokens,
        )

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def batch_token_budget(self) -> int:
        return self._batch_token_budget

    @property
    def transport(self) -> ApiTransportSpec:
        """生效的厂商传输配置（TASK-049）。"""
        return self._transport

    @property
    def concurrency(self) -> int:
        """并发批数（1 = 串行，TASK-049 §3.3）。"""
        return self._concurrency

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(model_id={self._spec.model_id!r}, "
            f"endpoint={self._endpoint!r}, api_key={'***' if self._api_key else None})"
        )

    def close(self) -> None:
        """关闭自建客户端；注入的客户端由调用方负责（避免替别人关连接池）。"""
        if self._owns_client:
            self._client.close()

    # -- 推理 -------------------------------------------------------------

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """CF-09 契约方法：按索引侧（passage）语义嵌入。"""
        return self.embed_side(texts, "passage")

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        """检索侧（query）语义嵌入；未配置前缀的模型与 ``embed`` 等价。"""
        return self.embed_side(texts, "query")

    def embed_array(self, texts: Sequence[str]) -> np.ndarray:
        """:meth:`embed` 的 ``(n, dim)`` float32 矩阵形态（索引流水线用，省掉 float 物化）。"""
        return self.embed_side_array(texts, "passage")

    def embed_side(self, texts: Sequence[str], side: Side) -> list[list[float]]:
        return self.embed_side_array(texts, side).tolist()

    def embed_side_array(self, texts: Sequence[str], side: Side) -> np.ndarray:
        if side not in ("passage", "query"):  # pragma: no cover - 防御性分支
            raise EmbeddingConfigError(f"side 必须是 'passage' / 'query'，收到 {side!r}")
        items = list(texts)
        if not items:
            return np.empty((0, self._spec.dim), dtype=np.float32)
        prefix = self._spec.query_prefix if side == "query" else self._spec.passage_prefix
        # 截断 + 按 token 预算分批（TASK-046 §D）：发请求前就消掉超长输入与批次尖峰。
        prepared = [self._prepare(with_prefix(text, prefix)) for text in items]
        batches = list(
            iter_batches_by_token_budget(
                prepared,
                batch_size=self._batch_size,
                token_budget=self._batch_token_budget,
                count_tokens=self._estimate_tokens,
            )
        )
        # TASK-049 §3.3：并发发送（默认 1 = 串行，行为与之前完全一致）。
        # **顺序必须保持**：结果按批次原顺序拼接（_embed_batch 内部已按 index 排序）。
        if self._concurrency > 1 and len(batches) > 1:
            return self._embed_batches_concurrent(batches)
        return np.concatenate([self._embed_batch(batch) for batch in batches])

    def _embed_batches_concurrent(self, batches: Sequence[Sequence[str]]) -> np.ndarray:
        """并发发送多批（TASK-049 §3.3）。

    失败语义与串行一致：**任一批最终失败 → 整次抛 ``EmbeddingError``**（不部分成功）。
    线程数取 ``min(concurrency, 批数)``；``httpx.Client`` 是线程安全的。
    """
        workers = min(self._concurrency, len(batches))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # map 保持输入顺序；异常在取值时抛出。
            results = list(pool.map(self._embed_batch, batches))
        return np.concatenate(results)

    # -- 截断与分批 -------------------------------------------------------

    def _prepare(self, text: str) -> str:
        """发送前截到 ``max_input_tokens``（卡内 §D-1）。"""
        limit = self._spec.max_input_tokens
        tokenizer = self._ensure_tokenizer()
        if tokenizer is not None:
            return _truncate_with_tokenizer(text, limit, tokenizer)
        # 退路：按 UTF-8 字节数截断（字节数 ≥ token 数，安全上界；英文会欠填）。
        return _truncate_by_bytes(text, limit)

    def _estimate_tokens(self, text: str) -> int:
        """token 数上界估计（供分批预算用；与 ``_prepare`` 同一口径）。"""
        tokenizer = self._ensure_tokenizer()
        if tokenizer is not None:
            return len(tokenizer.encode(text).ids)
        return len(text.encode("utf-8"))

    def _ensure_tokenizer(self) -> Tokenizer | None:
        """惰性加载 tokenizer；拿不到时回落字节估计（不联网失败不得阻断索引）。"""
        if self._tokenizer is not None or self._tokenizer_load_failed:
            return self._tokenizer
        repo_id = self._spec.tokenizer_repo_id
        if not repo_id:
            self._tokenizer_load_failed = True
            return None
        try:  # pragma: no cover - 依赖 HF 缓存/网络，离线测试走字节退路
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer as _Tokenizer

            path = hf_hub_download(repo_id=repo_id, filename="tokenizer.json")
            self._tokenizer = _Tokenizer.from_file(str(path))
        except Exception:
            self._tokenizer_load_failed = True
        return self._tokenizer

    def _embed_batch(self, batch: Sequence[str]) -> np.ndarray:
        # 请求体里的 model 必须是 **provider 认的名字**：registry 的 key（如 ``bge-m3``）只是
        # nova 侧标识，硅基流动要的是 ``BAAI/bge-m3``（TASK-046 §A 的核心断言）。
        payload: dict[str, Any] = {"model": self._spec.api_model, "input": list(batch)}
        if self._base64:
            # 向量以 base64(float32 小端) 返回：响应体约为 JSON 浮点的一半，且解码是
            # 一次 frombuffer，而不是逐个解析上千万个十进制浮点数。
            payload["encoding_format"] = "base64"
        body = self._post(payload)
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(batch):
            got = len(data) if isinstance(data, list) else "非列表"
            raise ApiResponseError(
                f"embedding 响应行数 {got} 与请求 {len(batch)} 不一致（endpoint={self._endpoint}）"
            )
        rows: list[tuple[int, Any]] = []
        for position, row in enumerate(data):
            if not isinstance(row, dict) or "embedding" not in row:
                raise ApiResponseError(
                    "embedding 响应缺少 embedding 字段"
                    f"（第 {position} 行，endpoint={self._endpoint}）"
                )
            index = row.get("index", position)
            if not isinstance(index, int) or index < 0 or index >= len(batch):
                raise ApiResponseError(
                    f"embedding 响应 index={index!r} 非法（第 {position} 行，"
                    f"请求 {len(batch)} 条，endpoint={self._endpoint}）"
                )
            rows.append((index, row["embedding"]))
        rows.sort(key=lambda item: item[0])

        vectors: list[np.ndarray] = []
        for _, raw in rows:
            vector = _as_float_array(raw, endpoint=self._endpoint)
            if len(vector) != self._spec.dim:
                raise EmbeddingDimMismatchError(
                    f"{self._spec.model_id} 返回维度 {len(vector)} 与注册表 dim={self._spec.dim} "
                    f"不一致（endpoint={self._endpoint}）；请修正注册表或显式覆盖 dim"
                    f"（dim 变更会改变 index_config 指纹，触发 D-07 二级失效）"
                )
            vectors.append(vector)
        # 卡内 §A：与本地实现一致，统一返回单位向量（向量库按余弦检索）。
        return l2_normalize(np.stack(vectors))

    # -- HTTP -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        headers.update(self._extra_headers)
        return headers

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        attempts = self._max_retries + 1
        last_network_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._client.post(self._endpoint, json=payload, headers=self._headers())
            except httpx.HTTPError as exc:
                last_network_error = exc
                if attempt + 1 < attempts:
                    self._sleep_before_retry(attempt, response=None)
                    continue
                raise ApiNetworkError(
                    self._redact(
                        f"embedding 请求网络失败（{type(exc).__name__}，endpoint={self._endpoint}，"
                        f"尝试 {attempt + 1}/{attempts}）：{exc!r}"
                    )
                ) from exc

            if response.status_code == 200:
                return self._decode(response)
            if response.status_code in _AUTH_STATUS:
                raise ApiAuthError(
                    self._redact(
                        "embedding 鉴权失败"
                        f"（HTTP {response.status_code}，endpoint={self._endpoint}）；"
                        "请检查 EMBED_API_KEY 是否有效（key 不进入日志/异常文本）"
                    )
                )
            if self._is_retryable(response.status_code) and attempt + 1 < attempts:
                self._sleep_before_retry(attempt, response=response)
                continue
            raise self._status_error(response, attempts, last_network_error)
        raise ApiNetworkError(  # pragma: no cover - 循环必然在内部 raise/return
            f"embedding 请求未完成（endpoint={self._endpoint}）"
        )

    @staticmethod
    def _is_retryable(status_code: int) -> bool:
        return status_code == 429 or status_code >= 500

    def _status_error(
        self, response: httpx.Response, attempts: int, last_network_error: Exception | None
    ) -> Exception:
        snippet = self._redact(response.text[:200].replace("\n", " "))
        detail = f"响应体片段：{snippet}"
        if response.status_code == 429:
            return ApiRateLimitError(
                f"embedding 被限流（HTTP 429，endpoint={self._endpoint}，已尝试 {attempts} 次）；"
                f"{detail}"
            )
        if response.status_code >= 500:
            return ApiResponseError(
                f"embedding 服务端错误（HTTP {response.status_code}，endpoint={self._endpoint}，"
                f"已尝试 {attempts} 次）；{detail}"
            )
        extra = f"（上次网络错误：{last_network_error!r}）" if last_network_error else ""
        return ApiResponseError(
            f"embedding 请求被拒绝（HTTP {response.status_code}，endpoint={self._endpoint}）；"
            f"{detail}{extra}"
        )

    def _sleep_before_retry(self, attempt: int, *, response: httpx.Response | None) -> None:
        delay = self._backoff_base * (2**attempt)
        if response is not None:
            retry_after = _parse_retry_after(response)
            if retry_after is not None:
                delay = max(delay, retry_after)
        self._sleep(delay)

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise ApiResponseError(
                self._redact(
                    f"embedding 响应不是合法 JSON（endpoint={self._endpoint}）：{exc!r}；"
                    f"响应体片段：{response.text[:200]!r}"
                )
            ) from exc
        if not isinstance(body, dict):
            raise ApiResponseError(
                f"embedding 响应结构非法（期望 JSON 对象，endpoint={self._endpoint}）"
            )
        error = body.get("error")
        if error is not None:
            raise ApiResponseError(
                self._redact(
                    f"embedding 响应包含 error 字段（endpoint={self._endpoint}）：{error!r}"
                )
            )
        return body

    def _redact(self, text: str) -> str:
        """异常/日志文本脱敏：key 本体与 ``Bearer <token>`` 形态都不外泄。"""
        if self._api_key:
            text = text.replace(self._api_key, "***")
        return _BEARER_PATTERN.sub(r"\1***", text)


def _truncate_by_bytes(text: str, max_bytes: int) -> str:
    """无 tokenizer 时的保守截断：保留累计 UTF-8 字节数 ≤ ``max_bytes`` 的前缀。

    byte-level BPE 的每个 token 至少对应 1 个字节，故「字节数」是 token 数的**安全上界**；
    代价是英文文本会欠填（英文 ~4 字节/token）。这是退路，不是推荐路径。
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if len(text[:mid].encode("utf-8")) <= max_bytes:
            low = mid
        else:
            high = mid - 1
    return text[:low]


def _truncate_with_tokenizer(text: str, max_tokens: int, tokenizer: Tokenizer) -> str:
    """精确按 token 截断，并用同一 tokenizer 回验（TASK-046 §D-1）。

    XLM-R 系（bge-m3 / e5）在片段边界重新分词会抖 ±2 token（实测原始切片 8189/8190 在 API 侧
    分别按 8191/8192 计，8188 → 8190），因此不能只截一次就发，必须回验。

    回验采用**从原始 ids 递减切片**而不是“解码→重编码→再解码”：后者在边界抖动下会反复回到
    同一长度（不收敛），一旦耗尽迭代就会退到字节退路，对中文是灾难性欠填（实测只填到 1984/8192）。
    这里每轮至少回退 1 token，单调收缩，必定收敛。
    """
    ids = tokenizer.encode(text).ids
    if len(ids) <= max_tokens:
        return text
    limit = max_tokens
    for _ in range(16):
        if limit < 1:  # pragma: no cover - 防御性分支
            break
        candidate = tokenizer.decode(ids[:limit], skip_special_tokens=False)
        actual = len(tokenizer.encode(candidate).ids)
        if actual <= max_tokens:
            return candidate
        limit -= actual - max_tokens + 1  # 至少回退 1 token，保证单调收缩
    return tokenizer.decode(ids[: max(1, max_tokens // 2)], skip_special_tokens=False)


def iter_batches_by_token_budget(
    items: Sequence[str], *, batch_size: int, token_budget: int, count_tokens: Callable[[str], int]
) -> Iterator[Sequence[str]]:
    """按**累计 token 预算**与条数上限切批（TASK-046 §D，两者取先到者）。

    单条超出预算的输入仍会单独成批（绝不拆输入、也不丢输入）：否则长 chunk 会导致死循环或
    静默丢弃。``count_tokens`` 应为 token 数的上界估计（无 tokenizer 时传字节数即可）。
    """
    if batch_size < 1:
        raise EmbeddingConfigError(f"batch_size 必须 ≥ 1，收到 {batch_size}")
    if token_budget < 1:
        raise EmbeddingConfigError(f"token_budget 必须 ≥ 1，收到 {token_budget}")
    current: list[str] = []
    used = 0
    for item in items:
        cost = count_tokens(item)
        if current and (len(current) >= batch_size or used + cost > token_budget):
            yield current
            current = []
            used = 0
        current.append(item)
        used += cost
    if current:
        yield current


def _as_float_array(raw: Any, *, endpoint: str) -> np.ndarray:
    """一行 embedding → float32 向量：JSON 浮点数组，或 base64 编码的 float32 小端字节。"""
    if isinstance(raw, str) and raw:
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ApiResponseError(f"embedding 向量不是合法 base64（{endpoint}）：{exc!r}") from exc
        if not data or len(data) % 4:
            raise ApiResponseError(
                f"embedding base64 向量字节数 {len(data)} 不是 float32 的整数倍（{endpoint}）"
            )
        return np.frombuffer(data, dtype="<f4")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ApiResponseError(f"embedding 向量非法（endpoint={endpoint}）：{type(raw).__name__}")
    try:
        return np.asarray([float(value) for value in raw], dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ApiResponseError(f"embedding 向量含非数值元素（{endpoint}）：{exc!r}") from exc


def _parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    if value <= 0:
        return None
    return min(value, MAX_RETRY_AFTER)
