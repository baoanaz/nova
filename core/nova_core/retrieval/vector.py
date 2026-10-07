"""Vector 通道（TASK-010）：query embedding（TTL 缓存）+ ANN 检索 + 超时/异常降级信号。

设计依据：``docs/design/Module/02-检索策略.md`` §4.2-c、§5（降级）：

- **必须调用 ``provider.embed_query()``**（CF-09 / R2）：e5 类模型 query/passage 前缀不同，
  用 ``embed()`` 会显著掉质量；索引侧一律用 ``embed()``，两侧不得混用；
- 同一 query 默认 60s 内复用（进程内 TTL 缓存），延迟主导项是 query embedding；
- 超时或异常**不在此吞掉**，而是抛 :class:`VectorChannelError` 子类，由 ``recall`` 主编排
  降级为 Exact+BM25 双通道并如实标记 ``degraded=True``（Module/02 §5 优雅降级）。

本模块不 import lancedb：``vector_store`` 只需满足 ``search(vector, top_k) -> list[VectorHit]``
（结构子类型即可，测试用内存桩，生产传 TASK-009 的 ``VectorStore``）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Protocol

from nova_core.interfaces import EmbeddingProvider
from nova_core.retrieval.fusion import CHANNEL_VECTOR, TIER_VECTOR, make_candidate
from nova_core.types import Candidate, VectorHit

__all__ = [
    "DEFAULT_QUERY_CACHE_TTL_S",
    "REASON_VECTOR",
    "QueryEmbeddingCache",
    "VectorChannelError",
    "VectorStoreLike",
    "VectorTimeoutError",
    "embed_query",
    "recall_vector",
]

REASON_VECTOR = "vector"
DEFAULT_QUERY_CACHE_TTL_S = 60.0
_DEFAULT_CACHE_MAXSIZE = 256


class VectorChannelError(RuntimeError):
    """向量通道不可用（provider/向量库异常、模型缺失等）——调用方据此降级。"""


class VectorTimeoutError(VectorChannelError):
    """向量通道超时（``limits.vector_timeout_s`` 内未返回）——调用方据此降级。"""


class VectorStoreLike(Protocol):
    """向量检索的最小接口（TASK-009 ``VectorStore`` 的结构子类型）。"""

    def search(self, vector: Sequence[float], top_k: int) -> list[VectorHit]: ...


class QueryEmbeddingCache:
    """进程内 query embedding TTL 缓存（同一 query 默认 60s 复用）。

    - key = ``(model_id, query 原文)``。**必须带模型身份**（TASK-REVIEW-RUNTIME P2-1）：
      缓存现在由 ``Engine`` 跨查询持有，而 ``Engine.set_provider()`` 可以在生命周期内换模型
      （``--replay`` 切离线 provider、D-47 换用户模型）。只按 query 做 key 会取到**另一个
      模型**的向量——维度不同会直接崩，维度相同则静默给出错误相似度，更难察觉；
    - 不做 query 归一化：分词器两侧已由 BM25 通道负责，向量侧原样输入；
    - 过期条目在 ``get`` 时惰性淘汰；``maxsize`` 溢出时按插入顺序淘汰最旧条目（FIFO）；
    - ``clock`` 可注入（测试用假时钟，避免 sleep）。

    模型身份通过 :meth:`bind_identity` 或构造参数给出；未给时退化为旧行为（只按 query），
    便于单测与不关心模型的调用点。
    """

    def __init__(
        self,
        ttl_s: float = DEFAULT_QUERY_CACHE_TTL_S,
        maxsize: int = _DEFAULT_CACHE_MAXSIZE,
        clock=time.monotonic,
        model: str | None = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._maxsize = maxsize
        self._clock = clock
        self._model = model
        self._entries: dict[str, tuple[float, tuple[float, ...]]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def model(self) -> str | None:
        """当前缓存的模型身份（``None`` = 未绑定，退化为只按 query 做 key）。"""
        return self._model

    def bind_identity(self, model: str | None) -> bool:
        """绑定/切换模型身份；变了就清空（旧向量不再适用）。返回是否发生了切换。"""
        with self._lock:
            if self._model == model:
                return False
            self._model = model
            self._entries.clear()
            return True

    def _key(self, query: str) -> str:
        return f"{self._model}\x00{query}" if self._model is not None else query

    def get(self, query: str) -> list[float] | None:
        with self._lock:
            key = self._key(query)
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            stored_at, vector = entry
            if self._ttl_s >= 0 and self._clock() - stored_at > self._ttl_s:
                del self._entries[key]
                self.misses += 1
                return None
            self.hits += 1
            return list(vector)

    def put(self, query: str, vector: Sequence[float]) -> None:
        with self._lock:
            key = self._key(query)
            if key not in self._entries and len(self._entries) >= self._maxsize:
                oldest = next(iter(self._entries))
                del self._entries[oldest]
            self._entries[key] = (self._clock(), tuple(float(v) for v in vector))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def embed_query(
    provider: EmbeddingProvider,
    query: str,
    cache: QueryEmbeddingCache | None = None,
) -> list[float]:
    """query 侧嵌入（缓存优先）；``provider.embed_query()`` 是唯一合法入口（CF-09）。"""
    if cache is not None:
        cached = cache.get(query)
        if cached is not None:
            return cached
    try:
        vectors = provider.embed_query([query])
    except VectorChannelError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一翻译为可降级的通道错误
        raise VectorChannelError(f"query embedding 失败：{type(exc).__name__}: {exc}") from exc
    if not vectors:
        raise VectorChannelError("query embedding 返回空结果（provider 契约：非空输入必非空输出）")
    vector = [float(value) for value in vectors[0]]
    if cache is not None:
        cache.put(query, vector)
    return vector


def _call_with_timeout(operation, timeout_s: float | None):
    """在独立线程里执行 ``operation`` 并施加超时（``None`` = 不限制）。"""
    if timeout_s is None:
        return operation()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nova-vector")
    try:
        future = pool.submit(operation)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeoutError as exc:
            future.cancel()
            raise VectorTimeoutError(
                f"vector 通道超时：{timeout_s}s 内未返回（Module/02 §5 降级为 Exact+BM25）"
            ) from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def recall_vector(
    provider: EmbeddingProvider,
    vector_store: VectorStoreLike,
    query: str,
    *,
    limit: int = 50,
    cache: QueryEmbeddingCache | None = None,
    timeout_s: float | None = None,
) -> list[Candidate]:
    """向量召回：``embed_query`` → ``vector_store.search(top_k)`` → 排名候选（tier 2）。

    超时/异常统一抛 :class:`VectorChannelError`（含超时子类），不在此降级——降级决策与
    ``degraded`` 标记由 ``recall`` 主编排负责（单一降级点，便于测试与解释）。
    """
    if limit <= 0:
        return []

    def _run() -> list[VectorHit]:
        vector = embed_query(provider, query, cache)
        try:
            return list(vector_store.search(vector, top_k=limit))
        except VectorChannelError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一翻译为可降级的通道错误
            raise VectorChannelError(
                f"向量库检索失败：{type(exc).__name__}: {exc}"
            ) from exc

    hits = _call_with_timeout(_run, timeout_s)
    return [
        make_candidate(
            hit.chunk_id,
            channel=CHANNEL_VECTOR,
            rank=rank,
            tier=TIER_VECTOR,
            reason=f"{REASON_VECTOR} {hit.score:.4f}",
        )
        for rank, hit in enumerate(hits, start=1)
    ]
