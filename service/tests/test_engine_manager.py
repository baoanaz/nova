"""TASK-031 验收：BlobStore / SyncState / BlobSource / EngineManager（含并发与端到端闭环）。"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from nova_core.hashing import blob_hash
from nova_core.pipeline.source import SourcePathError
from nova_core.types import BlobInput, ChangeSet
from nova_service import sync_state as sync_state_module
from nova_service.blobstore import BlobSource, BlobStore, validate_repo_path
from nova_service.runtime import EngineManager
from nova_service.sync_state import MAX_CHECKPOINTS, SyncState

from tests.conftest import (
    SAMPLE_DOC,
    SAMPLE_DOC_PATH,
    SAMPLE_FILES,
    SAMPLE_MODULE,
    SAMPLE_MODULE_PATH,
    TARGET_SYMBOL,
    upload_files,
)


def _content(path: str) -> bytes:
    return SAMPLE_FILES[path].encode("utf-8")


# --------------------------------------------------------------------------- BlobStore


def test_blob_store_put_get_exists_delete_usage(tmp_path: Path) -> None:
    store = BlobStore.open(tmp_path / "project")
    data = b"int main(void) { return 0; }\n"
    digest = blob_hash("src/main.c", data)

    assert store.exists(digest) is False
    assert store.put("src/main.c", digest, data) is True
    assert store.exists(digest) is True
    assert store.get(digest) == data
    assert store.usage() == (1, len(data))
    assert store.path_for(digest).name == digest
    assert store.path_for(digest).parent.name == digest[:2], "两级目录：hash[:2]/hash"

    assert store.delete(digest) is True
    assert store.delete(digest) is False
    assert store.usage() == (0, 0)
    with pytest.raises(FileNotFoundError):
        store.get(digest)


def test_blob_store_duplicate_put_does_not_overwrite(tmp_path: Path) -> None:
    """重复 put 同 hash：返回 False 且**不覆盖**既有字节（文件即真相）。"""
    store = BlobStore.open(tmp_path / "project")
    data = b"original\n"
    digest = blob_hash("src/a.py", data)
    assert store.put("src/a.py", digest, data) is True
    assert store.put("src/a.py", digest, b"tampered\n") is False
    assert store.get(digest) == data


def test_blob_store_rejects_unsafe_paths_and_bad_hashes(tmp_path: Path) -> None:
    store = BlobStore.open(tmp_path / "project")
    data = b"x"
    with pytest.raises(SourcePathError):
        store.put("../evil.py", blob_hash("x", data), data)
    with pytest.raises(SourcePathError):
        store.put("/etc/passwd", blob_hash("x", data), data)
    with pytest.raises(ValueError):
        store.path_for("not-a-hash")


@pytest.mark.parametrize("path", ["../evil.py", "/etc/passwd", "a\\b.py", "src//a.py", ""])
def test_validate_repo_path_rejects(path: str) -> None:
    with pytest.raises(SourcePathError):
        validate_repo_path(path)


def test_validate_repo_path_accepts_relative() -> None:
    assert validate_repo_path("src/deep/a.py") == "src/deep/a.py"


# --------------------------------------------------------------------------- SyncState


def test_sync_state_roundtrip(tmp_path: Path) -> None:
    state = SyncState.load(tmp_path)
    assert state.counts() == {"files": 0, "blobs": 0, "bytes": 0, "checkpoints": 0}

    assert state.record_file("src/a.py", "a" * 64, 12) is True
    assert state.record_file("src/a.py", "a" * 64, 12) is False, "同内容不算变化（幂等）"
    state.record_file("docs/b.md", "b" * 64, 34)
    state.set_head("main", "deadbeef")
    state.record_checkpoint("cp_1", ["a" * 64])
    state.save()

    reloaded = SyncState.load(tmp_path)
    assert reloaded.branch == "main"
    assert reloaded.commit == "deadbeef"
    assert reloaded.file_hashes() == {"src/a.py": "a" * 64, "docs/b.md": "b" * 64}
    assert reloaded.counts() == {"files": 2, "blobs": 2, "bytes": 46, "checkpoints": 1}
    assert reloaded.checkpoints == {"cp_1": ("a" * 64,)}
    assert json.loads(reloaded.path.read_text(encoding="utf-8"))["version"] == 1


def test_sync_state_corrupt_file_returns_empty_without_raising(tmp_path: Path) -> None:
    """损坏 JSON → 空状态（不抛异常），并留下可诊断的 warning。"""
    (tmp_path / "sync-state.json").write_text("{ this is not json", encoding="utf-8")
    state = SyncState.load(tmp_path)
    assert state.counts()["files"] == 0
    assert state.branch is None


def test_sync_state_tolerates_invalid_entries(tmp_path: Path) -> None:
    payload = {
        "version": 1,
        "branch": 5,
        "files": {"ok.py": {"blobHash": "h", "size": 1, "updatedAt": 2}, "bad.py": {"size": -1}},
        "checkpoints": {"cp": ["h"], "broken": "not-a-list"},
    }
    (tmp_path / "sync-state.json").write_text(json.dumps(payload), encoding="utf-8")
    state = SyncState.load(tmp_path)
    assert state.branch is None
    assert list(state.file_hashes()) == ["ok.py"]
    assert list(state.checkpoints) == ["cp"]


def test_sync_state_atomic_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原子写：序列化失败时磁盘上仍是旧的完整版本，且不留 ``.tmp`` 半截文件。"""
    state = SyncState.load(tmp_path)
    state.record_file("src/a.py", "a" * 64, 3)
    state.save()
    good = state.path.read_text(encoding="utf-8")

    state.record_file("src/b.py", "b" * 64, 4)

    def boom(_payload: object) -> str:
        raise RuntimeError("磁盘写坏（模拟中断）")

    monkeypatch.setattr(sync_state_module, "_dumps", boom)
    with pytest.raises(RuntimeError):
        state.save()

    monkeypatch.undo()
    assert state.path.read_text(encoding="utf-8") == good, "旧版本必须完整可读"
    assert not state.path.with_name(state.path.name + ".tmp").exists(), "不得留半截临时文件"
    assert SyncState.load(tmp_path).counts()["files"] == 1


