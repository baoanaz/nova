"""同步账本（TASK-031 §B；R35：M2a 落文件，不建第二套 DB）。

路径与结构（冻结，TASK-033 只在其上追加上层行为）::

    {data_root}/projects/{project_id}/sync-state.json
    {
      "version": 1, "branch": null, "commit": null,
      "files": { "src/a.py": { "blobHash": "...", "size": 123, "updatedAt": 1757500000 } },
      "checkpoints": { "<cid>": ["<blobHash>", "..."] }
    }

口径：

- **原子写**（``tmp`` + ``replace``）：进程被杀 / 序列化失败都不会留下半截 JSON；
- **损坏不抛异常**：解析失败或字段非法 → 返回空状态并记 warning（与 core 的
  ``Engine.read_manifest`` 同口径——同步账本是可重建的派生物，读不出来的正确反应是"当作没有"
  并让上层重传，而不是让整个服务 500）；
- ``files`` 的键是仓库相对路径（正斜杠），值是内容寻址的 ``blobHash`` + 大小 + 时间戳；
- ``checkpoints`` 内容寻址（同集合必同 id），保留最近 ``MAX_CHECKPOINTS`` 个（LRU）。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_CHECKPOINTS",
    "SYNC_STATE_FILENAME",
    "STATE_VERSION",
    "FileEntry",
    "SyncState",
]

logger = logging.getLogger("nova_service.sync_state")

SYNC_STATE_FILENAME = "sync-state.json"
STATE_VERSION = 1
#: 保留的 checkpoint 数量（TASK-033 §checkpoint；淘汰不影响已有查询）。
MAX_CHECKPOINTS = 3


@dataclass(frozen=True, slots=True)
class FileEntry:
    """一个已同步文件（账本条目）。"""

    blob_hash: str
    size: int
    updated_at: int

    def to_json(self) -> dict[str, Any]:
        return {"blobHash": self.blob_hash, "size": self.size, "updatedAt": self.updated_at}

    @classmethod
    def from_json(cls, raw: object) -> FileEntry | None:
        """宽容解析：字段缺失/类型不对 → ``None``（该条丢弃，不拖垮整份账本）。"""
        if not isinstance(raw, Mapping):
            return None
        blob_hash = raw.get("blobHash")
        size = raw.get("size")
        updated_at = raw.get("updatedAt")
        if not isinstance(blob_hash, str) or not blob_hash:
            return None
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return None
        if not isinstance(updated_at, int) or isinstance(updated_at, bool):
            updated_at = 0
        return cls(blob_hash=blob_hash, size=size, updated_at=updated_at)


class SyncState:
    """``sync-state.json`` 的内存视图（读写都经 :meth:`load` / :meth:`save`）。"""

    def __init__(
        self,
        project_dir: str | Path,
        *,
        version: int = STATE_VERSION,
        branch: str | None = None,
        commit: str | None = None,
        files: Mapping[str, FileEntry] | None = None,
        checkpoints: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._project_dir = Path(project_dir)
        self.version = version
        self.branch = branch
        self.commit = commit
        self._files: dict[str, FileEntry] = dict(files or {})
        self._pending_files: dict[str, str] = {}
        self._pending_deleted: set[str] = set()
        self._skipped_files: set[str] = set()
        #: 插入顺序即 LRU 顺序（最近使用在末尾）。
        self._checkpoints: dict[str, tuple[str, ...]] = {
            key: tuple(value) for key, value in (checkpoints or {}).items()
        }

    # ------------------------------------------------------------------ 读写

    @property
    def path(self) -> Path:
        return self._project_dir / SYNC_STATE_FILENAME

    @classmethod
    def load(cls, project_dir: str | Path) -> SyncState:
        """读账本；文件不存在或内容损坏 → 空状态（记 warning，不抛异常）。"""
        state = cls(project_dir)
        path = state.path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return state
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "同步账本不可读，按空状态处理（下次同步会重建）：%s (%s)", path, exc
            )
            return state
        if not isinstance(raw, Mapping):
            logger.warning("同步账本结构不正确，按空状态处理：%s", path)
            return state

        version = raw.get("version")
        state.version = version if isinstance(version, int) else STATE_VERSION
        state.branch = raw.get("branch") if isinstance(raw.get("branch"), str) else None
        state.commit = raw.get("commit") if isinstance(raw.get("commit"), str) else None
        files = raw.get("files")
        if isinstance(files, Mapping):
            for key, value in files.items():
                entry = FileEntry.from_json(value)
                if isinstance(key, str) and entry is not None:
                    state._files[key] = entry
        pending = raw.get("pendingFiles", {})
        if isinstance(pending, Mapping):
            state._pending_files = {
                path: kind for path, kind in pending.items()
                if path in state._files and kind in ("added", "modified")
            }
        for field, target in (("pendingDeleted", state._pending_deleted),
                              ("skippedFiles", state._skipped_files)):
            values = raw.get(field, [])
            if isinstance(values, list):
                target.update(value for value in values if isinstance(value, str))
        checkpoints = raw.get("checkpoints")
        if isinstance(checkpoints, Mapping):
            for key, value in checkpoints.items():
                if not isinstance(key, str) or isinstance(value, str):
                    continue
                if isinstance(value, Sequence):
                    state._checkpoints[key] = tuple(str(item) for item in value)
        return state

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "branch": self.branch,
            "commit": self.commit,
            "files": {
                path: entry.to_json() for path, entry in sorted(self._files.items())
            },
            "pendingFiles": dict(sorted(self._pending_files.items())),
            "pendingDeleted": sorted(self._pending_deleted),
            "skippedFiles": sorted(self._skipped_files),
            "checkpoints": {
                key: list(value) for key, value in self._checkpoints.items()
            },
        }

    def save(self) -> None:
        """原子落盘（``tmp`` + ``replace``）；序列化/写入失败不留半截文件。"""
        self._project_dir.mkdir(parents=True, exist_ok=True)
        target = self.path
        temporary = target.with_name(target.name + ".tmp")
        try:
            temporary.write_text(_dumps(self.to_json()), encoding="utf-8")
            temporary.replace(target)
        except BaseException:
            # 失败即清理临时文件：让磁盘上只有"旧的完整版本"或"新的完整版本"。
            temporary.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------ 读

    @property
    def files(self) -> Mapping[str, FileEntry]:
        return dict(self._files)

    @property
    def checkpoints(self) -> Mapping[str, tuple[str, ...]]:
        return dict(self._checkpoints)

    def file_hashes(self) -> dict[str, str]:
        """``path → blobHash``（``BlobSource.list_files`` 的输入）。"""
        return {path: entry.blob_hash for path, entry in self._files.items()}

    def blob_hashes(self) -> tuple[str, ...]:
        """账本引用到的全部 blob hash（稳定排序，去重）。"""
        return tuple(sorted({entry.blob_hash for entry in self._files.values()}))

    def counts(self) -> dict[str, int]:
        return {
            "files": len(self._files),
            "blobs": len(self.blob_hashes()),
            "bytes": sum(entry.size for entry in self._files.values()),
            "checkpoints": len(self._checkpoints),
        }

    def summary(self) -> dict[str, Any]:
        """账本摘要（HTTP ``sync.status`` 与 ``projects/{id}`` 共用）。"""
        counts = self.counts()
        return {
            "branch": self.branch,
            "commit": self.commit,
            "files": counts["files"],
            "blobs": {"count": counts["blobs"], "bytes": counts["bytes"]},
            "checkpoints": counts["checkpoints"],
        }

    # ------------------------------------------------------------------ 写

    def set_head(self, branch: str | None, commit: str | None) -> None:
        """记录同步时的分支/提交（None 不覆盖已有值，便于分批上传只带一次元数据）。"""
        if branch is not None:
            self.branch = branch
        if commit is not None:
            self.commit = commit

    def record_file(self, path: str, blob_hash: str, size: int) -> bool:
        """登记文件（同 path 覆盖）；返回条目是否发生变化（幂等判断用）。"""
        previous = self._files.get(path)
        entry = FileEntry(blob_hash=blob_hash, size=size, updated_at=int(time.time()))
        self._files[path] = entry
        return (
            previous is None
            or previous.blob_hash != entry.blob_hash
            or previous.size != entry.size
        )

    def remove_paths(self, paths: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """按 path 移除条目；返回 ``(deleted, unknown)``（重复删除进 ``unknown``，不报错）。"""
        deleted: list[str] = []
        unknown: list[str] = []
        for path in paths:
            if self._files.pop(path, None) is None:
                unknown.append(path)
            else:
                deleted.append(path)
        return tuple(deleted), tuple(unknown)

    @property
    def pending_files(self) -> Mapping[str, str]:
        return dict(self._pending_files)

    @property
    def pending_deleted(self) -> tuple[str, ...]:
        return tuple(sorted(self._pending_deleted))

    @property
    def pending(self) -> bool:
        return bool(self._pending_files or self._pending_deleted)

    @property
    def skipped_files(self) -> tuple[str, ...]:
        return tuple(sorted(self._skipped_files))

    def queue_file(self, path: str, *, added: bool) -> None:
        # A repeated upload before flush must retain its original added/modified classification.
        self._pending_files.setdefault(path, "added" if added else "modified")
        self._pending_deleted.discard(path)
        self._skipped_files.discard(path)

    def queue_deletions(self, paths: Iterable[str]) -> None:
        for path in paths:
            self._pending_files.pop(path, None)
            self._pending_deleted.add(path)
            self._skipped_files.discard(path)

    def complete_pending(self, skipped: Sequence[str]) -> None:
        """Only acknowledge after the complete ingest succeeded, under the project lock."""
        self._pending_files.clear()
        self._pending_deleted.clear()
        self._skipped_files.update(skipped)

    def record_checkpoint(self, checkpoint_id: str, blob_hashes: Sequence[str]) -> None:
        """登记 checkpoint（内容寻址，天然幂等）；超过上限则淘汰最久未使用的。"""
        self._checkpoints.pop(checkpoint_id, None)
        self._checkpoints[checkpoint_id] = tuple(blob_hashes)  # 重新插入 = 移到 LRU 末尾
        while len(self._checkpoints) > MAX_CHECKPOINTS:
            oldest = next(iter(self._checkpoints))
            self._checkpoints.pop(oldest)


def _dumps(payload: Mapping[str, Any]) -> str:
    """序列化入口（单独抽出：测试用它模拟"写到一半失败"）。

    紧凑格式：首次同步每上传一批就整份重写一次账本，带 ``indent`` 时 CPython 退回纯 Python
    编码器，几千条目每批要多花数十毫秒；紧凑格式走 C 编码器，内容与解析结果不变。
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
