"""检索通道 + RRF 融合（TASK-010）：General 路径（D-14 的 General 分支）。

设计依据：``docs/design/Module/02-检索策略.md`` §4.2/§4.3/§5。对外主入口 :func:`recall`：

```python
result = recall(store, "token 过期后在哪里刷新", provider=provider, vector_store=vectors)
result.candidates   # RRF 降序，~120 候选（CF-04 Candidate）
result.degraded     # 向量通道不可用/超时时为 True（Module/02 §5 优雅降级）
```

词法通道在 SQLite 所属线程执行，Vector 在后台与其重叠；各 ``recall_*``
返回有序候选列表，融合仍按原通道顺序统一收集（``fusion.merge``）。
轻路由四分支属 Phase 3（本卡只做 General），
图扩展与 rerank 属 TASK-011，ContextPack 组装属 TASK-012。

冻结口径（TASK-011/012 依赖，见任务卡"执行记录"）：

- 通道名：``exact``（Explicit，tier 0）/ ``inferred``（Inferred，tier 1）/ ``bm25``（tier 1）/
  ``vector``（tier 2）；``channel_ranks`` 的 key 即通道名，rank 1-based；
- ``reasons`` 文本前缀：``explicit symbol`` / ``explicit path`` / ``inferred symbol`` /
  ``bm25`` / ``vector``；
- 降级：``provider`` 或 ``vector_store`` 缺失、或向量通道抛错/超时 → 双通道结果 +
  ``degraded=True``（不抛异常；进程内 60s query embedding 缓存仍复用）。
"""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field

from nova_core.interfaces import EmbeddingProvider
from nova_core.retrieval.bm25 import REASON_BM25, recall_bm25
from nova_core.retrieval.exact import (
    REASON_EXPLICIT_PATH,
    REASON_EXPLICIT_SYMBOL,
    REASON_INFERRED,
    extract_inferred,
    parse_explicit,
    recall_explicit,
    recall_inferred,
)
from nova_core.retrieval.fusion import (
    CHANNEL_BM25,
    CHANNEL_EXACT,
    CHANNEL_INFERRED,
    CHANNEL_LITERAL,
    CHANNEL_VECTOR,
    DEFAULT_POOL_LIMIT,
    KIND_CODE,
    KIND_FALLBACK,
    KIND_SPEC,
    KIND_TEST,
    TIER_EXPLICIT,
    TIER_SEED,
    TIER_VECTOR,
    classify_kind,
    is_test_path,
    make_candidate,
    merge,
)
from nova_core.retrieval.literal import extract_literal_phrases, recall_literal
from nova_core.retrieval.rrf import RRF_K, RrfEntry, fuse_rankings, reciprocal_rank, rrf_score
from nova_core.retrieval.vector import (
    DEFAULT_QUERY_CACHE_TTL_S,
    REASON_VECTOR,
    QueryEmbeddingCache,
    VectorChannelError,
    VectorStoreLike,
    VectorTimeoutError,
    embed_query,
    recall_vector,
)
from nova_core.storage import Store
from nova_core.types import Candidate

__all__ = [
    "CHANNEL_BM25",
    "CHANNEL_EXACT",
    "CHANNEL_INFERRED",
    "CHANNEL_LITERAL",
    "CHANNEL_VECTOR",
    "DEFAULT_POOL_LIMIT",
    "DEFAULT_QUERY_CACHE_TTL_S",
    "KIND_CODE",
    "KIND_FALLBACK",
    "KIND_SPEC",
    "KIND_TEST",
    "REASON_BM25",
    "REASON_EXPLICIT_PATH",
    "REASON_EXPLICIT_SYMBOL",
    "REASON_INFERRED",
    "REASON_VECTOR",
    "RRF_K",
    "QueryEmbeddingCache",
    "RecallLimits",
    "RecallResult",
    "RrfEntry",
    "TIER_EXPLICIT",
    "TIER_SEED",
    "TIER_VECTOR",
    "VectorChannelError",
    "VectorStoreLike",
    "VectorTimeoutError",
    "classify_kind",
    "embed_query",
    "extract_inferred",
    "extract_literal_phrases",
    "fuse_rankings",
    "is_test_path",
    "make_candidate",
    "merge",
    "parse_explicit",
    "recall",
    "recall_bm25",
    "recall_explicit",
    "recall_inferred",
    "recall_literal",
    "recall_vector",
    "reciprocal_rank",
    "rrf_score",
]


