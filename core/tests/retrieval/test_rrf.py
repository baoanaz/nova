"""TASK-010 RRF 融合数学（DoD：手工构造两通道排名，断言分数与排序 + K=60 常量断言）。"""

from __future__ import annotations

import pytest
from nova_core.retrieval import rrf


def test_k_is_frozen_at_60() -> None:
    assert rrf.RRF_K == 60


def test_reciprocal_rank_math() -> None:
    assert rrf.reciprocal_rank(1) == pytest.approx(1 / 61)
    assert rrf.reciprocal_rank(60) == pytest.approx(1 / 120)
    assert rrf.rrf_score([1]) == pytest.approx(1 / 61)


def test_reciprocal_rank_rejects_zero_based() -> None:
    with pytest.raises(ValueError):
        rrf.reciprocal_rank(0)


def test_fuse_two_channels_manual_math() -> None:
    """两通道手工排名：a=(1,3) b=(2,1) c=(3,2) → b > a > c。"""
    entries = rrf.fuse_rankings({"bm25": ["a", "b", "c"], "vector": ["b", "c", "a"]})
    scores = {entry.chunk_id: entry.rrf_score for entry in entries}

    assert scores["a"] == pytest.approx(1 / 61 + 1 / 63)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 62)
    assert [entry.chunk_id for entry in entries] == ["b", "a", "c"]
    assert entries[0].channel_ranks == {"bm25": 2, "vector": 1}
    assert entries[0].channel_count == 2


def test_consensus_candidate_beats_single_channel_top() -> None:
    """共识优先：双通道（各第 5）排在单通道第 1 之前（D-16/RRF 的本质）。"""
    entries = rrf.fuse_rankings(
        {
            "bm25": ["only", "x", "y", "z", "consensus"],
            "vector": ["a", "b", "c", "d", "consensus"],
        }
    )
    order = [entry.chunk_id for entry in entries]
    assert order.index("consensus") < order.index("only")
    assert entries[0].chunk_id == "consensus"


def test_tie_break_prefers_more_channels() -> None:
    """同分时共识优先（确定性 tie-break 直接单测）。"""
    single = rrf.RrfEntry(chunk_id="single", rrf_score=0.01, channel_ranks={"bm25": 1})
    double = rrf.RrfEntry(chunk_id="double", rrf_score=0.01, channel_ranks={"bm25": 2, "vector": 2})
    ordered = sorted([single, double], key=rrf._entry_sort_key)
    assert [entry.chunk_id for entry in ordered] == ["double", "single"]


def test_duplicate_ids_within_channel_count_once() -> None:
    entries = rrf.fuse_rankings({"bm25": ["a", "a", "b"]})
    assert [entry.chunk_id for entry in entries] == ["a", "b"]
    assert entries[0].channel_ranks == {"bm25": 1}


def test_empty_input() -> None:
    assert rrf.fuse_rankings({}) == []
    assert rrf.fuse_rankings({"bm25": []}) == []
