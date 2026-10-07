"""TASK-REVIEW-RUNTIME P1-1 / P1-5（简版）：index-state.json 状态门控。

外部架构评审的原问题：SQLite 按文件提交、向量稍后更新，两者**不是原子提交**，
而 ``search()`` 不取锁 → 查询可能读到"SQLite 新、向量旧"的中间状态且**无从察觉**。

完整解法（generation staging + 原子切换）需要改目录布局，而当前数据根下已有多个
持久索引（langchain 179 MB），改布局等于全部重建。因此本轮实现评审给出的短期方案：
外部状态文件记录 ``building/ready/failed`` 与期望计数，让中间状态**可见**并显式降级。

锁定四条不变量：

1. 索引成功后 ``ready`` 且期望计数与库内一致；
2. 索引抛错后 ``failed``（且异常原样向上抛，不被吞）；
3. ``building`` → 查询报 degraded（可读原因含"构建中"）；
4. ``ready`` 但对账不一致（向量被外部清空）→ 查询报 degraded。
"""

from __future__ import annotations

from pathlib import Path

from nova_core.engine import Engine
from nova_core.pipeline.index_state import (
    IndexState,
    building_state,
    failed_state,
    read_index_state,
    write_index_state,
)
from nova_core.storage import Store

from .conftest import DESIGN_DOC, QUERY, TOKEN_MODULE, DeterministicBigramEmbedding


def _engine(tmp_path: Path) -> tuple[Engine, str]:
    engine = Engine.open(tmp_path, provider=DeterministicBigramEmbedding())
    project_id = engine.resolve_project("identity:p1-1", "p1-1").project_id
    return engine, project_id


def _upload(engine: Engine, project_id: str) -> None:
    from nova_core.hashing import blob_hash
    from nova_core.types import BlobInput, ChangeSet

    files = {"src/token_service.py": TOKEN_MODULE, "docs/token.md": DESIGN_DOC}
    payloads = [(path, data.encode("utf-8")) for path, data in files.items()]
    engine.ingest(
        project_id,
        ChangeSet(
            added=tuple(
                BlobInput(path=path, content=data, blob_hash=blob_hash(path, data))
                for path, data in payloads
            )
        ),
    )


def test_successful_ingest_marks_ready_with_matching_counts(tmp_path: Path) -> None:
    """不变量 1：索引成功后 ready，且期望计数与库内实际一致。"""
    engine, project_id = _engine(tmp_path)
    _upload(engine, project_id)

    state = read_index_state(engine.project_dir(project_id))
    assert state is not None
    assert state.status == "ready"

    with Store.open(engine.project_dir(project_id)) as store:
        assert state.expected_chunks == store.counts()["chunks"]
        same = state.mismatch_reason(
            chunks=store.counts()["chunks"], vectors=state.expected_vectors
        )
        assert same is None


def test_failed_ingest_marks_failed_and_reraises(tmp_path: Path, monkeypatch) -> None:
    """不变量 2：索引抛错 → failed，且异常原样向上抛（观测不得吞掉真实失败）。"""
    engine, project_id = _engine(tmp_path)

    import nova_core.pipeline.indexer as indexer_mod

    def boom(self, changes):  # noqa: ANN001, ARG001
        raise RuntimeError("模拟索引崩溃")

    monkeypatch.setattr(indexer_mod.Indexer, "ingest", boom)

    import pytest

    with pytest.raises(RuntimeError, match="模拟索引崩溃"):
        _upload(engine, project_id)

    state = read_index_state(engine.project_dir(project_id))
    assert state is not None
    assert state.status == "failed"
    assert state.reason is not None and "模拟索引崩溃" in state.reason


def test_building_state_degrades_search(tmp_path: Path) -> None:
    """不变量 3：索引构建中 → 查询 degraded 且原因可读。"""
    engine, project_id = _engine(tmp_path)
    _upload(engine, project_id)

    write_index_state(engine.project_dir(project_id), building_state())

    trace = engine.search_with_trace(project_id, QUERY)
    assert trace.degraded is True
    assert trace.degraded_reason is not None
    assert "构建中" in trace.degraded_reason


def test_ready_but_vector_mismatch_degrades_search(tmp_path: Path) -> None:
    """不变量 4：ready 但对账不一致 → degraded。

    这是“SQLite 已提交、向量没跟上”的最小可复现形态。
    （直接删 ``vectors/`` 目录会让 LanceDB 自身报错，测不到我们的对账分支；
    因此改用“文件声明了更多向量、实际没那么多”这一形态，它等价于半成品索引。）
    """
    engine, project_id = _engine(tmp_path)
    _upload(engine, project_id)

    with Store.open(engine.project_dir(project_id)) as store:
        chunks = store.counts()["chunks"]
    # 声明比实际多 1 个向量：等价于“向量阶段没跑完”。
    write_index_state(
        engine.project_dir(project_id),
        IndexState(
            status="ready",
            updated_at=0.0,
            expected_chunks=chunks,
            expected_vectors=chunks + 1,
        ),
    )

    trace = engine.search_with_trace(project_id, QUERY)
    assert trace.degraded is True
    assert trace.degraded_reason is not None
    assert "vectors 期望" in trace.degraded_reason


def test_no_state_file_does_not_degrade(tmp_path: Path) -> None:
    """无状态标记（升级前建的索引 / 手工放置的索引）→ 不降级，行为与旧版一致。"""
    engine, project_id = _engine(tmp_path)
    _upload(engine, project_id)

    # 模拟"升级前建的索引"：文件不存在。
    (engine.project_dir(project_id) / "index-state.json").unlink()
    assert read_index_state(engine.project_dir(project_id)) is None

    trace = engine.search_with_trace(project_id, QUERY)
    assert trace.degraded is False


def test_index_state_roundtrip_and_bad_payload() -> None:
    """状态文件解析健壮性：结构不符 → None（不抛错），不阻断调用方。"""
    assert IndexState.from_json({"status": "weird"}) is None
    assert IndexState.from_json("not a dict") is None
    assert IndexState.from_json(None) is None

    state = failed_state(stage="ingest", reason="磁盘满")
    restored = IndexState.from_json(state.to_json())
    assert restored == state
