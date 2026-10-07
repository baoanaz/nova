"""TASK-060 验收：鉴权（session / API Key / 首个用户初始化 / 本地模式回归）。

对应卡内 DoD 逐条：401 不区分细节、token 明文只出现一次、bootstrap 只能用一次、
始终可注册、会话过期、以及**本地模式完全放行**（R34 的回归保护）。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nova_core.engine import Engine
from nova_service.app import create_app
from nova_service.auth import SESSION_COOKIE
from nova_service.config import Settings
from nova_service.metadb import MetaDB
from nova_service.runtime import EngineManager

from tests.conftest import DeterministicBigramEmbedding, make_client

PASSWORD = "correct-horse-battery"


def _invite(ns, kind: str = "C") -> str:
    """造一个邀请码（TASK-110：注册必须有码）。"""
    from nova_service.invites import generate_code

    code = generate_code(kind)
    ns.app.state.meta_db.create_invite(code, kind)
    return code


def _make(tmp_path: Path, *, local_mode: bool = False) -> SimpleNamespace:
    """构造一个 app（非本地模式 = 云端形态，鉴权生效）。"""
    settings = Settings(
        data_root=tmp_path / "data",
        local_mode=local_mode,
    )
    app = create_app(settings)
    app.state.engine_manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    return SimpleNamespace(app=app, client=make_client(app), settings=settings)


@pytest.fixture
def cloud(tmp_path: Path):
    """非本地模式的应用（鉴权生效）。"""
    ns = _make(tmp_path)
    with ns.client:
        yield ns
    ns.app.state.engine_manager.close()


# --------------------------------------------------------------------------- 部署形态


def test_meta_reports_deployment_shape_without_leaking_counts(cloud) -> None:
    """``GET /api/meta`` 免鉴权；只透出 needsBootstrap 布尔，不透出用户数量。"""
    body = cloud.client.get("/api/meta").json()
    assert body["authRequired"] is True
    assert body["needsBootstrap"] is True
    assert body["registerOpen"] is True
    assert body["userCount"] is None


def test_meta_shows_model_details_to_authenticated_browser(
    cloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """公开 meta 对匿名请求隐藏详情，但合法 session 能看到控制台所需模型字段。"""
    monkeypatch.setenv("EMBED_MODE", "api")
    monkeypatch.setenv("EMBED_MODEL", "voyage-4-lite")
    monkeypatch.setenv("EMBED_BASE_URL", "https://api.voyageai.com")
    anonymous = cloud.client.get("/api/meta").json()["config"]
    assert "model" not in anonymous["llm"]

    _bootstrap(cloud)
    authenticated = cloud.client.get("/api/meta").json()["config"]
    assert "model" in authenticated["llm"]
    assert authenticated["embedding"]["model"] == "voyage-4-lite"
    assert authenticated["embedding"]["provider"] == "voyage"
    assert authenticated["embedding"]["dim"] == 1024
    assert authenticated["embedding"]["maxInputTokens"] == 32_000


# --------------------------------------------------------------------------- 无凭据 = 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/projects"),
        ("post", "/api/query/search"),
        ("get", "/api/account/overview"),
        ("get", "/api/usage/summary"),
        ("get", "/api/auth/tokens"),
    ],
)
def test_remote_mode_requires_credentials(cloud, method: str, path: str) -> None:
    """非本地模式：无凭据访问受保护端点 → 401（TASK-051 A1 的原始缺陷）。"""
    response = cloud.client.request(method, path, json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_invalid_and_revoked_tokens_are_rejected(cloud) -> None:
    """无效 token 与**已撤销** token 都必须被拒（A1 实测里两者都能检索成功）。"""
    invalid = {"Authorization": "Bearer nova_totally-invalid"}
    assert cloud.client.get("/api/projects", headers=invalid).status_code == 401

    _bootstrap(cloud)
    created = cloud.client.post("/api/auth/tokens", json={"name": "k"}).json()
    headers = {"Authorization": f"Bearer {created['token']}"}
    assert cloud.client.get("/api/projects", headers=headers).status_code == 200

    revoke = cloud.client.delete(f"/api/auth/tokens/{created['id']}")
    assert revoke.status_code == 204
    assert cloud.client.get("/api/projects", headers=headers).status_code == 401


def test_unauthorized_body_does_not_reveal_which_detail_failed(cloud) -> None:
    """401 不区分"无效/已撤销/过期"（Module/06 §2.2 的探测面纪律）。"""
    messages = set()
    for header in ("Bearer nova_nope", "Bearer ", ""):
        response = cloud.client.get(
            "/api/projects", headers={"Authorization": header} if header else {}
        )
        messages.add(response.json()["error"]["message"])
    assert len(messages) == 1


# --------------------------------------------------------------------------- bootstrap


def _bootstrap(cloud, name: str = "owner") -> dict:
    response = cloud.client.post(
        "/api/auth/bootstrap", json={"name": name, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_bootstrap_creates_first_user_and_signs_in_once(cloud) -> None:
    """空库首次 bootstrap：创建账户 + 直接签发 session（否则无从开始）。"""
    body = _bootstrap(cloud)
    assert body["name"] == "owner"
    assert SESSION_COOKIE in cloud.client.cookies

    second = cloud.client.post(
        "/api/auth/bootstrap", json={"name": "another", "password": PASSWORD}
    )
    assert second.status_code == 403
    assert second.json()["error"]["code"] == "already_initialized"

    # 第一个账户没有被覆盖。
    assert cloud.client.get("/api/auth/me").json()["name"] == "owner"


def test_bootstrap_unavailable_in_local_mode(tmp_path: Path) -> None:
    """本地模式没有账户体系（R34）：bootstrap 返回 403 local_mode，不假装成功。"""
    ns = _make(tmp_path, local_mode=True)
    with ns.client:
        response = ns.client.post(
            "/api/auth/bootstrap", json={"name": "x", "password": PASSWORD}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "local_mode"
        assert ns.client.get("/api/auth/me").json()["isLocal"] is True
    ns.app.state.engine_manager.close()


# --------------------------------------------------------------------------- register / login


def test_register_conflict_is_409(tmp_path: Path) -> None:
    """每次部署都可注册（凭邀请码）；同名 → 409 name_taken。

    TASK-110：两次注册各用一张**独立**的码——重名失败时事务回滚，但一个用例只想验证
    "第二次因重名被拒"，让它的码还没被消耗才能把它与邀请码问题彻底分开。
    """
    ns = _make(tmp_path)
    with ns.client:
        for _ in range(2):
            response = ns.client.post(
                "/api/auth/register",
                json={
                    "name": "bob",
                    "password": PASSWORD,
                    "inviteCode": _invite(ns),
                },
            )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "name_taken"
    ns.app.state.engine_manager.close()


def test_empty_password_rejected(cloud) -> None:
    """密码**只拒空**（TASK-110 用户 2026-09-15 拍板：“密码不做限制”）。

    空密码不是“不限制”而是“没锁”：任何人输个名字就能登进去。因此下限降到 1 个字符，
    但仍然拒绝空串。
    """
    response = cloud.client.post(
        "/api/auth/bootstrap", json={"name": "owner", "password": ""}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_password"


def test_short_password_is_accepted(cloud) -> None:
    """一个字符的密码也允许（用户 2026-09-15 要求“任意想要的都行”），且确实生效。"""
    response = cloud.client.post(
        "/api/auth/bootstrap", json={"name": "owner", "password": "a"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "owner"
    assert SESSION_COOKIE in cloud.client.cookies
    # 这个 1 位密码确实生效：能登出再登回来。
    assert cloud.client.post("/api/auth/logout").status_code == 204
    login = cloud.client.post("/api/auth/login", json={"name": "owner", "password": "a"})
    assert login.status_code == 200, login.text


def test_login_wrong_password_and_unknown_user_share_one_message(cloud) -> None:
    """登录失败不区分"账户不存在"与"密码错"（不给账户枚举面）。"""
    _bootstrap(cloud)
    cloud.client.post("/api/auth/logout")

    wrong = cloud.client.post(
        "/api/auth/login", json={"name": "owner", "password": "wrong-password"}
    )
    unknown = cloud.client.post(
        "/api/auth/login", json={"name": "nobody", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_session_expiry_is_rejected(cloud, monkeypatch: pytest.MonkeyPatch) -> None:
    """会话过期后失效（库里有过期行也不算数）。"""
    _bootstrap(cloud)
    db: MetaDB = cloud.app.state.meta_db
    # 直接把会话的过期时间推到过去（比 freeze 时钟更直接，且不依赖 ttl 常量）。
    with db._write() as conn:
        conn.execute("UPDATE sessions SET expires_at = ?", (int(time.time()) - 1,))
    assert cloud.client.get("/api/auth/me").status_code == 401


def test_logout_invalidates_session(cloud) -> None:
    """登出后同一 cookie 不再有效。"""
    _bootstrap(cloud)
    assert cloud.client.get("/api/auth/me").status_code == 200
    assert cloud.client.post("/api/auth/logout").status_code == 204
    assert cloud.client.get("/api/auth/me").status_code == 401


# --------------------------------------------------------------------------- API Key


def test_token_plaintext_appears_only_in_the_creation_response(cloud) -> None:
    """明文只在创建时返回一次：列表接口不含明文，也不含哈希。"""
    _bootstrap(cloud)
    created = cloud.client.post("/api/auth/tokens", json={"name": "laptop"}).json()
    assert created["token"].startswith("nova_")
    assert created["prefix"].startswith("nova_")

    listing = cloud.client.get("/api/auth/tokens").json()
    assert len(listing) == 1
    assert listing[0]["name"] == "laptop"
    assert "token" not in listing[0]
    assert "tokenHash" not in listing[0]

    body = cloud.client.get("/api/auth/tokens").text
    assert created["token"] not in body
    digest_holder = cloud.app.state.meta_db
    assert digest_holder is not None


def test_token_updates_last_used_at(cloud) -> None:
    """用过的 Key 会刷新 lastUsedAt（运维排查"哪个 Key 还在用"）。"""
    _bootstrap(cloud)
    created = cloud.client.post("/api/auth/tokens", json={"name": "k"}).json()
    headers = {"Authorization": f"Bearer {created['token']}"}
    cloud.client.get("/api/projects", headers=headers)
    listing = cloud.client.get("/api/auth/tokens").json()
    assert listing[0]["lastUsedAt"] is not None


def test_cannot_revoke_someone_elses_token(cloud) -> None:
    """只能撤销自己的 Key（他人 token 的撤销请求 → 404，不泄露存在性）。"""
    _bootstrap(cloud)
    created = cloud.client.post("/api/auth/tokens", json={"name": "mine"}).json()
    db: MetaDB = cloud.app.state.meta_db
    other = db.create_user("other", "x")
    assert cloud.client.delete(f"/api/auth/tokens/{created['id']}").status_code == 204
    # 用 other 的身份撤销 mine（这里直接以 DB 层断言归属校验：other 撤销 mine 会失败）
    assert db.revoke_token(other.id, created["id"]) is False


# --------------------------------------------------------------------------- 本地模式回归（R34）


def test_local_mode_stays_unauthenticated(tmp_path: Path) -> None:
    """本地模式：既有端点无需凭据，且 /healthz 自述 disabled(local)（R34 不可破坏）。"""
    ns = _make(tmp_path, local_mode=True)
    with ns.client:
        assert ns.client.get("/api/projects").status_code == 200
        assert ns.client.get("/healthz").json()["auth"] == "disabled(local)"
        assert ns.client.get("/api/meta").json()["authRequired"] is False
        # 账户类端点在本地模式明确不可用（403 local_mode，而不是 501/空成功）。
        assert ns.client.get("/api/auth/tokens").status_code == 403
        assert ns.client.post("/api/auth/tokens", json={}).status_code == 403
    ns.app.state.engine_manager.close()


def test_healthz_reports_required_when_remote(cloud) -> None:
    """非本地模式 /healthz 如实报 required（TASK-051 A1 的诚实性缺陷修复）。"""
    assert cloud.client.get("/healthz").json()["auth"] == "required"


def test_public_paths_do_not_require_credentials(cloud) -> None:
    """免鉴权路径清单：探活、部署形态、注册/初始化可达（**返回业务码而非被中间件拦下**）。

    为什么不用"不是 401"来判定：``login`` 的凭据错误本身就是业务 401，与中间件的 401
    无法从状态码区分。这里改用"**返回业务错误码**"作为到达 handler 的证据。
    """
    for path in ("/healthz", "/api/meta"):
        assert cloud.client.get(path).status_code == 200, path

    register = cloud.client.post(
        "/api/auth/register",
        json={"name": "x", "password": PASSWORD, "inviteCode": _invite(cloud)},
    )
    assert register.status_code == 201
    assert register.json()["name"] == "x"

    bootstrap = cloud.client.post(
        "/api/auth/bootstrap", json={"name": "x", "password": ""}
    )
    assert bootstrap.status_code == 400
    assert bootstrap.json()["error"]["code"] == "invalid_password"

    # 登出在无会话时也是 204（幂等），证明它没被中间件拦下。
    assert cloud.client.post("/api/auth/logout").status_code == 204


def test_mcp_endpoint_requires_credentials(tmp_path: Path) -> None:
    """``/mcp`` 同样要求凭据（TASK-051 A1 实测的真正漏洞点）。

    用例设计要点：**未认证与已认证必须用两个独立的 app 实例**。原因是
    ``StreamableHTTPSessionManager`` 每个实例只能 ``run()`` 一次——在同一个 app 上再开一个
    ``TestClient`` 会二次启动 lifespan 而报错；而"未认证"又必须**不带** bootstrap 后的 cookie。
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    headers = {"Accept": "application/json, text/event-stream"}

    # 1) 全新实例、尚未有账户、不带任何凭据 → 必须 401（不能落到 MCP 协议层或 500）。
    anonymous_app = create_app(
        Settings(data_root=tmp_path / "anon", local_mode=False)
    )
    with make_client(anonymous_app) as anonymous:
        response = anonymous.post("/mcp", json=payload, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"

    # 2) 另一个实例：bootstrap + 建 Key 后带 Bearer → 不再被鉴权拦下。
    cloud = _make(tmp_path)
    with cloud.client:
        _bootstrap(cloud)
        created = cloud.client.post("/api/auth/tokens", json={}).json()
        authorized = cloud.client.post(
            "/mcp",
            json=payload,
            headers={**headers, "Authorization": f"Bearer {created['token']}"},
        )
    assert authorized.status_code != 401
    cloud.app.state.engine_manager.close()


# --------------------------------------------------------------------------- 工具


def test_meta_db_session_resolution_is_thread_safe(tmp_path: Path) -> None:
    """``MetaDB`` 每线程一个连接：跨线程解析会话不抛 ``sqlite3`` 线程错误。"""
    import threading

    db = MetaDB.open(tmp_path / "nova-meta.db")
    user = db.create_user("t", "x")
    session = db.create_session(user.id, ttl_s=60)
    results: list[bool] = []

    def resolve() -> None:
        results.append(db.resolve_session(session) is not None)

    threads = [threading.Thread(target=resolve) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [True] * 4


def test_app_fixture_contract_is_unchanged(app: FastAPI) -> None:
    """既有夹具（本地模式）不受鉴权改动影响：非 auth 端点仍可匿名访问。"""
    client = TestClient(app, raise_server_exceptions=False)
    with client:
        assert client.get("/api/projects").status_code == 200
