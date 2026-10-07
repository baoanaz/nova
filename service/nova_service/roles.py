"""角色、头衔、配额与能力位（TASK-110 §3.2）——**单一事实源**。

为什么单独成文件（而不是把映射塞进 ``auth.py`` 或 ``config.py``）：本卡有四处需要"这个用户
是什么身份、因此他能做什么"：

1. 注册路径（邀请码类型 → 角色 + 头衔 + 编号，``invites`` / ``routers.auth``）；
2. 鉴权路径（角色 → 是否管理员，``routers.admin`` 的 ``require_admin``）；
3. 配额路径（角色 → 索引空间上限，``quota.py``）；
4. 展示路径（角色 → 头衔 + 能力位，``GET /api/auth/me`` 与前端）。

**头衔与权限必须同源**（卡内 §3.2 冻结）：前端展示的"拓荒者特权"直接读后端返回的
``capabilities``，不在 React 里另写一份角色判断——两处各写一份必然会漂移，而漂移的表现
是"页面上说能自定义 Key、点了却 403"这类最难查的静默失灵。

数据形态（用户 2026-09-15 拍板）：

| 身份 | 角色的值 | 头衔 | 索引空间 |
|---|---|---|---|
| 管理员 | ``admin`` | 执炬者 | 5 GiB |
| 内测玩家 | ``beta`` | 拓荒者 | 1 GiB |
| 公测玩家 | ``public`` | 旅人 | 500 MiB |
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CAN_CUSTOM_KEY",
    "EARLY_MEMBER_MAX",
    "KIND_TO_ROLE",
    "KINDS",
    "QUOTA_BY_ROLE",
    "ROLE_ADMIN",
    "ROLE_BETA",
    "ROLE_PUBLIC",
    "ROLES",
    "TITLE_BY_ROLE",
    "capabilities_for",
    "is_admin",
    "is_valid_role",
    "normalize_role",
    "quota_bytes_for",
    "title_for",
]

#: 三类角色（``users.role`` 的取值域；**旧库的行由迁移补成 ** ``public``）。
ROLE_ADMIN = "admin"
ROLE_BETA = "beta"
ROLE_PUBLIC = "public"

#: 全部合法角色（迁移/后台 PATCH 用它做校验，避免写进一个谁也认不出的值）。
ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_BETA, ROLE_PUBLIC)

#: 角色 → 头衔（**冗余存在 ``users.title``，但权威在这里**；见 :func:`title_for`）。
TITLE_BY_ROLE: dict[str, str] = {
    ROLE_ADMIN: "执炬者",
    ROLE_BETA: "拓荒者",
    ROLE_PUBLIC: "旅人",
}

#: 邀请码类型（**首字母即类型**，用户 2026-09-15 拍板）→ 角色。
KIND_TO_ROLE: dict[str, str] = {
    "A": ROLE_ADMIN,
    "B": ROLE_BETA,
    "C": ROLE_PUBLIC,
}

#: 合法邀请码类型（生成与核销都从这里取，避免两处各写一份 ``"ABC"``）。
KINDS: tuple[str, ...] = tuple(KIND_TO_ROLE)

#: 角色 → 索引空间上限（字节；用户 2026-09-15 指定）。
#:
#: 与 TASK-094 的全局默认（``Settings.storage_limit_per_*``）的分工：**本表是产品配额**，
#: ``Settings`` 那份是"没有角色信息时的兜底"（本地模式、旧库、未登录的隐式账户）。
#: 两者都为 0 才表示"不限"。
QUOTA_BY_ROLE: dict[str, int] = {
    ROLE_ADMIN: 5 * 1024**3,
    ROLE_BETA: 1 * 1024**3,
    ROLE_PUBLIC: 500 * 1024**2,
}

#: 编号只发给**前 N 名内测玩家**（用户要求："前 100 名显示 #001 ~ #100，之后不再发放"）。
EARLY_MEMBER_MAX = 100

#: 可自定义 API Key 的角色（用户："自定义 Key 是拓荒者专属"——管理员同样享有）。
CAN_CUSTOM_KEY: frozenset[str] = frozenset({ROLE_ADMIN, ROLE_BETA})


def normalize_role(value: str | None) -> str:
    """任意输入 → 合法角色（无法识别一律当公测）。

    为什么不抛异常：``users.role`` 的值来自**旧库**、迁移与人工 SQL 都可能是它。
    一个认不出的角色不该让整个请求 500（那会让全部用户登不进来），
    按最小权限回落 ``public`` 才是安全的失败方向。
    """
    candidate = (value or "").strip().lower()
    return candidate if candidate in ROLES else ROLE_PUBLIC


def is_valid_role(value: str | None) -> bool:
    """是否是一个合法角色（后台 PATCH 用它拒绝脏值；与 :func:`normalize_role` 分离）。"""
    return (value or "").strip().lower() in ROLES


def is_admin(role: str | None) -> bool:
    return normalize_role(role) == ROLE_ADMIN


def title_for(role: str | None) -> str:
    """角色的头衔（**展示用**）。

    权威永远是本表的映射而不是 ``users.title`` 列：那一列是冗余快照（便于后台直接 SELECT
    出人看的表），若哪天改了文案，以本函数为准——后台列表读列、``me`` 读本函数，
    两处显示不同时以本函数为重。
    """
    return TITLE_BY_ROLE[normalize_role(role)]


def quota_bytes_for(role: str | None, *, override: int | None = None) -> int:
    """该角色的**默认**索引空间上限（字节）；``override`` 非空时优先（后台给单人改的配额）。

    ``0`` 表示不限（沿用 TASK-094 的口径）。

    **注意**：本函数只回答"这个角色值多少"，不回答"这个用户的实际上限是多少"。
    后者还要考虑"本地模式无账户体系"与 `Settings` 兜底，那个判定在
    ``nova_service.quota.effective_user_limit_bytes``（**唯一口径**）——
    展示与硬拒都必须调它，否则页面上说的额度与实际拦住的那根线会是两个数。
    """
    if override is not None and override >= 0:
        return int(override)
    return QUOTA_BY_ROLE[normalize_role(role)]


def capabilities_for(
    role: str | None,
    *,
    early_member_no: int | None = None,
    quota_bytes: int | None = None,
    is_admin: bool | None = None,
) -> dict[str, Any]:
    """能力位（``GET /api/auth/me`` 的 ``capabilities``；前端据此渲染特权提示）。

    这是卡内 §3.2 那条纪律的落点：**头衔与权限同源**——前端只读这里，
    不自己写 ``role === "beta"``。因此新增一个特权只需改本函数 + 一处后端判定。

    ``quota_bytes`` 由调用方传入**该用户实际生效**的上限
    （``quota.effective_user_limit_bytes``，含后台对单人的覆盖与本地模式的无账户口径）：
    页面上显示的"1.0 GiB"必须与后端硬拒时用的那根线是同一个数。
    缺省（``None``）则退化为角色默认值——所以它是一个**可省略**的参数，
    而不是必须传 0（那会把"没传"变成"额度为 0"，与不限的语义撞车）。
    """
    normalized = normalize_role(role)
    limit = quota_bytes_for(normalized) if quota_bytes is None else int(quota_bytes)
    return {
        "canCustomKey": normalized in CAN_CUSTOM_KEY,
        "quotaBytes": limit,
        # 用户 2026-09-15 拍板：**不设项目数上限**（故本字段不存在，而不是给一个 0 或 null——
        # 前端据此不显示任何项目数限制文案）。
        "earlyMemberNo": early_member_no if normalized == ROLE_BETA else None,
        "isAdmin": normalized == ROLE_ADMIN if is_admin is None else is_admin,
    }
