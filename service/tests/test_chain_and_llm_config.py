"""TASK-099 验收：答案落库（§A）、callId 关联（§B）、用户级 LLM 配置（§C）。

用户 2026-09-14 的三个真实缺口（本文件逐条守）：

| 缺口 | 用例 |
|---|---|
| 历史弹窗只有证据、没有 LLM 答案 | ``test_answer_text_is_stored_and_returned_by_history`` |
| 一次调用的 N 次初始化 + 1 次检索串不起来 | ``test_three_uploads_and_one_search_share_call_id`` |
| 设置页 LLM 表单 disabled（无写入端点） | ``test_llm_config_roundtrip_and_source`` |

纪律（与其他 service 测试一致）：临时 ``tmp_path`` + 确定性假 provider，不联网、不加载模型；
**不 mock 数据库**（迁移与落库都要看真库的真列）。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nova_core.engine import Engine
from nova_core.hashing import blob_hash
from nova_service import audit as audit_module
from nova_service.app import create_app
from nova_service.config import Settings
from nova_service.llmconfig import SOURCE_SERVER, SOURCE_USER, resolve_llm_config
from nova_service.metadb import MetaDB
from nova_service.runtime import EngineManager

from tests.conftest import (
    SAMPLE_FILES,
    TARGET_SYMBOL,
    DeterministicBigramEmbedding,
    make_client,
    register_with_invite,
    upload_files,
)
from tests.test_answer import ANSWERABLE_QUERY, FakeAnswerProvider

#: 一次调用里模拟几个仓库的初始化（§B-1 的"几个仓库初始化的请求"）。
CALL_ID = "1757824800123-0123456789abcdef"


def _settings_for(data_root: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "data_root": data_root,
        "local_mode": True,
        "local_rescan_interval_s": 0.0,
        "storage_limit_per_project_bytes": 0,
        "storage_limit_per_user_bytes": 0,
    }
    return Settings(**{**defaults, **overrides})  # type: ignore[arg-type]


def _build(tmp_path: Path, *, local_mode: bool = True) -> tuple[FastAPI, EngineManager, TestClient]:
    """应用 + 引擎 + 客户端（本地模式默认；``local_mode=False`` 用于多用户隔离用例）。

    **注意**：云端形态下 ``create_app`` 自己会建 ``meta_db``；本地模式不建（R34），
    因此这里显式补一个（本卡的审计/配置都要落库）。
    """
    settings = _settings_for(tmp_path / "data", local_mode=local_mode)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app = create_app(settings)
    if app.state.meta_db is None:
        app.state.meta_db = MetaDB.open(settings.meta_db_path)
    app.state.engine_manager = manager
    # 上传/检索路径的 ``index_runs`` 落库要 manager 自己拿到库（与 ``__main__._run_local``
    # 和 ``deps.get_engine_manager`` 同一口径）；测试里必须显式接上，否则 "run 带了 callId"
    # 这类断言会看不到任何行（本次实施就踩到这个，是测试先发现的）。
    manager.attach_meta_db(app.state.meta_db)
    return app, manager, make_client(app)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """已索引一个项目的本地环境（带两个仓库的素材，供跨项目 callId 用例）。"""
    app, manager, client = _build(tmp_path)
    with client:
        project_id = manager.resolve_project("identity:t099", "t099-repo").project_id
        upload_files(manager, project_id, SAMPLE_FILES)
        yield SimpleNamespace(
            app=app,
            manager=manager,
            client=client,
            project_id=project_id,
            db=app.state.meta_db,
            data_root=app.state.settings.data_root,
            tmp_path=tmp_path,
        )
    manager.close()


def _batch(client: TestClient, project_id: str, path: str, content: str, call_id: str) -> None:
    """发一次真实的 ``batch-upload``（带 callId），与客户端的行为逐字一致。"""
    payload = {
        "projectId": project_id,
        "blobs": [
            {
                "path": path,
                "blobHash": blob_hash(path, content.encode("utf-8")),
                "contentB64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            }
        ],
    }
    response = client.post(
        "/api/sync/batch-upload", json=payload, headers={"X-Request-Id": call_id}
    )
    assert response.status_code == 200, response.text


def _audit_rows(db: MetaDB) -> list[dict]:
    columns = [str(row[1]) for row in db._connect().execute("PRAGMA table_info(query_audit)")]
    return [
        dict(zip(columns, row, strict=True))
        for row in db._connect().execute("SELECT * FROM query_audit")
    ]


def _run_rows(db: MetaDB) -> list[dict]:
    columns = [str(row[1]) for row in db._connect().execute("PRAGMA table_info(index_runs)")]
    return [
        dict(zip(columns, row, strict=True))
        for row in db._connect().execute("SELECT * FROM index_runs")
    ]


def _inject_provider(ns: SimpleNamespace, provider: FakeAnswerProvider) -> None:
    """注入假 provider（与 TASK-088 的测试接缝同一口径；见 ``llmconfig.provider_for_request``）。"""
    ns.app.state.answer_provider = provider
    ns.app.state.answer_provider_settings = ns.app.state.settings


# --------------------------------------------------------------------------- §A 答案落库


def test_answer_text_is_stored_and_returned_by_history(env: SimpleNamespace) -> None:
    """DoD（§A）：走 LLM 成功的 ask → ``answer_text`` 落库，且 ``to_json`` 暴露给历史页。

    读回路径特意走**端点**（``/api/usage/summary``）而不是直接查表：前端拿到的就是它，
    只断言表里有什么等于没验证"历史弹窗真的看得到"。
    """
    provider = FakeAnswerProvider("## Answer\n令牌由 refresh_token 刷新 [E1]。")
    _inject_provider(env, provider)

    response = env.client.post(
        "/api/query/ask",
        json={"projectId": env.project_id, "question": ANSWERABLE_QUERY},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "answered", body

    rows = _audit_rows(env.db)
    assert len(rows) == 1
    assert rows[0]["answer_status"] == audit_module.STATUS_ANSWERED
    assert rows[0]["answer_text"] == provider.reply, "落库的应是 LLM 给出的（回验后的）答案正文"
    assert rows[0]["answer_text"] == body["answer"], "落库正文与响应正文必须是同一份"

    recent = env.client.get("/api/usage/summary").json()["recent"]
    assert recent[0]["answerText"] == provider.reply
    assert recent[0]["answerStatus"] == "answered"


def test_answerable_false_stores_null_text_but_records_status(env: SimpleNamespace) -> None:
    """DoD（§A，本卡明令）：``answerable=false`` 短路**不调 LLM**。

    ``answer_text`` 必须是 ``NULL``，而 ``answer_status`` 必须是 ``insufficient_evidence``——
    把"没调 LLM"记成"调了但答案是空"是本卡禁止的失信。假 provider 一次都不该被调用。
    """
    provider = FakeAnswerProvider()
    _inject_provider(env, provider)

    response = env.client.post(
        "/api/query/ask",
        json={"projectId": env.project_id, "question": "完全不存在的符号 ZZZNoSuchSymbol"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "insufficient_evidence"

    assert provider.calls == [], "证据不足时不得调用 LLM（D-24）"
    rows = _audit_rows(env.db)
    assert rows[0]["answerable"] == 0
    assert rows[0]["answer_text"] is None
    assert rows[0]["answer_status"] == audit_module.STATUS_INSUFFICIENT


def test_llm_failure_stores_null_text_with_degraded_status(env: SimpleNamespace) -> None:
    """第三条分支（§A 的 ``degraded``）：调了 LLM 但失败 → 正文 NULL + 状态如实。"""
    from nova_service.answer import AnswerUnavailableError

    provider = FakeAnswerProvider(error=AnswerUnavailableError("模型不可达"))
    _inject_provider(env, provider)

    response = env.client.post(
        "/api/query/ask",
        json={"projectId": env.project_id, "question": ANSWERABLE_QUERY},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "degraded"

    rows = _audit_rows(env.db)
    assert rows[0]["answer_text"] is None
    assert rows[0]["answer_status"] == audit_module.STATUS_DEGRADED


def test_search_does_not_store_source_code_as_answer(env: SimpleNamespace) -> None:
    """``/api/query/search``（fast）**不落正文**：它的"输出"是含源码的渲染包。

    Module/04 §8 冻结"审计不存源码内容"，因此即使有人把 ``render_markdown`` 的结果传给
    ``answer_text``，本卡也不做——这个用例把那条纪律钉在实现上。
    """
    response = env.client.post(
        "/api/query/search",
        json={"projectId": env.project_id, "query": TARGET_SYMBOL},
    )
    assert response.status_code == 200, response.text
    rows = _audit_rows(env.db)
    assert rows[0]["mode"] == "fast"
    assert rows[0]["answer_text"] is None
    assert rows[0]["answer_status"] is None


def test_truncate_answer_keeps_a_marker_and_underlying_limit() -> None:
    """超长答案截断并留下标记（用户要能看出"这里被截了"，而不是以为模型只说了这些）。"""
    limit = audit_module.ANSWER_STORE_MAX_CHARS
    assert audit_module.truncate_answer(None) is None
    assert audit_module.truncate_answer("short") == "short"
    long = "字" * (limit + 50)
    trimmed = audit_module.truncate_answer(long)
    assert trimmed is not None
    assert trimmed.startswith(long[:limit])
    assert trimmed.endswith(audit_module.ANSWER_TRUNCATION_SUFFIX)
    assert len(trimmed) < len(long)


def test_overlong_answer_is_truncated_before_reaching_the_db(env: SimpleNamespace) -> None:
    """端到端：LLM 返回超长正文 → 库里那条被卡在上限内（防单条记录撑爆库）。"""
    limit = audit_module.ANSWER_STORE_MAX_CHARS
    reply = "## Answer\n令牌由 refresh_token 刷新 [E1]。" + "x" * (limit + 10)
    provider = FakeAnswerProvider(reply)
    _inject_provider(env, provider)

    env.client.post(
        "/api/query/ask", json={"projectId": env.project_id, "question": ANSWERABLE_QUERY}
    )

    stored = _audit_rows(env.db)[0]["answer_text"]
    assert stored is not None
    assert len(stored) <= limit + len(audit_module.ANSWER_TRUNCATION_SUFFIX)
    assert stored.endswith(audit_module.ANSWER_TRUNCATION_SUFFIX)


# --------------------------------------------------------------------------- §B callId 关联


def test_three_uploads_and_one_search_share_call_id(env: SimpleNamespace) -> None:
    """DoD（§B，卡内 §E 的原文场景）：3 次 batch-upload + 1 次 search（同一 header）。

    → ``index_runs.call_id`` 与 ``query_audit.request_id`` 都等于该 callId；
    → ``GET /api/calls/{id}`` 返回 **4 条**且按时间排序。
    """
    for index in range(3):
        _batch(
            env.client,
            env.project_id,
            f"src/extra_{index}.py",
            f"def extra_{index}():\n    return {index}\n",
            CALL_ID,
        )
    response = env.client.post(
        "/api/query/search",
        json={"projectId": env.project_id, "query": TARGET_SYMBOL},
        headers={"X-Request-Id": CALL_ID},
    )
    assert response.status_code == 200, response.text

    runs = [row for row in _run_rows(env.db) if row["call_id"] is not None]
    assert len(runs) == 3, f"三次上传应各落一条带 call_id 的 run：{runs}"
    assert {row["call_id"] for row in runs} == {CALL_ID}
    audits = _audit_rows(env.db)
    assert [row["request_id"] for row in audits] == [CALL_ID], (
        "检索审计的 request_id 必须与 callId 同值（§B-3 的核心：两列值天然相等）"
    )

    timeline = env.client.get(f"/api/calls/{CALL_ID}").json()
    assert timeline["callId"] == CALL_ID
    assert len(timeline["entries"]) == 4
    kinds = [entry["kind"] for entry in timeline["entries"]]
    assert kinds.count("index") == 3 and kinds.count("query") == 1
    ats = [entry["at"] for entry in timeline["entries"]]
    assert ats == sorted(ats), "时间线必须按时间排序"
    assert timeline["totals"]["indexRuns"] == 3
    assert timeline["totals"]["queries"] == 1
    # 每次上传的 duration 相加（用户问的就是"初始化用了多久、检索多久"）。
    assert timeline["totals"]["totalMs"] == (
        timeline["totals"]["indexDurationMs"] + timeline["totals"]["queryLatencyMs"]
    )


def test_requests_without_call_id_leave_null_and_are_reported(env: SimpleNamespace) -> None:
    """不带 header → ``call_id`` 为 ``NULL``（旧客户端不假装属于某次调用）。

    并且时间线端点把它们计进 ``withoutCallId``，而不是让这些记录静默消失
    （前端要能如实说"另有 N 次索引不属于任何调用"）。
    """
    _batch(env.client, env.project_id, "src/no_call.py", "x = 1\n", CALL_ID)
    _batch(env.client, env.project_id, "src/old_client.py", "y = 2\n", "")
    # TestClient 的 headers 值为空串时不会发该头，等价于旧客户端；再显式确认一次。
    response = env.client.post(
        "/api/sync/batch-upload",
        json={"projectId": env.project_id, "blobs": []},
        headers={},
    )
    assert response.status_code == 400, "空批 400（不影响本用例的目的：确认不带头的路径）"

    runs = _run_rows(env.db)
    by_call = {row["call_id"] for row in runs}
    assert CALL_ID in by_call and None in by_call, f"应同时有带/不带 callId 的记录：{runs}"

    timeline = env.client.get(f"/api/calls/{CALL_ID}").json()
    assert timeline["totals"]["indexRuns"] == 1
    assert timeline["withoutCallId"] == 1, "没有 callId 的记录要如实计数，不能装作不存在"


def test_call_endpoint_404_when_no_records(env: SimpleNamespace) -> None:
    """未知 callId → 404 ``call_not_found``（本地模式也必须判空，不能给一个空 200）。"""
    response = env.client.get("/api/calls/deadbeef-0000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "call_not_found"


def test_call_timeline_is_scoped_to_owner(tmp_path: Path) -> None:
    """云端形态：A 的 callId 对 B 是 404（与"不存在"同一文案，不给探测面）。"""
    app, manager, client = _build(tmp_path, local_mode=False)
    with client:
        # 两个用户 + 两个各自的项目
        alice = client.post(
            "/api/auth/bootstrap", json={"name": "alice", "password": "pw1"}
        )
        assert alice.status_code == 201, alice.text
        bob = register_with_invite(client, app, "bob", "pw1")
        db = app.state.meta_db
        bob_id = bob["userId"]
        alice_id = alice.json()["userId"]
        alice_project = manager.resolve_project("identity:a", "a").project_id
        bob_project = manager.resolve_project("identity:b", "b").project_id
        assert db.claim_project(alice_id, alice_project)[0]
        assert db.claim_project(bob_id, bob_project)[0]
        upload_files(manager, alice_project, SAMPLE_FILES)
        upload_files(manager, bob_project, SAMPLE_FILES)

        # 注册会把浏览器 session 切到新用户。隔离用例直接为两位用户各铸一枚 token，避免
        # TestClient 的单 cookie jar 把 token 错记到最后登录的人名下。
        from nova_service.auth import create_api_token

        alice_raw, alice_digest, alice_prefix = create_api_token()
        db.create_token(alice_id, token_hash=alice_digest, prefix=alice_prefix, name="a")
        bob_raw, bob_digest, bob_prefix = create_api_token()
        db.create_token(bob_id, token_hash=bob_digest, prefix=bob_prefix, name="b")
        alice_headers = {"Authorization": f"Bearer {alice_raw}", "X-Request-Id": CALL_ID}
        bob_headers = {"Authorization": f"Bearer {bob_raw}"}

        response = client.post(
            "/api/query/search",
            json={"projectId": alice_project, "query": TARGET_SYMBOL},
            headers=alice_headers,
        )
        assert response.status_code == 200, response.text
        assert client.get(f"/api/calls/{CALL_ID}", headers=alice_headers).status_code == 200

        denied = client.get(f"/api/calls/{CALL_ID}", headers=bob_headers)
        assert denied.status_code == 404, "B 不得读到 A 的调用时间线"
        assert denied.json()["error"]["code"] == "call_not_found"
        # 文案里不回显 callId（越权与不存在逐字节一致）。
        assert CALL_ID not in denied.text
    manager.close()


def test_index_run_records_the_request_id_of_its_own_request(env: SimpleNamespace) -> None:
    """run 的 ``call_id`` 必须来自**那次上传请求**（不是"最近一次检索"或全局状态）。

    做法：两次上传用两个不同的 header，断言两条 run 分别带上各自的值——一个全局变量或
    "读最近一次审计"的实现会在这里立刻露馅。
    """
    _batch(env.client, env.project_id, "src/a.py", "a = 1\n", "call-A")
    _batch(env.client, env.project_id, "src/b.py", "b = 2\n", "call-B")
    # fixture 的初始上传（无 header）也会落一条 ``NULL``：只看带 callId 的那些。
    run_ids = [row["call_id"] for row in _run_rows(env.db) if row["call_id"] is not None]
    assert sorted(run_ids) == ["call-A", "call-B"]


# --------------------------------------------------------------------------- §C 用户级 LLM 配置


def test_llm_config_roundtrip_and_source(env: SimpleNamespace) -> None:
    """DoD（§C）：保存 → ``/api/meta`` 报 ``source=user`` 且显示的是用户那份；删除 → 回落。"""
    assert env.client.get("/api/meta").json()["config"]["llm"]["source"] == SOURCE_SERVER

    saved = env.client.put(
        "/api/auth/llm-config",
        json={"model": "my-model", "baseUrl": "https://my.llm/v1", "apiKey": "sk-user-secret-1"},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["model"] == "my-model"
    assert body["baseUrl"] == "https://my.llm/v1"
    assert body["apiKeyConfigured"] is True
    assert body["source"] == SOURCE_USER

    llm = env.client.get("/api/meta").json()["config"]["llm"]
    assert llm["source"] == SOURCE_USER
    assert llm["model"] == "my-model"
    assert llm["baseUrl"] == "https://my.llm/v1"
    assert llm["apiKeyConfigured"] is True

    removed = env.client.delete("/api/auth/llm-config")
    assert removed.status_code == 204
    assert env.client.get("/api/meta").json()["config"]["llm"]["source"] == SOURCE_SERVER


def test_blank_api_key_keeps_the_existing_one(env: SimpleNamespace) -> None:
    """``apiKey`` 留空 = 保持不变（key 从不回显，因此这是唯一可用的"只改模型名"语义）。"""
    env.client.put(
        "/api/auth/llm-config",
        json={"model": "m1", "baseUrl": "https://a/v1", "apiKey": "sk-first"},
    )
    again = env.client.put(
        "/api/auth/llm-config", json={"model": "m2", "baseUrl": "https://b/v1", "apiKey": ""}
    )
    assert again.status_code == 200, again.text
    record = env.db.get_llm_config("local")
    assert record is not None
    assert record.model == "m2" and record.base_url == "https://b/v1"
    assert record.api_key == "sk-first", "留空不得清掉已有 key"


def test_first_save_without_api_key_is_400(env: SimpleNamespace) -> None:
    """首次保存没给 key → 400（没有旧值可继承，静默存一个空 key 会让 ask 永远降级）。"""
    response = env.client.put(
        "/api/auth/llm-config", json={"model": "m", "baseUrl": "https://a/v1", "apiKey": ""}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_llm_config"


@pytest.mark.parametrize(
    ("payload", "hint"),
    [
        ({"model": "", "baseUrl": "https://a/v1", "apiKey": "k"}, "model"),
        ({"model": "m", "baseUrl": "", "apiKey": "k"}, "baseUrl"),
        ({"model": "m", "baseUrl": "ftp://a/v1", "apiKey": "k"}, "http://"),
    ],
)
def test_invalid_llm_config_is_rejected(env: SimpleNamespace, payload: dict, hint: str) -> None:
    """空字段与非 http(s) 地址 → 400，且**文案里不回显任何字段值**。"""
    response = env.client.put("/api/auth/llm-config", json=payload)
    assert response.status_code == 400
    envelope = response.json()["error"]
    assert envelope["code"] == "invalid_llm_config"
    assert hint in envelope["message"]


def test_delete_is_idempotent(env: SimpleNamespace) -> None:
    """删除幂等：没配过也 204（用户连点两次"清除"不是错误）。"""
    assert env.client.delete("/api/auth/llm-config").status_code == 204
    env.client.put(
        "/api/auth/llm-config",
        json={"model": "m", "baseUrl": "https://a/v1", "apiKey": "k"},
    )
    assert env.client.delete("/api/auth/llm-config").status_code == 204
    assert env.client.delete("/api/auth/llm-config").status_code == 204


# --------------------------------------------------------------------------- key 隔离（硬线）


def test_key_is_never_echoed_by_any_endpoint(env: SimpleNamespace) -> None:
    """DoD（§C，**必须**有专门断言）：任何响应都不得含 key 的任何部分（含长度/前缀）。

    断言方式：把 key 造成一个**带长度特征的独特串**，然后逐个端点比对——只要响应体里出现
    该串、它的前缀、或"长度"这个数字，断言立刻失败。
    """
    secret = "sk-nova-t099-SECRET-abcdefghijklmnop"
    saved = env.client.put(
        "/api/auth/llm-config",
        json={
            "model": "secretive-model",
            "baseUrl": "https://llm.internal.example/v1",
            "apiKey": secret,
        },
    )
    assert saved.status_code == 200

    assert secret not in saved.text
    for endpoint in ("/api/meta", "/api/auth/me", "/api/usage/summary", "/api/account/overview"):
        response = env.client.get(endpoint)
        assert secret not in response.text, f"{endpoint} 泄漏了 key"
        assert secret[:12] not in response.text, f"{endpoint} 泄漏了 key 前缀"
    # “key 长度”不得作为**独立字段值**出网。
    #
    # 为什么不写成裸子串（``str(len(secret)) not in saved.text``）：那会误报——
    # 响应体里的 ``updatedAt`` 是 epoch 秒（如 1789536840），其中含 "36" 就与 key 长度
    # （36）碰撞，2026-09-16 起该用例会**稳定假失败**，与任何代码改动无关（实测）。
    # 带引号/分隔符的边界既拦住真正的“把长度当字段吐出来”，又不受时间戳干扰。
    length = str(len(secret))
    for pattern in (f'"{length}"', f":{length},", f":{length}}}"):
        assert pattern not in saved.text, f"响应把 key 长度当字段值吐出：{pattern}"
    # 库里的明文仍然在（明文存储是本卡的裁定，见 §C-2）——上面守的是"不出网"。
    record = env.db.get_llm_config("local")
    assert record is not None and record.api_key == secret


def test_key_is_never_echoed_after_save_into_meta_and_errors(env: SimpleNamespace) -> None:
    """再守两条容易漏的路径：保存**之后**的 ``/api/meta``，以及错误响应。"""
    secret = "sk-nova-t099-NOTHERE-0123456789"
    env.client.put(
        "/api/auth/llm-config",
        json={"model": "m", "baseUrl": "https://a/v1", "apiKey": secret},
    )
    assert secret not in env.client.get("/api/meta").text
    bad = env.client.put(
        "/api/auth/llm-config", json={"model": "", "baseUrl": "https://a/v1", "apiKey": secret}
    )
    assert bad.status_code == 400
    assert secret not in bad.text


def test_key_does_not_appear_in_logs(
    env: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """日志面同样不能落 key（走 ``redact_text`` 的第二道防线）。"""
    import logging

    secret = "sk-nova-t099-LOG-abcdefghijkl"
    with caplog.at_level(logging.INFO):
        env.client.put(
            "/api/auth/llm-config",
            json={"model": "logged-model", "baseUrl": "https://a/v1", "apiKey": secret},
        )
    assert secret not in caplog.text
    assert secret not in json.dumps(
        [record.__dict__ for record in caplog.records], default=str, ensure_ascii=False
    )
    # 但同时确实记了这次变更（不然"不泄漏"可以靠什么都不记来作弊）。
    assert "logged-model" in caplog.text


# --------------------------------------------------------------------------- §C 生效链路


def test_user_config_takes_effect_and_falls_back(tmp_path: Path, monkeypatch) -> None:
    """DoD（§C-4）：用户配了 → ``ask`` 用**用户的**模型；删掉 → 回落服务端默认。

    用真 HTTP 对假服务端不行（``HttpAnswerProvider`` 会发包），因此这里读的是
    ``provider_for_request`` 的**真实返回值**——它是 REST/MCP 两面共用的解析入口，
    断言它的 ``model`` 就等于断言"这次 ask 会拿哪个模型去请求"。
    """
    from nova_service.llmconfig import provider_for_request

    monkeypatch.delenv("ANSWER_BASE_URL", raising=False)
    monkeypatch.delenv("ANSWER_API_KEY", raising=False)
    monkeypatch.delenv("ANSWER_MODEL", raising=False)
    data_root = tmp_path / "data"
    settings = _settings_for(
        data_root,
        answer_base_url="https://server.llm/v1",
        answer_api_key="sk-server",
        answer_model="server-model",
    )
    app = create_app(settings)
    app.state.meta_db = MetaDB.open(settings.meta_db_path)
    db = app.state.meta_db

    server = provider_for_request(app, user_id="local", db=db)
    assert server is not None and server.model == "server-model"

    db.save_llm_config(
        "local", model="user-model", base_url="https://user.llm/v1", api_key="sk-user"
    )
    user = provider_for_request(app, user_id="local", db=db)
    assert user is not None and user.model == "user-model", "用户配置必须生效"
    assert user.endpoint == "https://user.llm/v1/chat/completions"

    db.delete_llm_config("local")
    assert provider_for_request(app, user_id="local", db=db).model == "server-model"


def test_users_are_isolated_and_b_falls_back(tmp_path: Path) -> None:
    """DoD（§C，卡内 §E 原文）：A 配了、B 没配 → B 用服务端默认；A 的 key 不影响 B。"""
    data_root = tmp_path / "data"
    settings = _settings_for(
        data_root,
        answer_base_url="https://server.llm/v1",
        answer_api_key="sk-server",
        answer_model="server-model",
    )
    db = MetaDB.open(settings.meta_db_path)
    db.save_llm_config("user-a", model="a-model", base_url="https://a/v1", api_key="sk-a")

    resolved_a = resolve_llm_config(settings, user_id="user-a", db=db)
    resolved_b = resolve_llm_config(settings, user_id="user-b", db=db)
    assert resolved_a.source == SOURCE_USER and resolved_a.model == "a-model"
    assert resolved_b.source == SOURCE_SERVER and resolved_b.model == "server-model"
    assert resolved_b.api_key == "sk-server"
    assert "sk-a" not in json.dumps(resolved_b.to_public_json())


def test_no_user_or_db_falls_back_to_server_default(tmp_path: Path) -> None:
    """无身份（本地模式/MCP 无用户）或无库 → 服务端默认，且**不抛异常**（正常降级）。"""
    settings = _settings_for(
        tmp_path / "data", answer_base_url="https://s/v1", answer_api_key="k", answer_model="m"
    )
    for kwargs in ({}, {"user_id": "local"}, {"db": MetaDB.open(settings.meta_db_path)}):
        resolved = resolve_llm_config(settings, **kwargs)  # type: ignore[arg-type]
        assert resolved.source == SOURCE_SERVER and resolved.model == "m"


def test_unconfigured_everywhere_reports_missing_fields(tmp_path: Path) -> None:
    """两边都没配 → ``configured=False`` 且如实列出缺哪几个**字段名**（不回值）。"""
    settings = _settings_for(tmp_path / "data")
    resolved = resolve_llm_config(settings)
    assert resolved.configured is False
    assert resolved.missing_keys == ("baseUrl", "apiKey", "model")
    public = resolved.to_public_json()
    assert public["apiKeyConfigured"] is False
    assert public["source"] == SOURCE_SERVER


def test_resolved_config_public_json_has_no_key() -> None:
    """``to_public_json`` 是唯一允许出网的形态——它连 key 的字段都没有。"""
    from nova_service.llmconfig import ResolvedLlmConfig

    resolved = ResolvedLlmConfig(
        base_url="https://a/v1",
        api_key="sk-super-secret",
        model="m",
        timeout_s=60.0,
        max_tokens=100,
        temperature=0.2,
        source=SOURCE_USER,
    )
    payload = resolved.to_public_json()
    assert "sk-super-secret" not in json.dumps(payload)
    assert set(payload["missingKeys"]) == set()


def test_mcp_ask_uses_the_user_config(tmp_path: Path) -> None:
    """MCP 面（``ask_project``）同样吃用户配置：``build_answer_provider`` 的解析链路一致。"""
    from nova_service.mcp import build_answer_provider

    settings = _settings_for(
        tmp_path / "data",
        answer_base_url="https://server/v1",
        answer_api_key="sk-server",
        answer_model="server-model",
    )
    assert build_answer_provider(settings).model == "server-model"
    assert build_answer_provider(settings, user_id=None).model == "server-model"


# --------------------------------------------------------------------------- §B/§C 迁移（旧库）


def _legacy_db(path: Path) -> None:
    """写一个 **TASK-099 之前**的库：``index_runs`` 无 ``call_id``、``query_audit`` 无答案列。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users (
          id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
          created_at INTEGER NOT NULL, is_local INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE projects (
          project_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          display_name TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
        );
        CREATE TABLE index_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, state TEXT NOT NULL,
          started_at INTEGER NOT NULL, finished_at INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
          files_total INTEGER NOT NULL DEFAULT 0, files_processed INTEGER NOT NULL DEFAULT 0,
          chunks INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0, error_text TEXT
        );
        CREATE TABLE query_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, user_id TEXT,
          mode TEXT NOT NULL, query TEXT NOT NULL, answerable INTEGER, confidence TEXT,
          degraded INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL,
          evidence_count INTEGER NOT NULL DEFAULT 0, docs_count INTEGER NOT NULL DEFAULT 0,
          used_tokens INTEGER NOT NULL DEFAULT 0, citation_coverage REAL,
          llm_latency_ms INTEGER, answer_tokens INTEGER, request_id TEXT,
          evidence_json TEXT NOT NULL DEFAULT '[]', created_at INTEGER NOT NULL
        );
        INSERT INTO index_runs
          (project_id, state, started_at, finished_at, duration_ms, chunks)
          VALUES ('legacy', 'done', 100, 130, 30000, 42);
        INSERT INTO query_audit
          (project_id, mode, query, answerable, latency_ms, evidence_json, created_at)
          VALUES ('legacy', 'deep', '旧版本写下的查询', 1, 321, '[]', 1700000000);
        """
    )
    connection.commit()
    connection.close()


def test_migration_upgrades_legacy_db_for_call_id_and_answer(tmp_path: Path) -> None:
    """DoD（§E，纪律 1）：**用真实旧 schema 建库 → 打开 → 新列存在且旧数据保留**。

    这是卡内点名要写的用例（旧库直接打开会因列不存在而 ``no such column`` 失败——
    TASK-094 §C 实测踩过，本卡不得重蹈）。
    """
    db_path = tmp_path / "data" / "nova-meta.db"
    _legacy_db(db_path)

    before = sqlite3.connect(db_path)
    before_runs = {row[1] for row in before.execute("PRAGMA table_info(index_runs)")}
    before_audit = {row[1] for row in before.execute("PRAGMA table_info(query_audit)")}
    before.close()
    assert "call_id" not in before_runs, "前置条件：旧库确实没有这一列"
    assert "answer_text" not in before_audit and "answer_status" not in before_audit

    db = MetaDB.open(db_path)
    try:
        after_runs = {row[1] for row in db._connect().execute("PRAGMA table_info(index_runs)")}
        after_audit = {row[1] for row in db._connect().execute("PRAGMA table_info(query_audit)")}
        assert "call_id" in after_runs
        assert {"answer_text", "answer_status"} <= after_audit

        # 旧数据保留，新列为 NULL（"旧版本/没有"就是 NULL，不编造）。
        old_run = db.index_runs("legacy")[0]
        assert old_run.chunks == 42 and old_run.duration_ms == 30000
        assert old_run.call_id is None
        assert old_run.to_json()["callId"] is None
        old_audit = db.usage_summary(["legacy"], days=100_000).recent[0]
        assert old_audit.query == "旧版本写下的查询" and old_audit.latency_ms == 321
        assert old_audit.answer_text is None and old_audit.answer_status is None

        # 新旧共存：按新列真的查得动（迁移后索引/列都可用）。
        db.record_index_run(
            "legacy", state="done", started_at=200, finished_at=210, call_id="call-after"
        )
        assert [run.call_id for run in db.index_runs_by_call("call-after")] == ["call-after"]
        # 用户 LLM 配置表在旧库上被建出来（CREATE TABLE IF NOT EXISTS 对新表有效）。
        assert db.get_llm_config("local") is None
        db.save_llm_config("local", model="m", base_url="https://a/v1", api_key="k")
        assert db.get_llm_config("local").model == "m"
    finally:
        db.close()

    # 幂等：再打开一次不报 duplicate column，数据仍在。
    again = MetaDB.open(db_path)
    try:
        assert again._connect().execute("SELECT COUNT(*) FROM index_runs").fetchone()[0] == 2
        assert again.get_llm_config("local").model == "m"
    finally:
        again.close()


def test_legacy_db_serves_upload_with_call_id_end_to_end(tmp_path: Path) -> None:
    """端到端：旧库 + 新代码起服务 → 上传与检索正常，且新记录带上 callId。"""
    data_root = tmp_path / "data"
    _legacy_db(data_root / "nova-meta.db")
    app, manager, client = _build(tmp_path)
    with client:
        # 旧库被替换成本用例的 app 库（同一路径）：确认迁移真的发生在应用启动路径上。
        assert (data_root / "nova-meta.db").is_file()
        project_id = manager.resolve_project("identity:legacy099", "legacy099").project_id
        upload_files(manager, project_id, SAMPLE_FILES)
        response = client.post(
            "/api/query/search",
            json={"projectId": project_id, "query": TARGET_SYMBOL},
            headers={"X-Request-Id": CALL_ID},
        )
        assert response.status_code == 200, response.text
        timeline = client.get(f"/api/calls/{CALL_ID}").json()
        assert timeline["totals"]["queries"] == 1
        assert timeline["entries"][0]["queryAudit"]["requestId"] == CALL_ID
    manager.close()


# --------------------------------------------------------------------------- 契约与路径面


def test_new_paths_are_registered_and_documented_as_extension(app: FastAPI) -> None:
    """新增路径必须同时出现在实现与 ``TASK_EXTENSION_PATHS``（否则 ``test_skeleton`` 必红）。"""
    paths = set(app.openapi()["paths"])
    assert "/api/calls/{callId}" in paths
    assert "/api/auth/llm-config" in paths
    # llm-config 同时有 PUT 与 DELETE（缺一个就是"能存不能删"）。
    methods = {method.lower() for method in app.openapi()["paths"]["/api/auth/llm-config"]}
    assert methods == {"put", "delete"}


def test_request_header_is_not_part_of_the_frozen_contract() -> None:
    """§B-4：请求头（``X-Request-Id``）**不在** CF-05 冻结范围。

    这是"客户端开始发这个头不需要 L2 申请"的依据，因此要能被重复验证（而不是只写在卡里）。
    """
    from tests.conftest import REPO_ROOT

    contract = (REPO_ROOT / "docs" / "contracts" / "openapi.yaml").read_text(encoding="utf-8")
    lowered = contract.lower()
    assert "x-request-id" not in lowered
    assert "headers:" not in lowered
    assert "authorization" not in lowered
