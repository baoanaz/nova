"""M1 跨模块 E2E 回归（TASK-016 §D / R13）——每条断言对应一个真实集成缺陷场景。

链路：真实 ``Store`` + ``VectorStore`` + ``Indexer`` + ``retrieval`` + ``contextpack``；
唯一替身是确定性假 embedding（见 ``conftest.DeterministicBigramEmbedding``）。

背景（编排者 E2E 实测，2026-09-10）：中文自然语言查询分词后 token 多，FTS5 隐式 AND
要求全部 token 同块出现 → BM25 通道恒零命中 → 只剩 Vector 单通道 → ``answerable`` 恒 False。
本文件是该缺陷的回归锚点，同时兑现 R13"跨模块集成回归缺位"的补齐。
"""

from __future__ import annotations

from nova_core.retrieval import CHANNEL_BM25, recall_bm25
from nova_core.text import segment

from .conftest import (
    MODULE_PATH,
    QUERY,
    SPEC_PATH,
    TARGET_SYMBOL,
    TOKEN_MODULE,
    M1Env,
    make_change_set,
)

#: 增量场景：只改 ``refresh_token`` 的实现体（一个 chunk 变化，其余复用）。
TOKEN_MODULE_UPDATED = TOKEN_MODULE.replace('return "old"', 'return "new"')


def test_bm25_recalls_chinese_natural_language_query(m1: M1Env) -> None:
    """断言 1（核心回归）：中文 6-token 查询的 BM25 通道非空，且目标符号排在 top-3。"""
    report = m1.index_all()
    assert report.errors == ()
    assert report.chunks_new > 0

    segmented = segment(QUERY)
    assert len(segmented.split()) >= 6, "本回归的前提是分词后 token 足够多"

    # 修复前后对比（同语料、同查询）：
    #   修复前默认语义 = FTS5 隐式 AND → 0 命中；修复后默认 OR → 非空。
    assert m1.store.fts_search(segmented, 50, operator="and") == []
    assert m1.store.fts_search(segmented, 50)

    run = m1.pipeline()
    assert run.recall.degraded is False
    assert CHANNEL_BM25 in run.recall.channels_used

    bm25_ranking = recall_bm25(m1.store, QUERY, limit=50)
    target = next(c for c in bm25_ranking if c.chunk_id == _target_chunk_id(run))
    assert target.channel_ranks[CHANNEL_BM25] <= 3


def test_two_channel_consensus_makes_pack_answerable(m1: M1Env) -> None:
    """断言 2：≥2 个候选带 ≥2 通道 → ``answerable is True``、confidence ∈ {medium, high}。

    附"修复前反事实"：只保留向量单通道（R11 时 BM25 恒空）→ ``answerable`` 仍为 False。
    """
    m1.index_all()
    run = m1.pipeline()

    consensus = [c for c in run.recall.candidates if len(c.channel_ranks) >= 2]
    assert len(consensus) >= 2
    assert run.pack.answerable is True
    assert run.pack.confidence in {"medium", "high"}

    assert m1.vector_only_pack().answerable is False


def test_spec_document_reaches_pack_docs(m1: M1Env) -> None:
    """断言 3：Markdown 设计文档块出现在 ``pack.docs``（D-13/D-42 一等资产兑现）。"""
    m1.index_all()
    run = m1.pipeline()

    assert run.pack.docs, "设计文档块必须装配进 pack.docs，而不是被代码证据挤掉"
    assert any(item.path == SPEC_PATH for item in run.pack.docs)
    assert any(
        item.doctype == "design" and item.heading_path and "令牌" in item.heading_path
        for item in run.pack.docs
    )


def test_delete_file_removes_evidence_and_orphan_vectors(m1: M1Env) -> None:
    """断言 4：删除文件后其证据不再出现，且向量层不残留孤儿。"""
    m1.index_all()
    before = m1.pipeline()
    code_chunk_ids = [c.chunk_id for c in before.recall.candidates if c.path == MODULE_PATH]
    assert code_chunk_ids
    assert m1.vectors.count() == m1.store.counts()["chunks"]

    report = m1.indexer.ingest(make_change_set(deleted=(MODULE_PATH,)))
    assert report.deleted == 1
    assert report.orphan_files == ()
    assert m1.vectors.get_hashes(code_chunk_ids) == {}
    assert m1.vectors.count() == m1.store.counts()["chunks"]

    after = m1.pipeline()
    assert all(candidate.path != MODULE_PATH for candidate in after.recall.candidates)
    assert all(item.path != MODULE_PATH for item in after.pack.evidence)
    assert all(item.path != MODULE_PATH for item in after.pack.docs)


def test_incremental_reindex_only_reembeds_changed_chunk(m1: M1Env) -> None:
    """断言 5：改一个函数的实现 → ``chunks_new == 1`` 且 ``chunks_reused >= 1``。"""
    m1.index_all()
    updated = TOKEN_MODULE_UPDATED
    (m1.repo / MODULE_PATH).write_text(updated, encoding="utf-8")

    report = m1.indexer.ingest(make_change_set(modified={MODULE_PATH: updated}))
    assert report.chunks_new == 1
    assert report.chunks_reused >= 1
    assert report.vectors_upserted == 1
    assert m1.vectors.count() == m1.store.counts()["chunks"]


# --------------------------------------------------------------------------- 工具


def _target_chunk_id(run) -> str:
    """从合并后的候选池取目标符号的 chunk_id（不硬编码行号，避免语料微调即失效）。"""
    return next(
        candidate.chunk_id
        for candidate in run.recall.candidates
        if candidate.symbol_fqn == TARGET_SYMBOL
    )
