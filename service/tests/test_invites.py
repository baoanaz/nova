"""TASK-110 P1/P2 验收：邀请码注册、身份分级、头衔编号、自定义 Key、按角色配额。

对应卡内 §5 的 P1 / P2 验收清单，逐条一个用例（docstring 标注守的是哪一条）。

三组口径（卡内冻结，本文件是它们的可执行证据）：

1. **注册必须有码**：无码 / 错形状 / 不存在 / 已失效 / 已过期 / 已用尽一律拒绝，
   且对外**同一文案**（注册是未登录端点，区分原因就是探测面）；
2. **并发安全**：``max_uses=1`` 的码被同时使用只有 1 个成功——用 ``used_count`` 断言，
   而不是靠"看起来没问题"（卡内明确要求"用影响行数断言"）；
3. **头衔与权限同源**：``/api/auth/me`` 的 ``capabilities`` 是唯一事实源，
   本文件既断言它的值，也断言**后端判定与它一致**（自定义 Key 的准入就是读它）。

纪律：临时 ``data_root`` + 确定性假 provider，不联网、不加载真实模型；并发用真线程
（``sqlite3`` 的 ``BEGIN IMMEDIATE`` 与 ``busy_timeout`` 是这条断言的一部分）。
"""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from nova_core.engine import Engine
from nova_core.hashing import blob_hash
from nova_service.app import create_app
from nova_service.auth import TOKEN_PREFIX
from nova_service.config import Settings
from nova_service.invites import (
    CODE_LENGTH,
    InviteRejected,
    generate_code,
    is_well_formed,
    normalize_code,
)
from nova_service.metadb import MetaDB
from nova_service.roles import (
    CAN_CUSTOM_KEY,
    EARLY_MEMBER_MAX,
    QUOTA_BY_ROLE,
    ROLE_ADMIN,
    ROLE_BETA,
    ROLE_PUBLIC,
    TITLE_BY_ROLE,
    capabilities_for,
    quota_bytes_for,
    title_for,
)
from nova_service.runtime import EngineManager

from tests.conftest import DeterministicBigramEmbedding, make_client

PASSWORD = "correct-horse-battery"
MB = 1024 * 1024


# --------------------------------------------------------------------------- 夹具


def _settings_for(data_root: Path, **overrides: object) -> Settings:
    """云端形态 + 关闭配额（除非用例显式覆盖）。"""
    defaults: dict[str, object] = {
        "data_root": data_root,
        "local_mode": False,
        "local_rescan_interval_s": 0.0,
    }
    return Settings(**{**defaults, **overrides})  # type: ignore[arg-type]


