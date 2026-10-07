"""TASK-090 验收：请求日志持久化与 trace id 查询。

对应任务卡"验收标准（DoD）"逐条。方案（§A 裁定）：**文件轮转 sink**
（``{data_root}/logs/request.log``，JSONL）——理由见 ``nova_service/requestlog.py`` 的模块 docstring
与任务卡执行记录：本地模式按 R34/``test_tenancy`` 不该建 ``nova-meta.db``，DB 方案会让本地模式
彻底没有日志。

覆盖矩阵：

| DoD 条目 | 用例 |
|---|---|
| 成功请求可按 requestId 查到（状态/路径/耗时/userId） | ``test_success_request_is_queryable`` |
| 5xx → 记录里有 errorCode 与堆栈 | ``test_server_error_records_code_and_traceback`` |
| 脱敏：``Authorization: Bearer sk-live-xxx`` 查不到 | ``test_secrets_never_reach_the_log`` |
| 窗口生效：超上限后最旧记录被清理 | ``test_window_prunes_oldest`` |
| 越权：B 查 A 的 requestId → 404（与不存在一致） | ``test_cross_user_lookup_is_...`` |
| 服务重启后日志仍在 | ``test_log_survives_restart`` |
| 业务失败（404 project_not_found）也带 errorCode | ``test_business_error_code_is_recorded`` |
| 归属：未认证 401 请求无 owner → 谁都查不到 | ``test_unauthenticated_request_has_no_owner`` |

纪律：``tmp_path`` 下的临时 data_root + 确定性假 provider，不联网、不加载真实模型。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from nova_core.engine import Engine
from nova_service import logging as nova_logging
from nova_service.app import create_app
from nova_service.config import Settings
from nova_service.requestlog import (
    MAX_TRACEBACK_CHARS,
    RequestLogEntry,
    lookup,
    read_entries,
)
from nova_service.runtime import EngineManager

from tests.conftest import DeterministicBigramEmbedding, make_client, make_invite

PASSWORD = "correct-horse-battery"
#: 真形态假 key（脱敏断言用；与 ``test_usage_api`` 同一形态）。
FAKE_API_KEY = "sk-live-SHOULD-NOT-LEAK-abc123XYZ"
#: 必然失败的路径（未认证 → 401；本地模式 → 404 not_found）。
MISSING_PATH = "/api/definitely-not-a-route"


# --------------------------------------------------------------------------- 夹具


def _local(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    """本地模式应用（无账户体系，R34）：日志归属按"无 owner 放行"处理。"""
    settings = Settings(  # type: ignore[arg-type]
        data_root=tmp_path / "data", local_mode=True, local_rescan_interval_s=0.0, **overrides
    )
    app = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app.state.engine_manager = manager
    return SimpleNamespace(
        app=app, settings=settings, manager=manager, log=settings.request_log_path
    )


def _cloud(tmp_path: Path) -> SimpleNamespace:
    """云端形态 + 两个真实用户（A / B），各有一个项目（越权用例需要真实 userId）。"""
    settings = Settings(
        data_root=tmp_path / "data",
        local_mode=False,
        local_rescan_interval_s=0.0,
    )
    app = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    manager.attach_meta_db(app.state.meta_db)
    app.state.engine_manager = manager
    return SimpleNamespace(
        app=app, settings=settings, manager=manager, log=settings.request_log_path
    )


def _token(client: TestClient, name: str) -> dict[str, str]:
    created = client.post("/api/auth/tokens", json={"name": name})
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['token']}"}


@pytest.fixture
def local_env(tmp_path: Path):
    ns = _local(tmp_path)
    ns.client = make_client(ns.app)
    with ns.client:
        yield ns
    ns.manager.close()


@pytest.fixture
def cloud_env(tmp_path: Path):
    ns = _cloud(tmp_path)
    ns.client = make_client(ns.app)
    with ns.client:
        boot = ns.client.post("/api/auth/bootstrap", json={"name": "alice", "password": PASSWORD})
        assert boot.status_code == 201, boot.text
        ns.alice_id = boot.json()["userId"]
        ns.alice = _token(ns.client, "alice-key")
        reg = ns.client.post(
            "/api/auth/register",
            json={
                "name": "bob",
                "password": PASSWORD,
                "inviteCode": make_invite(ns.app),
            },
        )
        assert reg.status_code == 201, reg.text
        ns.bob_id = reg.json()["userId"]
        ns.bob = _token(ns.client, "bob-key")
        yield ns
    ns.manager.close()


def _query(client: TestClient, request_id: str, headers: dict[str, str] | None = None):
    return client.get(f"/api/request-log/{request_id}", headers=headers)


# --------------------------------------------------------------- DoD：成功请求可查


def test_success_request_is_queryable(local_env: SimpleNamespace) -> None:
    """一次成功请求 → 按 ``requestId`` 查到，状态/路径/耗时齐全（DoD 第 1 条）。"""
    response = local_env.client.get("/healthz", headers={"X-Request-Id": "trace-success-1"})
    assert response.status_code == 200

    found = _query(local_env.client, "trace-success-1")
    assert found.status_code == 200, found.text
    payload = found.json()
    assert payload["requestId"] == "trace-success-1"
    assert payload["method"] == "GET"
    assert payload["path"] == "/healthz"
    assert payload["status"] == 200
    assert isinstance(payload["durationMs"], (int, float))
    assert payload["durationMs"] >= 0
    assert payload["errorCode"] is None
    assert payload["ts"]


def test_user_id_is_recorded(cloud_env: SimpleNamespace) -> None:
    """已认证请求 → 记录里带 ``userId``（便于按用户排查；DoD 第 1 条的 userId 项）。

    为什么用 ``/api/projects`` 而不是 ``/healthz``：**免鉴权路径（``PUBLIC_PATHS``）不解析身份**
    （鉴权中间件直接放行，不写 ``request.state.nova_user``），所以拿健康检查端点断言 ``userId``
    会永远为 ``null``。这不是缺陷（探活本来就不需要身份），但断言必须选一个真的走鉴权的端点。
    """
    ok = cloud_env.client.get("/api/projects", headers=cloud_env.alice)
    assert ok.status_code == 200, ok.text
    rid = ok.headers["X-Request-Id"]
    payload = _query(cloud_env.client, rid, headers=cloud_env.alice).json()
    assert payload["userId"] == cloud_env.alice_id


def test_project_id_is_recorded(cloud_env: SimpleNamespace) -> None:
    """路径参数里的 projectId 记进日志（业务报错时知道是哪个项目）。"""
    from nova_core.hashing import blob_hash  # noqa: F401  （仅为保持导入语义明确）

    resolved = cloud_env.client.post(
        "/api/projects/resolve",
        json={"identityKey": "identity:log-repo", "displayName": "log-repo"},
        headers=cloud_env.alice,
    )
    assert resolved.status_code == 200, resolved.text
    project_id = resolved.json()["projectId"]

    stats = cloud_env.client.get(
        f"/api/projects/{project_id}/index-stats", headers=cloud_env.alice
    )
    rid = stats.headers["X-Request-Id"]
    payload = _query(cloud_env.client, rid, headers=cloud_env.alice).json()
    assert payload["projectId"] == project_id


# --------------------------------------------------------------- DoD：5xx 有 code 与堆栈


def test_server_error_records_code_and_traceback(tmp_path: Path) -> None:
    """未捕获异常 → 500：记录里有 ``errorCode=internal_error`` 与**完整堆栈**（DoD 第 2 条）。

    纪律：堆栈只进日志；**响应仍是通用文案**（``errors.py`` 口径不变，不泄漏内部细节）。
    """
    ns = _local(tmp_path)

    @ns.app.get("/_test/task090_boom")
    async def _boom() -> None:
        raise RuntimeError("task090-internal-detail")

    with make_client(ns.app) as client:
        response = client.get("/_test/task090_boom")
        rid = response.headers["X-Request-Id"]

        assert response.status_code == 500
        assert "task090-internal-detail" not in response.text, "响应泄漏了内部细节"
        assert "Traceback" not in response.text, "响应泄漏了堆栈"

        payload = _query(client, rid).json()
        assert payload["errorCode"] == "internal_error"
        assert payload["status"] == 500
        assert payload["traceback"], "5xx 必须记录堆栈（排查的核心价值）"
        # 根因必须在（截断策略是"保头保尾"，异常链的根因在尾部）
        assert "RuntimeError" in payload["traceback"]
        assert "task090-internal-detail" in payload["traceback"]
    ns.manager.close()


def test_business_error_code_is_recorded(local_env: SimpleNamespace) -> None:
    """4xx（业务预期）→ 记 ``errorCode``，但**不记栈**（DoD：4xx 不记栈，只记 code）。"""
    response = local_env.client.get(MISSING_PATH)
    assert response.status_code == 404
    payload = _query(local_env.client, response.headers["X-Request-Id"]).json()
    assert payload["errorCode"] == "not_found"
    assert payload["traceback"] is None


def test_api_error_code_is_recorded(cloud_env: SimpleNamespace) -> None:
    """``ApiError``（如 404 ``project_not_found``）也要有 code —— 异常处理器在内层，
    必须能从响应体读出（这是本条最容易漏的一环）。"""
    response = cloud_env.client.get("/api/projects/does-not-exist", headers=cloud_env.alice)
    assert response.status_code == 404
    payload = _query(cloud_env.client, response.headers["X-Request-Id"], cloud_env.alice).json()
    assert payload["errorCode"] == "project_not_found"


# --------------------------------------------------------------- DoD：脱敏


def test_secrets_never_reach_the_log(tmp_path: Path) -> None:
    """脱敏断言（DoD 第 3 条）：带 ``Authorization: Bearer sk-live-xxx`` 的请求，
    查到的记录与**原始日志文件**里都搜不到该 key；也不存在记录请求头的字段。"""
    ns = _local(tmp_path)

    @ns.app.get("/_test/task090_keyed")
    async def _keyed() -> None:
        # 模拟把 key 拼进异常文本的场景（最坏情况：调用方手滑）
        raise RuntimeError(f"provider 报错 key={FAKE_API_KEY}")

    with make_client(ns.app) as client:
        response = client.get(
            "/_test/task090_keyed", headers={"Authorization": f"Bearer {FAKE_API_KEY}"}
        )
        rid = response.headers["X-Request-Id"]
        payload = _query(client, rid).json()

        rendered = json.dumps(payload, ensure_ascii=False)
        assert FAKE_API_KEY not in rendered, "查询响应里泄漏了 key"
        assert "Authorization" not in rendered, "记录里不该有请求头字段"
        # 连"原始 JSONL 文件"（绕过查询层的唯一真相）里也不能有
        raw = ns.log.read_text(encoding="utf-8")
        assert FAKE_API_KEY not in raw, "落盘日志里泄漏了 key"
    ns.manager.close()


def test_redaction_also_covers_bare_keys_without_key_name(tmp_path: Path) -> None:
    """裸 key（没有 ``api_key=`` 这类键名）也要脱敏——``redact_text`` 的键名规则罩不住。"""
    from nova_service.requestlog import redact_request_text

    assert redact_request_text(FAKE_API_KEY) == "***"
    assert redact_request_text("nova_ABCDEFGHIJKLMNOP 无效") == "*** 无效"


# --------------------------------------------------------------- DoD：窗口（有界保留）


def test_window_prunes_oldest(tmp_path: Path) -> None:
    """窗口生效（DoD 第 4 条）：旧文件被清理 + 查询只认仍在窗口内的记录。

    窗口有两个维度（§A）：**体积**（轮转，由 ``RotatingFileHandler`` 保证）与**天数**
    （``prune_log_files`` 保证）。这里同时实测两条：

    1. 造 3 个"旧"轮转文件 + 1 个"新"文件 → 清理后只剩新的，旧 requestId 查不到；
    2. ``backup_count`` 之外的文件不参与查询（体积上界的可观测面）。
    """
    from nova_service.logging import prune_log_files

    ns = _local(tmp_path)
    with make_client(ns.app) as client:
        fresh = client.get(MISSING_PATH)
        fresh_rid = fresh.headers["X-Request-Id"]

        ns.log.parent.mkdir(parents=True, exist_ok=True)
        stale_rid = "trace-stale-0001"
        stale = ns.log.with_name(f"{ns.log.name}.1")
        stale.write_text(
            json.dumps({"requestId": stale_rid, "ts": "2000-01-01T00:00:00Z", "msg": "request"}),
            encoding="utf-8",
        )
        # 把旧文件与当前文件的 mtime 都推回 30 天前
        old = time.time() - 30 * 86400
        os.utime(stale, (old, old))
        # 清理前：旧记录**在窗口内**、可查
        assert _query(client, stale_rid).status_code == 200

        removed = prune_log_files(ns.log, retention_days=14)
        assert removed >= 1, "超过保留天数的文件应当被清理"
        assert not stale.exists(), "旧轮转文件应被删除"
        assert ns.log.exists(), "当前文件不该被删"

        # 清理后：最旧的记录查不到（窗口边界），最新那条仍在
        assert _query(client, stale_rid).status_code == 404
        assert _query(client, fresh_rid).status_code == 200
    ns.manager.close()


def test_window_ignores_files_beyond_backup_count(tmp_path: Path) -> None:
    """体积窗口：``max_files`` 之外（编号过大 = 更旧）的备份不参与查询。"""
    ns = _local(tmp_path, log_max_bytes=1, log_backup_count=1)
    with make_client(ns.app):
        ns.log.parent.mkdir(parents=True, exist_ok=True)
        far = ns.log.with_name(f"{ns.log.name}.99")
        far.write_text(
            json.dumps(
                {"requestId": "trace-too-old", "ts": "2000-01-01T00:00:00Z", "msg": "request"}
            ),
            encoding="utf-8",
        )
        entries = read_entries(ns.log, max_files=1)
        assert all(entry.request_id != "trace-too-old" for entry in entries)
    ns.manager.close()


def test_rotation_keeps_log_bounded(tmp_path: Path) -> None:
    """体积上界：单文件写满即轮转，不会无限增长（窗口"按 MB"一维的实测）。"""
    ns = _local(tmp_path, log_max_bytes=1024, log_backup_count=2)
    with make_client(ns.app) as client:
        for index in range(60):
            client.get(f"{MISSING_PATH}?i={index}")
    files = sorted(p.name for p in ns.log.parent.glob(f"{ns.log.name}*"))
    assert ns.log.name in files
    assert len(files) <= 3, f"轮转备份数应受 backup_count 约束，实际 {files}"
    assert all(p.stat().st_size <= 1024 * 2 for p in ns.log.parent.glob(f"{ns.log.name}*"))
    ns.manager.close()


# --------------------------------------------------------------- DoD：越权


def test_cross_user_lookup_is_indistinguishable_from_missing(
    cloud_env: SimpleNamespace,
) -> None:
    """越权（DoD 第 5 条）：B 查 A 的 requestId → 404，且与"不存在的 id"**逐字一致**。"""
    owned = cloud_env.client.get("/api/projects", headers=cloud_env.alice)
    assert owned.status_code == 200, owned.text
    rid = owned.headers["X-Request-Id"]

    # A 自己能查到
    assert _query(cloud_env.client, rid, cloud_env.alice).status_code == 200
    # B 查 → 404
    denied = _query(cloud_env.client, rid, cloud_env.bob)
    assert denied.status_code == 404, denied.text
    # 与"根本不存在的 id"响应完全一致（不给探测面）
    missing = _query(cloud_env.client, "trace-does-not-exist", cloud_env.bob)
    assert missing.status_code == 404
    assert denied.json() == missing.json()


def test_unauthenticated_request_has_no_owner(cloud_env: SimpleNamespace) -> None:
    """未认证请求（401 无 userId）→ **谁都查不到**（与不存在同 404）。

    这是本卡与编排者裁定的归属规则（§C）：401 记录没有 owner，暴露 path 会是信息泄露面
    （path 里可能含别人的 projectId）。因此"本人"这个概念对 401 不成立。

    ``cookies.clear()`` 是必须的：``TestClient`` 共用一个 cookie jar，bootstrap 登录留下的
    session cookie 会让"不带 Authorization 头"的请求依然被认成 alice（``test_tenancy`` 已记录）。
    """
    cloud_env.client.cookies.clear()  # 真正变成未认证
    denied = cloud_env.client.get("/api/projects")
    assert denied.status_code == 401, denied.text
    rid = denied.headers.get("X-Request-Id")
    assert rid, "401 响应也必须回写 X-Request-Id（否则用户拿不到 id 可报）"

    # 带凭据者也是 404（无 owner → 谁都查不到）；不带凭据者是 401（受保护区域的正常行为）。
    for headers in (cloud_env.alice, cloud_env.bob):
        assert cloud_env.client.get(f"/api/request-log/{rid}", headers=headers).status_code == 404
    assert cloud_env.client.get(f"/api/request-log/{rid}").status_code == 401


def test_request_log_endpoint_requires_auth_in_cloud_mode(cloud_env: SimpleNamespace) -> None:
    """云瑞形态：该端点必须在受保护区域（**不得**进 ``PUBLIC_PATHS``）。"""
    from nova_service.app import PUBLIC_PATHS

    assert not any("/api/request-log" in path for path in PUBLIC_PATHS)
    cloud_env.client.cookies.clear()  # 清掉 session cookie，确保真的是"无凭据"
    response = cloud_env.client.get("/api/request-log/whatever")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


# --------------------------------------------------------------- DoD：重启后仍在


def test_log_survives_restart(tmp_path: Path) -> None:
    """服务重启后日志仍在（DoD 第 6 条）：**新建 app 实例**（新进程等价物）仍能查到。"""
    settings = Settings(data_root=tmp_path / "data", local_mode=True, local_rescan_interval_s=0.0)
    first = create_app(settings)
    with make_client(first) as client:
        response = client.get(MISSING_PATH, headers={"X-Request-Id": "trace-before-restart"})
        assert response.status_code == 404

    # 模拟重启：新 app、新 logger 配置（文件 handler 重新挂载到同一路径）
    nova_logging.configure_logging(
        settings.log_level,
        log_path=settings.request_log_path,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
        retention_days=settings.log_retention_days,
    )
    second = create_app(settings)
    with make_client(second) as client:
        found = _query(client, "trace-before-restart")
        assert found.status_code == 200, "重启后应当仍能按 requestId 查到"
        assert found.json()["path"] == MISSING_PATH


def test_log_is_written_to_disk_not_memory(local_env: SimpleNamespace) -> None:
    """落盘而非纯内存（DoD：重启后仍在的前提）：文件真实存在且内容是 JSONL。"""
    local_env.client.get(MISSING_PATH, headers={"X-Request-Id": "trace-on-disk"})
    assert local_env.log.exists(), "日志文件应当落盘"
    text = local_env.log.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines
    parsed = [json.loads(line) for line in lines]
    assert any(item.get("requestId") == "trace-on-disk" for item in parsed)


# --------------------------------------------------------------- 截断与形状


def _entry(**overrides: object) -> RequestLogEntry:
    """构造一个日志条目（仅用于形状/截断的单元断言，不经过服务）。"""
    base: dict[str, object] = {
        "request_id": "trace-unit",
        "ts": "2026-01-01T00:00:00Z",
        "level": "info",
        "logger": "nova_service.requestlog",
        "message": "request",
        "method": "GET",
        "path": "/healthz",
        "status": 200,
        "duration_ms": 1.0,
        "user_id": None,
        "project_id": None,
        "error_code": None,
        "error_message": None,
        "traceback": None,
        "extra": {},
    }
    base.update(overrides)
    return RequestLogEntry(**base)  # type: ignore[arg-type]


def test_traceback_truncation_keeps_head_and_tail() -> None:
    """§C 的截断策略：**保头保尾**——异常链的根因在尾部，只保头等于把最有用的部分丢掉。"""
    long_stack = "HEAD-MARKER\n" + "x" * (MAX_TRACEBACK_CHARS * 2) + "\nTAIL-MARKER"
    rendered = _entry(traceback=long_stack).to_json()["traceback"]
    assert len(rendered) <= MAX_TRACEBACK_CHARS + 64
    assert "中间省略" in rendered, "截断标记应当可见（不假装是完整堆栈）"
    assert "HEAD-MARKER" in rendered
    assert "TAIL-MARKER" in rendered, "尾部（根因所在的异常链末端）必须保留"


def test_long_fields_are_truncated(tmp_path: Path) -> None:
    """端到端：超大堆栈不会把响应撑爆（响应体有上界）。"""
    ns = _local(tmp_path)

    @ns.app.get("/_test/task090_long")
    async def _long() -> None:
        raise RuntimeError("y" * 100_000)

    with make_client(ns.app) as client:
        response = client.get("/_test/task090_long")
        payload = _query(client, response.headers["X-Request-Id"]).json()
        assert len(payload["traceback"]) <= MAX_TRACEBACK_CHARS + 64
        assert "中间省略" in payload["traceback"]
        assert len(response.content) < 64 * 1024, "响应体应当有上界（截断生效）"
    ns.manager.close()


def test_lookup_returns_none_for_missing_file(tmp_path: Path) -> None:
    """日志文件不存在（尚无任何请求）→ 查不到，不抛异常。"""
    assert lookup(tmp_path / "nope" / "request.log", "trace-x") is None


def test_related_logs_carry_handler_stack(tmp_path: Path) -> None:
    """已处理的 5xx（映射成 503 的引擎错误）：主条目之外附带处理器那行的堆栈。

    为什么需要它：``errors.py`` 的异常处理器自己打堆栈（``exc_info=exc``），堆栈落在**另一条**
    记录上；只回主条目的话，用户报 503 却看到空堆栈。
    """
    from nova_service.errors import ApiError, map_engine_error

    ns = _local(tmp_path)
    with make_client(ns.app) as client:
        # 直接驱动映射（TASK-035 的 503 路径），模拟处理器打完堆栈后的两条记录
        from nova_service.requestlog import capture_request

        mapping = map_engine_error(OSError("disk full"))
        assert isinstance(mapping, ApiError) and mapping.status == 507
        capture_request(
            request_id="trace-handled-5xx",
            method="POST",
            path="/api/query/search",
            status=507,
            duration_ms=1.0,
            error_code=mapping.code,
        )
        capture_request(
            request_id="trace-handled-5xx",
            method="POST",
            path="/api/query/search",
            status=507,
            duration_ms=1.0,
            error_code=mapping.code,
            exc_info=OSError("disk full"),
        )
        payload = _query(client, "trace-handled-5xx").json()
        assert payload["errorCode"] == "storage_error"
        assert payload["relatedLogCount"] >= 1
        stacks = [item.get("traceback") for item in payload["relatedLogs"]]
        assert any(stacks), "旁路日志里应当有堆栈"
    ns.manager.close()