@dataclass(frozen=True, slots=True)
class RecallLimits:
    """各通道配额（Module/02 §4.2/§4.3 的默认值；Phase 3 路由只改配额，不新增通道）。"""

    explicit: int = 20               # Exact-Explicit 全量进池上限
    literal: int = 30                # Literal 字面量通道进池上限（TASK-101 §A）
    inferred: int = 20               # Exact-Inferred 普通种子 top 20
    bm25: int = 50                   # BM25 top 50
    vector: int = 50                 # Vector top 50
    pool: int = DEFAULT_POOL_LIMIT   # RRF 池规模（~120）
    vector_timeout_s: float | None = 5.0   # 向量通道超时（None = 不限制）
    query_cache_ttl_s: float = DEFAULT_QUERY_CACHE_TTL_S   # 同 query 复用窗口（60s）


@dataclass(frozen=True, slots=True)
class RecallResult:
    """召回结果：候选池 + 通道健康度（``degraded`` 供 03 组装降级 confidence）。"""

    query: str
    candidates: list[Candidate] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None
    channels_used: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.candidates)


def recall(
    store: Store,
    query: str,
    *,
    provider: EmbeddingProvider | None = None,
    vector_store: VectorStoreLike | None = None,
    limits: RecallLimits | None = None,
    cache: QueryEmbeddingCache | None = None,
    query_vector: Future[list[float]] | None = None,
) -> RecallResult:
    """General 路径三路召回 + RRF 融合（本卡主入口）。

    ``provider`` / ``vector_store`` 为 soft 依赖（TASK-008/009）：缺失时向量通道标记降级，
    Exact+BM25 双通道结果照出（"RRF 自动退化为双通道融合"，Module/02 §5）。
    """
    active = limits or RecallLimits()
    explicit = parse_explicit(query)
    inferred_tokens = extract_inferred(query)
    # 弱字面量短语不与符号通道重复（符号能表达的交给符号通道，见 ``extract_literal_phrases``）。
    literal_phrases = extract_literal_phrases(
        query, covered=(*explicit.symbols, *inferred_tokens)
    )
    query_cache = cache if cache is not None else QueryEmbeddingCache(
        ttl_s=active.query_cache_ttl_s
    )

    # 词法 Store 留在调用线程；向量通道保留原执行超时守卫并与词法工作重叠。
    # 外层等待扣除已重叠时间，不能在词法完成后再追加一次完整超时。
    pool = None
    future = None
    vector_started = time.monotonic()
    if provider is not None and vector_store is not None:
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nova-vector")
        future = pool.submit(
            recall_vector, provider, vector_store, query,
            limit=active.vector, cache=query_cache, timeout_s=active.vector_timeout_s,
            query_vector=query_vector,
        )
    try:
        channels: dict[str, list[Candidate]] = {}
        explicit_candidates = recall_explicit(store, explicit.symbols, limit=active.explicit)
        if explicit_candidates:
            channels[CHANNEL_EXACT] = explicit_candidates
        literal_candidates = recall_literal(store, literal_phrases, limit=active.literal)
        if literal_candidates:
            channels[CHANNEL_LITERAL] = literal_candidates
        inferred_candidates = recall_inferred(store, inferred_tokens, limit=active.inferred)
        if inferred_candidates:
            channels[CHANNEL_INFERRED] = inferred_candidates
        bm25_candidates = recall_bm25(store, query, limit=active.bm25)
        if bm25_candidates:
            channels[CHANNEL_BM25] = bm25_candidates

        degraded = False
        degraded_reason: str | None = None
        if future is None:
            degraded = True
            degraded_reason = "vector 通道不可用：未提供 embedding provider / 向量库"
        else:
            timeout = active.vector_timeout_s
            remaining = None if timeout is None else max(
                0.0, timeout - (time.monotonic() - vector_started)
            )
            try:
                try:
                    vector_candidates = future.result(timeout=remaining)
                except FutureTimeoutError as exc:
                    # 区分 provider/原向量守卫抛出的 TimeoutError 与外层等待超时。
                    if future.done() and future.exception(timeout=0) is exc:
                        raise
                    future.cancel()
                    raise VectorTimeoutError(
                        f"vector 通道超时：{timeout}s 内未返回（Module/02 §5 降级为 Exact+BM25）"
                    ) from exc
            except Exception as exc:  # noqa: BLE001 - 与原路径相同的可观察降级
                degraded = True
                degraded_reason = f"vector 通道降级：{type(exc).__name__}: {exc}"
            else:
                if vector_candidates:
                    # 插入顺序不随线程完成顺序变化；融合、排名及 reasons 完全保持。
                    channels[CHANNEL_VECTOR] = vector_candidates

        candidates = merge(
            channels, store=store, pool_limit=active.pool, explicit_paths=explicit.paths,
        )
        return RecallResult(
            query=query, candidates=candidates, degraded=degraded,
            degraded_reason=degraded_reason, channels_used=tuple(channels),
        )
    finally:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
