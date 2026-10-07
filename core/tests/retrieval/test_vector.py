"""TASK-010 Vector 通道（DoD：60s 内复用只调 1 次 embedding；异常/超时降级不抛）。"""

from __future__ import annotations

import pytest
from nova_core.retrieval import recall
from nova_core.retrieval.fusion import CHANNEL_VECTOR, TIER_VECTOR
from nova_core.retrieval.vector import (
    QueryEmbeddingCache,
    VectorChannelError,
    VectorTimeoutError,
    embed_query,
    recall_vector,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_embed_query_uses_provider_embed_query_only(provider) -> None:
    cache = QueryEmbeddingCache(ttl_s=60.0)
    vector = embed_query(provider, "token 过期", cache)
    assert len(vector) == provider.profile.dim
    assert provider.embed_query_calls == 1
    assert provider.embed_calls == 0  # CF-09/R2：检索侧不得走 embed()
    assert provider.queries == ["token 过期"]


def test_same_query_within_30s_calls_embedding_once(provider) -> None:
    """DoD：同一 query 30 秒内两次调用 → embedding 只调 1 次（计数 fake）。"""
    clock = FakeClock()
    cache = QueryEmbeddingCache(ttl_s=60.0, clock=clock)
    first = embed_query(provider, "token 过期", cache)
    clock.now = 30.0
    second = embed_query(provider, "token 过期", cache)
    assert first == second
    assert provider.embed_query_calls == 1
    assert cache.hits == 1


def test_cache_expires_after_ttl(provider) -> None:
    clock = FakeClock()
    cache = QueryEmbeddingCache(ttl_s=60.0, clock=clock)
    embed_query(provider, "token 过期", cache)
    clock.now = 61.0
    embed_query(provider, "token 过期", cache)
    assert provider.embed_query_calls == 2
    assert cache.misses == 2


def test_cache_is_fifo_bounded(provider) -> None:
    cache = QueryEmbeddingCache(ttl_s=60.0, maxsize=2)
    for query in ("a", "b", "c"):
        embed_query(provider, query, cache)
    assert len(cache) == 2
    assert embed_query(provider, "a", cache) is not None
    assert provider.embed_query_calls == 4  # a 被淘汰 → 重新嵌入


def test_model_identity_is_part_of_cache_key(provider) -> None:
    """P2-1：同一 query 在不同模型下不得互相命中。

    为什么必须有这条：缓存现在由 ``Engine`` 跨查询持有，而 ``set_provider()``
    能在生命周期内换模型（``--replay`` 切离线 provider、D-47 换用户模型）。
    只按 query 做 key 就会取到**另一个模型**的向量——维度相同吋静默给出错误相似度。
    """
    cache = QueryEmbeddingCache(ttl_s=60.0)
    cache.bind_identity("api:model-a")
    embed_query(provider, "token 过期", cache)
    assert provider.embed_query_calls == 1

    # 同模型 → 命中
    assert embed_query(provider, "token 过期", cache) == embed_query(provider, "token 过期", cache)
    assert provider.embed_query_calls == 1

    # 换模型 → 必须重算（且旧条目被清空）
    assert cache.bind_identity("api:model-b") is True
    assert len(cache) == 0
    embed_query(provider, "token 过期", cache)
    assert provider.embed_query_calls == 2

    # 绑同一身份 → 不清空、不重算
    assert cache.bind_identity("api:model-b") is False
    assert len(cache) == 1


def test_unbound_cache_falls_back_to_query_only_key(provider) -> None:
    """未绑身份时退化为旧行为（单测与不关心模型的调用点）。"""
    cache = QueryEmbeddingCache(ttl_s=60.0)
    assert cache.model is None
    embed_query(provider, "token 过期", cache)
    embed_query(provider, "token 过期", cache)
    assert provider.embed_query_calls == 1


def test_recall_vector_ranks_hits(vector_cls) -> None:
    stub = vector_cls([("a", 0.9), ("b", 0.8), ("c", 0.7)])
    candidates = recall_vector(
        _FixedProvider(),
        stub,
        "token",
        limit=2,
        cache=QueryEmbeddingCache(),
    )
    assert [c.chunk_id for c in candidates] == ["a", "b"]
    assert candidates[0].tier == TIER_VECTOR
    assert candidates[0].channel_ranks == {CHANNEL_VECTOR: 1}
    assert stub.calls == 1


def test_recall_vector_wraps_provider_error(provider_cls) -> None:
    failing = provider_cls(raise_on_query=RuntimeError("模型缺失"))
    with pytest.raises(VectorChannelError):
        recall_vector(failing, _NullStore(), "token", cache=QueryEmbeddingCache())


def test_recall_vector_wraps_search_error(vector_cls, provider) -> None:
    stub = vector_cls(raise_on_search=RuntimeError("lancedb boom"))
    with pytest.raises(VectorChannelError):
        recall_vector(provider, stub, "token", cache=QueryEmbeddingCache())


def test_recall_vector_timeout(provider_cls) -> None:
    slow = provider_cls(delay_s=0.5)
    with pytest.raises(VectorTimeoutError):
        recall_vector(slow, _NullStore(), "token", cache=QueryEmbeddingCache(), timeout_s=0.02)


def test_recall_degrades_when_provider_missing(store, seed_file, sym, vector_stub) -> None:
    seed_file(
        store,
        path="src/a.py",
        symbols=[sym("f", "f", start=1)],
        bodies={"f": "def f():\n    refresh_token\n"},
    )
    result = recall(store, "refresh_token", provider=None, vector_store=vector_stub)
    assert result.degraded is True
    assert result.degraded_reason is not None
    assert CHANNEL_VECTOR not in result.channels_used
    assert [c.chunk_id for c in result.candidates] == ["src/a.py:f:1"]


def test_recall_degrades_on_vector_exception_without_raising(
    store, seed_file, sym, provider_cls, vector_cls
) -> None:
    """DoD：vector 通道抛异常 → 返回双通道结果 + degraded 标记，不抛异常。"""
    seed_file(
        store,
        path="src/a.py",
        symbols=[sym("f", "f", start=1)],
        bodies={"f": "def f():\n    refresh_token\n"},
    )
    failing = provider_cls(raise_on_query=RuntimeError("api 抖动"))
    stub = vector_cls([("src/a.py:f:1", 0.9)])

    result = recall(store, "refresh_token", provider=failing, vector_store=stub)
    assert result.degraded is True
    assert "api 抖动" in (result.degraded_reason or "")
    assert stub.calls == 0  # provider 先失败，未触达向量库
    assert [c.chunk_id for c in result.candidates] == ["src/a.py:f:1"]


def test_recall_degrades_on_vector_timeout(
    store, seed_file, sym, provider_cls, vector_stub
) -> None:
    from nova_core.retrieval import RecallLimits

    seed_file(
        store,
        path="src/a.py",
        symbols=[sym("f", "f", start=1)],
        bodies={"f": "def f():\n    refresh_token\n"},
    )
    slow = provider_cls(delay_s=0.5)
    result = recall(
        store,
        "refresh_token",
        provider=slow,
        vector_store=vector_stub,
        limits=RecallLimits(vector_timeout_s=0.02),
    )
    assert result.degraded is True
    assert result.candidates


class _FixedProvider:
    """只需 ``embed_query`` 的最简 provider（recall_vector 不读 profile）。"""

    def embed_query(self, texts):
        return [[0.1] * 8 for _ in texts]


class _NullStore:
    def search(self, vector, top_k):  # pragma: no cover - 不应被触达
        raise AssertionError("search 不应被调用")
