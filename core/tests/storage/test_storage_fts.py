"""中文 FTS：索引/查询双侧同一分词器（D-20 / D-45）+ bm25 语义（含 R11/TASK-016 OR 语义）。"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from nova_core.storage import Store
from nova_core.storage.store import FTS_COLUMN_WEIGHTS
from nova_core.text import segment
from nova_core.types import ChunkDef, ParsedFile


def test_chinese_query_hits_only_after_segmentation(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    chunk = make_chunk(
        fqn="refresh",
        content="def refresh():\n    # 刷新令牌的过期时间\n    return None\n",
        docstring="刷新令牌的过期逻辑",
    )
    store.apply_file_change(make_parsed(), [chunk], "file-hash-1")

    hits = store.fts_search(segment("刷新令牌"), 10)
    assert [chunk_id for chunk_id, _ in hits] == [chunk.id]
    # 不分词时 unicode61 把整串当一个 token → 不命中（证明是预分词生效，而非 tokenizer 巧合）。
    assert store.fts_search("刷新令牌", 10) == []


def test_chinese_query_matches_english_identifiers_in_content(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    chunk = make_chunk(
        fqn="refresh_token",
        content="def refresh_token():\n    # 令牌刷新\n    pass\n",
    )
    unrelated = make_chunk(fqn="other", start=9, content="def other():\n    pass\n")
    store.apply_file_change(make_parsed(), [chunk, unrelated], "file-hash-1")
    hits = store.fts_search(segment("令牌 refresh_token"), 10)
    assert [chunk_id for chunk_id, _ in hits] == [chunk.id]


def test_bm25_orders_more_relevant_first(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    strong = make_chunk(fqn="a", start=1, content="令牌 令牌 令牌 刷新\n")
    weak = make_chunk(fqn="b", start=5, content="令牌\n")
    store.apply_file_change(make_parsed(), [weak, strong], "file-hash-1")

    hits = store.fts_search(segment("令牌"), 10)
    assert [chunk_id for chunk_id, _ in hits] == [strong.id, weak.id]
    scores = [score for _, score in hits]
    assert all(score < 0 for score in scores)  # bm25 原始分（越小越相关）
    assert scores[0] < scores[1]


def test_limit_and_empty_query(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    chunks = [
        make_chunk(fqn="a", start=1, content="alpha token\n"),
        make_chunk(fqn="b", start=5, content="alpha token again\n"),
    ]
    store.apply_file_change(make_parsed(), chunks, "file-hash-1")
    assert len(store.fts_search(segment("alpha token"), 1)) == 1
    assert store.fts_search("", 10) == []
    assert store.fts_search("   ", 10) == []


def test_update_removes_stale_fts_rows(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    parsed = make_parsed()
    old = make_chunk(fqn="f", content="def f():\n    # 旧注释：缓存预热\n    pass\n")
    store.apply_file_change(parsed, [old], "file-hash-1")
    assert store.fts_search(segment("缓存预热"), 10) != []

    new = make_chunk(fqn="f", content="def f():\n    # 新注释：连接复用\n    pass\n")
    store.apply_file_change(parsed, [new], "file-hash-2")
    assert store.fts_search(segment("缓存预热"), 10) == []
    assert store.fts_search(segment("连接复用"), 10)[0][0] == new.id


def test_operator_and_boundary_facts_are_unchanged_by_query_side_cleaning(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    """TASK-020：清洗在检索层（``retrieval.bm25``）纯函数，**Store 语义不变**。

    两个边界都在本测试里固定（R20 实测修正后的因果，2026-09-10）：

    - 标点（``？``）在 unicode61 下不产生词项（当分隔符）→ **不影响 OR，也不影响 AND**；
    - **可切分但库外（DF=0）** 的 token 仍如实使 ``operator="and"`` 恒空，而 OR 不受影响
      ——清洗是调用方（``bm25.filter_bm25_tokens``）的事，Store 不猜库内内容。
    """
    chunk = make_chunk(fqn="f", start=1, content="def f():\n    # 令牌刷新\n    pass\n")
    store.apply_file_change(make_parsed(), [chunk], "file-hash-1")

    punctuated = segment("令牌刷新？")
    assert "？" in punctuated.split()  # 前提：分词产出标点 token
    assert [chunk_id for chunk_id, _ in store.fts_search(punctuated, 10, operator="and")] == [
        chunk.id
    ]
    assert [chunk_id for chunk_id, _ in store.fts_search(punctuated, 10)] == [chunk.id]

    with_off_index = segment("令牌刷新 Kubernetes")
    assert store.fts_search(with_off_index, 10, operator="and") == []
    assert [chunk_id for chunk_id, _ in store.fts_search(with_off_index, 10)] == [chunk.id]

    # OR 结果对「零贡献 token」不敏感：含标点/库外词的 MATCH 串与清洗后的结果逐项一致。
    cleaned = segment("令牌刷新")
    assert store.fts_search(with_off_index, 10) == store.fts_search(cleaned, 10)
    assert store.fts_search(punctuated, 10) == store.fts_search(cleaned, 10)


def test_fts_syntax_characters_in_query_are_not_operators(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    chunk = make_chunk(fqn="f", content="def f():\n    return a AND b\n")
    store.apply_file_change(make_parsed(), [chunk], "file-hash-1")
    # 引号包裹每个 token：AND/OR/NEAR 之类的字符不会被当成 FTS 语法。
    assert store.fts_search("a AND b", 10)[0][0] == chunk.id
    assert store.fts_search("NOT EXISTS", 10) == []


# ---------------------------------------------------------------------------
# R11（TASK-016）：多词召回语义 = OR（默认）/ AND（显式）
# ---------------------------------------------------------------------------


def test_multi_token_chinese_query_needs_or_semantics(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    """本卡核心回归锚点（R11）：同一语料下 OR 非空、AND 为空。

    真实缺陷：中文自然语言查询分词后 token 多，FTS5 隐式 AND 要求全部 token 同块出现
    → BM25 通道恒零命中 → 只剩 Vector 单通道 → ``answerable`` 恒 False。
    """
    refresh_chunk = make_chunk(
        fqn="refresh", start=1, content="def refresh():\n    # 令牌刷新\n    pass\n"
    )
    expiry_chunk = make_chunk(
        fqn="expire", start=7, content="def expire():\n    # 过期时间\n    pass\n"
    )
    store.apply_file_change(make_parsed(), [refresh_chunk, expiry_chunk], "file-hash-1")

    segmented = segment("令牌过期后在哪里刷新")
    assert len(segmented.split()) >= 6  # 多 token 是问题的前提

    hits = store.fts_search(segmented, 10)  # 默认 operator="or"
    assert hits, "OR 语义下多 token 中文查询必须非空（R11 回归）"
    assert {chunk_id for chunk_id, _ in hits} == {refresh_chunk.id, expiry_chunk.id}
    # AND 保留高精度语义：没有 chunk 同时含全部 6 个 token。
    assert store.fts_search(segmented, 10, operator="and") == []
    # 分值仍是原始 bm25（负数，越小越相关）。
    assert all(score < 0 for _, score in hits)


def test_operator_and_keeps_exact_multi_token_match(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    """``operator="and"`` 不是缩水版 OR：全 token 命中时仍能召回（且只召回它）。"""
    both = make_chunk(fqn="a", start=1, content="def a():\n    # 令牌刷新\n    pass\n")
    only_token = make_chunk(fqn="b", start=5, content="def b():\n    # 令牌\n    pass\n")
    store.apply_file_change(make_parsed(), [both, only_token], "file-hash-1")

    segmented = segment("令牌刷新")
    assert [chunk_id for chunk_id, _ in store.fts_search(segmented, 10, operator="and")] == [
        both.id
    ]
    assert {chunk_id for chunk_id, _ in store.fts_search(segmented, 10)} == {
        both.id,
        only_token.id,
    }


def test_invalid_operator_rejected(store: Store) -> None:
    with pytest.raises(ValueError, match="operator"):
        store.fts_search("令牌", 10, operator="xor")  # type: ignore[arg-type]


def test_signature_column_weight_outranks_incidental_mentions(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    """B：符号名列加权（5×）→ 符号名命中排在长正文偶然提及之前。

    同一 token：符号名命中块只出现 2 次（签名 1 + 正文 1），正文噪声块出现 6 次；
    不加权时噪声块胜，加权后符号名块胜——这正是列权重存在的意义。
    """
    assert FTS_COLUMN_WEIGHTS == (1.0, 5.0, 1.0)  # content, signature, docstring
    name_hit = make_chunk(
        path="src/auth.py",
        fqn="令牌刷新",
        signature="def 令牌刷新()",
        content="def 令牌刷新():\n    return None\n",
    )
    body_hit = make_chunk(
        path="src/other.py",
        fqn="alpha",
        signature="def alpha()",
        content="def alpha():\n    " + "令牌 " * 5 + "\n",
    )
    store.apply_file_change(make_parsed(path="src/auth.py"), [name_hit], "file-hash-1")
    store.apply_file_change(make_parsed(path="src/other.py"), [body_hit], "file-hash-2")

    hits = store.fts_search(segment("令牌"), 10)
    assert [chunk_id for chunk_id, _ in hits] == [name_hit.id, body_hit.id]
