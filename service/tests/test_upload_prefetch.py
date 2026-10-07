"""上传期预取（service 侧端到端）：按客户端协议分批 deferIndexing 上传 → flush → 检索。

预取只改变计算发生的时间：开 / 关预取的两个项目，索引全部逻辑行一致，且开启时
flush 的 ingest 确实取用了上传期算好的结果。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from nova_core.storage.db import DB_FILENAME
from nova_service.runtime import EngineManager

from tests.test_sync_api import encode_blob

_ROOT = Path(__file__).resolve().parents[2] / "core"
_SOURCES = sorted((_ROOT / "nova_core").rglob("*.py"))[:60]


def _resolve(client, key: str) -> str:
    return client.post(
        "/api/projects/resolve", json={"identityKey": key, "displayName": key}
    ).json()["projectId"]


def _sync(client, manager: EngineManager, project: str) -> object:
    reports = []
    original = manager.ingest

    def recording(pid, changes):  # noqa: ANN001, ANN202
        report = original(pid, changes)
        reports.append(report)
        return report

    manager.ingest = recording  # type: ignore[method-assign]
    try:
        for start in range(0, len(_SOURCES), 15):  # 多批上传（与客户端分批一致）
            blobs = [
                encode_blob(source.relative_to(_ROOT).as_posix(), source.read_bytes())
                for source in _SOURCES[start : start + 15]
            ]
            response = client.post(
                "/api/sync/batch-upload",
                json={"projectId": project, "deferIndexing": True, "blobs": blobs},
            )
            assert response.status_code == 200, response.text
        prefetcher = manager.engine.prefetcher
        if prefetcher is not None:  # 等子进程把上传期的预取做完（模拟上传窗口足够长）
            prefetcher.wait_idle(timeout=120)
        assert client.post("/api/sync/flush", json={"projectId": project}).status_code == 200
        manager._indexers[project].join(30)  # noqa: SLF001
        status = client.get(f"/api/sync/status/{project}").json()
        assert status["pendingJobs"] == 0
    finally:
        manager.ingest = original  # type: ignore[method-assign]
    assert len(reports) == 1
    return reports[0]


def _dump(manager: EngineManager, project: str) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(manager.project_dir(project) / DB_FILENAME)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'chunks_fts_%' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        dump = {}
        for table in tables:
            cursor = conn.execute(f"SELECT * FROM {table}")  # noqa: S608 - 表名来自库本身
            columns = [d[0] for d in cursor.description]
            keep = [i for i, name in enumerate(columns) if name != "indexed_at"]
            dump[table] = sorted((tuple(row[i] for i in keep) for row in cursor), key=repr)
        dump["fts"] = sorted(
            (tuple(row) for row in conn.execute(
                "SELECT c.id, f.content_seg, f.signature_seg, f.docstring_seg, f.file_path"
                " FROM chunks_fts f JOIN chunks c ON c.rowid = f.rowid"
            )),
            key=repr,
        )
        return dump
    finally:
        conn.close()


def test_upload_prefetch_matches_inline_end_to_end(client, engine_manager) -> None:
    prefetched_project = _resolve(client, "prefetch-on")
    report = _sync(client, engine_manager, prefetched_project)
    assert report.files_parsed == len(_SOURCES)
    assert report.prefetched == len(_SOURCES)

    prefetcher = engine_manager.engine.prefetcher
    engine_manager.engine.set_prefetcher(None)
    try:
        inline_project = _resolve(client, "prefetch-off")
        inline_report = _sync(client, engine_manager, inline_project)
    finally:
        engine_manager.engine.set_prefetcher(prefetcher)
    assert inline_report.prefetched == 0
    assert _dump(engine_manager, prefetched_project) == _dump(engine_manager, inline_project)
    assert prefetcher is not None and prefetcher.held_bytes == 0

    search = client.post(
        "/api/query/search",
        json={"projectId": prefetched_project, "query": "prepare file", "maxTokens": 2000},
    )
    assert search.status_code == 200, search.text
