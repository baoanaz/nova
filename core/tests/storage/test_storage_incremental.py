"""增量对账：FileDelta 三类 id 语义 + 幂等 + 契约校验（CF-08 / D-43）。"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from nova_core.storage import Store
from nova_core.text import segment
from nova_core.types import ChunkDef, ParsedFile


def test_first_write_marks_everything_new(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    chunks = [
        make_chunk(fqn="f", start=1, content="def f():\n    return 1\n"),
        make_chunk(fqn="g", start=5, content="def g():\n    return 2\n"),
    ]
    delta = store.apply_file_change(make_parsed(), chunks, "file-hash-1")
    assert delta.path == "src/a.py"
    assert set(delta.new_chunk_ids) == {chunks[0].id, chunks[1].id}
    assert delta.reused_chunk_ids == ()
    assert delta.removed_chunk_ids == ()
    assert store.counts()["chunks"] == 2


def test_changed_function_only_that_chunk_is_new(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    parsed = make_parsed()
    func_f = make_chunk(fqn="f", start=1, content="def f():\n    return 1\n")
    func_g = make_chunk(fqn="g", start=5, content="def g():\n    return 2\n")
    store.apply_file_change(parsed, [func_f, func_g], "file-hash-1")

    changed_f = make_chunk(fqn="f", start=1, content="def f():\n    return 99\n")
    delta = store.apply_file_change(parsed, [changed_f, func_g], "file-hash-2")

    assert delta.new_chunk_ids == (changed_f.id,)
    assert delta.reused_chunk_ids == (func_g.id,)
    # 变化的 id 只出现在 new，不重复出现在 removed（下游 upsert/delete 顺序无关）。
    assert delta.removed_chunk_ids == ()
    assert store.counts()["chunks"] == 2
    stored = store.chunk_by_id(changed_f.id)
    assert stored is not None and stored.content == changed_f.content
    assert stored.content_hash == changed_f.content_hash


def test_line_shift_reuses_hash_but_removes_old_ids(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    """chunk_id 含 start_line（D-04 不追求跨代稳定）：行号漂移 → 新 id 复用旧 hash。"""
    parsed = make_parsed()
    content_f = "def f():\n    return 1\n"
    content_g = "def g():\n    return 2\n"
    old_f = make_chunk(fqn="f", start=1, content=content_f)
    old_g = make_chunk(fqn="g", start=10, content=content_g)
    store.apply_file_change(parsed, [old_f, old_g], "file-hash-1")

    new_f = make_chunk(fqn="f", start=2, content=content_f)
    new_g = make_chunk(fqn="g", start=11, content=content_g)
    delta = store.apply_file_change(parsed, [new_f, new_g], "file-hash-2")

    assert delta.new_chunk_ids == ()
    assert delta.reused_chunk_ids == (new_f.id, new_g.id)
    assert set(delta.removed_chunk_ids) == {old_f.id, old_g.id}
    assert store.chunk_by_id(old_f.id) is None
    assert store.chunk_by_id(new_f.id) is not None


def test_removed_is_id_based_not_hash_based(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    """同文件两个同内容 chunk：删掉一个，其 id 必须进 removed（尽管 hash 仍存在）。"""
    parsed = make_parsed()
    same = "def f():\n    return 1\n"
    kept = make_chunk(fqn="f", start=1, content=same)
    dropped = make_chunk(fqn="g", start=5, content=same)
    store.apply_file_change(parsed, [kept, dropped], "file-hash-1")

    delta = store.apply_file_change(parsed, [kept], "file-hash-2")
    assert delta.new_chunk_ids == ()
    assert delta.reused_chunk_ids == (kept.id,)
    assert delta.removed_chunk_ids == (dropped.id,)


def test_reapply_same_change_is_idempotent(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    parsed = make_parsed()
    chunks = [
        make_chunk(fqn="f", start=1, content="def f():\n    return 1\n"),
        make_chunk(fqn="g", start=5, content="def g():\n    return 2\n"),
    ]
    store.apply_file_change(parsed, chunks, "file-hash-1")
    delta = store.apply_file_change(parsed, chunks, "file-hash-1")

    assert delta.new_chunk_ids == ()
    assert set(delta.reused_chunk_ids) == {chunks[0].id, chunks[1].id}
    assert delta.removed_chunk_ids == ()
    assert store.counts()["chunks"] == 2
    # FTS 行随 chunks 重建，不重复累积。
    # 语义变更（R11/TASK-016）：fts_search 默认改为 OR，这里要断言“不重复累积”必须显式用
    # operator="and"（否则两个 chunk 都含 token "return"，计数无法区分累积与多命中）。
    assert len(store.fts_search(segment("return 2"), 10, operator="and")) == 1


def test_chunk_with_foreign_file_path_rejected(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    foreign = make_chunk(path="src/other.py")
    with pytest.raises(ValueError, match="不一致"):
        store.apply_file_change(make_parsed(), [foreign], "file-hash-1")
    assert store.counts()["chunks"] == 0
