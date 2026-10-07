"""冻结 hash 语义（D-43 / CF-02）。

三种 hash 用途不同，禁止互相替代：
  blob_hash(path, content)   —— 同步协议与源码镜像（Module/05 ↔ Module/01）
  file_content_hash(content) —— files 表文件级变更检测
  chunk_content_hash(content) —— chunk 级向量复用对账

规范：路径一律 UTF-8、仓库相对、正斜杠；内容文本统一以 \\n 换行（写入前规范化）。
"""

from __future__ import annotations

import hashlib

__all__ = ["blob_hash", "chunk_content_hash", "file_content_hash", "normalize_newlines"]


def normalize_newlines(content: str) -> str:
    """统一换行为 \\n（CRLF/CR → LF），供 hash 与切片共用。"""
    return content.replace("\r\n", "\n").replace("\r", "\n")


def blob_hash(path: str, content: bytes) -> str:
    """sha256(path_bytes || 0x00 || content_bytes)（Module/01 §2.4 / D-43）。"""
    h = hashlib.sha256()
    h.update(path.encode("utf-8"))
    h.update(b"\x00")
    h.update(content)
    return h.hexdigest()


def file_content_hash(content: bytes) -> str:
    """sha256(文件原始内容字节)（Module/01 §2.4 / D-43）。"""
    return hashlib.sha256(content).hexdigest()


def chunk_content_hash(content: str) -> str:
    """sha256(规范化后的完整切片内容 UTF-8 字节)（Module/01 §2.4 / D-43）。"""
    return hashlib.sha256(normalize_newlines(content).encode("utf-8")).hexdigest()
