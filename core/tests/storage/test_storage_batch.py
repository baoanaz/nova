"""写事务批处理与冷启动快路径（TASK-114 / P1-3）。

两条不变量：

1. **新文件不空转**：``files`` 里没有该路径时不发定位 SELECT / 4 条 DELETE
   （langchain 冷启动实测这些空转占 11.4s 的绝大部分）；
2. **批内单文件失败隔离**：``write_batch`` 下每个文件仍是独立 SAVEPOINT——
   失败只回滚该文件，其余文件正常提交；外层异常则整批回滚。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from nova_core.storage import Store
from nova_core.storage.db import connect, ensure_database
from nova_core.types import ChunkDef, ParsedFile


@pytest.fixture
def traced_store(tmp_path: Path) -> Iterator[tuple[Store, sqlite3.Connection]]:
    """自建 Store 以便拿到连接做 SQL trace（白盒回归：直接断言"没发哪些语句"）。"""
    conn = connect(tmp_path / "index.db")
    ensure_database(conn)
    store = Store(conn, tmp_path)
    try:
        yield store, conn
    finally:
        conn.close()


def _parsed(path: str) -> ParsedFile:
    return ParsedFile(path=path, language="python")


def test_new_file_does_not_position_or_delete(
    traced_store: tuple[Store, sqlite3.Connection],
    make_chunk: Callable[..., ChunkDef],
) -> None:
    """首次写入一个文件：不发 `FROM chunks WHERE file_path` 定位、不发 DELETE。"""
    store, conn = traced_store
    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    chunk = make_chunk()

    delta = store.apply_file_change(_parsed(chunk.file_path), [chunk], "filehash")

    joined = "\n".join(traced)
    assert "FROM chunks WHERE file_path" not in joined, "新文件不该做旧行定位"
    assert "DELETE FROM chunks" not in joined
    assert "DELETE FROM symbols" not in joined
    assert delta.new_chunk_ids == (chunk.id,)
    assert delta.reused_chunk_ids == ()
    assert delta.removed_chunk_ids == ()


def test_existing_file_still_clears_and_reconciles(
    traced_store: tuple[Store, sqlite3.Connection],
    make_chunk: Callable[..., ChunkDef],
) -> None:
    """已存在的文件仍要清旧行并给出对账结果（快路径不得改变语义）。"""
    store, conn = traced_store
    chunk = make_chunk()
    store.apply_file_change(_parsed(chunk.file_path), [chunk], "filehash")

    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    delta = store.apply_file_change(_parsed(chunk.file_path), [chunk], "filehash")

    assert any("DELETE FROM chunks" in statement for statement in traced)
    assert delta.new_chunk_ids == ()
    assert delta.reused_chunk_ids == (chunk.id,)


def test_write_batch_isolates_failed_file(
    traced_store: tuple[Store, sqlite3.Connection],
    make_chunk: Callable[..., ChunkDef],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中间文件写失败只回滚它自己；前后文件照常提交（TASK-018 §C 语义不变）。"""
    store, _ = traced_store
    first = make_chunk(path="src/a.py", fqn="a", content="def a():\n    return 1\n")
    broken = make_chunk(path="src/b.py", fqn="b", content="def b():\n    return 2\n")
    third = make_chunk(path="src/c.py", fqn="c", content="def c():\n    return 3\n")

    from nova_core.storage import store as store_module

    real_insert_fts = store_module._insert_fts_row

    def flaky_insert_fts(
        conn: sqlite3.Connection, rowid: int, chunk: ChunkDef, segments=None
    ) -> None:
        if chunk.file_path == "src/b.py":
            raise RuntimeError("fts boom")
        real_insert_fts(conn, rowid, chunk, segments)

    monkeypatch.setattr(store_module, "_insert_fts_row", flaky_insert_fts)

    with store.write_batch():
        store.apply_file_change(_parsed("src/a.py"), [first], "h1")
        with pytest.raises(RuntimeError, match="fts boom"):
            store.apply_file_change(_parsed("src/b.py"), [broken], "h2")
        store.apply_file_change(_parsed("src/c.py"), [third], "h3")

    found = {chunk.id for chunk in store.chunks_by_ids([first.id, broken.id, third.id])}
    assert found == {first.id, third.id}
    assert store.counts()["files"] == 2


def test_write_batch_rolls_back_everything_on_outer_failure(
    traced_store: tuple[Store, sqlite3.Connection],
    make_chunk: Callable[..., ChunkDef],
) -> None:
    """批内未捕获的异常 → 整批回滚（ingest 的原子语义）。"""
    store, _ = traced_store
    chunk = make_chunk()

    with pytest.raises(RuntimeError, match="outer"):
        with store.write_batch():
            store.apply_file_change(_parsed(chunk.file_path), [chunk], "h")
            raise RuntimeError("outer")

    assert store.counts()["files"] == 0
    assert store.counts()["chunks"] == 0