def _build(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    settings = _settings_for(tmp_path / "data", **overrides)
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


@pytest.fixture
def cloud(tmp_path: Path) -> Iterator[SimpleNamespace]:
    ns = _build(tmp_path)
    with ns.client:
        yield ns
    ns.manager.close()


def _make_invite(ns: SimpleNamespace, kind: str = "C", **kwargs: object) -> str:
    code = generate_code(kind)
    ns.meta_db.create_invite(code, kind, **kwargs)  # type: ignore[arg-type]
    return code


def _register(ns: SimpleNamespace, name: str, code: str) -> dict:
    response = ns.client.post(
        "/api/auth/register",
        json={"name": name, "password": PASSWORD, "inviteCode": code},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bootstrap(ns: SimpleNamespace, name: str = "alice") -> dict:
    """空库初始化：TASK-110 起这条路径建的账户**直接是管理员**。"""
    response = ns.client.post(
        "/api/auth/bootstrap", json={"name": name, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _key_for(ns: SimpleNamespace, user_id: str) -> str:
    """直接为某个用户造一把随机 Key，返回明文（用于在同一客户端上切换身份）。

    为什么不登录后再建：``TestClient`` 只有一个 cookie jar，切换登录会覆盖前一个用户的
    会话；两个 Bearer 头是同一客户端上最干净的换身份方式（TASK-061 的 ``test_tenancy`` 同法）。
    """
    from nova_service.auth import create_api_token

    raw, digest, prefix = create_api_token()
    ns.meta_db.create_token(user_id, token_hash=digest, prefix=prefix, name="switch")
    return raw


# --------------------------------------------------------------------------- 码的形状


def test_generated_codes_match_the_frozen_shape() -> None:
    """码面是 6 位大写字母/数字，且**首字母即类型**（用户 2026-09-15 拍板）。

    首字母必须是字母（类型字母 A/B/C）；其余 5 位可以是字母或数字（与卡内示例 ``A7K2M9``
    一致）。
    """
    for kind in "ABC":
        for _ in range(50):
            code = generate_code(kind)
            assert len(code) == CODE_LENGTH
            assert code[0] == kind
            assert code.isupper()
            assert code.isalnum()
            assert is_well_formed(code)
    body = {generate_code("C")[1:] for _ in range(200)}
    assert any(char.isdigit() for item in body for char in item), "随机部分应包含数字"
    assert any(char.isalpha() for item in body for char in item)


def test_well_formed_rejects_wrong_shapes() -> None:
    """形状校验：长度 / 首字母 / 字符集三者都要满足。"""
    assert is_well_formed("A7K2M9")
    assert is_well_formed("B3NQ8W")
    assert is_well_formed("C05RT2")
    assert is_well_formed("ABCDEF")
    assert not is_well_formed("A7K2M")  # 太短
    assert not is_well_formed("A7K2M9X")  # 太长
    assert not is_well_formed("D7K2M9")  # 首字母不是 A/B/C
    assert not is_well_formed("17K2M9")  # 首字符不是字母
    assert not is_well_formed("A7K2M-")
    assert not is_well_formed("A7K2M9 ")  # 中间空格（strip 只去首尾）
    assert not is_well_formed("")
    assert not is_well_formed("A7K2MÉ")


def test_normalize_code_accepts_lowercase_and_padding() -> None:
    """用户输入去空白 + 转大写（复制粘贴/手机自动大写的摩擦不该变成"码无效"）。"""
    assert normalize_code("  b3nq8w ") == "B3NQ8W"
    assert normalize_code(None) == ""
    assert normalize_code("") == ""


# --------------------------------------------------------------------------- P1 注册


def test_register_without_invite_is_rejected(cloud: SimpleNamespace) -> None:
    """P1：无邀请码注册 → 400 ``invalid_invite``（**不是** 500，也不是默默放行）。"""
    response = cloud.client.post(
        "/api/auth/register", json={"name": "nobody", "password": PASSWORD}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_invite"
    assert cloud.meta_db.user_count() == 0, "被拒的注册不得建出账户"


def test_register_with_malformed_code_is_rejected(cloud: SimpleNamespace) -> None:
    """形状不对的码同样被拒（且在**查库之前**就拒，不暴露"码存不存在"）。"""
    for bad in ("A7K2M", "X7K2M9", "A7K2M0-", "zz", "1234567"):
        response = cloud.client.post(
            "/api/auth/register",
            json={"name": f"u{len(bad)}", "password": PASSWORD, "inviteCode": bad},
        )
        assert response.status_code == 400, bad
        assert response.json()["error"]["code"] == "invalid_invite"
    assert cloud.meta_db.user_count() == 0


def test_register_rejection_messages_are_indistinguishable(cloud: SimpleNamespace) -> None:
    """不存在的码、已失效的码与已用尽的码 → **同一文案**。

    注册是**未登录**端点：任何区分都会变成枚举工具。
    （形状不合法的码是例外——那条在**查库之前**就返回，不泄露"库里有没有这个码"，
    因此它与"查过库后的拒绝"用不同文案是刻意的，见 ``test_register_with_malformed_code``。）
    """
    revoked = _make_invite(cloud, "C")
    cloud.meta_db.revoke_invite(revoked)
    exhausted = _make_invite(cloud, "C", max_uses=1)
    _register(cloud, "taker", exhausted)

    messages = set()
    for code in ("CZZZZZ", revoked, exhausted):
        response = cloud.client.post(
            "/api/auth/register",
            json={"name": f"u{code}", "password": PASSWORD, "inviteCode": code},
        )
        assert response.status_code == 400
        messages.add(response.json()["error"]["message"])
    assert len(messages) == 1, f"对外文案必须一致：{messages}"


def test_a_code_registers_admin(cloud: SimpleNamespace) -> None:
    """P1：A 码 → ``role=admin``、``title=执炬者``、**无编号**。"""
    body = _register(cloud, "boss", _make_invite(cloud, "A"))
    assert body["role"] == ROLE_ADMIN
    assert body["title"] == "执炬者"
    assert body["earlyMemberNo"] is None
    assert body["capabilities"]["isAdmin"] is True
    assert body["capabilities"]["canCustomKey"] is True


def test_b_code_registers_beta_with_number_one(cloud: SimpleNamespace) -> None:
    """P1：B 码 → ``role=beta``、``title=拓荒者``、首位编号 1（展示为 #0001）。"""
    body = _register(cloud, "first", _make_invite(cloud, "B"))
    assert body["role"] == ROLE_BETA
    assert body["title"] == "拓荒者"
    assert body["earlyMemberNo"] == 1
    assert body["capabilities"]["canCustomKey"] is True
    assert body["capabilities"]["isAdmin"] is False


def test_c_code_registers_public(cloud: SimpleNamespace) -> None:
    """P1：C 码 → ``role=public``、``title=旅人``、无编号、无自定义 Key 特权。"""
    body = _register(cloud, "plain", _make_invite(cloud, "C"))
    assert body["role"] == ROLE_PUBLIC
    assert body["title"] == "旅人"
    assert body["earlyMemberNo"] is None
    assert body["capabilities"]["canCustomKey"] is False


def test_beta_numbers_are_sequential_and_start_at_one(cloud: SimpleNamespace) -> None:
    """P1：内测编号按注册顺序递增（前 N 名各得一个）。"""
    numbers = [
        _register(cloud, f"beta{i}", _make_invite(cloud, "B"))["earlyMemberNo"]
        for i in range(3)
    ]
    assert numbers == [1, 2, 3]


def test_number_101_is_not_assigned(cloud: SimpleNamespace) -> None:
    """P1：**第 101 名内测**拿不到编号（用户要求"前 100 名，之后不再发放"）。

    直接写 100 行用户而不是真注册 100 次：本用例要钉的是**分配规则的边界**，
    而不是"注册接口能被调用 100 次"（那是另一件事，且要慢两个数量级）。
    """
    db = cloud.meta_db
    for index in range(1, EARLY_MEMBER_MAX + 1):
        db.create_user(f"beta{index}", "h", role=ROLE_BETA, early_member_no=index)
    body = _register(cloud, "latecomer", _make_invite(cloud, "B"))
    assert body["role"] == ROLE_BETA, "第 101 名仍是内测身份"
    assert body["earlyMemberNo"] is None, "第 101 名起不再发编号"


def test_used_up_code_is_rejected(cloud: SimpleNamespace) -> None:
    """P1：同一码超用 → 拒绝（``max_uses=1``）。"""
    code = _make_invite(cloud, "C", max_uses=1)
    _register(cloud, "a", code)
    response = cloud.client.post(
        "/api/auth/register",
        json={"name": "b", "password": PASSWORD, "inviteCode": code},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_invite"


def test_multi_use_code_allows_exactly_max_uses(cloud: SimpleNamespace) -> None:
    """``max_uses=3``：前 3 个成功、第 4 个被拒（多用途码是"分享码"的形态）。"""
    code = _make_invite(cloud, "C", max_uses=3)
    for index in range(3):
        _register(cloud, f"u{index}", code)
    response = cloud.client.post(
        "/api/auth/register",
        json={"name": "u3", "password": PASSWORD, "inviteCode": code},
    )
    assert response.status_code == 400
    assert cloud.meta_db.invite(code)["usedCount"] == 3


def test_revoked_code_is_rejected(cloud: SimpleNamespace) -> None:
    """P1：失效码 → 拒绝。"""
    code = _make_invite(cloud, "C")
    assert cloud.meta_db.revoke_invite(code)
    response = cloud.client.post(
        "/api/auth/register",
        json={"name": "x", "password": PASSWORD, "inviteCode": code},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_invite"


def test_expired_code_is_rejected(cloud: SimpleNamespace) -> None:
    """P1：过期码 → 拒绝（``expires_at`` 已过）。"""
    code = generate_code("C")
    cloud.meta_db.create_invite(code, "C", expires_at=int(time.time()) - 60)
    response = cloud.client.post(
        "/api/auth/register",
        json={"name": "x", "password": PASSWORD, "inviteCode": code},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_invite"


def test_name_conflict_does_not_burn_the_code(cloud: SimpleNamespace) -> None:
    """重名 409 时事务回滚：**码不被浪费**（否则用户白丢一张码还得再要）。"""
    first = _make_invite(cloud, "C")
    _register(cloud, "dup", first)
    assert cloud.meta_db.invite(first)["usedCount"] == 1

    clash = _make_invite(cloud, "C")
    response = cloud.client.post(
        "/api/auth/register",
        json={"name": "dup", "password": PASSWORD, "inviteCode": clash},
    )
    assert response.status_code == 409
    assert cloud.meta_db.invite(clash)["usedCount"] == 0, "重名失败不该消耗码"

    # 码仍然可用（没被烧掉）：换个名字就能注册上。
    _register(cloud, "not-dup", clash)


def test_concurrent_use_of_single_use_code_admits_exactly_one(cloud: SimpleNamespace) -> None:
    """P1（**并发安全**）：``max_uses=1`` 的码被同时抢用，**只有一个成功**。

    为什么必须真并发：卡内 §3.1 冻结的写法是"带 ``WHERE used_count < max_uses`` 的 UPDATE +
    以影响行数判成败"。若谁把它改回"先 SELECT 判再用"，**单线程测试全绿**，
    只有真并发才会暴露超发。

    为什么直接打 ``create_user_with_invite`` 而不走 HTTP：核销与建账户的原子性就发生在这一层
    （一个 ``BEGIN IMMEDIATE``），HTTP 面只是它的薄壳。直接并发这一层能精确地把断言钉在
    "事务边界"上，而不受 ``TestClient`` 单实例 lifespan 的限制干扰。
    """
    db = cloud.meta_db
    code = _make_invite(cloud, "C", max_uses=1)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        try:
            db.create_user_with_invite(f"race{index}", "hash", code=code)
            outcome = "created"
        except InviteRejected as exc:
            outcome = f"rejected:{exc.reason}"
        except Exception as exc:  # noqa: BLE001 - 把线程里真正发生了什么带出来
            outcome = f"{type(exc).__name__}: {exc}"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("created") == 1, f"只能有一个成功：{outcomes}"
    assert sorted(outcomes).count("rejected:exhausted") == 5, outcomes
    assert db.invite(code)["usedCount"] == 1, "核销计数不得超发"
    assert db.user_count() == 1, "只有成功的那一个建出了账户"


def test_concurrent_registration_over_http_admits_exactly_one(tmp_path: Path) -> None:
    """同一断言的 **HTTP 面**版本：并发注册只有 1 个 201，其余 400 ``invalid_invite``。

    与上一条的关系：上一条钉事务边界，这一条钉"路由层真的把这个保证用上了"
    （两件事都可能单独出错：事务写对了但路由先用别的方式判了码）。
    共用一个 ``TestClient``：它内部走 anyio portal，多线程发请求是被支持的。
    """
    ns = _build(tmp_path)
    code = _make_invite(ns, "C", max_uses=1)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        try:
            response = ns.client.post(
                "/api/auth/register",
                json={"name": f"race{index}", "password": PASSWORD, "inviteCode": code},
            )
            outcome = str(response.status_code)
        except Exception as exc:  # noqa: BLE001
            outcome = f"{type(exc).__name__}: {exc}"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(6)]
    with ns.client:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    ns.manager.close()

    assert outcomes.count("201") == 1, f"只能有一个成功：{outcomes}"
    assert outcomes.count("400") == 5, outcomes
    assert ns.meta_db.invite(code)["usedCount"] == 1
    assert ns.meta_db.user_count() == 1


def test_bootstrap_creates_admin_in_empty_database(cloud: SimpleNamespace) -> None:
    """P1（全新路径）：空库 ``bootstrap`` 建的账户**直接是管理员**。

    否则是死锁——邀请码只能由管理员创建，而空库里没有任何管理员。
    """
    body = _bootstrap(cloud, "owner")
    assert body["role"] == ROLE_ADMIN
    assert body["title"] == "执炬者"
    assert body["capabilities"]["isAdmin"] is True


def test_existing_admin_name_is_promoted_on_startup(tmp_path: Path) -> None:
    """P1（迁移路径）：已有库里 ``NOVA_ADMIN_NAME`` 指定的账户启动时被提为管理员。"""
    data_root = tmp_path / "data"
    settings = _settings_for(data_root, admin_name="owner")
    # 先造一个"迁移前就存在"的普通账户（模拟旧库）。
    seed = MetaDB.open(settings.meta_db_path)
    seed.create_user("owner", "h")
    seed.create_user("other", "h")
    assert seed.find_user_by_name_exact("owner").role == ROLE_PUBLIC

    app = create_app(settings)
    manager = EngineManager.open(
        data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app.state.engine_manager = manager
    with make_client(app):
        db: MetaDB = app.state.meta_db
        promoted = db.find_user_by_name_exact("owner")
        assert promoted is not None and promoted.role == ROLE_ADMIN
        assert promoted.title == "执炬者"
        # 只提升指定名字，不牵连其他人。
        bystander = db.find_user_by_name_exact("other")
        assert bystander is not None and bystander.role == ROLE_PUBLIC
    manager.close()


def test_admin_promotion_is_idempotent_and_tolerates_missing_name(tmp_path: Path) -> None:
    """提升是幂等的；名字不存在时只记日志，**不报错**（空库场景本该如此）。"""
    settings = _settings_for(tmp_path / "data", admin_name="ghost")
    app = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app.state.engine_manager = manager
    with make_client(app):
        assert app.state.meta_db.user_count() == 0
    # 再来一次（同名不存在的库）：仍然不报错。
    again = create_app(settings)
    again.state.engine_manager = manager
    with make_client(again):
        pass
    manager.close()


def test_legacy_user_defaults_to_public_and_keeps_working(tmp_path: Path) -> None:
    """P1（**旧用户回归**）：迁移前存在的账户默认 ``public``，登录与凭据照常可用。

    这是本卡最重要的"不破坏既有接入"断言：``users`` 加列后旧行必须仍能登录、
    仍能用自己的 Key 调 MCP。
    """
    data_root = tmp_path / "data"
    settings = _settings_for(data_root)
    # 手搭一个"旧库"：只有 TASK-110 之前的列。
    import sqlite3

    settings.meta_db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(str(settings.meta_db_path))
    legacy.executescript(
        "CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,"
        " password_hash TEXT NOT NULL, created_at INTEGER NOT NULL,"
        " is_local INTEGER NOT NULL DEFAULT 0);"
    )
    legacy.execute(
        "INSERT INTO users (id, name, password_hash, created_at, is_local)"
        " VALUES ('legacy', 'oldie', 'x', 1, 0)"
    )
    legacy.commit()
    legacy.close()

    app = create_app(settings)
    manager = EngineManager.open(
        data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app.state.engine_manager = manager
    with make_client(app):
        db: MetaDB = app.state.meta_db
        old = db.find_user_by_name_exact("oldie")
        assert old is not None
        assert old.role == ROLE_PUBLIC, "旧行必须回落公测（迁移只加列）"
        assert old.title == "旅人"
        assert old.early_member_no is None and old.banned_at is None
    manager.close()


# --------------------------------------------------------------------------- me 视图


def test_me_carries_role_title_and_capabilities(cloud: SimpleNamespace) -> None:
    """P1/P2：``/api/auth/me`` 返回能力位（**头衔与权限同源**的唯一事实源）。"""
    _register(cloud, "beta", _make_invite(cloud, "B"))
    body = cloud.client.get("/api/auth/me").json()
    assert body["role"] == ROLE_BETA
    assert body["title"] == "拓荒者"
    assert body["earlyMemberNo"] == 1
    assert body["capabilities"] == {
        "canCustomKey": True,
        "quotaBytes": QUOTA_BY_ROLE[ROLE_BETA],
        "earlyMemberNo": 1,
        "isAdmin": False,
    }


def test_capabilities_matrix_matches_roles_module() -> None:
    """能力位矩阵与 ``roles`` 的常量**逐项一致**（防两处各写一份漂移）。"""
    assert capabilities_for(ROLE_ADMIN)["canCustomKey"] is True
    assert capabilities_for(ROLE_BETA)["canCustomKey"] is True
    assert capabilities_for(ROLE_PUBLIC)["canCustomKey"] is False
    assert CAN_CUSTOM_KEY == {ROLE_ADMIN, ROLE_BETA}
    for role in (ROLE_ADMIN, ROLE_BETA, ROLE_PUBLIC):
        assert capabilities_for(role)["quotaBytes"] == quota_bytes_for(role)
        assert title_for(role) == TITLE_BY_ROLE[role]
    assert QUOTA_BY_ROLE[ROLE_PUBLIC] == 500 * MB
    assert QUOTA_BY_ROLE[ROLE_BETA] == 1024 * MB
    assert QUOTA_BY_ROLE[ROLE_ADMIN] == 5 * 1024 * MB


def test_unknown_role_falls_back_to_public() -> None:
    """认不出的角色一律回落公测（安全失败方向：不因为一行脏数据让全部请求 500）。"""
    assert capabilities_for("root")["isAdmin"] is False
    assert capabilities_for("root")["canCustomKey"] is False
    assert capabilities_for(None)["quotaBytes"] == QUOTA_BY_ROLE[ROLE_PUBLIC]
    assert capabilities_for("  ADMIN  ")["isAdmin"] is True, "大小写与空格要归一"


# --------------------------------------------------------------------------- P2 自定义 Key


def _create_key(ns: SimpleNamespace, *, key: str = "", name: str = "") -> object:
    return ns.client.post("/api/auth/tokens", json={"key": key, "name": name})


def test_beta_can_create_custom_key(cloud: SimpleNamespace) -> None:
    """P2：内测用户建自定义 Key 成功，且**能用于 MCP 鉴权**（明文即凭据）。"""
    _register(cloud, "beta", _make_invite(cloud, "B"))
    custom = f"{TOKEN_PREFIX}mykey1234567890abcd"
    response = _create_key(cloud, key=custom, name="mine")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token"] == custom, "自定义场景下返回的明文必须就是用户给的那串"
    assert body["isCustom"] is True

    # 用这把 Key 调一个受保护端点：能进即为鉴权链认可。
    headers = {"Authorization": f"Bearer {custom}"}
    assert cloud.client.get("/api/projects", headers=headers).status_code == 200

    # 通过 MCP 面同样认（内测用户用自定义 Key 接编辑器是主要场景）。
    assert cloud.client.get("/api/auth/me", headers=headers).json()["role"] == ROLE_BETA


def test_public_user_custom_key_is_403(cloud: SimpleNamespace) -> None:
    """P2：公测用户传自定义 Key → **403**，文案必须说明这是拓荒者特权。"""
    _register(cloud, "plain", _make_invite(cloud, "C"))
    response = _create_key(cloud, key=f"{TOKEN_PREFIX}mykey1234567890abcd")
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "custom_key_forbidden"
    assert "拓荒者" in error["message"], "必须明确说明这是拓荒者特权（不是静默忽略）"


def test_public_user_can_still_get_random_key(cloud: SimpleNamespace) -> None:
    """公测用户不传 ``key`` 时**照常**拿到随机 Key（默认行为不变）。

    TASK-110 改版（用户 2026-09-15 要求）：随机 Key 是 ``nova_`` + **恰好 16 位**字母/数字，
    **不含符号**。
    """
    _register(cloud, "plain", _make_invite(cloud, "C"))
    response = _create_key(cloud)
    assert response.status_code == 200
    body = response.json()
    token = body["token"]
    assert token.startswith(TOKEN_PREFIX)
    body_part = token[len(TOKEN_PREFIX) :]
    assert len(body_part) == 16, f"随机部分应为 16 位：{token}"
    assert body_part.isalnum() and body_part.isascii(), f"只含字母/数字：{token}"
    assert body["isCustom"] is False


def test_custom_key_without_prefix_is_400(cloud: SimpleNamespace) -> None:
    """P2：不以 ``nova_`` 开头 → 400。"""
    _register(cloud, "beta", _make_invite(cloud, "B"))
    response = _create_key(cloud, key="my-custom-key-1234567")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_custom_key"


def test_custom_key_short_body_is_rejected(cloud: SimpleNamespace) -> None:
    """正文为空 → 400（TASK-110 改版：只要求 ``nova_`` 后面**非空**）。

    历史背景：本卡第一版要求正文 ≥16 字符；用户 2026-09-15 拍板放宽到
    “nova_1 都可以”。因此长度下限从 16 降到 1，而长度上限仍然保留（DoS 面）。
    """
    _register(cloud, "beta", _make_invite(cloud, "B"))
    response = _create_key(cloud, key=f"{TOKEN_PREFIX}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_custom_key"


def test_custom_key_minimal_body_is_accepted(cloud: SimpleNamespace) -> None:
    """``nova_1`` 可用（用户明确举的例子）。"""
    _register(cloud, "beta", _make_invite(cloud, "B"))
    response = _create_key(cloud, key=f"{TOKEN_PREFIX}1")
    assert response.status_code == 200, response.text
    assert response.json()["token"] == f"{TOKEN_PREFIX}1"


def test_documented_example_keys_are_accepted(cloud: SimpleNamespace) -> None:
    """**示例必须真的能用**（前端 placeholder 与手册里的那一串）。

    来历（实测踩到）：占位符与手册示例曾写成 ``nova_my-project-2026``，而当时要求正文 ≥16
    字符、它只有 15，用户照着抄会直接得到 400。任何写进文档/界面的示例都必须是
    **端到端可用**的，否则它就是在教用户犯错。

    改动这里时请同步 ``web/src/pages/ApiKeysPage.tsx`` 的 placeholder 与
    ``docs/handbook/getting-started/agent接入与API-Key.md``。
    """
    _register(cloud, "beta", _make_invite(cloud, "B"))
    for example in (f"{TOKEN_PREFIX}my-laptop-key-2026", f"{TOKEN_PREFIX}1"):
        response = _create_key(cloud, key=example, name=example)
        assert response.status_code == 200, f"文档示例不可用：{example} → {response.text}"


def test_custom_key_arbitrary_chars_are_accepted(cloud: SimpleNamespace) -> None:
    """字符集**不限制**（用户 2026-09-15 拍板："后面可以附带任意的数字和字母，这里不做限制"）。

    唯一约束是库里没有一样的 Key（→ 409）。中文、空格、符号都允许——它们只要能被
    放进客户端的配置里就是合法的 Key。
    """
    _register(cloud, "beta", _make_invite(cloud, "B"))
    for example in (f"{TOKEN_PREFIX}中文也行", f"{TOKEN_PREFIX}with space", f"{TOKEN_PREFIX}a/b+c"):
        response = _create_key(cloud, key=example, name=example)
        assert response.status_code == 200, f"{example} → {response.text}"


def test_custom_key_conflict_is_409(cloud: SimpleNamespace) -> None:
    """P2：与既有 Key 冲突 → 409 ``key_taken``（唯一索引）。"""
    _register(cloud, "beta", _make_invite(cloud, "B"))
    custom = f"{TOKEN_PREFIX}duplicatekey12345678"
    assert _create_key(cloud, key=custom).status_code == 200
    response = _create_key(cloud, key=custom)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "key_taken"


def test_custom_key_can_be_revoked_and_is_listed_as_custom(cloud: SimpleNamespace) -> None:
    """自定义 Key 与随机 Key 走**同一套**列表/撤销逻辑（不是第二套分支）。"""
    _register(cloud, "beta", _make_invite(cloud, "B"))
    custom = f"{TOKEN_PREFIX}listablekey12345678"
    created = _create_key(cloud, key=custom).json()
    listed = cloud.client.get("/api/auth/tokens").json()
    assert [item["id"] for item in listed] == [created["id"]]
    assert listed[0]["isCustom"] is True
    assert cloud.client.delete(f"/api/auth/tokens/{created['id']}").status_code == 204
    headers = {"Authorization": f"Bearer {custom}"}
    assert cloud.client.get("/api/projects", headers=headers).status_code == 401


# --------------------------------------------------------------------------- P2 配额


def test_quota_by_role_is_frozen() -> None:
    """P2：三个角色的索引空间与用户指定的数字一致（500MB / 1GB / 5GB）。"""
    assert quota_bytes_for(ROLE_PUBLIC) == 500 * MB
    assert quota_bytes_for(ROLE_BETA) == 1024 * MB
    assert quota_bytes_for(ROLE_ADMIN) == 5 * 1024 * MB


def test_me_reports_role_quota(cloud: SimpleNamespace) -> None:
    """P2：``me`` 的能力位里给出**按角色**的额度（前端据此显示"1.0 GiB"）。"""
    _register(cloud, "beta", _make_invite(cloud, "B"))
    assert cloud.client.get("/api/auth/me").json()["capabilities"]["quotaBytes"] == 1024 * MB


def test_quota_override_wins_over_role(tmp_path: Path) -> None:
    """后台对单人的配额覆盖优先于角色默认（``effective_user_limit_bytes`` 的优先级 1）。"""
    from nova_service.quota import effective_user_limit_bytes

    ns = _build(tmp_path)
    settings = ns.settings
    assert effective_user_limit_bytes(settings, role=ROLE_BETA, override=12345) == 12345
    assert effective_user_limit_bytes(settings, role=ROLE_BETA) == 1024 * MB
    with ns.client:
        pass
    ns.manager.close()


def test_overview_shows_role_quota(cloud: SimpleNamespace) -> None:
    """P2：``/api/account/overview`` 的 ``storage.user.limitBytes`` 是角色额度。"""
    _register(cloud, "beta", _make_invite(cloud, "B"))
    body = cloud.client.get("/api/account/overview").json()
    assert body["storage"]["user"]["limitBytes"] == 1024 * MB
    assert body["account"]["role"] == ROLE_BETA
    assert body["account"]["title"] == "拓荒者"
    assert body["account"]["earlyMemberNo"] == 1


def test_upload_is_rejected_when_over_role_quota(cloud: SimpleNamespace) -> None:
    """P2：公测用户超过角色配额时**上传被拒**（413）。

    用 ``quota_bytes`` 覆盖把上限压到 1 字节，避免真传 500 MB：被测的是**判定路径**
    （读角色 → 算上限 → 比较 → 拒绝），不是"能不能传大文件"。
    项目归属用当前登录用户的 id 登记（``resolve`` 不会自动 claim 到指定用户）。
    """
    body = _register(cloud, "plain", _make_invite(cloud, "C"))
    user_id = body["userId"]
    project = cloud.manager.resolve_project("identity:quota", "quota").project_id
    cloud.meta_db.claim_project(user_id, project, "quota")
    cloud.meta_db.set_user_quota(user_id, 1)

    content = b"x = 1\n"
    response = cloud.client.post(
        "/api/sync/batch-upload",
        json={
            "projectId": project,
            "blobs": [
                {
                    "path": "src/x.py",
                    "blobHash": blob_hash("src/x.py", content),
                    "contentB64": base64.b64encode(content).decode(),
                }
            ],
        },
    )
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "quota_exceeded"


def test_upload_within_role_quota_succeeds(cloud: SimpleNamespace) -> None:
    """P2：额度内上传照常（硬拒不能变成"谁都传不上去"）。"""
    body = _register(cloud, "plain", _make_invite(cloud, "C"))
    project = cloud.manager.resolve_project("identity:ok", "ok").project_id
    cloud.meta_db.claim_project(body["userId"], project, "ok")
    content = b"x = 1\n"
    response = cloud.client.post(
        "/api/sync/batch-upload",
        json={
            "projectId": project,
            "blobs": [
                {
                    "path": "src/x.py",
                    "blobHash": blob_hash("src/x.py", content),
                    "contentB64": base64.b64encode(content).decode(),
                }
            ],
        },
    )
    assert response.status_code == 200, response.text


def test_quota_is_per_user_not_global(cloud: SimpleNamespace) -> None:
    """P2：配额按**账户**隔离——A 的占用不计入 B 的额度。

    两个用户各自注册自己的项目并 claim 给自己，然后把 A 的上限压到 1 字节：
    A 的上传被拒；B 用自己的 Bearer Key 上传同一份内容成功。
    """
    rich = _register(cloud, "rich", _make_invite(cloud, "B"))
    poor = _register(cloud, "poor", _make_invite(cloud, "B"))
    rich_project = cloud.manager.resolve_project("identity:rich", "rich").project_id
    poor_project = cloud.manager.resolve_project("identity:poor", "poor").project_id
    cloud.meta_db.claim_project(rich["userId"], rich_project, "rich")
    cloud.meta_db.claim_project(poor["userId"], poor_project, "poor")
    cloud.meta_db.set_user_quota(rich["userId"], 1)

    content = b"x = 1\n"
    blob = {
        "path": "src/x.py",
        "blobHash": blob_hash("src/x.py", content),
        "contentB64": base64.b64encode(content).decode(),
    }
    # A（rich）上限 1 字节 → 他自己的上传被拒。
    rich_headers = {"Authorization": f"Bearer {_key_for(cloud, rich['userId'])}"}
    rejected = cloud.client.post(
        "/api/sync/batch-upload",
        json={"projectId": rich_project, "blobs": [blob]},
        headers=rich_headers,
    )
    assert rejected.status_code == 413, rejected.text

    # B（poor）额度按角色默认 → 不受 A 的影响。
    poor_headers = {"Authorization": f"Bearer {_key_for(cloud, poor['userId'])}"}
    accepted = cloud.client.post(
        "/api/sync/batch-upload",
        json={"projectId": poor_project, "blobs": [blob]},
        headers=poor_headers,
    )
    assert accepted.status_code == 200, accepted.text


def test_search_still_works_when_over_quota(cloud: SimpleNamespace) -> None:
    """P2：超限时**检索不受影响**（与上传硬拒相对——读路径继续服务）。"""
    body = _register(cloud, "plain", _make_invite(cloud, "C"))
    project = cloud.manager.resolve_project("identity:read", "read").project_id
    cloud.meta_db.claim_project(body["userId"], project, "read")
    content = b"token_service_refresh\n"
    cloud.client.post(
        "/api/sync/batch-upload",
        json={
            "projectId": project,
            "blobs": [
                {
                    "path": "src/x.py",
                    "blobHash": blob_hash("src/x.py", content),
                    "contentB64": base64.b64encode(content).decode(),
                }
            ],
        },
    )
    # 已经超限（上限压到 1 字节）——但检索仍必须可用。
    cloud.meta_db.set_user_quota(body["userId"], 1)
    response = cloud.client.post(
        "/api/query/search", json={"projectId": project, "query": "refresh"}
    )
    assert response.status_code == 200, response.text


def test_platform_cli_registration_requires_invite_in_local_mode_is_403(
    tmp_path: Path,
) -> None:
    """本地模式没有账户体系（R34）：注册仍 403 ``local_mode``（本卡不改这条口径）。"""
    settings = Settings(data_root=tmp_path / "data", local_mode=True)
    app: FastAPI = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app.state.engine_manager = manager
    with make_client(app) as client:
        response = client.post(
            "/api/auth/register",
            json={"name": "x", "password": PASSWORD, "inviteCode": "C7K2M9"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "local_mode"
    manager.close()
