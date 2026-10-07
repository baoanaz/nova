"""索引状态标记 ``index-state.json``（TASK-REVIEW-RUNTIME P1-1 / P1-5 简版）。

## 解决什么问题

外部架构评审 P1-1 / P1-5 指出：SQLite（按文件提交）与 LanceDB（稍后更新）**不是原子提交**，
而 ``search()`` 不取锁，因此查询可能读到"SQLite 新、向量旧"的中间状态，且**无从察觉**。

完整解法是 generation staging + 原子切换 manifest，但那需要改目录布局——当前
``~/.nova/bench/<model>-d<dim>/projects/<id>/`` 下已有多个持久索引（langchain 179 MB），
改布局等于全部重建。本模块实现评审给出的**短期方案**：一个外部状态文件，记录
``building / ready / failed`` 与**期望的 chunk / vector 数**，让中间状态**可见**，
由查询侧显式降级而不是静默给错结果。

## 为什么放在项目目录之外

放在 ``projects/<id>/`` 内会被索引自身当作数据；本文件是"关于这份索引的元数据"，
与 ``project.json`` 同级但独立存在，删除/重建索引时随目录一起消失即可。

## 与 ``scan_manifest.json`` 的区别

那个记的是"上次扫描看到了哪些文件"（增量输入）；本文件记的是"这份索引此刻是否自洽"
（查询门控）。两者生命周期不同，不合并。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

__all__ = [
    "INDEX_STATE_FILENAME",
    "IndexState",
    "clear_index_state",
    "read_index_state",
    "write_index_state",
]

logger = logging.getLogger(__name__)

#: 状态文件名（位于项目目录内，与 ``project.json`` / ``index.db`` 同级）。
INDEX_STATE_FILENAME = "index-state.json"

#: ``status`` 取值。
IndexStatus = Literal["building", "ready", "failed"]


@dataclass(frozen=True, slots=True)
class IndexState:
    """一份索引的自洽状态快照。

    ``expected_chunks`` / ``expected_vectors`` 是**期望值**（阶段开始时算出的目标数），
    与库里的实际值比对就能发现"SQLite 已提交、向量没跟上"这类半成品状态——
    这正是原评审 P1-1 说的"部分缺失/旧向量/孤儿向量"检测的最低成本形式。
    """

    status: IndexStatus
    updated_at: float
    expected_chunks: int = 0
    expected_vectors: int = 0
    stage: str | None = None
    reason: str | None = None

    def to_json(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: object) -> IndexState | None:
        """从 JSON 还原；结构不符 → ``None``（不抛错，调用方按"无状态"处理）。"""
        if not isinstance(payload, dict):
            return None
        status = payload.get("status")
        if status not in ("building", "ready", "failed"):
            return None
        try:
            return cls(
                status=status,
                updated_at=float(payload.get("updated_at") or 0.0),
                expected_chunks=int(payload.get("expected_chunks") or 0),
                expected_vectors=int(payload.get("expected_vectors") or 0),
                stage=payload.get("stage") if isinstance(payload.get("stage"), str) else None,
                reason=payload.get("reason") if isinstance(payload.get("reason"), str) else None,
            )
        except (TypeError, ValueError):
            return None

    def mismatch_reason(self, *, chunks: int, vectors: int) -> str | None:
        """与库内实际计数比对，返回可读的不一致原因（一致 → ``None``）。

        **只在 ``ready`` 上调用**：``building`` / ``failed`` 的不一致是预期状态，
        不是"对账失败"。
        """
        problems: list[str] = []
        if self.expected_chunks and chunks != self.expected_chunks:
            problems.append(f"chunks 期望 {self.expected_chunks}、实际 {chunks}")
        if self.expected_vectors and vectors != self.expected_vectors:
            problems.append(f"vectors 期望 {self.expected_vectors}、实际 {vectors}")
        if vectors == 0 and chunks > 0:
            problems.append(f"向量索引为空（chunks={chunks}）")
        return "；".join(problems) if problems else None


def index_state_path(project_dir: Path) -> Path:
    return Path(project_dir) / INDEX_STATE_FILENAME


def write_index_state(project_dir: Path, state: IndexState) -> None:
    """原子写入（tmp + replace）。

    写失败**只告警不抛**：状态标记是观测手段，不能因为它写不进去就让索引失败——
    那会把"可观测性缺陷"升级成"可用性缺陷"。
    """
    path = index_state_path(project_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    except OSError as exc:  # noqa: BLE001 - 观测不阻断索引
        logger.warning("index-state 写入失败（不影响索引结果）：%s", exc)


def read_index_state(project_dir: Path) -> IndexState | None:
    """读状态；文件不存在/损坏 → ``None``（= "无状态"，调用方不做门控）。"""
    path = index_state_path(project_dir)
    if not path.is_file():
        return None
    try:
        return IndexState.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def clear_index_state(project_dir: Path) -> None:
    """删除状态文件（索引目录被移除时清理）。"""
    try:
        index_state_path(project_dir).unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001
        logger.warning("index-state 删除失败：%s", exc)


def building_state(*, chunks: int = 0, vectors: int = 0) -> IndexState:
    """构建中状态。参数名与 :func:`ready_state` 保持一致（避免调用方两套字段名互相错配）。"""
    return IndexState(
        status="building",
        updated_at=time.time(),
        expected_chunks=chunks,
        expected_vectors=vectors,
        stage="indexing",
    )


def ready_state(*, chunks: int, vectors: int) -> IndexState:
    return IndexState(
        status="ready",
        updated_at=time.time(),
        expected_chunks=chunks,
        expected_vectors=vectors,
    )


def failed_state(*, stage: str, reason: str) -> IndexState:
    return IndexState(
        status="failed",
        updated_at=time.time(),
        stage=stage,
        reason=reason,
    )
