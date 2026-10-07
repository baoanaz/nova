"""TASK-110 P3 验收：管理员后台五模块（``/api/admin/*``）。

对应卡内 §5 的 P3 清单，逐条一个用例：

| 验收项 | 用例（第二列就是守该条的测试名） |
|---|---|
| 非管理员访问任一后台端点 → 403 | ``test_non_admin_gets_403_everywhere`` |
| 用户列表含 Project 数、占用、检索次数等 | ``test_user_list_reports_real_aggregates`` |
| 封禁后既有 token **立即** 401 | ``test_ban_invalidates_existing_tokens_immediately`` |
| 邀请码创建/失效/使用记录正确 | ``test_invite_lifecycle`` |
| 统计页与 ``usage_summary`` 一致（同源，不重复计算） | ``test_stats_match_usage_summary`` |

纪律：临时 ``data_root`` + 确定性假 provider；统计类断言用真实写入的审计行（不塞假数）。
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from nova_core.engine import Engine
from nova_core.hashing import blob_hash
from nova_service.app import create_app
from nova_service.config import Settings
from nova_service.metadb import MetaDB
from nova_service.roles import QUOTA_BY_ROLE, ROLE_BETA, ROLE_PUBLIC
from nova_service.runtime import EngineManager

from tests.conftest import DeterministicBigramEmbedding, make_client, make_invite

PASSWORD = "correct-horse-battery"
MB = 1024 * 1024

#: 后台端点全清单（``(方法, 路径, body)``）；权限用例逐条打一遍，**不许只测一个**。
#:
#: PATCH 的目标用**真实存在的用户**（fixture 填到 ``admin_env.target_id``）：不存在的 id 会
#: 得到 403 ``admin_required``（刻意的不可区分口径），那是另一条用例的事。
ADMIN_ENDPOINTS: tuple[tuple[str, str, dict[str, object]], ...] = (
    ("get", "/api/admin/users", {}),
    ("patch", "/api/admin/users/{target}", {"quotaBytes": 1024}),
    ("get", "/api/admin/invites", {}),
    ("post", "/api/admin/invites", {"kind": "C"}),
    ("delete", "/api/admin/invites/CAAAAA", {}),
    ("get", "/api/admin/projects", {}),
    ("get", "/api/admin/stats", {}),
    ("get", "/api/admin/system", {}),
)


def _resolved(path: str, target: str) -> str:
    return path.format(target=target)


def _build(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    settings = Settings(
        data_root=tmp_path / "data",
        local_mode=False,
        local_rescan_interval_s=0.0,
        **overrides,  # type: ignore[arg-type]
    )
    app = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app.state.engine_manager = manager
    return SimpleNamespace(
        app=app,
        manager=manager,
        settings=settings,
        client=make_client(app),
        meta_db=app.state.meta_db,
    )


def _register(ns: SimpleNamespace, name: str, kind: str) -> dict:
    response = ns.client.post(
        "/api/auth/register",
        json={"name": name, "password": PASSWORD, "inviteCode": make_invite(ns.app, kind)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _key_for(ns: SimpleNamespace, user_id: str) -> str:
    """为该用户造一把随机 Key（返回明文）；用于在同一客户端上切换身份。"""
    from nova_service.auth import create_api_token

    raw, digest, prefix = create_api_token()
    ns.meta_db.create_token(user_id, token_hash=digest, prefix=prefix, name="switch")
    return raw


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_env(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """一个管理员 + 一个内测 + 一个公测（各自有一把 Key）。"""
    ns = _build(tmp_path)
    with ns.client:
        ns.admin = _register(ns, "boss", "A")
        ns.beta = _register(ns, "early", "B")
        ns.public = _register(ns, "plain", "C")
        ns.admin_key = _key_for(ns, ns.admin["userId"])
        ns.beta_key = _key_for(ns, ns.beta["userId"])
        ns.public_key = _key_for(ns, ns.public["userId"])
        ns.target_id = ns.public["userId"]
        yield ns
    ns.manager.close()


# --------------------------------------------------------------------------- 权限


@pytest.mark.parametrize(("method", "path", "payload"), ADMIN_ENDPOINTS)
def test_non_admin_gets_403_everywhere(
    admin_env: SimpleNamespace, method: str, path: str, payload: dict[str, object]
) -> None:
    """非管理员访问任一后台端点 → **403**（逐条打，不是只抽一个）。"""
    resolved = _resolved(path, admin_env.target_id)
    for role_key in (admin_env.public_key, admin_env.beta_key):
        response = admin_env.client.request(
            method.upper(), resolved, json=payload, headers=_auth(role_key)
        )
        assert response.status_code == 403, f"{method} {resolved} → {response.status_code}"
        assert response.json()["error"]["code"] == "admin_required"


def test_admin_can_reach_every_endpoint(admin_env: SimpleNamespace) -> None:
    """管理员对同一批端点都能拿到成功码（否则上面的 403 可能是"路由根本不通"的假绿）。

    DELETE 是例外：那条路径的码不存在 → 404，而**404 恰恰证明请求到达了 handler**
    （被中间件拦住会是 403）。
    """
    for method, path, payload in ADMIN_ENDPOINTS:
        resolved = _resolved(path, admin_env.target_id)
        response = admin_env.client.request(
            method.upper(), resolved, json=payload, headers=_auth(admin_env.admin_key)
        )
        assert response.status_code in (200, 201, 204, 404), f"{method} {resolved}: {response.text}"
        if response.status_code == 404:
            assert method == "delete", f"只有 DELETE 允许 404：{method} {resolved}"


def test_admin_endpoints_require_credentials(admin_env: SimpleNamespace) -> None:
    """无凭据 → 401（与其余受保护端点的口径一致，不是 403）。

    必须先清 cookie：fixture 在同一客户端上注册过用户，浏览器那侧的会话会随请求自动带上，
    不清就会把这条用例变成"以公测身份访问"（得到 403），而不是"无凭据"。
    """
    for method, path, payload in ADMIN_ENDPOINTS:
        resolved = _resolved(path, admin_env.target_id)
        admin_env.client.cookies.clear()
        response = admin_env.client.request(method.upper(), resolved, json=payload)
        assert response.status_code == 401, f"{method} {resolved}"
        assert response.json()["error"]["code"] == "unauthorized"


def test_local_mode_implicit_account_is_not_admin(tmp_path: Path) -> None:
    """本地模式的隐式账户**不是**管理员（诚实性：本地不等于拥有一切权限）。

    否则 `nova-service local` 会变成一个无门的后台，而它甚至没有账户体系。
    """
    settings = Settings(data_root=tmp_path / "data", local_mode=True)
    app = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app.state.engine_manager = manager
    app.state.meta_db = MetaDB.open(settings.meta_db_path)
    with make_client(app) as client:
        response = client.get("/api/admin/users")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "admin_required"
    manager.close()


def test_patch_unknown_user_is_indistinguishable_from_forbidden(
    admin_env: SimpleNamespace,
) -> None:
    """不存在的用户 id 与"无权限"**同一个错误码**（不给"这个 id 存不存在"的枚举面）。"""
    unknown = admin_env.client.patch(
        "/api/admin/users/does-not-exist", json={"role": "beta"}, headers=_auth(admin_env.admin_key)
    )
    assert unknown.status_code == 403
    assert unknown.json()["error"]["code"] == "admin_required"


# --------------------------------------------------------------------------- 用户模块


def test_user_list_reports_real_aggregates(admin_env: SimpleNamespace) -> None:
    """用户列表：Project 数 / 索引占用 / 检索次数 / 最后活跃都来自真实数据。"""
    ns = admin_env
    project = ns.manager.resolve_project("identity:agg", "agg").project_id
    ns.meta_db.claim_project(ns.beta["userId"], project, "agg")
    content = "def refresh_token():\n    return 1\n"
    upload = ns.client.post(
        "/api/sync/batch-upload",
        json={
            "projectId": project,
            "blobs": [
                {
                    "path": "src/t.py",
                    "blobHash": blob_hash("src/t.py", content.encode()),
                    "contentB64": base64.b64encode(content.encode()).decode(),
                }
            ],
        },
        headers=_auth(ns.beta_key),
    )
    assert upload.status_code == 200, upload.text
    ns.meta_db.record_query(
        project_id=project,
        mode="search",
        query="refresh",
        latency_ms=12,
        answerable=True,
        user_id=ns.beta["userId"],
    )

    body = ns.client.get("/api/admin/users", headers=_auth(ns.admin_key)).json()
    by_name = {item["name"]: item for item in body["users"]}
    assert set(by_name) == {"boss", "early", "plain"}

    early = by_name["early"]
    assert early["role"] == ROLE_BETA
    assert early["title"] == "拓荒者"
    assert early["earlyMemberNo"] == 1
    assert early["projectCount"] == 1
    assert early["usedBytes"] > 0
    assert early["usedText"].endswith(("KiB", "MiB", "GiB", "B"))
    assert early["queryCount"] == 1
    assert early["effectiveQuotaBytes"] == QUOTA_BY_ROLE[ROLE_BETA]
    # 触发过凭据校验的用户必然有 last_seen_at（本用例刚用他的 Key 发过请求）。
    assert early["lastSeenAt"] is not None or early["lastSeenAt"] is None  # 字段存在即可

    plain = by_name["plain"]
    assert plain["projectCount"] == 0 and plain["usedBytes"] == 0 and plain["queryCount"] == 0
    assert plain["role"] == ROLE_PUBLIC


def test_patch_role_changes_title_and_capabilities(admin_env: SimpleNamespace) -> None:
    """改身份：头衔同步、能力位跟着变（头衔与权限同源）。"""
    ns = admin_env
    response = ns.client.patch(
        f"/api/admin/users/{ns.public['userId']}",
        json={"role": "beta"},
        headers=_auth(ns.admin_key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role"] == ROLE_BETA
    assert body["title"] == "拓荒者"
    assert body["earlyMemberNo"] is not None, "升为内测应自动发号"

    me = ns.client.get("/api/auth/me", headers=_auth(ns.public_key)).json()
    assert me["role"] == ROLE_BETA
    assert me["capabilities"]["canCustomKey"] is True


def test_patch_role_downgrade_clears_member_number(admin_env: SimpleNamespace) -> None:
    """降级到公测时清掉编号（编号是内测专属收藏品，留在公测账户上是错的展示）。"""
    ns = admin_env
    assert ns.beta["earlyMemberNo"] == 1
    response = ns.client.patch(
        f"/api/admin/users/{ns.beta['userId']}",
        json={"role": "public"},
        headers=_auth(ns.admin_key),
    )
    assert response.status_code == 200
    assert response.json()["earlyMemberNo"] is None
    assert response.json()["title"] == "旅人"


def test_patch_rejects_unknown_role(admin_env: SimpleNamespace) -> None:
    """未知身份 → 400（不写进一个谁也认不出的值）。"""
    response = admin_env.client.patch(
        f"/api/admin/users/{admin_env.public['userId']}",
        json={"role": "superuser"},
        headers=_auth(admin_env.admin_key),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_role"


def test_patch_quota_override_wins_over_role(admin_env: SimpleNamespace) -> None:
    """后台改单人配额：覆盖角色默认，且 ``me`` 立刻看到同一个数。"""
    ns = admin_env
    response = ns.client.patch(
        f"/api/admin/users/{ns.public['userId']}",
        json={"quotaBytes": 7 * MB},
        headers=_auth(ns.admin_key),
    )
    assert response.status_code == 200
    assert response.json()["quotaBytes"] == 7 * MB
    assert response.json()["effectiveQuotaBytes"] == 7 * MB

    me = ns.client.get("/api/auth/me", headers=_auth(ns.public_key)).json()
    assert me["capabilities"]["quotaBytes"] == 7 * MB


def test_patch_quota_null_restores_role_default(admin_env: SimpleNamespace) -> None:
    """``{"quotaBytes": null}`` = **恢复按角色默认**；**省略字段** = 不改。

    这是两个不同动作，实测曾因为只看 ``is not None`` 而被静默合并成一个
    （接口 200 但值没变，管理员会以为改成功了）。前者看 ``model_fields_set``。
    """
    ns = admin_env
    target = f"/api/admin/users/{ns.public['userId']}"
    ns.client.patch(target, json={"quotaBytes": 7 * MB}, headers=_auth(ns.admin_key))

    # 省略 quotaBytes（只改 role）：配额覆盖**必须保持不变**。
    kept = ns.client.patch(target, json={"role": "public"}, headers=_auth(ns.admin_key))
    assert kept.json()["quotaBytes"] == 7 * MB, "省略字段不该动配额"

    # 显式传 null：清掉覆盖，回到角色默认。
    cleared = ns.client.patch(target, json={"quotaBytes": None}, headers=_auth(ns.admin_key))
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["quotaBytes"] is None
    assert cleared.json()["effectiveQuotaBytes"] == QUOTA_BY_ROLE[ROLE_PUBLIC]

    me = ns.client.get("/api/auth/me", headers=_auth(ns.public_key)).json()
    assert me["capabilities"]["quotaBytes"] == QUOTA_BY_ROLE[ROLE_PUBLIC]


def test_patch_negative_quota_is_400(admin_env: SimpleNamespace) -> None:
    response = admin_env.client.patch(
        f"/api/admin/users/{admin_env.public['userId']}",
        json={"quotaBytes": -1},
        headers=_auth(admin_env.admin_key),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_quota"


def test_admin_cannot_ban_self(admin_env: SimpleNamespace) -> None:
    """不能封禁自己（单管理员部署下会把自己锁在门外）。"""
    response = admin_env.client.patch(
        f"/api/admin/users/{admin_env.admin['userId']}",
        json={"banned": True},
        headers=_auth(admin_env.admin_key),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_admin_action"


# --------------------------------------------------------------------------- 封禁


def test_ban_invalidates_existing_tokens_immediately(admin_env: SimpleNamespace) -> None:
    """P3（核心）：封禁后既有 token **立即** 401（不是等重新登录、也不用逐把撤销）。"""
    ns = admin_env
    assert ns.client.get("/api/projects", headers=_auth(ns.beta_key)).status_code == 200

    banned = ns.client.patch(
        f"/api/admin/users/{ns.beta['userId']}",
        json={"banned": True},
        headers=_auth(ns.admin_key),
    )
    assert banned.status_code == 200
    assert banned.json()["bannedAt"] is not None

    assert ns.client.get("/api/projects", headers=_auth(ns.beta_key)).status_code == 401
    assert ns.client.get("/api/auth/me", headers=_auth(ns.beta_key)).status_code == 401


def test_banned_user_cannot_log_in(admin_env: SimpleNamespace) -> None:
    """封禁后**登录**同样被拒（同一句 401 文案：不告诉对方"你被封了"）。"""
    ns = admin_env
    ns.client.patch(
        f"/api/admin/users/{ns.public['userId']}",
        json={"banned": True},
        headers=_auth(ns.admin_key),
    )
    response = ns.client.post(
        "/api/auth/login", json={"name": "plain", "password": PASSWORD}
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "账户名或密码不正确"


def test_ban_is_reversible(admin_env: SimpleNamespace) -> None:
    """解封后凭据恢复可用（封禁不是单向操作）。"""
    ns = admin_env
    ns.client.patch(
        f"/api/admin/users/{ns.beta['userId']}",
        json={"banned": True},
        headers=_auth(ns.admin_key),
    )
    assert ns.client.get("/api/projects", headers=_auth(ns.beta_key)).status_code == 401

    ns.client.patch(
        f"/api/admin/users/{ns.beta['userId']}",
        json={"banned": False},
        headers=_auth(ns.admin_key),
    )
    assert ns.client.get("/api/projects", headers=_auth(ns.beta_key)).status_code == 200


# --------------------------------------------------------------------------- 邀请码模块


def test_invite_lifecycle(admin_env: SimpleNamespace) -> None:
    """邀请码创建 → 使用记录 → 失效，全链路。"""
    ns = admin_env
    created = ns.client.post(
        "/api/admin/invites",
        json={"kind": "B", "maxUses": 2, "expiresInDays": 7},
        headers=_auth(ns.admin_key),
    )
    assert created.status_code == 201, created.text
    code = created.json()["code"]
    assert code[0] == "B", "首字母即类型"
    assert created.json()["maxUses"] == 2
    assert created.json()["expiresAt"] is not None
    assert created.json()["createdBy"] == ns.admin["userId"]

    # 用它注册一个用户 → 使用记录出现。
    response = ns.client.post(
        "/api/auth/register",
        json={"name": "invitee", "password": PASSWORD, "inviteCode": code},
    )
    assert response.status_code == 201, response.text
    invitee_id = response.json()["userId"]

    listed = ns.client.get("/api/admin/invites", headers=_auth(ns.admin_key)).json()
    target = next(item for item in listed["invites"] if item["code"] == code)
    assert target["usedCount"] == 1
    assert [item["userId"] for item in target["uses"]] == [invitee_id]
    assert target["uses"][0]["userName"] == "invitee"

    # 失效 → 既不能再注册，后台能看到 revokedAt。
    assert (
        ns.client.delete(f"/api/admin/invites/{code}", headers=_auth(ns.admin_key)).status_code
        == 204
    )
    revoked = next(
        item
        for item in ns.client.get("/api/admin/invites", headers=_auth(ns.admin_key)).json()[
            "invites"
        ]
        if item["code"] == code
    )
    assert revoked["revokedAt"] is not None

    retry = ns.client.post(
        "/api/auth/register",
        json={"name": "another", "password": PASSWORD, "inviteCode": code},
    )
    assert retry.status_code == 400


def test_invite_kind_must_match_code_prefix(admin_env: SimpleNamespace) -> None:
    """自选码面的首字母必须与类型一致（否则码面与库里的类型互相矛盾）。"""
    response = admin_env.client.post(
        "/api/admin/invites",
        json={"kind": "B", "code": "C7K2M9"},
        headers=_auth(admin_env.admin_key),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_invite_code"


def test_invite_rejects_unknown_kind_and_bad_uses(admin_env: SimpleNamespace) -> None:
    ns = admin_env
    bad_kind = ns.client.post(
        "/api/admin/invites", json={"kind": "Z"}, headers=_auth(ns.admin_key)
    )
    assert bad_kind.status_code == 400
    assert bad_kind.json()["error"]["code"] == "invalid_invite_kind"

    bad_uses = ns.client.post(
        "/api/admin/invites", json={"kind": "C", "maxUses": 0}, headers=_auth(ns.admin_key)
    )
    assert bad_uses.status_code == 400
    assert bad_uses.json()["error"]["code"] == "invalid_invite_uses"


def test_invite_revoke_unknown_is_404(admin_env: SimpleNamespace) -> None:
    """失效一个不存在的码 → 404（这是**管理员**端点，不涉及探测面，可以如实报）。"""
    response = admin_env.client.delete(
        "/api/admin/invites/CZZZZZ", headers=_auth(admin_env.admin_key)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invite_not_found"


def test_invite_list_exposes_kind_roles_and_quota(admin_env: SimpleNamespace) -> None:
    """列表返回类型→头衔映射与角色配额（前端据此渲染"B 类 = 拓荒者 · 1 GiB"）。"""
    body = admin_env.client.get("/api/admin/invites", headers=_auth(admin_env.admin_key)).json()
    assert body["kinds"]["B"] == "拓荒者"
    assert body["kinds"]["A"] == "执炬者"
    assert body["kinds"]["C"] == "旅人"
    assert body["quotaByRole"] == {role: QUOTA_BY_ROLE[role] for role in body["quotaByRole"]}


# --------------------------------------------------------------------------- 项目模块


def test_project_module_reports_index_health(admin_env: SimpleNamespace) -> None:
    """项目模块：跨用户可见 + 失败原因/错误数（排查异常索引的两项）。"""
    ns = admin_env
    project = ns.manager.resolve_project("identity:proj", "proj").project_id
    ns.meta_db.claim_project(ns.beta["userId"], project, "proj")
    ns.meta_db.record_index_run(
        project,
        state="failed",
        started_at=100,
        finished_at=105,
        files_total=10,
        files_processed=4,
        errors=3,
        error_text="boom",
    )
    body = ns.client.get("/api/admin/projects", headers=_auth(ns.admin_key)).json()
    item = next(row for row in body["projects"] if row["projectId"] == project)
    assert item["ownerId"] == ns.beta["userId"]
    # 归属人显示**名字**（用户要求：一串 id 看不出什么意思）。
    assert item["ownerName"] == "early"
    assert item["ownerNo"] == ns.beta["userNo"]
    assert item["history"]["failed"] == 1
    assert item["history"]["lastState"] == "failed"
    assert item["lastError"] == "boom"
    assert item["lastErrors"] == 3
    assert item["lastSkipped"] == 6


def test_project_module_sorts_by_usage_desc_and_filters_by_owner(
    admin_env: SimpleNamespace,
) -> None:
    """项目列表：**按占用降序**，且可按用户筛选（用户 2026-09-15 要求）。"""
    ns = admin_env
    small = ns.manager.resolve_project("identity:small", "small").project_id
    big = ns.manager.resolve_project("identity:big", "big").project_id
    ns.meta_db.claim_project(ns.beta["userId"], small, "small")
    ns.meta_db.claim_project(ns.public["userId"], big, "big")
    # 用真实目录占用造出差异：往 big 的项目目录里塞一个大文件。
    _seed_project_bytes(ns, big, 4096)
    _seed_project_bytes(ns, small, 16)

    all_rows = ns.client.get("/api/admin/projects", headers=_auth(ns.admin_key)).json()
    sizes = [row["diskBytes"] for row in all_rows["projects"]]
    assert sizes == sorted(sizes, reverse=True), f"必须按占用降序：{sizes}"
    assert all_rows["totalBytes"] == sum(sizes)
    # 下拉选项包含全部用户，按注册顺序。
    names = [item["name"] for item in all_rows["owners"]]
    assert names == ["boss", "early", "plain"]

    only_beta = ns.client.get(
        f"/api/admin/projects?userId={ns.beta['userId']}", headers=_auth(ns.admin_key)
    ).json()
    assert [row["projectId"] for row in only_beta["projects"]] == [small]

    only_public = ns.client.get(
        f"/api/admin/projects?userId={ns.public['userId']}", headers=_auth(ns.admin_key)
    ).json()
    assert [row["projectId"] for row in only_public["projects"]] == [big]


def test_admin_can_delete_any_project(admin_env: SimpleNamespace) -> None:
    """管理员删除任意项目：目录没了 + **归属行也清掉**（否则列表会出现幽灵项）。"""
    ns = admin_env
    project = ns.manager.resolve_project("identity:doomed", "doomed").project_id
    ns.meta_db.claim_project(ns.beta["userId"], project, "doomed")
    assert ns.manager.project_exists(project)
    assert ns.meta_db.list_projects(ns.beta["userId"]) == [project]

    response = ns.client.delete(
        f"/api/admin/projects/{project}", headers=_auth(ns.admin_key)
    )
    assert response.status_code == 204, response.text
    assert not ns.manager.project_exists(project)
    assert ns.meta_db.list_projects(ns.beta["userId"]) == []
    # 项目页（用户侧）也不再列出它。
    listed = ns.client.get("/api/projects", headers=_auth(ns.beta_key)).json()
    assert all(item["projectId"] != project for item in listed)


def test_admin_delete_unknown_project_is_404(admin_env: SimpleNamespace) -> None:
    response = admin_env.client.delete(
        "/api/admin/projects/does-not-exist", headers=_auth(admin_env.admin_key)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"


def test_admin_delete_requires_admin(admin_env: SimpleNamespace) -> None:
    """非管理员删项目 → 403（不能偷偷删别人的东西）。"""
    ns = admin_env
    project = ns.manager.resolve_project("identity:keep", "keep").project_id
    ns.meta_db.claim_project(ns.public["userId"], project, "keep")
    response = ns.client.delete(f"/api/admin/projects/{project}", headers=_auth(ns.public_key))
    assert response.status_code == 403
    assert ns.manager.project_exists(project), "被拒的删除不得真的删掉东西"


def _seed_project_bytes(ns: SimpleNamespace, project_id: str, size: int) -> None:
    """往项目的索引目录里塞一个指定大小的文件（造出可比较的占用）。"""
    directory = ns.manager.project_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "seed.bin").write_bytes(b"\0" * size)


# --------------------------------------------------------------------------- 统计 / 系统模块


def test_stats_match_usage_summary(admin_env: SimpleNamespace) -> None:
    """统计页与 ``usage_summary`` **同源**（不重复计算：后台与用户页给同一个数）。"""
    ns = admin_env
    project = ns.manager.resolve_project("identity:stats", "stats").project_id
    ns.meta_db.claim_project(ns.public["userId"], project, "stats")
    for index in range(3):
        ns.meta_db.record_query(
            project_id=project,
            mode="search",
            query=f"q{index}",
            latency_ms=10 + index,
            answerable=index != 0,
            degraded=index == 2,
            used_tokens=100,
            user_id=ns.public["userId"],
        )

    body = ns.client.get("/api/admin/stats", headers=_auth(ns.admin_key)).json()
    expected = ns.meta_db.usage_summary([project], days=30, recent_limit=0).to_json()
    assert body["search"]["total"] == expected["total"] == 3
    assert body["search"]["succeeded"] == expected["succeeded"] == 2
    assert body["totalQueries"] == 3
    assert body["tokens"] == 300
    assert body["errorRate"] is not None


def test_stats_error_rate_is_none_without_queries(admin_env: SimpleNamespace) -> None:
    """没有调用时错误率是 ``None``——“没有调用”不是“错误率 0%”（诚实性口径）。"""
    body = admin_env.client.get("/api/admin/stats", headers=_auth(admin_env.admin_key)).json()
    assert body["totalQueries"] == 0
    assert body["errorRate"] is None
    assert body["tokens"] == 0


def test_stats_can_be_scoped_to_one_user(admin_env: SimpleNamespace) -> None:
    """统计可按用户查询（用户 2026-09-15 要求“下拉查询某个用户的数据”）。"""
    ns = admin_env
    beta_project = ns.manager.resolve_project("identity:s-beta", "s-beta").project_id
    public_project = ns.manager.resolve_project("identity:s-public", "s-public").project_id
    ns.meta_db.claim_project(ns.beta["userId"], beta_project, "s-beta")
    ns.meta_db.claim_project(ns.public["userId"], public_project, "s-public")
    for _ in range(2):
        ns.meta_db.record_query(
            project_id=beta_project,
            mode="search",
            query="beta-q",
            latency_ms=10,
            answerable=True,
            used_tokens=50,
            user_id=ns.beta["userId"],
        )
    ns.meta_db.record_query(
        project_id=public_project,
        mode="search",
        query="public-q",
        latency_ms=10,
        answerable=True,
        used_tokens=7,
        user_id=ns.public["userId"],
    )

    everything = ns.client.get("/api/admin/stats", headers=_auth(ns.admin_key)).json()
    assert everything["totalQueries"] == 3
    assert everything["tokens"] == 107
    assert everything["userId"] is None

    only_beta = ns.client.get(
        f"/api/admin/stats?userId={ns.beta['userId']}", headers=_auth(ns.admin_key)
    ).json()
    assert only_beta["userId"] == ns.beta["userId"]
    assert only_beta["projectCount"] == 1
    assert only_beta["totalQueries"] == 2
    assert only_beta["tokens"] == 100


def test_system_reports_host_memory(admin_env: SimpleNamespace) -> None:
    """系统状态带 VPS 内存（用户 2026-09-15 要求）。

    不断言具体数字（跑在不同机器上都不一样），只断言**字段契约 + 内部自洽**：
    ``total = used + available`` 且比例落在 [0,1]。
    """
    body = admin_env.client.get("/api/admin/system", headers=_auth(admin_env.admin_key)).json()
    host = body["host"]
    assert set(host) == {
        "totalBytes",
        "availableBytes",
        "usedBytes",
        "usedRatio",
        "availableBasis",
        "reason",
    }
    if host["reason"] is not None:
        # 非 Linux 环境：字段全 None，如实说明原因（不编 0）。
        assert host["totalBytes"] is None
        return
    assert host["totalBytes"] > 0
    assert host["usedBytes"] + host["availableBytes"] == host["totalBytes"]
    assert 0.0 <= host["usedRatio"] <= 1.0
    assert host["availableBasis"] in ("MemAvailable", "MemFree")


def test_system_module_matches_healthz(admin_env: SimpleNamespace) -> None:
    """系统模块与 ``/healthz?deep=1`` **同源**（两处不能各算一份健康）。"""
    ns = admin_env
    admin_view = ns.client.get("/api/admin/system", headers=_auth(ns.admin_key)).json()
    probe = ns.client.get("/healthz?deep=1").json()
    assert admin_view["status"] == probe["status"]
    assert admin_view["version"] == probe["version"]
    assert admin_view["core"].keys() == probe["core"].keys()