def test_sync_state_remove_paths_is_idempotent(tmp_path: Path) -> None:
    state = SyncState.load(tmp_path)
    state.record_file("src/a.py", "a" * 64, 1)
    assert state.remove_paths(["src/a.py", "src/ghost.py"]) == (("src/a.py",), ("src/ghost.py",))
    assert state.remove_paths(["src/a.py"]) == ((), ("src/a.py",)), "重复删除进 unknown，不报错"


def test_sync_state_checkpoint_lru_keeps_three(tmp_path: Path) -> None:
    state = SyncState.load(tmp_path)
    for index in range(MAX_CHECKPOINTS + 1):
        state.record_checkpoint(f"cp_{index}", [f"{index}" * 64])
    assert len(state.checkpoints) == MAX_CHECKPOINTS
    assert list(state.checkpoints) == ["cp_1", "cp_2", "cp_3"], "最早的 cp_0 被淘汰"


# --------------------------------------------------------------------------- BlobSource


def test_blob_source_reads_ledger_files_and_rejects_escapes(tmp_path: Path) -> None:
    blobs = BlobStore.open(tmp_path / "project")
    for path in (SAMPLE_MODULE_PATH, SAMPLE_DOC_PATH):
        data = _content(path)
        blobs.put(path, blob_hash(path, data), data)
    state = SyncState.load(tmp_path / "project")
    for path in (SAMPLE_MODULE_PATH, SAMPLE_DOC_PATH):
        state.record_file(path, blob_hash(path, _content(path)), len(_content(path)))

    source = BlobSource(blobs, state)
    assert source.list_files() == (SAMPLE_DOC_PATH, SAMPLE_MODULE_PATH), "稳定排序"
    assert source.read(SAMPLE_MODULE_PATH) == _content(SAMPLE_MODULE_PATH)

    with pytest.raises(FileNotFoundError):
        source.read("src/not-in-ledger.py")
    with pytest.raises(SourcePathError):
        source.read("../escape.py")


def test_blob_source_missing_blob_raises_file_not_found(tmp_path: Path) -> None:
    """账本有、镜像缺失 → ``FileNotFoundError``（core 会记进 report.errors，不中断整次 ingest）。"""
    state = SyncState.load(tmp_path / "project")
    state.record_file("src/a.py", "c" * 64, 10)
    source = BlobSource(BlobStore.open(tmp_path / "project"), state)
    with pytest.raises(FileNotFoundError):
        source.read("src/a.py")


