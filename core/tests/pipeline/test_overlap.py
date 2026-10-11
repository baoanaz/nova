"""Tail dispatch / commit rollback / persistence instrumentation invariants."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import pytest

from .conftest import PY_MODULE


def _tail(monkeypatch, embedding):
    started = threading.Event()
    original = embedding.embed

    def embed(texts):
        started.set()
        return original(texts)

    monkeypatch.setattr(embedding, "embed", embed)
    monkeypatch.setattr(
        "nova_core.pipeline.embedding_sink.embed_window_size", lambda _: 4000
    )
    return started


def test_tail_runs_before_write_batch_exits(
    monkeypatch, indexer, embedding, change_set, store, vectors
) -> None:
    started = _tail(monkeypatch, embedding)
    original = store.write_batch

    @contextmanager
    def batch():
        with original():
            yield
            assert store._conn.in_transaction  # noqa: SLF001 - verify original transaction
            assert started.wait(3), "tail must be dispatched before COMMIT"

    monkeypatch.setattr(store, "write_batch", batch)
    report = indexer.ingest(change_set(added={"a.py": PY_MODULE}))
    assert not report.errors
    assert vectors.count() == store.counts()["chunks"] > 0
    names = {name for name, _, _ in report.timings}
    assert {
        "flush_pending", "persist_reconcile", "persist_chunks", "persist_fts",
        "persist_symbols", "persist_specs", "persist_edges", "persist_unresolved",
    } <= names
    assert store._write_clock is None  # noqa: SLF001 - instrumentation restored


def test_real_commit_failure_rolls_back_and_retry_is_visible(
    monkeypatch, indexer, change_set, store, vectors
) -> None:
    connection = store._conn  # noqa: SLF001
    failed = []

    class CommitFault:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, sql, *args, **kwargs):
            if sql == "COMMIT" and not failed:
                chunks = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
                if chunks:
                    assert connection.in_transaction
                    failed.append(True)
                    # Fail exactly at COMMIT without ending the underlying transaction.
                    raise sqlite3.OperationalError("injected actual COMMIT failure")
            return connection.execute(sql, *args, **kwargs)

    monkeypatch.setattr(store, "_conn", CommitFault())
    changes = change_set(added={"a.py": PY_MODULE})
    with pytest.raises(sqlite3.OperationalError, match="injected actual COMMIT failure"):
        indexer.ingest(changes)
    assert failed
    assert not connection.in_transaction
    assert store.counts()["chunks"] == 0

    report = indexer.ingest(changes)
    assert not report.errors and not connection.in_transaction
    count = store.counts()["chunks"]
    assert vectors.count() == count > 0
    # A fresh reader must see durable committed rows, not only this Store's transaction.
    with sqlite3.connect(store._project_dir / "index.db") as reader:  # noqa: SLF001
        assert reader.execute("SELECT count(*) FROM chunks").fetchone()[0] == count


def test_batch_body_failure_keeps_sql_atomic_and_retry_repairs_vectors(
    monkeypatch, indexer, embedding, change_set, store, vectors
) -> None:
    started = _tail(monkeypatch, embedding)
    original = store.write_batch
    graph = indexer._resolve  # noqa: SLF001
    graph_calls = []

    def resolve(*args, **kwargs):
        graph_calls.append(1)
        return graph(*args, **kwargs)

    @contextmanager
    def fail_commit():
        with original():
            yield
            assert started.wait(3)
            raise OSError("injected commit failure")

    changes = change_set(added={"a.py": PY_MODULE})
    monkeypatch.setattr(indexer, "_resolve", resolve)
    monkeypatch.setattr(store, "write_batch", fail_commit)
    with pytest.raises(OSError, match="injected commit failure"):
        indexer.ingest(changes)
    assert store.counts()["chunks"] == 0
    assert not graph_calls
    assert store._write_clock is None  # noqa: SLF001

    monkeypatch.setattr(store, "write_batch", original)
    report = indexer.ingest(changes)
    assert not report.errors and len(graph_calls) == 1
    assert vectors.count() == store.counts()["chunks"] > 0
    assert store.fts_search("helper")
