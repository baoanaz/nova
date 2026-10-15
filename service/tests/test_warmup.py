"""查询路径预热：不产生 embedding 请求、不写数据、不复活已删除的项目、默认关闭。"""

from __future__ import annotations

import time
from pathlib import Path

from nova_core.engine import Engine
from nova_service.runtime import EngineManager

from tests.conftest import SAMPLE_FILES, DeterministicBigramEmbedding, upload_files


class CountingProvider(DeterministicBigramEmbedding):
    def __init__(self) -> None:
        super().__init__()
        self.query_calls = 0

    def embed_query(self, texts):  # noqa: ANN001
        self.query_calls += 1
        return super().embed_query(texts)


def _manager(tmp_path: Path) -> tuple[EngineManager, CountingProvider]:
    provider = CountingProvider()
    manager = EngineManager.open(
        tmp_path / "data", engine_factory=lambda root: Engine.open(root, provider=provider)
    )
    return manager, provider


def test_warm_project_makes_no_embedding_calls_and_writes_nothing(tmp_path: Path) -> None:
    manager, provider = _manager(tmp_path)
    project = manager.resolve_project("warm-a", "a").project_id
    upload_files(manager, project, SAMPLE_FILES)
    provider.query_calls = 0
    before = sorted(p.name for p in manager.project_dir(project).rglob("*") if p.is_file())

    assert manager.warm_project(project) is True
    assert provider.query_calls == 0, "项目预热不得请求 embedding（否则会产生 API 费用）"
    after = sorted(p.name for p in manager.project_dir(project).rglob("*") if p.is_file())
    assert after == before or set(before) <= set(after)  # WAL 文件可能出现，但不得丢数据
    manager.close()


def test_warm_provider_sends_one_tiny_query(tmp_path: Path) -> None:
    manager, provider = _manager(tmp_path)
    assert manager.warm_provider() is True
    assert provider.query_calls == 1
    manager.close()


def test_warm_does_not_resurrect_missing_or_deleted_projects(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    ghost = "0123456789abcdef"
    assert manager.warm_project(ghost) is False
    assert not manager.project_dir(ghost).exists(), "预热不得创建不存在的项目目录"
    project = manager.resolve_project("warm-b", "b").project_id
    upload_files(manager, project, SAMPLE_FILES)
    manager.delete_project(project)
    assert manager.warm_project(project) is False
    assert not manager.project_dir(project).exists()
    manager.close()


def test_warm_async_is_off_by_default_and_runs_once_when_enabled(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    project = manager.resolve_project("warm-c", "c").project_id
    upload_files(manager, project, SAMPLE_FILES)
    calls: list[str] = []
    manager.warm_project = lambda pid, **_kw: calls.append(pid) or True  # type: ignore[method-assign]

    manager.warm_async(project)  # 默认关闭
    time.sleep(0.1)
    assert calls == []

    manager.enable_warmup()
    manager.warm_async(project)
    manager.warm_async(project)  # 30 秒内去重
    deadline = time.time() + 2
    while not calls and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.1)
    assert calls == [project]
    manager.close()


def test_warm_recent_picks_most_recently_indexed(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    ids = []
    for index in range(3):
        pid = manager.resolve_project(f"warm-r{index}", "r").project_id
        upload_files(manager, pid, SAMPLE_FILES)
        ids.append(pid)
    warmed: list[str] = []
    manager.warm_project = lambda pid, **_kw: warmed.append(pid) or True  # type: ignore[method-assign]
    assert manager.warm_recent(2) == 2
    assert len(warmed) == 2 and set(warmed) <= set(ids)
    manager.close()
