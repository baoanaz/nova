"""查询延迟优化的行为护栏：提前发起 query embedding、请求内只读缓存、SQLite 读 PRAGMA。"""

from __future__ import annotations

import threading
from pathlib import Path

from nova_core.engine import Engine
from nova_core.storage import Store
from nova_core.storage.db import connect
from nova_core.storage.readcache import ReadCachedStore

from .conftest import (
    DESIGN_DOC,
    MODULE_PATH,
    QUERY,
    SPEC_PATH,
    TOKEN_MODULE,
    DeterministicBigramEmbedding,
    write_repo,
)


def _engine(tmp_path: Path, provider) -> tuple[Engine, str]:
    repo = tmp_path / "repo"
    write_repo(repo, {MODULE_PATH: TOKEN_MODULE, SPEC_PATH: DESIGN_DOC})
    engine = Engine(tmp_path / "data", provider=provider)
    handle = engine.resolve_project("latency", "latency")
    engine.ingest_repo(handle.project_id, repo)
    return engine, handle.project_id


def test_query_embedding_starts_before_the_index_is_opened(tmp_path, monkeypatch) -> None:
    """embedding 请求在 ``Store.open`` 返回之前就已发出（二者重叠，而不是串行）。"""
    started = threading.Event()

    class SpyProvider(DeterministicBigramEmbedding):
        def embed_query(self, texts):  # noqa: ANN001
            started.set()
            return super().embed_query(texts)

    engine, project = _engine(tmp_path, SpyProvider())
    engine.query_cache.clear()
    started.clear()
    real_open = Store.open.__func__

    def slow_open(cls, directory):  # noqa: ANN001
        # 打开索引时 embedding 必须已经开跑；若仍是串行，这里会等满超时并导致断言失败。
        assert started.wait(timeout=5), "query embedding 没有在打开索引前发起"
        return real_open(cls, directory)

    monkeypatch.setattr(Store, "open", classmethod(slow_open))
    trace = engine.search_with_trace(project, QUERY, 14_000)
    assert not trace.degraded and "vector" in trace.channels_used


def test_results_identical_with_and_without_read_cache(tmp_path) -> None:
    """只读缓存不改变输出：同一个索引，缓存代理与直连 Store 的组装结果逐字一致。"""
    engine, project = _engine(tmp_path, DeterministicBigramEmbedding())
    cached = engine.search_with_trace(project, QUERY, 14_000).pack

    original = ReadCachedStore.__getattr__
    ReadCachedStore.__getattr__ = lambda self, name: getattr(self._store, name)  # 关闭记忆
    try:
        plain = engine.search_with_trace(project, QUERY, 14_000).pack
    finally:
        ReadCachedStore.__getattr__ = original
    assert [(e.path, e.lines, e.content) for e in cached.evidence] == [
        (e.path, e.lines, e.content) for e in plain.evidence
    ]
    assert cached.answerable == plain.answerable and cached.confidence == plain.confidence


def test_read_cache_memoizes_and_isolates_returned_lists(tmp_path) -> None:
    engine, project = _engine(tmp_path, DeterministicBigramEmbedding())
    with Store.open(engine.project_dir(project)) as raw:
        store = ReadCachedStore(raw)
        first = store.exact_symbols("TokenService.refresh_token", limit=None)
        first.append("polluted")  # 调用方原地修改（如 expand 的 edges.extend）
        second = store.exact_symbols("TokenService.refresh_token", limit=None)
        assert "polluted" not in second, "缓存必须给每个调用方独立的列表"
        assert store.hits == 1 and store.misses == 1
        store.edges_for("x", kinds=["calls"])
        store.edges_for("x", kinds=["calls"])  # list 参数也能作为 key
        assert store.hits == 2
        assert store.counts()["files"] >= 1, "非记忆方法直通"


def test_sqlite_read_pragmas_are_applied(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "x.db")
    assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY
    assert conn.execute("PRAGMA cache_size").fetchone()[0] == -16384
    conn.close()
    monkeypatch.setenv("NOVA_SQLITE_MMAP_MB", "0")
    conn = connect(tmp_path / "y.db")
    assert conn.execute("PRAGMA mmap_size").fetchone()[0] == 0, "可用环境变量关闭 mmap"
    conn.close()
