"""``/api/sync/*`` 同步 API（TASK-033；Module/05 §3.3 的服务端一半，D-27 懒同步）。

四个端点（路径与 CF-05 逐字对齐）：

| 端点 | 语义 |
|---|---|
| ``POST /api/sync/batch-upload`` | 批量上传 blob（每批解码后 ≤1MiB；按 blobHash 幂等）+ 增量索引 |
| ``POST /api/sync/checkpoint`` | 提交 scope blob 集合 → 内容寻址的 ``checkpointId``（保留 3 个） |
| ``POST /api/sync/deletions`` | 通知删除路径（按 (projectId, path) 幂等；级联删索引与镜像） |
| ``GET  /api/sync/status/{projectId}`` | core ``sync_status`` 全字段 + 同步侧追加字段 |

口径（本卡冻结）：

- **``accepted`` 表示“服务端已持久化 blob”，不等于“已索引成功”**（Module/05 §3.6）：索引失败明细在
  ``report.errors``，二进制/不可解码文件在 ``report.skippedFiles``；客户端不得据此判定检索可用；
- **校验顺序固定**（每种错误都能单独复现）：空批 → 逐条 base64 解码（累计超限立即 413）→
  路径安全 → ``blobHash`` 与 CF-02 的 ``blob_hash(path, content)`` 一致（宁可拒绝，
  也不让账本被污染）；
- 默认保留请求内索引；``deferIndexing`` 只持久化上传和待处理项，之后显式 ``flush``。
- flush 复用单项目后台 worker；``pendingJobs`` / ``indexProgress`` 如实报告待处理及运行状态。
- 待处理项持久化在同步账本，失败/服务重启后通过 flush 重试。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Request
from nova_core.hashing import blob_hash
from nova_core.pipeline import IngestReport
from nova_core.pipeline.source import SourcePathError
from nova_core.types import BlobInput
from pydantic import BaseModel

from nova_service.auth import quota_identity
from nova_service.blobstore import validate_repo_path
from nova_service.deps import get_engine_manager, get_settings, require_project_id
from nova_service.errors import ApiError
from nova_service.quota import enforce_upload_limit

router = APIRouter(tags=["sync"])

#: 单批解码后的字节上限（Module/05 §4：≤1MB/批，notace 实测参数）。
MAX_BATCH_BYTES = 1024 * 1024


class BlobPayload(BaseModel):
    """一个待上传的源码 blob（CF-05：``contentB64`` = base64(文件原始字节)）。"""

    path: str
    blobHash: str
    contentB64: str


class BatchUploadRequest(BaseModel):
    """``POST /api/sync/batch-upload`` 的请求体。"""

    projectId: str | None = None
    branch: str | None = None
    commit: str | None = None
    blobs: list[BlobPayload] = []
    deferIndexing: bool = False


class CheckpointRequest(BaseModel):
    """``POST /api/sync/checkpoint`` 的请求体。"""

    projectId: str | None = None
    blobHashes: list[str] = []


class DeletionsRequest(BaseModel):
    """``POST /api/sync/deletions`` 的请求体。"""

    projectId: str | None = None
    paths: list[str] = []
    deferIndexing: bool = False


class FlushRequest(BaseModel):
    projectId: str | None = None


@router.post("/api/sync/batch-upload")
def batch_upload(payload: BatchUploadRequest, request: Request) -> dict[str, Any]:
    """批量上传：blob 镜像 → 账本 → 一次增量 ``ingest``（同 project 串行，见 EngineManager）。

    TASK-110 起，**进门先过配额硬拒**（``quota.enforce_upload_limit``）：超过当前身份
    索引空间上限时 413 返回，不落任何字节——在写入之后才拒会把用户的账本与索引搞成
    半成品（而配额本来就能在上传前算出来）。检索侧仍然只告警（TASK-094 不变）。
    """
    manager = get_engine_manager(request)
    project_id = require_project_id(request, payload.projectId)
    if not payload.blobs:
        raise ApiError("empty_batch", "blobs 不能为空（空批不发请求）", 400)

    decoded = _decode_blobs(payload.blobs)
    with manager.project_lock(project_id):
        require_project_id(request, project_id)
        _enforce_quota(request, manager, project_id, decoded)
        blobs, state = manager.project_paths(project_id)
        known = set(state.files)
        accepted: list[str] = []
        for item in decoded:
            blobs.put(item.path, item.blob_hash, item.content)
            state.record_file(item.path, item.blob_hash, len(item.content))
            state.queue_file(item.path, added=item.path not in known)
            accepted.append(item.blob_hash)
        state.set_head(payload.branch, payload.commit)
        state.save()
        report = None if payload.deferIndexing else manager.flush_sync(project_id)
    return {
        "accepted": accepted,
        "skipped": list(report.skipped_files) if report is not None else [],
        "report": report_json(report) if report is not None else None,
        "indexingDeferred": payload.deferIndexing,
    }


@router.post("/api/sync/flush")
def flush_index(payload: FlushRequest, request: Request) -> dict[str, Any]:
    manager = get_engine_manager(request)
    project_id = require_project_id(request, payload.projectId)
    return {"indexProgress": manager.start_sync(project_id).to_json()}


@router.post("/api/sync/checkpoint")
def create_checkpoint(payload: CheckpointRequest, request: Request) -> dict[str, str]:
    """内容寻址的 checkpoint（同集合必同 id；账本保留最近 ``MAX_CHECKPOINTS`` 个）。"""
    manager = get_engine_manager(request)
    project_id = require_project_id(request, payload.projectId)
    checkpoint_id = checkpoint_id_for(payload.blobHashes)
    with manager.project_lock(project_id):
        require_project_id(request, project_id)
        state = manager.sync_state(project_id)
        state.record_checkpoint(checkpoint_id, sorted(set(payload.blobHashes)))
        state.save()
    return {"checkpointId": checkpoint_id}


@router.post("/api/sync/deletions")
def report_deletions(payload: DeletionsRequest, request: Request) -> dict[str, Any]:
    """删除通知（固定顺序）：账本移除 → 无引用的 blob 镜像删除 → 一次 ``ingest(deleted=...)``。

    幂等：重复删除的路径进 ``unknown`` 且不报错（Module/05 §9-3 口径）；账本里没有的路径
    不做任何索引动作（``deleted`` 为空时不触发 ingest）。
    """
    manager = get_engine_manager(request)
    project_id = require_project_id(request, payload.projectId)
    with manager.project_lock(project_id):
        require_project_id(request, project_id)
        blobs, state = manager.project_paths(project_id)
        before = state.files
        deleted, unknown = state.remove_paths(payload.paths)
        state.queue_deletions(deleted)
        # Save the durable deletion before removing its now-unreferenced content.
        state.save()
        still_referenced = set(state.blob_hashes())
        for path in deleted:
            digest = before[path].blob_hash
            if digest not in still_referenced:
                blobs.delete(digest)
        if not payload.deferIndexing and state.pending:
            manager.flush_sync(project_id)
    result: dict[str, Any] = {"deleted": list(deleted), "unknown": list(unknown)}
    if payload.deferIndexing:
        result["indexingDeferred"] = True
    return result


@router.get("/api/sync/status/{projectId}")
def sync_status(  # noqa: N803 - CF-05 路径参数名
    projectId: str, request: Request, progressOnly: bool = False
) -> dict[str, Any]:
    """同步/索引状态（core 全字段 + 同步侧追加字段）。"""
    manager = get_engine_manager(request)
    # TASK-061 §C：经 require_project_id（存在 + 归属）；越权与不存在同为 404。
    project_id = require_project_id(request, projectId)
    return manager.sync_status(project_id, progress_only=progressOnly)


# --------------------------------------------------------------------------- 校验与工具


def checkpoint_id_for(blob_hashes: Sequence[str]) -> str:
    """``"cp_" + sha256("\\n".join(sorted(set(hashes))))[:16]``（内容寻址 → 天然幂等）。"""
    material = "\n".join(sorted(set(blob_hashes)))
    return "cp_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _decode_blobs(items: Sequence[BlobPayload]) -> list[BlobInput]:
    """逐条解码 + 校验（顺序固定：base64 → 累计批大小 → 路径安全 → hash 一致）。"""
    decoded: list[BlobInput] = []
    total = 0
    for item in items:
        try:
            content = base64.b64decode(item.contentB64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ApiError(
                "invalid_content_encoding",
                f"blob {item.path!r} 的 contentB64 不是合法 base64：{exc}",
                400,
            ) from None
        total += len(content)
        if total > MAX_BATCH_BYTES:
            raise ApiError(
                "batch_too_large",
                f"单批解码后总字节超过上限 {MAX_BATCH_BYTES}（客户端需分批上传）",
                413,
            )
        try:
            validate_repo_path(item.path)
        except SourcePathError as exc:
            raise ApiError("invalid_path", str(exc), 400) from None
        expected = blob_hash(item.path, content)
        if expected != item.blobHash:
            raise ApiError(
                "blob_hash_mismatch",
                f"blob {item.path!r} 的 blobHash 与 CF-02 的 blob_hash(path, content) 不一致"
                "（拒绝写入，避免污染账本）",
                400,
            )
        decoded.append(BlobInput(path=item.path, content=content, blob_hash=expected))
    return decoded


def _enforce_quota(
    request: Request,
    manager: Any,
    project_id: str,
    decoded: Sequence[BlobInput],
) -> None:
    """上传前配额硬拒（TASK-110；用户 2026-09-15 拍板"超限拒绝新索引"）。

    ``incoming_bytes`` 用**解码后的真实字节数**而不是 ``contentB64`` 的长度：base64 会把体量
    放大 4/3，拿它当额度会把用户的真实占用算高 33%（属于"算了但算错"）。
    """
    user_id, role, override = quota_identity(request)
    enforce_upload_limit(
        manager,
        get_settings(request),
        project_id=project_id,
        db=getattr(request.app.state, "meta_db", None),
        user_id=user_id,
        role=role,
        override=override,
        incoming_bytes=sum(len(item.content) for item in decoded),
    )


def report_json(report: IngestReport) -> dict[str, Any]:
    """``IngestReport`` → 同步 API 的报告字段（client（TASK-040）依赖的形态）。"""
    return {
        "added": report.added,
        "modified": report.modified,
        "deleted": report.deleted,
        "chunksNew": report.chunks_new,
        "chunksReused": report.chunks_reused,
        "chunksRemoved": report.chunks_removed,
        "filesParsed": report.files_parsed,
        "errors": list(report.errors),
        "skippedFiles": list(report.skipped_files),
    }


__all__ = [
    "MAX_BATCH_BYTES",
    "BatchUploadRequest",
    "CheckpointRequest",
    "DeletionsRequest",
    "batch_upload",
    "checkpoint_id_for",
    "create_checkpoint",
    "report_deletions",
    "report_json",
    "sync_status",
]
