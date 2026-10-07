"""``/api/admin/*``：管理员后台五模块（TASK-110 §3.5）。

| 端点 | 模块 | 数据源 |
|---|---|---|
| ``GET /api/admin/users`` | 用户 | ``users`` + 每人的项目数/占用/检索次数/最后活跃 |
| ``PATCH /api/admin/users/{id}`` | 用户 | 封禁/恢复、改配额、改身份 |
| ``GET /api/admin/invites`` | 邀请码 | ``invites`` + ``invite_uses`` |
| ``POST /api/admin/invites`` | 邀请码 | 创建（类型/次数/有效期） |
| ``DELETE /api/admin/invites/{code}`` | 邀请码 | 失效（软删） |
| ``GET /api/admin/projects`` | 项目 | ``index_runs``（失败原因、跳过文件） |
| ``GET /api/admin/stats`` | 调用统计 | ``query_audit`` + ``usage_summary`` |
| ``GET /api/admin/system`` | 系统状态 | 与 ``/healthz?deep=1`` **同源** |

两条纪律（卡内 §3.5）：

1. **统一 ``require_admin``**：非管理员一律 403 ``admin_required``，且**不泄露资源是否存在**
   （不存在与无权限用同一个码；否则可以用 404/403 的差异枚举出系统里有哪些用户/项目）；
   **不碰** ``engine_manager`` 与 ``contextpack``：本模块全部是读聚合 + 少量写。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from nova_service.auth import get_meta_db
from nova_service.deps import get_engine_manager, get_settings
from nova_service.errors import ApiError
from nova_service.invites import CODE_LENGTH, generate_code, is_well_formed, normalize_code
from nova_service.logging import get_logger
from nova_service.metadb import MetaDB, User
from nova_service.quota import format_bytes, project_usage_bytes
from nova_service.roles import (
    KINDS,
    QUOTA_BY_ROLE,
    ROLE_BETA,
    ROLES,
    is_valid_role,
    normalize_role,
    title_for,
)
from nova_service.stats import account_overview

router = APIRouter(tags=["admin"])

logger = get_logger("nova_service.routers.admin")

#: 目标用户不存在时的文案。**与"无权限"用同一个错误码**（不泄露该 id 是否存在）：
#: 后台端点的调用者本来就是管理员，这里防的不是越权而是"用 403/404 的差异枚举系统里的用户 id"。
_USER_NOT_FOUND = "用户不存在或不可操作"

#: 后台列表的默认/上限条数（防御性：不能让一次列表把整个库拉进内存）。
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
#: 邀请码有效期上限（天）。不设 ``expiresInDays`` = 永久（卡内 DDL 的 ``expires_at NULL``）。
MAX_INVITE_DAYS = 3650
#: 单个邀请码可用次数上限（防手滑写个 ``10**9``）。
MAX_INVITE_USES = 10_000


class InviteCreateRequest(BaseModel):
    """``POST /api/admin/invites`` 的请求体。"""

    kind: str = Field(default="C", description="A=管理员 / B=内测 / C=公测")
    maxUses: int = 1
    expiresInDays: int | None = None
    #: 可选：指定码面（6 位大写字母）。不传则随机生成。
    code: str = ""


class UserPatchRequest(BaseModel):
    """``PATCH /api/admin/users/{id}`` 的请求体（**只改给到的字段**）。

    为什么要区分"没传"与"传了 null"：``quotaBytes`` 的 ``null`` 表示"恢复按角色默认"，
    与"不改配额"（省略该字段）是两件事。

    ``model_fields_set`` 就是区分它们的依据（见 :func:`patch_user` 里的 ``"quotaBytes" in
    payload.model_fields_set``）：Pydantic 会把**显式传入的 null** 与**没传**分开——
    只看 `payload.quotaBytes is not None` 会把"恢复默认"静默当成"不改"。
    """

    role: str | None = None
    quotaBytes: int | None = None
    banned: bool | None = None


# --------------------------------------------------------------------------- 依赖


def require_admin(request: Request) -> User:
    """管理员依赖（卡内 §3.5：所有后台端点统一走它）。

    非管理员 → 403 ``admin_required``；**本地模式的隐式账户也不是管理员**——
    本地模式没有账户体系，把它当管理员会让 `nova-service local` 变成一个无门的后台。
    """
    from nova_service.auth import require_user

    user = require_user(request).user
    if normalize_role(user.role) != "admin":
        logger.warning("非管理员访问后台被拒：user=%s path=%s", user.id, request.url.path)
        raise ApiError(
            code="admin_required",
            message="该接口仅管理员可用（当前身份不是管理员）",
            status=403,
        )
    return user


def _db(request: Request) -> MetaDB:
    """后台必须有库（没有账户库就谈不上用户/邀请码/统计）。"""
    db = getattr(request.app.state, "meta_db", None)
    if not isinstance(db, MetaDB):
        raise ApiError(
            code="meta_db_unavailable",
            message="元数据库未就绪（nova-meta.db）：后台数据无处读取",
            status=503,
        )
    return get_meta_db(request)


# --------------------------------------------------------------------------- 用户模块


@router.get("/api/admin/users")
def list_users(request: Request) -> dict[str, Any]:
    """用户列表（含 Project 数 / 索引占用 / 检索次数 / 最后活跃）。

    数据来源（**不新建表**，卡内 §2）：``users`` + 每人的项目归属 + ``query_audit`` 计数 +
    ``index_runs`` 最近状态。占用走 ``quota.project_usage_bytes``（与首页/告警同一口径的目录求和），
    否则后台显示的数字会比用户看到的配额数字大或小，两边都不再可信。
    """
    require_admin(request)
    db = _db(request)
    manager = get_engine_manager(request)
    limit = _limit(request)
    users = db.list_users(limit=limit)
    payload: list[dict[str, Any]] = []
    for user in users:
        owned = db.list_projects(user.id)
        used = sum(project_usage_bytes(manager, project_id) for project_id in owned)
        usage = db.usage_summary(owned, days=3650, recent_limit=0)
        payload.append(
            {
                "userId": user.id,
                "name": user.name,
                "createdAt": user.created_at,
                "isLocal": user.is_local,
                "role": normalize_role(user.role),
                "title": title_for(user.role),
                "earlyMemberNo": user.early_member_no,
                "userNo": user.user_no,
                "quotaBytes": user.quota_bytes,
                "effectiveQuotaBytes": _effective_quota(user),
                "bannedAt": user.banned_at,
                "lastSeenAt": user.last_seen_at,
                "projectCount": len(owned),
                "usedBytes": used,
                "usedText": format_bytes(used),
                "queryCount": usage.total,
            }
        )
    return {"users": payload, "limit": limit}


@router.patch("/api/admin/users/{userId}")
def patch_user(userId: str, payload: UserPatchRequest, request: Request) -> dict[str, Any]:  # noqa: N803
    """改身份 / 配额 / 封禁状态（**只改给到的字段**）。

    - ``banned=true`` 的生效是**立即**的：``auth.authenticate`` 每次校验都看 ``banned_at``，
      同时删掉该用户全部会话（见 ``MetaDB.set_user_banned``）；
    - ``role`` 改到内测且此人还没有编号时**自动发号**（否则会出现\"内测但没有 #编号\"的账户，
      而编号正是内测身份的展示面）；改离内测则清号（编号是内测专属）；
    - 不允许管理员**封禁自己**：一键把自己锁在门外是纯粹的运维事故，且没有第二个管理员能救。
    """
    admin = require_admin(request)
    db = _db(request)
    target = db.get_user(userId)
    if target is None:
        # 与"无权限"同一个码（不泄露该 id 是否存在）。
        raise ApiError(code="admin_required", message=_USER_NOT_FOUND, status=403)
    if payload.banned and target.id == admin.id:
        raise ApiError(
            code="invalid_admin_action",
            message="不能封禁自己：请让另一位管理员操作（单管理员部署下这会把自己锁在门外）",
            status=400,
        )
    if payload.role is not None:
        if not is_valid_role(payload.role):
            raise ApiError(
                code="invalid_role",
                message=f"未知身份：{payload.role}（合法值：{'/'.join(ROLES)}）",
                status=400,
            )
        role = normalize_role(payload.role)
        member_no = None
        if role == ROLE_BETA and target.role != ROLE_BETA and target.early_member_no is None:
            member_no = _next_member_no(db)
        updated = db.set_user_role(target.id, role, early_member_no=member_no)
        if updated is None:  # pragma: no cover - 上面刚取到，仅并发删除才会走到
            raise ApiError(code="admin_required", message=_USER_NOT_FOUND, status=403)
    # 用 ``model_fields_set`` 而不是 ``is not None``：**显式传 null** = 恢复按角色默认，
    # **省略字段** = 不改。只看 `is not None` 会把前者静默当成后者（实测踩到：
    # 管理员传 `{"quotaBytes": null}` 想把单人覆盖清掉，接口 200 但值没变）。
    if "quotaBytes" in payload.model_fields_set:
        if payload.quotaBytes is not None and payload.quotaBytes < 0:
            raise ApiError(
                code="invalid_quota",
                message="quotaBytes 不能为负数（0 表示不限；null 表示恢复按角色默认）",
                status=400,
            )
        db.set_user_quota(target.id, payload.quotaBytes)
    if payload.banned is not None:
        db.set_user_banned(target.id, banned=payload.banned)
    logger.info(
        "后台修改用户：admin=%s target=%s role=%s quota=%s banned=%s",
        admin.id,
        target.id,
        payload.role,
        payload.quotaBytes,
        payload.banned,
    )
    final = db.get_user(target.id)
    if final is None:  # pragma: no cover
        raise ApiError(code="admin_required", message=_USER_NOT_FOUND, status=403)
    return _user_detail(final)


# --------------------------------------------------------------------------- 邀请码模块


@router.get("/api/admin/invites")
def list_invites(request: Request) -> dict[str, Any]:
    """邀请码列表 + 使用记录（后台邀请码模块）。"""
    require_admin(request)
    db = _db(request)
    invites = db.list_invites(limit=_limit(request))
    return {
        "invites": invites,
        "kinds": {kind: title_for(role) for kind, role in _kind_roles().items()},
        "quotaByRole": {role: QUOTA_BY_ROLE[role] for role in ROLES},
    }


@router.post("/api/admin/invites", status_code=201)
def create_invite(payload: InviteCreateRequest, request: Request) -> dict[str, Any]:
    """创建邀请码（指定类型/次数/有效期；码面可自选）。

    - ``kind`` 决定新用户身份：``A``→管理员，``B``→内测（**自动发编号**，前 100 名），``C``→公测；
    - ``maxUses``：1 = 一人一码；> 1 = 分享码（用尽即失效）；
    - ``expiresInDays``：不传 = 永久（卡内 DDL 的 ``expires_at NULL``）；
    - ``code``：不传则随机生成（首字母即类型）；自选时必须是**该类型的形状**
      （首字母必须匹配 kind，否则码面与库里的类型会互相矛盾）。
    """
    admin = require_admin(request)
    db = _db(request)
    kind = (payload.kind or "C").strip().upper()
    if kind not in KINDS:
        raise ApiError(
            code="invalid_invite_kind",
            message=f"未知邀请码类型：{payload.kind}（合法值：{'/'.join(KINDS)}）",
            status=400,
        )
    if payload.maxUses < 1 or payload.maxUses > MAX_INVITE_USES:
        raise ApiError(
            code="invalid_invite_uses",
            message=f"maxUses 必须在 1~{MAX_INVITE_USES} 之间",
            status=400,
        )
    expires_at: int | None = None
    if payload.expiresInDays is not None:
        if payload.expiresInDays < 1 or payload.expiresInDays > MAX_INVITE_DAYS:
            raise ApiError(
                code="invalid_invite_expiry",
                message=f"expiresInDays 必须在 1~{MAX_INVITE_DAYS} 之间（不传 = 永久）",
                status=400,
            )
        import time as _time

        expires_at = int(_time.time()) + int(payload.expiresInDays) * 86400

    code = normalize_code(payload.code) if payload.code else generate_code(kind)
    if not is_well_formed(code):
        raise ApiError(
            code="invalid_invite_code",
            message=f"邀请码必须是 {CODE_LENGTH} 位字母（首字母即类型，如 {kind}7K2M9）",
            status=400,
        )
    if code[0] != kind:
        raise ApiError(
            code="invalid_invite_code",
            message=f"邀请码首字母必须与类型一致：{kind} 类码应以 {kind} 开头（收到 {code}）",
            status=400,
        )
    try:
        created = db.create_invite(
            code, kind, created_by=admin.id, max_uses=payload.maxUses, expires_at=expires_at
        )
    except Exception as exc:  # noqa: BLE001 - 唯一约束：码已存在
        if "UNIQUE" in str(exc).upper():
            raise ApiError(
                code="invite_code_taken", message=f"邀请码已存在：{code}", status=409
            ) from None
        raise
    logger.info(
        "创建邀请码：admin=%s code=%s kind=%s uses=%s",
        admin.id,
        code,
        kind,
        payload.maxUses,
    )
    return created


@router.delete("/api/admin/invites/{code}")
def revoke_invite(code: str, request: Request) -> Response:  # noqa: A002 - 路径参数名与契约对齐
    """失效邀请码（软删：``revoked_at``；使用记录保留供排查）。"""
    require_admin(request)
    db = _db(request)
    normalized = normalize_code(code)
    if not db.revoke_invite(normalized):
        raise ApiError(
            code="invite_not_found", message=f"邀请码不存在或已失效：{normalized}", status=404
        )
    logger.info("失效邀请码：%s", normalized)
    return Response(status_code=204)


# --------------------------------------------------------------------------- 项目模块


@router.get("/api/admin/projects")
def list_projects(
    request: Request,
    userId: str | None = None,  # noqa: N803 - 与前端查询参数名一致
    limit: int = 0,
) -> dict[str, Any]:
    """全部项目 + 索引健康（排查异常索引：失败原因、跳过文件、归属人之名）。

    用户 2026-09-15 要求：

    - ``userId`` 不传 = **全部人员的项目**，传了 = 只看那一个人的；
    - **按占用从大到小排序**（两个视图都是）：先看谁在占空间才有排查价值；
    - 归属人显示**名称**（一串 hex id 对运营来说看不出意思）。

    **跨用户**（管理员视角），因此不套 ``_owned_ids``：这是本端点与
    ``GET /api/projects`` 的唯一区别，也是它必须走 ``require_admin`` 的原因。

    ``limit`` > 0 时才截断（默认 0 = 不限制）：项目数本身不大，但排序前得全部算完，
    截断只能发生在最后一步。
    """
    require_admin(request)
    db = _db(request)
    manager = get_engine_manager(request)
    owners = _project_owners(db)
    payload: list[dict[str, Any]] = []
    for item in manager.list_projects():
        project_id = str(item["projectId"])
        owner_id = owners.get(project_id)
        if userId is not None and owner_id != userId:
            continue
        stats = db.index_stats(project_id, limit=5)
        recent = stats.recent
        latest = recent[0] if recent else None
        owner = db.get_user(owner_id) if owner_id else None
        payload.append(
            {
                "projectId": project_id,
                "displayName": str(item.get("displayName") or ""),
                "attachedRoot": item.get("attachedRoot"),
                "ownerId": owner_id,
                # 归属人的展示名（用户要求"显示名称，一串编号我也不知道什么意思"）。
                # 未认领（归属为空）时如实为 None，前端显示“未认领”而不是编一个名字。
                "ownerName": None if owner is None else owner.name,
                "ownerNo": None if owner is None else owner.user_no,
                "diskBytes": project_usage_bytes(manager, project_id),
                "indexProgress": item.get("indexProgress"),
                "history": {
                    "total": stats.total,
                    "succeeded": stats.succeeded,
                    "failed": stats.failed,
                    "lastState": stats.last_state,
                    "lastRunAt": stats.last_run_at,
                },
                # 失败原因与错误条数：排查异常索引最常看的两项（卡内模块 3）。
                "lastError": None if latest is None else latest.error_text,
                "lastErrors": 0 if latest is None else latest.errors,
                "lastSkipped": (
                    None if latest is None else latest.files_total - latest.files_processed
                ),
            }
        )
    # 占用从大到小；同占用按 projectId 稳定排序（否则每次刷新行会跳）。
    payload.sort(key=lambda row: (-int(row["diskBytes"]), str(row["projectId"])))
    if limit > 0:
        payload = payload[:limit]
    return {
        "projects": payload,
        "totalBytes": sum(int(row["diskBytes"]) for row in payload),
        "owners": _owner_options(db),
    }


@router.delete("/api/admin/projects/{projectId}")
def delete_project(projectId: str, request: Request) -> Response:  # noqa: A002,N803
    """删除任意项目（管理员权限；用户 2026-09-15 要求）。

    与 ``DELETE /api/projects/{id}`` 的区别：那个要过归属校验（只能删自己的），
    这个不过——它是运营释放空间的最后手段（用户要求"删除后，用户存储的索引就没了"）。

    删除是 D-03 的级联删除（整个项目目录 rm -rf，含 ``index.db`` / ``vectors/`` /
    ``blobs/`` / 同步账本），**不动用户的源码**；但项目的**归属行**也要一并清掉，
    否则 ``GET /api/projects`` 会列出项目但目录已不在（会变成幽灵项）。
    """
    admin = require_admin(request)
    db = _db(request)
    manager = get_engine_manager(request)
    if not manager.project_exists(projectId):
        raise ApiError(code="project_not_found", message=f"项目不存在：{projectId}", status=404)
    manager.delete_project(projectId)
    removed = db.delete_project_owner(projectId)
    logger.warning("管理员删除项目：admin=%s project=%s 归属行=%s", admin.id, projectId, removed)
    return Response(status_code=204)


# --------------------------------------------------------------------------- 统计模块


@router.get("/api/admin/stats")
def stats(
    request: Request,
    days: int = 30,
    userId: str | None = None,  # noqa: N803 - 与前端查询参数名一致
) -> dict[str, Any]:
    """Search / Ask 次数、Token、错误率（**与用户侧同源**，不重复计算）。

    口径复用 ``account_overview``（它内部走 ``usage_summary`` + ``all_index_stats``），
    只是把项目范围换成**全量**或**某个用户的项目**。这样后台数字与用户在首页看到的数字
    出自同一个函数——否则两个页面会给出不同的"检索次数"，而谁也不知道该信哪个。

    ``userId``（用户 2026-09-15 要求"用户查询"）：不传 = 全站；
    传了 = 只算那个人的项目（项目列表从 ``projects`` 表反查，不采信任意 id）。
    """
    require_admin(request)
    db = _db(request)
    manager = get_engine_manager(request)
    window = max(1, min(int(days), 3650))
    all_listed = manager.list_projects()
    if userId is None:
        listed = all_listed
    else:
        owned = set(db.list_projects(userId))
        listed = [item for item in all_listed if str(item["projectId"]) in owned]
    project_ids = [str(item["projectId"]) for item in listed]
    overview = account_overview(
        user_name="(all)",
        user_created_at=0,
        is_local=False,
        project_ids=project_ids,
        projects=listed,
        db=db,
        days=window,
        settings=get_settings(request),
    )
    detail = db.usage_summary(project_ids, days=window, recent_limit=0)
    total = detail.total
    return {
        "days": window,
        "userId": userId,
        "projectCount": len(project_ids),
        "search": detail.to_json(),
        "index": overview["index"],
        # 错误率（denominator = 全部调用；无调用时为 **None**——"没有调用"不是"错误率 0%"）。
        "errorRate": None if total == 0 else round(detail.failed / total, 4),
        "totalQueries": total,
        "tokens": db.used_tokens_sum(project_ids, days=window),
        "owners": _owner_options(db),
    }


# --------------------------------------------------------------------------- 系统模块


@router.get("/api/admin/system")
def system(request: Request) -> dict[str, Any]:
    """系统状态：服务形态 + Embedding / LLM 是否可用 + **VPS 内存占用**。

    为什么直接复用 ``healthz`` 的实现而不是各写一份：两处若分开算，后台说"embedding 正常"
    而探活说"不可用"时，运维只能猜哪个是真的。

    TASK-110 追加 ``host``（用户 2026-09-15 要求"加一个 vps 当前内存占用情况"）：
    与 ``healthz`` **同源**的信息在前，主机内存作为额外一节附在后面。
    """
    require_admin(request)
    from nova_service.routers.ops import healthz
    from nova_service.stats import host_memory

    payload = healthz(request, deep=1)
    payload["host"] = host_memory()
    return payload


# --------------------------------------------------------------------------- 辅助


def _limit(request: Request) -> int:
    raw = request.query_params.get("limit")
    try:
        value = int(raw) if raw else DEFAULT_LIMIT
    except ValueError:
        value = DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _effective_quota(user: User) -> int:
    from nova_service.roles import quota_bytes_for

    return quota_bytes_for(user.role, override=user.quota_bytes)


def _next_member_no(db: MetaDB) -> int | None:
    """下一个可用的内测编号；已发满 100 个 → ``None``（用户要求：之后不再发）。"""
    from nova_service.roles import EARLY_MEMBER_MAX

    taken = {
        user.early_member_no
        for user in db.list_users(limit=MAX_LIMIT)
        if user.early_member_no is not None
    }
    for candidate in range(1, EARLY_MEMBER_MAX + 1):
        if candidate not in taken:
            return candidate
    return None


def _project_owners(db: MetaDB) -> dict[str, str]:
    """``projectId → user_id``（后台项目模块显示归属人）。"""
    owners: dict[str, str] = {}
    for user in db.list_users(limit=MAX_LIMIT):
        for project_id in db.list_projects(user.id):
            owners.setdefault(project_id, user.id)
    return owners


def _owner_options(db: MetaDB) -> list[dict[str, Any]]:
    """归属人下拉选项（后台“选择全部人员 / 筛选某个用户”用；用户 2026-09-15 要求）。

    只回 ``{userId, name, userNo}`` 三个字段：下拉框只需要“看得懂的名字”，
    把整份用户行（含项目数、占用）灌进去会让响应体随用户数线性膨胀。
    按 ``user_no`` 升序（= 注册顺序），运营找人时与用户列表的顺序一致。
    """
    users = db.list_users(limit=MAX_LIMIT)
    users.sort(key=lambda item: (item.user_no is None, item.user_no or 0))
    return [
        {"userId": user.id, "name": user.name, "userNo": user.user_no, "role": user.role}
        for user in users
    ]


def _user_detail(user: User) -> dict[str, Any]:
    return {
        "userId": user.id,
        "name": user.name,
        "createdAt": user.created_at,
        "isLocal": user.is_local,
        "role": normalize_role(user.role),
        "title": title_for(user.role),
        "earlyMemberNo": user.early_member_no,
        # 全站顺序号（所有人都有；前端显示为 ID #001）。
        "userNo": user.user_no,
        "quotaBytes": user.quota_bytes,
        "effectiveQuotaBytes": _effective_quota(user),
        "bannedAt": user.banned_at,
        "lastSeenAt": user.last_seen_at,
    }


def _kind_roles() -> dict[str, str]:
    from nova_service.roles import KIND_TO_ROLE

    return dict(KIND_TO_ROLE)
