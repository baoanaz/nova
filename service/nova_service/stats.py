"""账户资料与用量/历史聚合（TASK-062/064 的服务层合成）。

为什么单独成文件：**"平均耗时/成功失败次数"来自两个不同的源**——

- **内存态进度**（:class:`~nova_service.indexer.ProjectIndexer`）：当前这一次跑到哪了，
  服务重启即丢（D-30 的如实呈现）；
- **落库历史**（``nova-meta.db`` 的 ``index_runs`` / ``query_audit``）：跨次、跨重启的统计。

两者含义不同、生命周期不同，合成规则集中在这里（唯一一处），页面只消费结果，
不各自拼凑——否则"成功次数"在首页与历史页会给出不同的数。

口径（冻结给 web）：

- ``avgDurationMs`` **只统计成功**的索引（失败 run 的耗时是"失败得多快"，混入即误导）；
- ``processedFiles`` 与 ``totalFiles`` **不同量纲**（前者只数真正解析的文件），不做除法；
- 无数据时一律 ``None`` / ``0``，**不填假值**。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nova_service.config import Settings
from nova_service.metadb import MetaDB

__all__ = ["IndexStatsBundle", "account_overview", "dir_size_bytes", "host_memory"]


@dataclass(frozen=True, slots=True)
class IndexStatsBundle:
    """索引统计（内存态当前进度 + 落库历史聚合 + 磁盘占用）。"""

    project_id: str
    display_name: str
    attached_root: str | None
    #: 内存态：本次进度的六字段（服务重启后为 idle，口径见 M2a 手册 §2）。
    current: dict[str, Any]
    #: 落库聚合：total/succeeded/failed/avgDurationMs/...（``index_stats().to_json()``）。
    history: dict[str, Any]
    #: 磁盘占用（字节；data_root/projects/{id} 的 du）。
    disk_bytes: int

    def to_json(self) -> dict[str, Any]:
        return {
            "projectId": self.project_id,
            "displayName": self.display_name,
            "attachedRoot": self.attached_root,
            "current": self.current,
            "history": self.history,
            "diskBytes": self.disk_bytes,
        }


def dir_size_bytes(path: Path) -> int:
    """目录占用（递归求和；不存在 → 0）。**只统计、不改动**。"""
    if not path.is_dir():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:  # 并发删除/权限：跳过该文件，不让统计失败
            continue
    return total


def host_memory() -> dict[str, Any]:
    """主机内存占用（后台系统状态展示；用户 2026-09-15 要求）。

    数据源 ````/proc/meminfo``（Linux，零依赖）：服务跑在 2C2G 的 VPS 上，
    "内存还剩多少"是判断能不能再收一个用户的实际依据。

    诚实性口径（与全库一致）：

    - ``MemAvailable`` 优先（内核估计的**还能用**的量，含可回收缓存）；没有它时退回
      ``MemFree``，并**如实标注**用的是哪个口径（``availableBasis``）；
    - 读不到（非 Linux / 权限）→ 三个字段全 ``None`` + 一行 ``reason``，**不编 0**：
      "没读到"与"用了 0"是两件事。
    """
    path = Path("/proc/meminfo")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "totalBytes": None,
            "availableBytes": None,
            "usedBytes": None,
            "usedRatio": None,
            "availableBasis": None,
            "reason": f"读不到 /proc/meminfo（{type(exc).__name__}）：仅 Linux 可测",
        }
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            kib = int(parts[0])  # /proc/meminfo 的值以 kB 为单位
        except ValueError:
            continue
        values[name.strip()] = kib * 1024
    total = values.get("MemTotal")
    if total is None:
        return {
            "totalBytes": None,
            "availableBytes": None,
            "usedBytes": None,
            "usedRatio": None,
            "availableBasis": None,
            "reason": "/proc/meminfo 里没有 MemTotal（格式意外）",
        }
    available = values.get("MemAvailable")
    basis = "MemAvailable"
    if available is None:
        available = values.get("MemFree", 0)
        basis = "MemFree"
    used = max(0, total - available)
    return {
        "totalBytes": total,
        "availableBytes": available,
        "usedBytes": used,
        "usedRatio": round(used / total, 4) if total else None,
        "availableBasis": basis,
        "reason": None,
    }


def account_overview(
    *,
    user_name: str,
    user_created_at: int,
    is_local: bool,
    project_ids: list[str],
    projects: list[dict[str, Any]],
    db: MetaDB,
    days: int = 30,
    settings: Settings | None = None,
    user_limit_bytes: int | None = None,
    role: str | None = None,
    title: str | None = None,
    early_member_no: int | None = None,
) -> dict[str, Any]:
    """首页账户面板的数据：账户资料 + 跨项目索引统计 + 查询用量 + 存储配额（TASK-094 §B4）。

    ``projects`` 是 ``EngineManager.list_projects()`` 的原始项（含 ``sync`` 与 ``indexProgress``），
    本函数**不改写**它们，只做聚合与转名——保证"页面看到的项目数"与"项目列表页"一致。

    ``storage``（TASK-094）直接复用 ``projects`` 里已经算好的 ``diskBytes``：**不重复遍历目录**。
    ``settings`` 为 ``None`` 时（旧调用方/单测）不输出 ``storage`` 键——宁可少一个展示字段，
    也不编一份用量。
    """
    index = db.all_index_stats(project_ids, limit=10)
    usage = db.usage_summary(project_ids, days=days, recent_limit=10)
    payload: dict[str, Any] = {
        "account": {
            "name": user_name,
            "createdAt": user_created_at,
            "isLocal": is_local,
            "projectCount": len(project_ids),
            # TASK-110 §1.3/§1.4：账户页的头衔与编号（``role`` 也跟出去，前端据此分页）。
            "role": role,
            "title": title,
            "earlyMemberNo": early_member_no,
        },
        "index": index.to_json(),
        "usage": usage.to_json(),
        "projects": projects,
        "days": days,
    }
    if settings is not None:
        payload["storage"] = _storage_overview(
            settings, projects, project_ids, user_limit_bytes=user_limit_bytes
        )
    return payload


def _storage_overview(
    settings: Settings,
    projects: list[dict[str, Any]],
    project_ids: list[str],
    *,
    user_limit_bytes: int | None = None,
) -> dict[str, Any]:
    """存储用量与判定（TASK-094 §B4；与 tool 告警**同一份判定**）。

    ``projectId`` 取项目列表里的第一个：``account_overview`` 是跨项目视图，没有"当前项目"这个
    概念，但 :class:`QuotaStatus` 需要它才能给项目维度的判定。页面只用 ``status`` 与两个维度，
    因此这里选列表首项（**不是**"随便选一个当成用户的项目"——项目维度会因此只代表那一项，
    用户维度才是页面真正显示的"已用/上限"）。项目维度逐项状态由 ``projects[].diskBytes`` 在前端
    行内标注。

    ``user_limit_bytes``（TASK-110）：该用户按角色生效的上限；不传回落 ``Settings`` 全局默认。
    """
    # 局部导入打破 stats ↔ quota 的导入环（quota 顶部 import stats.dir_size_bytes）。
    from nova_service.quota import status_from_sizes

    sizes = {str(item.get("projectId", "")): int(item.get("diskBytes") or 0) for item in projects}
    subject = project_ids[0] if project_ids else ""
    return status_from_sizes(
        sizes, settings, project_id=subject, user_limit=user_limit_bytes
    ).to_json()
