"""``/healthz``、账户概览、索引历史与查询用量端点。

分工：

| 端点 | 卡 | 语义 |
|---|---|---|
| ``GET /healthz`` | TASK-030/034 | 存活 + 依赖检查 + 项目进度（**免鉴权**） |
| ``GET /api/account/overview`` | TASK-062/064 | 首页仪表盘：账户资料 + 索引成功/失败/平均耗时 |
| ``GET /api/projects/{id}/index-runs`` | TASK-062 | 单项目索引历史 |
| ``GET /api/projects/{id}/index-stats`` | TASK-062 | 单项目索引统计（当前进度 + 落库聚合） |
| ``GET /api/usage/projects/{id}`` | TASK-064 | 单项目查询用量（**替换 501 占位**） |
| ``GET /api/usage/summary`` | TASK-064 | 跨项目用量汇总 |
| ``GET /api/request-log/{requestId}`` | TASK-090 | 按 trace id 查请求日志（**只读**） |
| ``GET /api/calls/{callId}`` | TASK-099 | 一次 Tool 调用的完整时间线（初始化 ×N + 检索）|

口径见 ``docs/design/Module/04-AI总结.md`` §8（审计存档）与 ``docs/tasks/TASK-062`` §C/§D。
**诚实性**：无数据时一律 ``null``/``0``；``citationCoverageAvg`` 在 LLM 接入前恒为 ``null``
（"尚未测量"不是 0）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from nova_service.auth import quota_identity
from nova_service.deps import get_engine_manager, get_settings, require_project_id
from nova_service.errors import ApiError
from nova_service.logging import get_logger, redact_text
from nova_service.metadb import MetaDB
from nova_service.quota import effective_user_limit_bytes
from nova_service.requestlog import MAX_RELATED_LOGS, lookup_all
from nova_service.roles import normalize_role, title_for
from nova_service.stats import account_overview

router = APIRouter(tags=["ops"])

logger = get_logger("nova_service.routers.ops")

#: 历史/用量端点的默认与上限查询条数。
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
#: 用量窗口默认与上限（天）。
DEFAULT_DAYS = 30
MAX_DAYS = 365


@router.get("/healthz")
def healthz(request: Request, deep: int = 0) -> dict[str, Any]:
    """存活 + 依赖检查（``security: []``；``deep=1`` 追加 provider 探测）。"""
    settings = get_settings(request)
    core: dict[str, Any] = {"importable": _core_importable()}
    if deep:
        core.update(_probe_embedding_provider())
    return {
        "status": "ok",
        "version": settings.version,
        "dataRoot": str(settings.data_root),
        "localMode": settings.local_mode,
        # TASK-060：按**真实**鉴权状态报告（此前按 local_mode 硬编码，TASK-051 A1 记为诚实性缺陷）。
        "auth": "disabled(local)" if settings.local_mode else "required",
        "core": core,
        "projects": _project_progress(request),
    }


def _project_progress(request: Request) -> list[dict[str, Any]]:
    """本地模式的项目进度（TASK-034 §D）：**纯内存**，不触碰 core，不加载模型。

    索引进行中仍返回本字段（值就是当前 ``indexProgress``），``/healthz`` 仍 200——
    "服务是活的"与"索引没跑完"是两件事，不能混为一谈（卡内 §D）。
    """
    manager = getattr(request.app.state, "engine_manager", None)
    if manager is None:  # 尚未有请求碰过 core：不为了探活去构造 EngineManager
        return []
    projects: list[dict[str, Any]] = []
    try:
        listed = manager.list_projects()
    except Exception as exc:  # 探活端点自身不因 core 异常而 500（同上口径）
        return [{"error": redact_text(f"{type(exc).__name__}: {exc}")}]
    for item in listed:
        projects.append(
            {
                "projectId": item["projectId"],
                "attachedRoot": item.get("attachedRoot"),
                "indexProgress": item.get("indexProgress"),
            }
        )
    return projects


# --------------------------------------------------------------------------- 账户概览


@router.get("/api/account/overview")
def overview(request: Request, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """首页仪表盘（账户资料 + 索引成功/失败/平均耗时 + 查询用量）。

    身份与项目范围由 ``app.py`` 的鉴权依赖保证：这里只聚合**当前用户**的项目
    （未鉴权时按本地模式的全量项目）。
    """
    manager = get_engine_manager(request)
    db = _meta_db(request)
    user = getattr(request.state, "nova_user", None)
    window = max(1, min(int(days), MAX_DAYS))

    listed = manager.list_projects()
    user_id = getattr(user, "id", None)
    if user_id is not None and db is not None:
        owned = set(db.list_projects(user_id))
        # 未登记归属的项目（本地 attach 后尚未 claim）不计入概览，避免"看到不属于自己的项目"。
        listed = [item for item in listed if str(item["projectId"]) in owned]
    settings = get_settings(request)
    # TASK-110：账户页要显示头衔/编号与"按角色的额度"；上限与上传硬拒同一函数算出。
    user_id, role, override = quota_identity(request)
    return account_overview(
        user_name=getattr(user, "name", "local"),
        user_created_at=getattr(user, "created_at", 0),
        is_local=bool(getattr(user, "is_local", True)),
        project_ids=[str(item["projectId"]) for item in listed],
        projects=listed,
        db=db,
        days=window,
        settings=settings,
        user_limit_bytes=effective_user_limit_bytes(settings, role=role, override=override),
        role=normalize_role(getattr(user, "role", None)) if user_id else None,
        title=title_for(getattr(user, "role", None)) if user_id else None,
        early_member_no=getattr(user, "early_member_no", None),
    )


# --------------------------------------------------------------------------- 索引历史


@router.get("/api/projects/{id}/index-runs")
def project_index_runs(
    id: str, request: Request, limit: int = DEFAULT_LIMIT
) -> list[dict[str, Any]]:
    """单项目索引历史（最近 ``limit`` 条，按结束时间倒序）。"""
    db = _require_meta_db(request)
    # TASK-061 §C：消费 projectId 的端点经 require_project_id（存在 + 归属）。
    project_id = require_project_id(request, id)
    return [run.to_json() for run in db.index_runs(project_id, limit=_limit(limit))]


@router.get("/api/projects/{id}/index-stats")
def project_index_stats(id: str, request: Request, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """单项目索引统计：内存态当前进度 + 落库历史聚合 + 磁盘占用。"""
    manager = get_engine_manager(request)
    project_id = require_project_id(request, id)  # TASK-061 §C
    return manager.index_stats(project_id, limit=_limit(limit))


@router.get("/api/index-stats")
def all_index_stats(request: Request, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """跨项目索引汇总（当前用户的项目）。"""
    manager = get_engine_manager(request)
    db = _require_meta_db(request)
    ids = _visible_project_ids(request, manager)
    stats = db.all_index_stats(ids, limit=_limit(limit)).to_json()
    stats["diskBytes"] = sum(
        manager.index_stats(pid, limit=0)["diskBytes"] for pid in ids
    )
    return stats


# --------------------------------------------------------------------------- 查询用量


@router.get("/api/usage/projects/{id}")
def project_usage(id: str, request: Request, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """单项目查询用量（04 §8 审计存档的读取口；**替换 501 占位**）。"""
    db = _require_meta_db(request)
    project_id = require_project_id(request, id)  # TASK-061 §C：存在 + 归属
    summary = db.usage_summary([project_id], days=_days(days)).to_json()
    summary["projectId"] = project_id
    summary["days"] = _days(days)
    return summary


@router.get("/api/usage/summary")
def usage_summary(request: Request, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """跨项目用量汇总（当前用户的项目）。"""
    manager = get_engine_manager(request)
    db = _require_meta_db(request)
    ids = _visible_project_ids(request, manager)
    summary = db.usage_summary(ids, days=_days(days)).to_json()
    summary["days"] = _days(days)
    return summary


# --------------------------------------------------------------------------- 请求日志（trace 查询）


@router.get("/api/request-log/{requestId}")
def request_log(requestId: str, request: Request) -> dict[str, Any]:
    """按 ``requestId`` 查这次请求的结构化日志（TASK-090 §C；**只读**）。

    鉴权与归属（§C，本卡裁定）：

    - 路径**不在** ``app.PUBLIC_PATHS`` 内 → 云瑞形态必须带凭据（否则 401）：日志是信息泄露面，
      不能公开；
    - **只查自己的**：只有条目里的 ``userId`` 与调用者一致才返回；
    - 查别人的 / 不存在的 / 没有 owner 的（如未认证的 401 请求、路径未匹配到路由）
      一律 **404 ``request_log_not_found`` + 同一条文案**——与 Module/06 §2.2 的"不给探测面"一致
      （403 会泄露"这个 id 存在"）。本地模式（无账户）按 R34 放行。

    返回形状：主条目（``message == "request"`` 那一行，即 :func:`capture_request` 落的）+ 可选
    ``relatedLogs``（同一 requestId 下的其余日志行，新的在前）——**已处理**的 5xx 堆栈在后者里
    （见 :func:`nova_service.requestlog.lookup_all`），不带上它则"503 报错查日志"会看到空堆栈。

    路径参数用 ``{requestId}`` 而非 ``{id}``：本路由**不**消费 projectId，走的是日志归属判定，
    与 ``/api/projects/{id}`` 的 ``require_project_id`` 无关。
    """
    settings = get_settings(request)
    user = getattr(request.state, "nova_user", None)
    user_id = getattr(user, "id", None)
    entries = lookup_all(settings.request_log_path, requestId, user_id=user_id)
    primary = next((entry for entry in entries if entry.message == "request"), None)
    if primary is None:
        raise _request_log_not_found(requestId)
    payload = primary.to_json()
    payload["requestId"] = requestId
    related = [entry.to_json() for entry in entries if entry is not primary]
    payload["relatedLogs"] = related[:MAX_RELATED_LOGS]
    payload["relatedLogCount"] = len(related)
    return payload


def _request_log_not_found(request_id: str) -> ApiError:
    """统一的"查不到"错误（越权 / 不存在 / 无 owner 共用）。

    文案里**不回显** ``request_id``（也不含任何随入参变化的部分）：越权与真不存在的响应因此
    **逐字节一致**，调用方无法据此建立"这个 id 存在"的预言机（Module/06 §2.2）。
    传参只为类型自洽（也确实需要这个入参来构造语义），保留它以便将来需要时不必改签名。
    """
    _ = request_id
    return ApiError(
        code="request_log_not_found",
        message=(
            "请求日志不存在，或不属于当前账户，或已被窗口清理（可用 X-Request-Id 重新请求复现）"
        ),
        status=404,
    )


# --------------------------------------------------------------------------- 调用时间线（§B）


@router.get("/api/calls/{callId}")
def call_timeline(callId: str, request: Request) -> dict[str, Any]:
    """一次 Tool 调用的**完整时间线**（TASK-099 §B-2）：初始化 ×N + 检索，按时间排序。

    解决的真实问题（用户 2026-09-14）：一次 Agent 调用会发 N 个 ``batch-upload``（各落一条
    ``index_runs``）+ 1 个 ``query/search``（落一条 ``query_audit``），而服务端**无从知道**
    它们属于同一次调用。现在客户端在同一次调用里给所有请求发同一个 ``X-Request-Id``，
    服务端把它当 ``index_runs.call_id`` 与 ``query_audit.request_id`` 落库，于是本端点能把
    它们合并成一条时间线（§B-3：**两个列名值天然相等**，不需要映射表）。

    归属（§B 的缺失面补充）：时间线是**跨项目**的（一次调用可能同时初始化多个仓库），
    因此无法用单个 ``require_project_id`` 判定。规则：

    - 本地模式（无账户，R34）→ 放行；
    - 云端 → 该 callId 必须出现在**当前用户可见项目**的 ``index_runs`` 里，否则 404；
    - 两种情况都查不到记录 → 404 ``call_not_found``（与"没有这个 callId"同一文案，不给探测面）。

    ``withoutCallId``：统计**没有** call_id 的索引记录数（旧客户端不发头时这些记录不属任何
    调用）。把它一并返回，是为了让前端能如实说"另有 N 次索引不属于任何调用"，
    而不是让这些记录静默消失（卡内 §B-3 的"无关联就每行独立展示"降级需要这个数）。
    """
    manager = get_engine_manager(request)
    db = _require_meta_db(request)
    user = getattr(request.state, "nova_user", None)
    user_id = getattr(user, "id", None)
    if user_id is not None and callId not in db.visible_call_ids(
        _visible_project_ids(request, manager)
    ):
        raise _call_not_found()

    from nova_service.metadb import CALL_TIMELINE_LIMIT

    runs = db.index_runs_by_call(callId)
    queries = db.queries_by_request_id(callId)
    if not runs and not queries:
        # 本地模式没有归属过滤，必须显式判空（否则任何字符串都能换来一个空 200）。
        raise _call_not_found()
    entries = [
        {"kind": "index", "at": run.started_at, "finishedAt": run.finished_at,
         "indexRun": run.to_json()}
        for run in runs[:CALL_TIMELINE_LIMIT]
    ]
    entries += [
        {"kind": "query", "at": record.created_at, "finishedAt": record.created_at,
         "queryAudit": record.to_json()}
        for record in queries[:CALL_TIMELINE_LIMIT]
    ]
    # 按时间排序（同一秒内索引在前：先建索引才能检索）；
    # index_runs 的 ``at`` 是**秒**、query_audit 的 ``created_at`` 也是秒——粒度一致，可比。
    entries.sort(key=lambda item: (item["at"], 0 if item["kind"] == "index" else 1))
    index_ms = sum(int(run.duration_ms) for run in runs)
    query_ms = sum(int(record.latency_ms) for record in queries)
    return {
        "callId": callId,
        "entries": entries,
        "totals": {
            # 初始化耗时之和 + 检索耗时之和：用户问的就是"初始化用了多久、检索多久"
            # （卡内背景引用的原话）。两个数是**相加关系**，不做平均（一次调用的归总）。
            "indexDurationMs": index_ms,
            "queryLatencyMs": query_ms,
            "totalMs": index_ms + query_ms,
            "indexRuns": len(runs),
            "queries": len(queries),
        },
        "withoutCallId": _count_runs_without_call_id(request, manager),
    }


def _count_runs_without_call_id(request: Request, manager: Any) -> int:
    """当前用户可见项目里**没有** call_id 的索引记录数（旧客户端/启动索引）。

    **旁路**：算不出来就返回 0（不因为一个统计数字让时间线端点失败）。
    """
    try:
        db = _meta_db(request)
        if db is None:
            return 0
        ids = _visible_project_ids(request, manager)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        row = db._connect().execute(
            f"SELECT COUNT(*) AS n FROM index_runs"
            f" WHERE project_id IN ({placeholders}) AND call_id IS NULL",
            list(ids),
        ).fetchone()
        return int(row["n"]) if row is not None else 0
    except Exception:  # noqa: BLE001 - 旁路统计
        return 0


def _call_not_found() -> ApiError:
    """统一的"查不到这次调用"错误（越权 / 不存在 / 旧客户端没带 callId 共用）。

    文案里**不回显** ``callId``：越权与真不存在的响应因此逐字节一致，
    调用方无法据此建立"这个 id 存在"的预言机（与 TASK-090 的 ``_request_log_not_found``
    同一条纪律）。
    """
    return ApiError(
        code="call_not_found",
        message=(
            "这次调用不存在，或不属于当前账户，或历史记录已被清理"
            "（旧版客户端不发 X-Request-Id 时不会产生调用记录）"
        ),
        status=404,
    )


# --------------------------------------------------------------------------- 辅助


def _meta_db(request: Request) -> MetaDB | None:
    db = getattr(request.app.state, "meta_db", None)
    return db if isinstance(db, MetaDB) else None


def _require_meta_db(request: Request) -> MetaDB:
    """元数据库不可用 → 503（而不是返回空列表让人以为"从来没有索引过"）。"""
    db = _meta_db(request)
    if db is None:
        raise ApiError(
            code="meta_db_unavailable",
            message="元数据库未就绪（nova-meta.db）：请通过 create_app 启动服务",
            status=503,
        )
    return db


def _visible_project_ids(request: Request, manager: Any) -> list[str]:
    """当前用户可见的 projectId（未鉴权时退化为全量，本地模式口径）。"""
    all_ids = [str(item["projectId"]) for item in manager.list_projects()]
    db = _meta_db(request)
    user = getattr(request.state, "nova_user", None)
    user_id = getattr(user, "id", None)
    if db is None or user_id is None:
        return all_ids
    owned = set(db.list_projects(user_id))
    return [pid for pid in all_ids if pid in owned]


def _limit(value: int) -> int:
    return max(1, min(int(value), MAX_LIMIT))


def _days(value: int) -> int:
    return max(1, min(int(value), MAX_DAYS))


def _core_importable() -> bool:
    """``nova_core`` 顶层包是否可导入（不触发 embedding 模型加载）。"""
    try:
        import nova_core  # noqa: F401  （只验证可导入性）
    except Exception:  # pragma: no cover - 依赖缺失/损坏时如实报 False
        return False
    return True


def _probe_embedding_provider() -> dict[str, Any]:
    """探测 embedding provider（仅 ``deep=1``）：ok + profile，或 ok=false + reason。"""
    try:
        from nova_core.embedding import EmbeddingConfig, create_provider

        config = EmbeddingConfig.from_env()
        provider = create_provider(config)
        ensure_loaded = getattr(provider, "ensure_loaded", None)
        if callable(ensure_loaded):  # 本地 ONNX：加载模型文件（缺文件在此暴露）
            ensure_loaded()
        else:  # API provider：发一次最小请求
            provider.embed(["nova healthz probe"])
        profile = provider.profile
        return {
            "ok": True,
            "modelId": profile.model_id,
            "dim": profile.dim,
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": redact_text(f"{type(exc).__name__}: {exc}"),
        }
