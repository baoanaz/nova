"""TASK-084 验收：查询审计真的接上了吗（``/api/usage/**`` 从恒空到有数）。

本文件是 **TASK-064 「查询审计与用量端点」的补做验收**。TASK-064 交付了
``query_audit`` 表与 ``usage_summary`` 聚合，但 ``audit.py`` 没建、``record_query()``
全仓零调用，于是 ``/api/usage/**`` 恒为空（编排者实测，2026-09-13）。TASK-084 把写入侧
接上，并在本文件逐条钉住。

覆盖（TASK-084 §C 的清单 + TASK-064 DoD 的可测项）：

- 成功 search → ``total=1, succeeded=1``，``latencyMs`` 是**真实测量值**；
- ``answerable=false`` → ``insufficient=1``（与 ``failed`` 分开计数）；
- **审计失败不影响检索**：``metadb.record_query`` 抛异常 → ``/api/query/search`` 仍 200；
- **业务失败也要落**（``_require_index`` 的 409/500、参数校验的 400）→ ``failed`` 计数；
- ``ask`` 的条目 ``citationCoverage`` 为 **null**（不是 0）；
- **不存源码内容**：库里的 ``evidence_json`` 无代码片段字段；
- **脱敏**：把 API key / base_url 形态的串放进 query 与异常 → 库里查不到；
- 保留策略：超过 1000 条裁剪最旧的；
- ``days`` 窗口过滤生效；
- 服务重启后统计仍在（落库而非内存）；
- ``/api/usage/summary``、``/api/usage/projects/{id}``、``/api/account/overview`` 三处一致。

纪律：一律用 ``tmp_path`` 下的临时 data_root + 确定性假 provider（不联网、不加载模型）。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nova_core.engine import Engine
from nova_service import audit
from nova_service.app import create_app
from nova_service.config import Settings
from nova_service.metadb import QUERY_AUDIT_KEEP, MetaDB
from nova_service.runtime import EngineManager

from tests.conftest import (
    SAMPLE_FILES,
    SAMPLE_MODULE_PATH,
    DeterministicBigramEmbedding,
    make_client,
    upload_files,
)

#: 必然 "有答案" 的查询（假 provider 下 answerable=true，见 test 内断言）。
ANSWERABLE_QUERY = "TokenService.refresh_token"
#: 必然 "证据不足" 的查询（假 provider 下 answerable=false）。
INSUFFICIENT_QUERY = "zzzz qqqq 与语料完全无关的主题"
#: 真实 key 形态的敏感串（脱敏断言用；与 ``test_error_mapping`` 同一形态）。
FAKE_API_KEY = "nova_SHOULD-NOT-LEAK-abc123XYZ"
#: 开源生态里常见形态的裸 key（没有 ``nova_`` 前缀，靠键名规则脱敏）。
FAKE_OPENAI_KEY = "sk-live-SHOULD-NOT-LEAK-abc123"
#: 内网 endpoint 形态（说明：``redact_text`` 不脱敏 URL，只有 ``api_key=`` 这类键值才脱）。
FAKE_ENDPOINT = "https://internal-embedding.example.com/v1"


# --------------------------------------------------------------------------- 夹具


def _make(tmp_path: Path, **overrides: object) -> tuple[FastAPI, EngineManager, TestClient]:
    """构造应用 + EngineManager + TestClient（每个测试独立 data_root，本地模式）。"""
    defaults: dict[str, object] = {
        "data_root": tmp_path / "data",
        "local_mode": True,
        "local_rescan_interval_s": 0.0,  # 关掉懒重扫：审计耗时口径才稳定
    }
    settings = Settings(**{**defaults, **overrides})  # type: ignore[arg-type]
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app = create_app(settings)
    # 本地模式默认不建库（R34 无账户体系），但审计与索引历史都需要它：与
    # ``nova-service local`` 的实际启动路径一致（__main__ 里同样显式注入）。
    app.state.meta_db = MetaDB.open(settings.meta_db_path)
    app.state.engine_manager = manager
    return app, manager, make_client(app)


@pytest.fixture
def usage(tmp_path: Path):
    """已建好 MetaDB 的应用 + 一个已索引 2 个文件的项目。"""
    app, manager, client = _make(tmp_path)
    with client:
        project_id = manager.resolve_project("identity:audit", "audit-repo").project_id
        upload_files(manager, project_id, SAMPLE_FILES)
        yield SimpleNamespace(
            app=app,
            manager=manager,
            client=client,
            project_id=project_id,
            db=app.state.meta_db,
            tmp_path=tmp_path,
        )
    manager.close()


def _search(ns: SimpleNamespace, query: str, **extra: object) -> dict:
    response = ns.client.post(
        "/api/query/search", json={"projectId": ns.project_id, "query": query, **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ask(ns: SimpleNamespace, question: str) -> dict:
    response = ns.client.post(
        "/api/query/ask", json={"projectId": ns.project_id, "question": question}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _summary(ns: SimpleNamespace, days: int | None = None) -> dict:
    suffix = f"?days={days}" if days is not None else ""
    response = ns.client.get(f"/api/usage/summary{suffix}")
    assert response.status_code == 200, response.text
    return response.json()


def _rows(ns: SimpleNamespace) -> list[sqlite3.Row]:
    return ns.db._connect().execute("SELECT * FROM query_audit ORDER BY id").fetchall()


# --------------------------------------------------------------------------- 成功后落库


def test_successful_search_is_recorded_with_real_measurements(usage: SimpleNamespace) -> None:
    """一次成功 search → ``total=1, succeeded=1``，且各字段是**真实值**而非占位。"""
    before = _summary(usage)
    assert before["total"] == 0, "提问前必须是空的（本卡要修的正是这个恒 0）"

    body = _search(usage, ANSWERABLE_QUERY)
    meta = body["meta"]
    assert meta["answerable"] is True, "语料前提：这个查询应当有答案"

    summary = _summary(usage)
    assert summary["total"] == 1
    assert summary["succeeded"] == 1
    assert summary["insufficient"] == 0
    assert summary["failed"] == 0

    record = summary["recent"][0]
    assert record["query"] == ANSWERABLE_QUERY
    assert record["mode"] == audit.MODE_SEARCH == "fast"
    assert record["answerable"] is True
    assert record["confidence"] == meta["confidence"]
    assert record["evidenceCount"] == meta["evidenceCount"]
    assert record["docsCount"] == meta["docsCount"]
    assert record["usedTokens"] == meta["budget"]["usedTokens"]
    assert record["citationCoverage"] is None, "尚未测量不是 0（TASK-064 §A）"


def test_latency_is_actually_measured_not_hardcoded(usage: SimpleNamespace) -> None:
    """``latencyMs`` 是真实测量值：> 0，且与响应耗时同量级（不是写死的常量）。

    做法：先跑一次拿基线，再用可注入的延迟把 ``EngineManager.search`` 拖慢 200ms，
    断言**增量** ≈ 200ms。这比 "``> 0``" 强——写死 ``latency_ms=1`` 也能过 ``> 0``。
    """
    _search(usage, ANSWERABLE_QUERY)
    baseline = _summary(usage)["recent"][0]["latencyMs"]
    assert baseline > 0, "真实测量值必须 > 0（写死的 0 立刻暴露）"

    injected_ms = 200
    original = usage.manager.search

    def slow_search(*args: object, **kwargs: object) -> object:
        time.sleep(injected_ms / 1000.0)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    usage.manager.search = slow_search  # type: ignore[method-assign]
    try:
        _search(usage, ANSWERABLE_QUERY, maxTokens=8000)
    finally:
        usage.manager.search = original  # type: ignore[method-assign]

    slowed = int(_summary(usage)["recent"][0]["latencyMs"])
    assert slowed >= baseline + injected_ms * 0.8, (
        f"注入 {injected_ms}ms 延迟后耗时必须跟着长：baseline={baseline} slowed={slowed}"
    )
    assert slowed < 60_000, "耗时单位是毫秒（若写成秒不会超过这个数）"


def test_insufficient_evidence_is_counted_separately(usage: SimpleNamespace) -> None:
    """``answerable=false`` → ``insufficient=1``（不是 ``failed``，也不是 ``succeeded``）。"""
    _search(usage, ANSWERABLE_QUERY)
    _search(usage, INSUFFICIENT_QUERY)

    summary = _summary(usage)
    assert summary["total"] == 2
    assert summary["succeeded"] == 1
    assert summary["insufficient"] == 1
    assert summary["failed"] == 0, "证据不足 ≠ 失败（两件事分开计数）"
    # confidence 分布取自真实值（有答案 medium/high，证据不足 low）。
    assert summary["confidenceDistribution"].get("low") == 1
    assert summary["topQueries"] and summary["topQueries"][0]["query"] == ANSWERABLE_QUERY


def test_ask_records_deep_mode_with_null_citation_coverage(usage: SimpleNamespace) -> None:
    """**未配置 LLM** 的 ``ask``：``mode="deep"``、``degraded=true``、``citationCoverage=null``。

    TASK-088 起：配置了 ``ANSWER_*`` 才会走 LLM（那时三项观测值才有数）；本用例构造的应用
    没有 LLM 配置，因此 ``citationCoverage`` / ``llmLatencyMs`` / ``answerTokens`` 均为 ``null``
    ——"没测过"不许填 0 冒充（TASK-064 §A）。
    """
    _ask(usage, ANSWERABLE_QUERY)

    summary = _summary(usage)
    assert summary["total"] == 1
    assert summary["citationCoverageAvg"] is None, "没走 LLM 时恒为 null（不许填 0 冒充）"
    record = summary["recent"][0]
    assert record["mode"] == audit.MODE_ASK == "deep"
    assert record["degraded"] is True, "未配置 LLM → D-26 降级包，如实记 degraded"
    assert record["answerable"] is True, "降级的是\"没有 LLM 总结\"，检索本身仍有答案"
    assert record["llmLatencyMs"] is None, "TASK-088 §E：未调 LLM 就没有耗时"
    assert record["answerTokens"] is None


# --------------------------------------------------------------------------- 失败路径


def test_business_failure_is_recorded(usage: SimpleNamespace) -> None:
    """**业务失败也要落**：空索引项目上检索 → 409，且 ``failed`` 计数 +1（TASK-084 §B）。"""
    empty = usage.manager.resolve_project("identity:empty", "empty-repo").project_id

    response = usage.client.post(
        "/api/query/search", json={"projectId": empty, "query": ANSWERABLE_QUERY}
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "index_in_progress"

    summary = _summary(usage)
    assert summary["total"] == 1, "失败也要落一条（否则\"失败次数\"恒为 0 就是谎话）"
    assert summary["failed"] == 1
    assert summary["succeeded"] == 0 and summary["insufficient"] == 0
    record = summary["recent"][0]
    assert record["answerable"] is None, "没走到判定那一步 → null（不是 False）"
    assert record["degraded"] is True


def test_validation_failure_is_recorded_even_without_project(usage: SimpleNamespace) -> None:
    """**校验失败也要落**：空白 query（400）时连 projectId 都没解析出来，仍记一条。"""
    response = usage.client.post(
        "/api/query/search", json={"projectId": usage.project_id, "query": "   "}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_query"

    summary = _summary(usage)
    assert summary["total"] == 1
    assert summary["failed"] == 1
    assert summary["recent"][0]["projectId"] == usage.project_id


def test_unexpected_exception_is_recorded_and_still_500(usage: SimpleNamespace) -> None:
    """未预期异常：审计记一条，**响应语义不变**（仍是 500 ``internal_error``）。"""
    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("模拟引擎崩溃")

    usage.manager.search = boom  # type: ignore[method-assign]
    response = usage.client.post(
        "/api/query/search", json={"projectId": usage.project_id, "query": ANSWERABLE_QUERY}
    )
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "internal_error"

    summary = _summary(usage)
    assert summary["total"] == 1
    assert summary["failed"] == 1


def test_audit_failure_does_not_break_retrieval(
    usage: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**核心纪律（卡内必须有）**：``metadb.record_query`` 抛异常 → ``search`` 仍 200。

    审计是旁路：记不上账不能让用户拿不到检索结果。
    """
    def exploding_record_query(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(usage.db, "record_query", exploding_record_query)

    body = _search(usage, ANSWERABLE_QUERY)
    assert body["markdown"] and body["meta"]["evidenceCount"] >= 1, "检索结果完好无损"
    assert _rows(usage) == [], "审计确实失败了（没写进去），但请求没受影响"

    # 失败路径同样不得传染（409 分支也走 audit）。
    empty = usage.manager.resolve_project("identity:empty2", "empty2").project_id
    failed = usage.client.post(
        "/api/query/search", json={"projectId": empty, "query": ANSWERABLE_QUERY}
    )
    assert failed.status_code == 409, "审计炸了也不能把 409 变成 500"


def test_ask_audit_failure_does_not_break_retrieval(
    usage: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ask`` 的同一纪律：审计抛异常时 ``ask`` 仍 200 + 降级包。"""
    monkeypatch.setattr(
        usage.db,
        "record_query",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("nope")),
    )
    body = _ask(usage, ANSWERABLE_QUERY)
    assert body["status"] == "degraded"
    assert body["answer"]


def test_audit_without_meta_db_is_silently_skipped(tmp_path: Path) -> None:
    """元数据库不可用（本地模式默认不建库）→ 审计静默跳过，检索照常。"""
    app, manager, client = _make(tmp_path)
    app.state.meta_db = None
    with client:
        project_id = manager.resolve_project("identity:nodb", "nodb").project_id
        upload_files(manager, project_id, SAMPLE_FILES)
        response = client.post(
            "/api/query/search", json={"projectId": project_id, "query": ANSWERABLE_QUERY}
        )
        assert response.status_code == 200, "没有元数据库时检索必须照常"
    manager.close()


# --------------------------------------------------------------------------- 脱敏与隐私


def test_no_source_content_is_stored(usage: SimpleNamespace) -> None:
    """**不存源码内容**：``evidence_json`` 只有 ``id/path/lines/tier/score``（04 §8 冻结）。"""
    _search(usage, ANSWERABLE_QUERY, includePack=True)

    rows = _rows(usage)
    assert len(rows) == 1
    import json

    evidence = json.loads(rows[0]["evidence_json"])
    assert evidence, "必须有证据元数据（否则统计没有意义）"
    # TASK-108：字段集扩展了 symbol/group/reason（历史页要展示工具返回的**结构**：
    # 分组 / 符号名 / 召回依据）。仍然**不含 content**（源码正文）——那是 04 §8 的红线。
    for item in evidence:
        assert set(item) == {
            "id",
            "path",
            "lines",
            "tier",
            "score",
            "symbol",
            "group",
            "reason",
        }, item
        assert "content" not in item
    # 证据里的源码正文（content）绝不在库里：拿一个只可能出现在代码正文的串来钉。
    assert "return \"old\"" not in rows[0]["evidence_json"]
    assert SAMPLE_MODULE_PATH in rows[0]["evidence_json"], "路径是元数据，应当保留"


def test_secrets_are_redacted_before_storage(usage: SimpleNamespace) -> None:
    """**脱敏**：query 文本与失败摘要都不含 API key（在**落库前**完成，不是只在日志层）。

    用户完全可能把 key 粘进查询框；断言直接查库（不是查日志）。
    """
    _search(usage, f"为什么 {FAKE_API_KEY} 报错？endpoint={FAKE_ENDPOINT}")
    _search(usage, f"sk 形态也试一下 {FAKE_OPENAI_KEY} 与 {FAKE_OPENAI_KEY}")

    for row in _rows(usage):
        assert FAKE_API_KEY not in row["query"], "裸 nova_ key 不得落库"
        assert FAKE_OPENAI_KEY not in row["query"], "裸 sk- key 不得落库"
        assert "***" in row["query"], f"脱敏占位符应当在：{row['query']}"
    assert _rows(usage)[0]["query"].count("***") >= 1

    # 键值形态（provider 报错里最常见的形态）同样不得漏。
    keyed = audit.redact_query_text(f"api_key={FAKE_OPENAI_KEY} token={FAKE_API_KEY}")
    assert FAKE_OPENAI_KEY not in keyed and FAKE_API_KEY not in keyed

    # 失败路径的 reason 走日志（不落库），脱敏走的是同一个 :func:`audit.redact_query_text`。
    secret_reason = f"ApiNetworkError: api_key={FAKE_OPENAI_KEY}（endpoint={FAKE_ENDPOINT}）"
    redacted = audit.redact_query_text(secret_reason)
    assert FAKE_OPENAI_KEY not in redacted
    assert "***" in redacted


# --------------------------------------------------------------------------- 保留策略与窗口


def test_retention_keeps_only_the_newest_records(usage: SimpleNamespace) -> None:
    """保留策略：每项目最近 :data:`QUERY_AUDIT_KEEP` 条，超出裁剪**最旧**的。"""
    db = usage.db
    # 直接写库造数据（比跑 1000+ 次检索快得多，且测的是保留逻辑本身）。
    inserted = QUERY_AUDIT_KEEP + 5
    for index in range(inserted):
        db.record_query(
            project_id=usage.project_id,
            mode="fast",
            query=f"q{index}",
            latency_ms=1,
            answerable=True,
            confidence="high",
            now=1_700_000_000 + index,  # 单调递增：id 与 created_at 同序
        )

    rows = _rows(usage)
    assert len(rows) == QUERY_AUDIT_KEEP, f"应当裁剪到 {QUERY_AUDIT_KEEP} 条"
    queries = {str(row["query"]) for row in rows}
    assert "q0" not in queries, "最旧的被裁掉"
    assert f"q{inserted - 1}" in queries, "最新的留下"
    # 另一个项目的历史不受影响（裁剪按项目隔离）。
    other = usage.manager.resolve_project("identity:other", "other").project_id
    db.record_query(
        project_id=other, mode="fast", query="other-q", latency_ms=1, answerable=True
    )
    assert len(_rows(usage)) == QUERY_AUDIT_KEEP + 1


def test_days_window_filters_old_records(usage: SimpleNamespace) -> None:
    """``?days=30`` 不包含 40 天前的记录；``?days=90`` 包含。"""
    now = int(time.time())
    usage.db.record_query(
        project_id=usage.project_id, mode="fast", query="old", latency_ms=5,
        answerable=True, now=now - 40 * 86400,
    )
    usage.db.record_query(
        project_id=usage.project_id, mode="fast", query="recent", latency_ms=5,
        answerable=True, now=now - 86400,
    )

    last_30 = _summary(usage, days=30)
    assert last_30["total"] == 1
    assert last_30["recent"][0]["query"] == "recent"
    assert _summary(usage, days=90)["total"] == 2


def test_stats_survive_a_restart(usage: SimpleNamespace, tmp_path: Path) -> None:
    """**服务重启后统计仍在**（落库而非内存）：换一个进程级 MetaDB 实例再查。"""
    _search(usage, ANSWERABLE_QUERY)

    fresh = MetaDB.open(usage.app.state.settings.meta_db_path)
    summary = fresh.usage_summary([usage.project_id], days=30)
    assert summary.total == 1, "从磁盘重开也必须有数据"
    assert summary.recent[0].query == ANSWERABLE_QUERY
    fresh.close()


# --------------------------------------------------------------------------- 三个读取口一致


def test_usage_and_overview_endpoints_agree(usage: SimpleNamespace) -> None:
    """``/api/usage/summary``、``/api/usage/projects/{id}``、``/api/account/overview`` 一致。"""
    _search(usage, ANSWERABLE_QUERY)
    _search(usage, INSUFFICIENT_QUERY)

    summary = _summary(usage)
    per_project = usage.client.get(f"/api/usage/projects/{usage.project_id}")
    assert per_project.status_code == 200, per_project.text
    per_project = per_project.json()
    overview = usage.client.get("/api/account/overview").json()

    assert summary["total"] == per_project["total"] == overview["usage"]["total"] == 2
    assert per_project["projectId"] == usage.project_id
    assert per_project["days"] == 30
    assert summary["succeeded"] == overview["usage"]["succeeded"] == 1
    assert summary["insufficient"] == overview["usage"]["insufficient"] == 1
    assert per_project["citationCoverageAvg"] is None
    assert per_project["recent"][0]["queryId"] == summary["recent"][0]["queryId"]


# --------------------------------------------------------------------------- audit 单元面


def test_evidence_meta_field_set_is_frozen() -> None:
    """``evidence`` 落库字段集是冻结的（04 §8）：只增不改，且不含正文。

    TASK-108 新增 ``symbol``/``group``/``reason``（历史页展示工具返回的结构）。
    """
    assert tuple(audit.evidence_field_names()) == (
        "id",
        "path",
        "lines",
        "tier",
        "score",
        "symbol",
        "group",
        "reason",
    )
    assert "content" not in audit.evidence_field_names(), "绝不存源码正文"


def test_record_query_without_db_is_noop() -> None:
    """``db=None`` → 静默跳过（本地模式默认不建库的路径）。"""
    audit.record_query(None, project_id="p", mode="fast", query="q", pack=None, latency_ms=1)
    audit.record_query_error(None, project_id="p", mode="fast", query="q", latency_ms=1, reason="r")


def test_redact_query_text_also_masks_bare_keys() -> None:
    """裸 key 兜底：``redact_text`` 的既有规则不够（它只管 ``api_key=xxx`` 这类键值）。"""
    assert audit.redact_query_text(FAKE_API_KEY) == "***"
    assert audit.redact_query_text(f"token {FAKE_OPENAI_KEY} 报错") == "token *** 报错"
    # 普通中文/英文查询不受影响（不能把正常查询打成 ***）。
    assert audit.redact_query_text("令牌过期后在哪里刷新") == "令牌过期后在哪里刷新"
    assert audit.redact_query_text("refresh_token 怎么用") == "refresh_token 怎么用"


def test_audit_records_are_scoped_by_project(usage: SimpleNamespace) -> None:
    """审计按项目隔离：另一个项目的查询不污染本项目统计。"""
    other = usage.manager.resolve_project("identity:other-project", "other").project_id
    _search(usage, ANSWERABLE_QUERY)
    usage.client.post(
        "/api/query/search", json={"projectId": other, "query": "   "}
    )  # 400，落在 other 上

    mine = usage.client.get(f"/api/usage/projects/{usage.project_id}").json()
    theirs = usage.client.get(f"/api/usage/projects/{other}").json()
    assert mine["total"] == 1 and mine["failed"] == 0
    assert theirs["total"] == 1 and theirs["failed"] == 1
