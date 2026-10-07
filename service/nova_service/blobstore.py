"""blob 内容寻址镜像（TASK-031 §B；R35：文件即存储，不建 DB）。

目录（service 拥有；core 的 ``index.db`` / ``vectors/`` / ``project.json`` 不在此列）::

    {data_root}/projects/{project_id}/blobs/{blob_hash[:2]}/{blob_hash}

- **内容寻址**：文件名就是 ``blob_hash(path, content)``（CF-02，含 path）→ 天然去重、天然幂等；
  同一 hash 重复 ``put`` 返回 ``False`` 且**不覆盖既有字节**（防御性：文件即真相）；
- ``BlobSource`` 实现 core 的 ``SourceProvider`` 协议，把"账本 + 镜像"暴露成引擎可读的源码
  视图（``list_files`` = 账本里的全部 path，``read`` = 按账本 hash 取原始字节）——这是
  ``Engine.ingest(..., source=...)`` 的服务端实现；
- **路径安全**：仓库相对路径一律经 :func:`validate_repo_path`（拒绝绝对路径、反斜杠、``..``、
  空段），与 ``nova_core.pipeline.source.SourcePathError`` 同一口径（直接复用其异常类型）；
- 缺失的 blob 一律 ``FileNotFoundError``（core 会记进 ``report.errors`` 并继续，不中断整次
  ingest）。
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from nova_core.pipeline.source import SourcePathError

from nova_service.sync_state import FileEntry, SyncState

__all__ = ["BLOBS_DIRNAME", "BlobSource", "BlobStore", "validate_repo_path"]

#: blob 镜像所在子目录名。
BLOBS_DIRNAME = "blobs"
#: ``blob_hash`` 形态（sha256 十六进制小写）。
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_repo_path(path: str) -> str:
    """校验仓库相对路径（正斜杠、无空段/``.``/``..``）；非法则抛 ``SourcePathError``。"""
    if not path or path.startswith("/") or "\\" in path:
        raise SourcePathError(f"非法仓库相对路径：{path!r}")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise SourcePathError(f"非法仓库相对路径：{path!r}")
    return path


class BlobStore:
    """``blobs/`` 镜像的读写门面（per project）。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @classmethod
    def open(cls, project_dir: str | Path) -> BlobStore:
        return cls(Path(project_dir) / BLOBS_DIRNAME)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, blob_hash: str) -> Path:
        """内容寻址路径 ``{root}/{hash[:2]}/{hash}``。"""
        if not _HASH_RE.match(blob_hash):
            raise ValueError(f"非法 blob_hash：{blob_hash!r}（期望 64 位小写十六进制 sha256）")
        return self._root / blob_hash[:2] / blob_hash

    def exists(self, blob_hash: str) -> bool:
        return self.path_for(blob_hash).is_file()

    def put(self, path: str, blob_hash: str, data: bytes) -> bool:
        """写入 blob；已存在返回 ``False``（不覆盖既有字节）。``path`` 仅用于路径安全校验。"""
        validate_repo_path(path)
        target = self.path_for(blob_hash)
        if target.is_file():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        # Publish only a complete, fsynced blob; a disconnected/restarted writer
        # must never leave a truncated content-addressed target that retries reuse.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
            for directory in (target.parent, self._root, self._root.parent):
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return True

    def get(self, blob_hash: str) -> bytes:
        """读取 blob 原始字节；缺失抛 ``FileNotFoundError``。"""
        return self.path_for(blob_hash).read_bytes()

    def delete(self, blob_hash: str) -> bool:
        """删除 blob；返回是否真的删掉了（不存在返回 ``False``）。"""
        target = self.path_for(blob_hash)
        if not target.is_file():
            return False
        target.unlink()
        return True

    def usage(self) -> tuple[int, int]:
        """``(blob 数量, 总字节数)``。"""
        if not self._root.is_dir():
            return (0, 0)
        count = 0
        total = 0
        for item in self._root.rglob("*"):
            if item.is_file():
                count += 1
                total += item.stat().st_size
        return (count, total)


class BlobSource:
    """``SourceProvider`` 实现：账本（哪些文件）+ 镜像（文件内容）→ 引擎可读的源码视图。

    传入的 ``state`` 是构造时的快照；调用方每次 ingest 前应重新构造
    （``EngineManager.blob_source`` 每次从磁盘 ``load``，见 TASK-031 §C）。
    """

    def __init__(self, blobs: BlobStore, state: SyncState) -> None:
        self._blobs = blobs
        self._files: Mapping[str, FileEntry] = state.files

    def list_files(self) -> tuple[str, ...]:
        """项目已知全部文件（稳定排序）——``full_reparse`` / ``reembed`` 的枚举依据。"""
        return tuple(sorted(self._files))

    def read(self, path: str) -> bytes:
        """按账本 hash 取原始字节；账本没有或镜像缺失 → ``FileNotFoundError``。"""
        validate_repo_path(path)
        entry = self._files.get(path)
        if entry is None:
            raise FileNotFoundError(path)
        return self._blobs.get(entry.blob_hash)
