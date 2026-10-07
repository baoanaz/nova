"""TASK-010 召回主编排（三通道合并、tier 口径、缓存复用、降级端到端）。"""

from __future__ import annotations

from nova_core.retrieval import (
    CHANNEL_BM25,
    CHANNEL_EXACT,
    CHANNEL_INFERRED,
    CHANNEL_LITERAL,
    CHANNEL_VECTOR,
    QueryEmbeddingCache,
    RecallLimits,
    recall,
)


def _seed_corpus(store, seed_file, sym) -> None:
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[
            sym("refresh", "TokenService.refresh", kind="method", start=45, end=82),
            sym("rotate", "TokenStore.rotate", kind="method", start=200, end=210),
        ],
        bodies={
            "TokenService.refresh": (
                "def refresh(self):\n    # token 过期 后 刷新\n    return refreshToken\n"
            ),
            "TokenStore.rotate": "def rotate(self):\n    return 'rotate tokens'\n",
        },
    )
    seed_file(
        store,
        path="src/cache.py",
        symbols=[sym("refreshToken", "refreshToken", start=5, end=9)],
        bodies={"refreshToken": "def refreshToken():\n    return 1\n"},
    )
    seed_file(
        store,
        path="src/ui/refresh_panel.py",
        symbols=[sym("refreshPanel", "refreshPanel", start=3, end=9)],
        bodies={"refreshPanel": "def refreshPanel():\n    return 'only ui noise'\n"},
    )


def test_recall_merges_three_channels(store, seed_file, sym, provider, vector_cls) -> None:
    """四通道（exact/inferred/bm25/vector）合并；候选池按 RRF 降序 + tier 口径正确。"""
    _seed_corpus(store, seed_file, sym)
    stub = vector_cls(
        [
            ("src/auth/token_service.py:TokenService.refresh:45", 0.95),
            ("src/ui/refresh_panel.py:refreshPanel:3", 0.5),
        ]
    )
    result = recall(
        store,
        "TokenService.refresh refreshToken",
        provider=provider,
        vector_store=stub,
    )

    assert result.degraded is False
    # TASK-101 §A：字面量通道（查询里的 ``refreshToken``/``TokenService.refresh`` 均触发）
    assert result.channels_used == (
        CHANNEL_EXACT,
        CHANNEL_LITERAL,
        CHANNEL_INFERRED,
        CHANNEL_BM25,
        CHANNEL_VECTOR,
    )
    by_id = {c.chunk_id: c for c in result.candidates}

    service = by_id["src/auth/token_service.py:TokenService.refresh:45"]
    # 字面量通道同时命中（查询里的 ``TokenService.refresh`` 是连续子串），
    # 三通道共识（exact+bm25+vector）仍是主导，见下方 rrf_score 断言。
    assert service.channel_ranks == {
        CHANNEL_EXACT: 1,
        CHANNEL_LITERAL: 1,
        CHANNEL_BM25: 1,
        CHANNEL_VECTOR: 1,
    }
    assert service.tier == 0
    assert service.kind == "code"
    assert service.path == "src/auth/token_service.py"
    assert service.score == service.rrf_score
    assert any("explicit symbol" in reason for reason in service.reasons)
    assert result.candidates[0].chunk_id == service.chunk_id

    cache = by_id["src/cache.py:refreshToken:5"]
    # R11/TASK-016：fts_search 默认 OR → 只含查询词之一（refreshToken）的块也进 BM25 候选
    # （旧断言假设隐式 AND，只有同时含 TokenService.refresh 与 refreshToken 的块才入池）。
    assert cache.channel_ranks == {CHANNEL_INFERRED: 1, CHANNEL_BM25: 2}
    assert cache.tier == 1

    panel = by_id["src/ui/refresh_panel.py:refreshPanel:3"]
    assert panel.channel_ranks == {CHANNEL_VECTOR: 2}
    assert panel.tier == 2

    # 三通道共识（exact+bm25+vector）胜过单通道候选
    assert service.rrf_score > cache.rrf_score > panel.rrf_score


def test_recall_marks_inferred_channel(store, seed_file, sym, provider, vector_stub) -> None:
    _seed_corpus(store, seed_file, sym)
    result = recall(store, "refreshPanel 的样式", provider=provider, vector_store=vector_stub)
    by_id = {c.chunk_id: c for c in result.candidates}
    panel = by_id["src/ui/refresh_panel.py:refreshPanel:3"]
    assert panel.channel_ranks[CHANNEL_INFERRED] == 1
    assert panel.tier == 1


def test_recall_reuses_query_embedding_across_calls(
    store, seed_file, sym, provider, vector_stub
) -> None:
    """同一 query 两次 recall → 进程内缓存命中，embedding 只调 1 次。"""
    _seed_corpus(store, seed_file, sym)
    cache = QueryEmbeddingCache(ttl_s=60.0)
    recall(store, "token 过期 刷新", provider=provider, vector_store=vector_stub, cache=cache)
    recall(store, "token 过期 刷新", provider=provider, vector_store=vector_stub, cache=cache)
    assert provider.embed_query_calls == 1
    assert cache.hits == 1


def test_recall_pool_limit(store, seed_file, sym, provider, vector_stub) -> None:
    for index in range(6):
        seed_file(
            store,
            path=f"src/f{index}.py",
            symbols=[sym(f"f{index}", f"f{index}", start=1)],
            bodies={f"f{index}": "def f():\n    refresh_token\n"},
        )
    result = recall(
        store,
        "refresh_token",
        provider=provider,
        vector_store=vector_stub,
        limits=RecallLimits(pool=3),
    )
    assert len(result.candidates) == 3


def test_recall_explicit_path_uplifts_recalled_candidate(store, seed_file, sym, provider,
                                                        vector_cls) -> None:
    _seed_corpus(store, seed_file, sym)
    stub = vector_cls([("src/ui/refresh_panel.py:refreshPanel:3", 0.9)])
    result = recall(
        store,
        "src/ui/refresh_panel.py 是什么",
        provider=provider,
        vector_store=stub,
    )
    panel = next(c for c in result.candidates if "refresh_panel" in c.chunk_id)
    assert panel.tier == 0
    assert "explicit path" in panel.reasons


def test_recall_empty_index_returns_empty_pool(store, provider, vector_stub) -> None:
    result = recall(store, "token 过期 刷新", provider=provider, vector_store=vector_stub)
    assert result.candidates == []
    assert result.degraded is False
    assert result.channels_used == ()


def test_recall_result_len(store, seed_file, sym, provider, vector_stub) -> None:
    _seed_corpus(store, seed_file, sym)
    result = recall(store, "refresh_token", provider=provider, vector_store=vector_stub)
    assert len(result) == len(result.candidates)
