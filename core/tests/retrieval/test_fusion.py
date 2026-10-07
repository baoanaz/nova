"""TASK-010 融合与去重（DoD：共识优先、chunk_id 去重、tier 元数据、显式路径升档）。"""

from __future__ import annotations

import pytest
from nova_core.retrieval.fusion import (
    CHANNEL_BM25,
    CHANNEL_EXACT,
    CHANNEL_INFERRED,
    CHANNEL_VECTOR,
    KIND_CODE,
    KIND_FALLBACK,
    KIND_SPEC,
    KIND_TEST,
    REASON_EXPLICIT_PATH,
    TIER_EXPLICIT,
    TIER_SEED,
    TIER_VECTOR,
    classify_kind,
    is_test_path,
    make_candidate,
    merge,
)
from nova_core.types import SpecBlockDef


def test_dedup_by_chunk_id_merges_channels_and_ranks() -> None:
    merged = merge(
        {
            CHANNEL_BM25: [make_candidate("x", channel=CHANNEL_BM25, rank=1, tier=TIER_SEED)],
            CHANNEL_VECTOR: [make_candidate("x", channel=CHANNEL_VECTOR, rank=3, tier=TIER_VECTOR)],
        }
    )
    assert len(merged) == 1
    candidate = merged[0]
    assert candidate.channel_ranks == {CHANNEL_BM25: 1, CHANNEL_VECTOR: 3}
    assert candidate.tier == TIER_SEED  # 命中的最低（最可信）档位
    assert candidate.rrf_score == candidate.score


def test_consensus_candidate_ranks_first() -> None:
    bm25 = [
        make_candidate("only_bm25", channel=CHANNEL_BM25, rank=1, tier=TIER_SEED),
        make_candidate("consensus", channel=CHANNEL_BM25, rank=5, tier=TIER_SEED),
    ]
    vector = [
        make_candidate("consensus", channel=CHANNEL_VECTOR, rank=5, tier=TIER_VECTOR),
    ]
    merged = merge({CHANNEL_BM25: bm25, CHANNEL_VECTOR: vector})
    assert merged[0].chunk_id == "consensus"


def test_tier_is_metadata_not_order_by() -> None:
    """D-17：tier 不作排序键——同分时由确定性 tie-break（而非 tier）决定顺序。"""
    merged = merge(
        {
            CHANNEL_EXACT: [
                make_candidate("explicit", channel=CHANNEL_EXACT, rank=1, tier=TIER_EXPLICIT)
            ],
            CHANNEL_BM25: [
                make_candidate("bm25_top", channel=CHANNEL_BM25, rank=1, tier=TIER_SEED)
            ],
        }
    )
    by_id = {c.chunk_id: c for c in merged}
    assert by_id["explicit"].tier == TIER_EXPLICIT
    assert by_id["bm25_top"].tier == TIER_SEED
    assert by_id["explicit"].rrf_score == pytest.approx(by_id["bm25_top"].rrf_score)
    assert [c.chunk_id for c in merged] == ["bm25_top", "explicit"]  # chunk_id 字典序兑底


def test_explicit_wins_when_it_also_has_a_worse_single_rank() -> None:
    """显式命中 + 另一通道命中（共识）→ 胜过单通道头名。"""
    merged = merge(
        {
            CHANNEL_EXACT: [
                make_candidate("target", channel=CHANNEL_EXACT, rank=2, tier=TIER_EXPLICIT)
            ],
            CHANNEL_BM25: [
                make_candidate("bm25_top", channel=CHANNEL_BM25, rank=1, tier=TIER_SEED),
                make_candidate("target", channel=CHANNEL_BM25, rank=3, tier=TIER_SEED),
            ],
        }
    )
    assert merged[0].chunk_id == "target"
    assert merged[0].tier == TIER_EXPLICIT


def test_pool_limit_truncates() -> None:
    channel = [
        make_candidate(f"c{i}", channel=CHANNEL_BM25, rank=i + 1, tier=TIER_SEED) for i in range(10)
    ]
    assert len(merge({CHANNEL_BM25: channel}, pool_limit=4)) == 4


def test_merge_empty_channels() -> None:
    assert merge({}) == []
    assert merge({CHANNEL_BM25: []}) == []