# --------------------------------------------------------------------------- EngineManager


def test_resolve_project_is_idempotent(engine_manager: EngineManager) -> None:
    first = engine_manager.resolve_project("identity:same-repo", "repo")
    second = engine_manager.resolve_project("identity:same-repo", "other name")
    third = engine_manager.resolve_project("identity:other-repo", "other")
    assert first.created is True
    assert second.created is False
    assert second.project_id == first.project_id
    assert third.project_id != first.project_id
    assert engine_manager.project_exists(first.project_id) is True
    assert [item["projectId"] for item in engine_manager.list_projects()] != []


def test_concurrent_ingest_same_project_is_serialized(
    engine_manager: EngineManager, project_id: str
) -> None:
    """两个线程同时 ingest 同一 project：不互踩（不抛异常、最终 chunk 数正确）。"""
    upload_files(engine_manager, project_id, SAMPLE_FILES)
    baseline = engine_manager.sync_status(project_id)["chunks"]
    assert baseline > 0

    updated_module = SAMPLE_MODULE.replace('return "old"', 'return "new"')
    updated_doc = SAMPLE_DOC.replace("令牌过期时由", "令牌失效时由")
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def worker(path: str, content: str) -> None:
        data = content.encode("utf-8")
        changes = ChangeSet(
            modified=(BlobInput(path=path, content=data, blob_hash=blob_hash(path, data)),)
        )
        barrier.wait(timeout=10)  # 尽量同时进入 ingest（真正压到 per-project 锁上）
        try:
            engine_manager.ingest(project_id, changes)
        except BaseException as exc:  # noqa: BLE001 - 测试要把任何异常都带出来
            failures.append(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(worker, SAMPLE_MODULE_PATH, updated_module),
            pool.submit(worker, SAMPLE_DOC_PATH, updated_doc),
        ]
        for future in futures:
            future.result(timeout=120)

    assert not failures, f"并发 ingest 抛异常：{failures}"
    status = engine_manager.sync_status(project_id)
    assert status["chunks"] == baseline, "两次 modified 都是同结构改动：chunk 数不变"
    assert status["filesIndexed"] == 2


def test_delete_project_removes_directory_and_is_idempotent(
    engine_manager: EngineManager, project_id: str
) -> None:
    upload_files(engine_manager, project_id, SAMPLE_FILES)
    directory = engine_manager.project_dir(project_id)
    assert directory.is_dir()

    assert engine_manager.delete_project(project_id) is True
    assert not directory.exists(), "core 的 rm -rf 覆盖 blobs/ 与 sync-state.json"
    assert engine_manager.project_exists(project_id) is False
    assert engine_manager.delete_project(project_id) is False


def test_upload_index_search_closed_loop(
    engine_manager: EngineManager, project_id: str
) -> None:
    """**端到端小闭环**：上传 2 个文件（含 1 个 markdown）→ 状态数字正确 → 检索命中符号。"""
    result = upload_files(engine_manager, project_id, SAMPLE_FILES)
    report = result["report"]
    assert report.errors == ()
    assert report.skipped_files == ()

    status = engine_manager.sync_status(project_id)
    assert status["filesIndexed"] == 2
    assert status["chunks"] > 0
    assert status["symbols"] > 0
    assert status["blobs"] == {"count": 2, "bytes": sum(len(_content(p)) for p in SAMPLE_FILES)}
    assert status["filesIndexed"] == len(SAMPLE_FILES)
    assert status["lastIndexedAt"] is not None

    trace = engine_manager.search(project_id, TARGET_SYMBOL)
    paths = {item.path for item in [*trace.pack.evidence, *trace.pack.docs]}
    assert SAMPLE_MODULE_PATH in paths, f"未命中上传文件：{sorted(paths)}"
    assert trace.pack.answerable is True

    docs_trace = engine_manager.search(project_id, "令牌过期后在哪里刷新")
    assert SAMPLE_DOC_PATH in {item.path for item in docs_trace.pack.docs}, (
        "markdown 也进了索引（spec 块）"
    )
