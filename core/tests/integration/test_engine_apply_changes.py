"""TASK-035 §C 回归：``Engine.apply_changes`` 公开面（替代跨包调 ``Engine._ingest``）。

**立项理由**：``nova_service.runtime.EngineManager.ingest`` 原先直接调 ``Engine._ingest``
（跨包调私有方法）。本卡在 core 侧补一个公开包装 ``apply_changes``，签名与 ``_ingest`` 一致，
**``_ingest`` 保留为内部实现**（``ingest`` 仍在用），service 改调公开方法。

本文件锁定三条不变量：

1. ``apply_changes`` 返回 ``IngestReport`` 且与 ``ingest``（只回 job id）落在同一份索引状态上；
2. ``full=True`` 走 ``full_reparse``（与 ``_ingest`` 的同名参数语义一致）；
3. ``source=[...]`` 被真正接住（``DirectorySource`` 未绑定本地目录时不会静默清空）。

CI 不联网、不加载模型（确定性假 embedding，见本目录 ``conftest``）。
"""

from __future__ import annotations

from pathlib import Path

from nova_core.engine import Engine
from nova_core.hashing import blob_hash
from nova_core.pipeline import IngestReport
from nova_core.storage import Store
from nova_core.types import BlobInput, ChangeSet

from .conftest import DESIGN_DOC, QUERY, TOKEN_MODULE, DeterministicBigramEmbedding
from .test_ingest_source import LedgerSource

#: 上传文件集（与 test_ingest_source 同源：1 个 python 模块 + 1 份 markdown 设计文档）。
UPLOADED_FILES: dict[str, bytes] = {
    "src/token_service.py": TOKEN_MODULE.encode("utf-8"),
    "docs/token.md": DESIGN_DOC.encode("utf-8"),
}


def _change_set(files: dict[str, bytes]) -> ChangeSet:
    return ChangeSet(
        added=tuple(
            BlobInput(path=path, content=data, blob_hash=blob_hash(path, data))
            for path, data in files.items()
        )
    )


def _counts(engine: Engine, project_id: str) -> dict[str, int]:
    with Store.open(engine.project_dir(project_id)) as store:
        return store.counts()


def _engine(tmp_path: Path) -> tuple[Engine, str]:
    engine = Engine.open(tmp_path, provider=DeterministicBigramEmbedding())
    project_id = engine.resolve_project("identity:apply-changes", "apply-changes").project_id
    return engine, project_id


def test_apply_changes_returns_ingest_report_and_indexes(tmp_path: Path) -> None:
    """公开包装返回 ``IngestReport``（service 的同步 API 需要它，CF-07 的 ``ingest`` 只回 id）。"""
    engine, project_id = _engine(tmp_path)

    report = engine.apply_changes(project_id, _change_set(UPLOADED_FILES))

    assert isinstance(report, IngestReport)
    assert report.added == len(UPLOADED_FILES)
    assert report.files_parsed == len(UPLOADED_FILES)
    assert report.errors == ()
    counts = _counts(engine, project_id)
    assert counts["files"] == len(UPLOADED_FILES)
    assert counts["chunks"] > 0
    assert engine.search(project_id, QUERY).answerable is True


def test_apply_changes_and_ingest_share_the_same_index_state(tmp_path: Path) -> None:
    """``apply_changes`` 与 ``ingest`` 落在同一份索引状态上（不是两条并行路径）。"""
    engine, project_id = _engine(tmp_path)
    engine.apply_changes(project_id, _change_set(UPLOADED_FILES))
    after_apply = _counts(engine, project_id)

    job_id = engine.ingest(project_id, ChangeSet())  # CF-07 面未变：仍返回 str
    assert isinstance(job_id, str) and job_id
    assert _counts(engine, project_id) == after_apply


def test_apply_changes_source_is_honoured(tmp_path: Path) -> None:
    """``source`` 与 ``full`` 透传到 ``_ingest``：传 source 时重建不会静默清空向量索引。"""
    engine, project_id = _engine(tmp_path)
    engine.apply_changes(project_id, _change_set(UPLOADED_FILES))
    before = _counts(engine, project_id)

    report = engine.apply_changes(
        project_id, ChangeSet(), source=LedgerSource(UPLOADED_FILES), full=True
    )

    assert report.files_parsed == len(UPLOADED_FILES), "full 重建遍历了 source.list_files()"
    assert _counts(engine, project_id) == before, "重建幂等：不重复写入"
    assert engine.search(project_id, QUERY).answerable is True
