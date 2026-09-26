"""TASK-013 §A/§B/DoD：ingest 增量语义与 status 与 Store 实况一致。

覆盖卡内验收：`ingest → status` 字段一致；二次 ingest 无重嵌入；改一个函数只重嵌一个 chunk；
`--full` 走全量重解析；删文件清理索引行。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from zace_core.storage import Store
from zace_core.vectors import VectorStore

from .conftest import LOGGING_PY, REPO_FILES, TEST_DIM

RunCli = Callable[..., tuple[int, str, str]]


def _json_out(out: str) -> dict:
    return json.loads(out)


def test_ingest_populates_index_and_status_matches_store(
    repo: Path, data_root: Path, run_cli: RunCli
) -> None:
    code, out, err = run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    assert code == 0, err
    assert f"files: added={len(REPO_FILES)} modified=0 deleted=0" in out
    assert "invalidation=none" in out
    project_id = _project_id(out)

    code, out, err = run_cli("status", "--repo", str(repo), "--data", str(data_root), "--json")
    assert code == 0, err
    status = _json_out(out)

    with Store.open(data_root / "projects" / project_id) as store:
        counts = store.counts()
        freshness = store.freshness()

    assert status["project_id"] == project_id
    assert status["files_indexed"] == counts["files"] == len(REPO_FILES)
    assert status["chunks"] == counts["chunks"] > 0
    assert status["symbols"] == counts["symbols"] > 0
    assert status["edges"] == counts["edges"] >= 1
    assert status["pending_jobs"] == 0
    assert status["last_indexed_at"] == freshness.indexed_at


def test_status_text_output_has_card_fields(repo: Path, data_root: Path, run_cli: RunCli) -> None:
    run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    code, out, _err = run_cli("status", "--repo", str(repo), "--data", str(data_root))
    assert code == 0
    for field in ("files:", "chunks:", "symbols:", "edges:", "freshness:"):
        assert field in out


def test_second_ingest_is_incremental_and_reembeds_nothing(
    repo: Path, data_root: Path, run_cli: RunCli, provider
) -> None:
    run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    # 首次：一次批量嵌入（TASK-115 的窗口在 pipeline 层全局攒满再派发，调用次数与 K 无关）。
    assert provider.calls == 1
    calls_after_first = provider.calls

    code, out, err = run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    assert code == 0, err
    assert "files: added=0 modified=0 deleted=0 parsed=0" in out
    assert "chunks: new=0 reused=0 removed=0" in out
    assert "vectors: upserted=0 deleted=0" in out
    assert provider.calls == calls_after_first, "二次 ingest 不应产生任何嵌入调用"


def test_modified_file_reembeds_only_changed_chunk(
    repo: Path, data_root: Path, run_cli: RunCli, provider
) -> None:
    run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    before = len(provider.texts)

    # 只改一个函数的 docstring：module 块（1-2 行）与函数正文签名不变 → 只该重嵌 1 个 chunk。
    changed = LOGGING_PY.replace("把事件名格式化成一行日志。", "把事件名格式化成一行结构化日志。")
    assert changed != LOGGING_PY
    (repo / "src/util/logging.py").write_text(changed, encoding="utf-8")

    code, out, err = run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    assert code == 0, err
    assert "modified=1" in out
    assert len(provider.texts) - before == 1


def test_full_flag_triggers_full_reparse(
    repo: Path, data_root: Path, run_cli: RunCli, provider
) -> None:
    run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    first_pass = len(provider.texts)
    assert first_pass > 0

    code, out, err = run_cli("ingest", "--repo", str(repo), "--data", str(data_root), "--full")
    assert code == 0, err
    assert "mode: full_reparse (invalidation=full_reparse)" in out
    assert f"parsed={len(REPO_FILES)}" in out
    # 全量重解析 = 原子重建向量表 + 重写全部存量 chunk 的向量。
    # TASK-114 起统一走向量 sink：存量内容在跨项目内容缓存（TASK-111）里命中，
    # 因此向量表被完整重建、但**零新增 embedding**（旧实现每次 --full 都重嵌一遍、白烧额度）。
    assert len(provider.texts) == first_pass
    project_dir = data_root / "projects" / _project_id(out)
    with Store.open(project_dir) as store, VectorStore.open(project_dir, TEST_DIM) as vectors:
        assert store.counts()["chunks"] > 0
        assert vectors.count() == store.counts()["chunks"]


def test_deleted_file_is_removed_from_index(
    repo: Path, data_root: Path, run_cli: RunCli
) -> None:
    code, out, err = run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    assert code == 0, err
    project_id = _project_id(out)
    project_dir = data_root / "projects" / project_id
    with Store.open(project_dir) as store:
        before = store.counts()

    (repo / "src/util/logging.py").unlink()
    code, out, err = run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    assert code == 0, err
    assert "deleted=1" in out
    # 跨进程删除：TASK-007 无法枚举旧 chunk id 清向量（已在流水线卡记录），须显式告警；
    # 索引行必须清干净（下面的 Store 断言），否则检索侧会返回已删文件的证据。
    assert "向量无法枚举" in err

    with Store.open(project_dir) as store:
        after = store.counts()
    assert after["files"] == before["files"] - 1
    assert after["chunks"] < before["chunks"]

    # 孤儿向量不构成证据：检索侧按 chunk_id 回库找不到切片 → 被丢弃（TASK-007 记录的行为）。
    code, out, err = run_cli(
        "search", "log_event 在哪里实现的", "--repo", str(repo), "--data", str(data_root)
    )
    assert code == 0, err
    assert "src/util/logging.py" not in out


def _project_id(out: str) -> str:
    line = next(line for line in out.splitlines() if line.startswith("project:"))
    return line.split(":", 1)[1].strip().split(" ", 1)[0]