def test_enrichment_fills_metadata_and_classifies_kind(store, seed_file, sym) -> None:
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[sym("refresh", "TokenService.refresh", kind="method", start=45, end=82)],
    )
    seed_file(store, path="core/tests/test_auth.py", symbols=[sym("t", "test_refresh", start=1)])
    seed_file(
        store,
        path="docs/auth.md",
        language="markdown",
        spec_blocks=[
            SpecBlockDef(
                path="docs/auth.md",
                heading="Token Refresh",
                heading_path="认证 > Token Refresh",
                level=2,
                start_line=30,
                end_line=52,
                content="刷新流程说明\n",
                doctype="design",
            )
        ],
    )
    merged = merge(
        {
            CHANNEL_BM25: [
                make_candidate(
                    "src/auth/token_service.py:TokenService.refresh:45",
                    channel=CHANNEL_BM25,
                    rank=1,
                    tier=TIER_SEED,
                ),
                make_candidate(
                    "core/tests/test_auth.py:test_refresh:1",
                    channel=CHANNEL_BM25,
                    rank=2,
                    tier=TIER_SEED,
                ),
                make_candidate(
                    "docs/auth.md:认证 > Token Refresh:30",
                    channel=CHANNEL_BM25,
                    rank=3,
                    tier=TIER_SEED,
                ),
            ]
        },
        store=store,
    )
    by_id = {c.chunk_id: c for c in merged}
    code = by_id["src/auth/token_service.py:TokenService.refresh:45"]
    assert (code.kind, code.path, code.start_line, code.end_line) == (
        KIND_CODE,
        "src/auth/token_service.py",
        45,
        82,
    )
    assert code.symbol_fqn == "TokenService.refresh"
    assert by_id["core/tests/test_auth.py:test_refresh:1"].kind == KIND_TEST
    assert by_id["docs/auth.md:认证 > Token Refresh:30"].kind == KIND_SPEC


def test_missing_chunks_are_dropped(store, seed_file, sym) -> None:
    seed_file(store, path="src/a.py", symbols=[sym("f", "f", start=1)])
    merged = merge(
        {
            CHANNEL_BM25: [
                make_candidate("src/a.py:f:1", channel=CHANNEL_BM25, rank=1, tier=TIER_SEED),
                make_candidate("src/gone.py:g:1", channel=CHANNEL_BM25, rank=2, tier=TIER_SEED),
            ]
        },
        store=store,
    )
    assert [c.chunk_id for c in merged] == ["src/a.py:f:1"]


def test_explicit_path_uplifts_tier_and_reason(store, seed_file, sym) -> None:
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[sym("refresh", "TokenService.refresh", kind="method", start=45)],
    )
    seed_file(store, path="src/other.py", symbols=[sym("o", "o", start=1)])
    merged = merge(
        {
            CHANNEL_BM25: [
                make_candidate(
                    "src/auth/token_service.py:TokenService.refresh:45",
                    channel=CHANNEL_BM25,
                    rank=1,
                    tier=TIER_SEED,
                ),
                make_candidate("src/other.py:o:1", channel=CHANNEL_BM25, rank=2, tier=TIER_SEED),
            ]
        },
        store=store,
        explicit_paths=["token_service.py"],
    )
    by_id = {c.chunk_id: c for c in merged}
    uplifted = by_id["src/auth/token_service.py:TokenService.refresh:45"]
    assert uplifted.tier == TIER_EXPLICIT
    assert REASON_EXPLICIT_PATH in uplifted.reasons
    assert by_id["src/other.py:o:1"].tier == TIER_SEED
    assert REASON_EXPLICIT_PATH not in by_id["src/other.py:o:1"].reasons


def test_no_explicit_paths_keeps_tiers() -> None:
    merged = merge(
        {CHANNEL_INFERRED: [make_candidate("a", channel=CHANNEL_INFERRED, rank=1, tier=TIER_SEED)]}
    )
    assert merged[0].tier == TIER_SEED


def test_reasons_kept_unique_per_channel() -> None:
    merged = merge(
        {
            CHANNEL_BM25: [
                make_candidate(
                    "x", channel=CHANNEL_BM25, rank=1, tier=TIER_SEED, reason="bm25 -1.0"
                )
            ],
            CHANNEL_VECTOR: [
                make_candidate("x", channel=CHANNEL_VECTOR, rank=2, tier=TIER_VECTOR, reason="v")
            ],
        }
    )
    assert merged[0].reasons == ["bm25 -1.0", "bm25 rank 1", "v", "vector rank 2"]


def test_kind_and_test_path_helpers() -> None:
    assert classify_kind("src/a.py", "function") == KIND_CODE
    assert classify_kind("src/a.py", "fallback_block") == KIND_FALLBACK
    assert classify_kind("docs/a.md", "spec_block") == KIND_SPEC
    assert classify_kind("core/tests/test_a.py", "function") == KIND_TEST
    assert is_test_path("core/tests/test_a.py") is True
    assert is_test_path("src/test_helpers.py") is True
    assert is_test_path("src/a_test.cc") is True
    assert is_test_path("src/contest.py") is False
    assert is_test_path("src/latest.py") is False
