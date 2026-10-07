"""TASK-032 验收：``/api/query/search`` 与 ``/api/query/ask``（含 CF-03 校验与降级包）。

TASK-087 追加：``answerable=false`` → ``status="insufficient_evidence"`` 的结构化证据不足包
（D-24 短路）与它的审计落库；``answerable=true`` 的行为保持不变（降级包，TASK-088 接 LLM）。

TASK-088 改动（本文件在 TASK-088 交付物清单之外，属最小必要同步，已在执行记录里登记）：
``ask`` 已接 LLM，降级文案由旧文案（"Phase 3 尚未接入"——接入后就成了假话）改为两条
按原因的诚实说明；因此 :func:`test_ask_returns_degraded_package_not_500` 的断言跟着改语义：
**未配置**（本测试的夹具环境）→ 200 + ``status="degraded"`` + 未配置说明，绝不 500。

两卡合并后（2026-09-14）的实际分支顺序：``answerable=false`` → 短路包（不调 LLM）；
否则未配置 → 降级包；调用失败 → 降级包；成功 → ``status="answered"``。
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nova_core.engine import Engine
from nova_core.types import ChangeSet
from nova_service.app import create_app
from nova_service.config import Settings
from nova_service.metadb import MetaDB
from nova_service.packmeta import DECISION_SUMMARY_LIMIT, meta_field_names
from nova_service.routers.query import (
    DEGRADED_NOTICE,
    INSUFFICIENT_NOTICE,
    MAX_MAX_TOKENS,
    MAX_QUERY_CHARS,
)
from nova_service.runtime import EngineManager

from tests.conftest import (
    REPO_ROOT,
    SAMPLE_DOC_PATH,
    SAMPLE_FILES,
    SAMPLE_MODULE_PATH,
    TARGET_SYMBOL,
    DeterministicBigramEmbedding,
    make_client,
    upload_files,
)

#: 一份必然"缺失"的查询场景用不到的语料路径（见 test_missing_evidence_*）。
MISSING_SYMBOL = TARGET_SYMBOL
#: 与任何上传内容都不重叠的自然语言问题（用于空索引/校验类断言）。
UNKNOWN_PROJECT = "0123456789abcdef"

#: 必然 "证据不足" 的问题（与语料无任何共识命中；TASK-087 §B 的短路分支入口）。
#: 与 ``service/tests/test_usage_api.py`` 的同名常量同一形态（假 provider 下 answerable=false）。
INSUFFICIENT_QUESTION = "zzzz qqqq 与语料完全无关的主题"


@pytest.fixture
def indexed(engine_manager: EngineManager, project_id: str) -> str:
    """已上传并索引 2 个文件（含 1 个 markdown）的项目。"""
    upload_files(engine_manager, project_id, SAMPLE_FILES)
    return project_id


def _search(client: TestClient, project_id: str, query: str, **extra: object) -> dict:
    response = client.post(
        "/api/query/search", json={"projectId": project_id, "query": query, **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- search


def test_search_returns_rendered_markdown_with_line_numbers(
    client: TestClient, indexed: str
) -> None:
    body = _search(client, indexed, TARGET_SYMBOL)
    assert set(body) == {"markdown", "meta"}
    markdown = body["markdown"]
    assert markdown.startswith("## Relevant Context")
    assert "### Code" in markdown
    assert f"{SAMPLE_MODULE_PATH}:" in markdown, "证据行必须是 path:行号 形态"
    assert "| " in markdown, "代码正文带行号（agent 可直接对齐 Edit）"


def test_meta_agrees_with_the_included_pack(client: TestClient, indexed: str) -> None:
    """``meta`` 的计数/判定/预算必须与 ``includePack=true`` 返回的 CF-03 包一致。"""
    body = _search(client, indexed, TARGET_SYMBOL, includePack=True)
    meta = body["meta"]
    pack = meta["pack"]
    assert meta["answerable"] == pack["answerable"]
    assert meta["confidence"] == pack["confidence"]
    assert meta["mode"] == pack["mode"]
    assert meta["evidenceCount"] == len(pack["evidence"])
    assert meta["docsCount"] == len(pack["docs"])
    assert meta["flowsCount"] == len(pack["flows"])
    assert meta["missingEvidence"] == pack["missingEvidence"]
    assert meta["budget"] == pack["budget"]
    assert meta["freshness"] == pack["freshness"]
    assert meta["evidenceCount"] + meta["docsCount"] > 0


def test_meta_field_set_is_frozen(client: TestClient, indexed: str) -> None:
    """``meta`` 字段集是 TASK-040（client）的输入契约：只增不改。"""
    meta = _search(client, indexed, TARGET_SYMBOL)["meta"]
    assert set(meta) == set(meta_field_names())
    assert meta["channelsUsed"], "channelsUsed 来自 search_with_trace（R33）"
    assert meta["candidateCount"] >= meta["evidenceCount"]
    assert meta["degraded"] is False
    assert meta["degradedReason"] is None
    assert meta["pack"] is None, "默认不返回包体"
    assert meta["checkpointId"] is None


def test_checkpoint_id_is_recorded_but_not_used(
    client: TestClient, indexed: str
) -> None:
    """``checkpointId`` 只透传记录进 meta（TASK-033 才登记/校验）。"""
    with_id = _search(client, indexed, TARGET_SYMBOL, checkpointId="cp_abc")
    without = _search(client, indexed, TARGET_SYMBOL)
    assert with_id["meta"]["checkpointId"] == "cp_abc"
    assert with_id["meta"]["evidenceCount"] == without["meta"]["evidenceCount"]


def test_include_pack_passes_cf03_schema(client: TestClient, indexed: str) -> None:
    """``includePack=true`` 时 ``meta.pack`` 通过 CF-03 schema 校验。"""
    schema = json.loads(
        (REPO_ROOT / "docs" / "contracts" / "contextpack.schema.json").read_text(encoding="utf-8")
    )
    pack = _search(client, indexed, TARGET_SYMBOL, includePack=True)["meta"]["pack"]
    jsonschema.validate(instance=pack, schema=schema)


def test_missing_evidence_is_passed_through(
    client: TestClient, engine_manager: EngineManager, indexed: str
) -> None:
    """**必然缺失的场景**：文档引用的符号被删除后，缺失证据必须如实上报。

    口径说明：M1/M2a 里“查询一个不存在的符号”**不会**产生 ``no_context_match``——向量通道
    总会返回若干候选（R22 已登记该诚实性缺口，属 TASK-050 校准范畴）。因此这里用**可复现**的
    缺失来源：删掉被文档引用的代码文件 → 文档引用变 stale（G4），组装层如实产出
    ``stale_doc_reference``（TASK-033 的 ``/api/sync/deletions`` 落地后走同一链路）。
    """
    _blobs, state = engine_manager.project_paths(indexed)
    state.remove_paths([SAMPLE_MODULE_PATH])
    state.save()
    engine_manager.ingest(indexed, ChangeSet(deleted=(SAMPLE_MODULE_PATH,)))

    body = _search(client, indexed, "令牌过期后在哪里刷新")
    missing = body["meta"]["missingEvidence"]
    assert missing, "文档引用了已删除符号，缺失证据必须非空"
    assert all(item["code"] and item["message"] for item in missing)
    assert any(item["code"] == "stale_doc_reference" for item in missing)
    assert any(item["symbol"] == MISSING_SYMBOL for item in missing)
    assert SAMPLE_DOC_PATH in body["markdown"]


def test_missing_evidence_retrieval_truncated(client: TestClient, indexed: str) -> None:
    """预算裁剪也是"必然缺失"的一种：极小 ``maxTokens`` 必须如实标注（不静默丢证据）。"""
    body = _search(client, indexed, "令牌过期后在哪里刷新", maxTokens=600)
    missing = body["meta"]["missingEvidence"]
    assert [item["code"] for item in missing] == ["retrieval_truncated"]
    assert body["meta"]["budget"]["truncated"] is True
    assert body["meta"]["budget"]["omittedCount"] >= 1


def test_empty_index_returns_409_for_search_and_ask(client: TestClient) -> None:
    """空索引 → 409 ``index_in_progress``（D-30 例外条款；不返回 200 空包）。"""
    project_id = client.post(
        "/api/projects/resolve", json={"identityKey": "identity:empty"}
    ).json()["projectId"]

    search = client.post(
        "/api/query/search", json={"projectId": project_id, "query": "任何查询"}
    )
    assert search.status_code == 409
    assert search.json()["error"]["code"] == "index_in_progress"
    assert "索引" in search.json()["error"]["message"]

    ask = client.post(
        "/api/query/ask", json={"projectId": project_id, "question": "任何问题"}
    )
    assert ask.status_code == 409
    assert ask.json()["error"]["code"] == "index_in_progress"


# --------------------------------------------------------------------------- ask（降级包）


def test_ask_returns_degraded_package_not_500(client: TestClient, indexed: str) -> None:
    """未配置 ``ANSWER_*``（L5）：200 + ``status="degraded"`` + 非空 answer + 证据概览。

    **绝不 500**：没有 LLM 配置时也返回可直接用的检索包，并告诉管理员该配什么
    （TASK-088 §D；路由/失败/证据不足三条降级分支的细节见 ``service/tests/test_answer.py``）。
    """
    response = client.post(
        "/api/query/ask",
        json={"projectId": indexed, "question": "令牌过期后在哪里刷新"},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"answer", "status", "evidenceSummary", "meta"}
    assert body["status"] == "degraded"
    assert body["answer"].startswith(DEGRADED_NOTICE)
    assert "## Relevant Context" in body["answer"], "answer 必须含渲染好的上下文正文"
    assert "ANSWER_BASE_URL" in body["answer"], "未配置时要说清缺哪个环境变量"
    assert body["meta"]["degraded"] is True
    assert body["meta"]["degradedReason"] == DEGRADED_NOTICE


def test_ask_evidence_summary_shape(client: TestClient, indexed: str) -> None:
    summary = client.post(
        "/api/query/ask", json={"projectId": indexed, "question": TARGET_SYMBOL}
    ).json()["evidenceSummary"]
    assert 0 < len(summary) <= DECISION_SUMMARY_LIMIT
    for item in summary:
        assert set(item) == {"id", "type", "path", "lines", "tier", "score"}
        assert item["id"].startswith("E")
        assert item["type"] in {"code", "test", "spec"}


# ------------------------------------------------------------------ ask 短路（§B）


def _ask(client: TestClient, project_id: str, question: str) -> dict:
    response = client.post("/api/query/ask", json={"projectId": project_id, "question": question})
    assert response.status_code == 200, response.text
    return response.json()


def test_ask_insufficient_evidence_returns_structured_package(
    client: TestClient, engine_manager: EngineManager, indexed: str
) -> None:
    """``answerable=false`` → 结构化证据不足包（D-24）：三键齐全且 ``bestEffortContext`` 非空。

    ``missingEvidence`` 非空用**可复现**的缺失来源：
    删掉被文档引用的代码文件 → 文档引用变 stale（G4；同 test_missing_evidence_is_passed_through）。
    口径说明：M1/M2a 里"向量通道总有候选"，单靠无关问题**不会**产生
    ``no_context_match``（R22 已登记，属 TASK-050 校准）。
    """
    _blobs, state = engine_manager.project_paths(indexed)
    state.remove_paths([SAMPLE_MODULE_PATH])
    state.save()
    engine_manager.ingest(indexed, ChangeSet(deleted=(SAMPLE_MODULE_PATH,)))

    body = _ask(client, indexed, INSUFFICIENT_QUESTION)

    assert body["status"] == "insufficient_evidence"
    assert {"bestEffortContext", "missingEvidence", "nextQueries", "meta"} <= set(body)
    # 有什么给什么：即使证据不足，命中的内容仍渲染出来（不许返回空壳）。
    assert body["bestEffortContext"].startswith("## Relevant Context")
    assert body["bestEffortContext"].strip()
    assert body["meta"]["answerable"] is False
    assert body["meta"]["degraded"] is True
    assert body["meta"]["degradedReason"] == INSUFFICIENT_NOTICE
    assert isinstance(body["missingEvidence"], list)
    assert isinstance(body["nextQueries"], list)
    assert body["missingEvidence"], "证据不足必须说清缺什么（D-24 的'有用的失败'）"
    assert body["nextQueries"], "且必须给改问建议（Module/03 §5 确定性生成）"
    for line in body["missingEvidence"]:
        assert line.startswith("["), "可读文本形如 '[code] (symbol) message'"
    # 短路包里没有 "answer"（那是 LLM 的词，TASK-088）；也没有证据概览（无答可引）。
    assert "answer" not in body
    assert "evidenceSummary" not in body


def test_ask_when_answerable_keeps_the_degraded_package(client: TestClient, indexed: str) -> None:
    """``answerable=true`` → 行为与今天一致（``status="degraded"``）；LLM 接入是 TASK-088。"""
    body = _ask(client, indexed, TARGET_SYMBOL)
    assert body["meta"]["answerable"] is True, "语料前提：这个提问应当有答案"
    assert body["status"] == "degraded"
    assert body["answer"].startswith(DEGRADED_NOTICE)
    assert "bestEffortContext" not in body, "有答案时不得走 D-24 短路包"


def test_ask_short_circuit_is_audited_as_deep_mode(tmp_path: Path) -> None:
    """短路路径**也要落审计**（TASK-084 的 ``_audited``）：一条 ``mode=deep``、
    ``degraded=true``、``answerable=false`` 的记录（建库口径同 ``test_usage_api``）。"""
    settings = Settings(
        data_root=tmp_path / "data", local_mode=True, local_rescan_interval_s=0.0
    )  # type: ignore[arg-type]
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app: FastAPI = create_app(settings)
    app.state.meta_db = MetaDB.open(settings.meta_db_path)
    app.state.engine_manager = manager
    try:
        project_id = manager.resolve_project("identity:audit-short", "audit-short").project_id
        upload_files(manager, project_id, SAMPLE_FILES)
        with make_client(app) as client:
            body = _ask(client, project_id, INSUFFICIENT_QUESTION)
        assert body["status"] == "insufficient_evidence"

        assert app.state.meta_db is not None
        rows = (
            app.state.meta_db._connect()
            .execute("SELECT mode, degraded, answerable FROM query_audit ORDER BY id")
            .fetchall()
        )
        assert len(rows) == 1
        assert rows[0]["mode"] == "deep"
        assert rows[0]["degraded"] == 1
        assert rows[0]["answerable"] == 0, "短路路径必须如实记 answerable=false（不是 null）"
    finally:
        manager.close()


# --------------------------------------------------------------------------- 校验与错误


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"query": ""}, "invalid_query"),
        ({"query": "   "}, "invalid_query"),
        ({"query": "x" * (MAX_QUERY_CHARS + 1)}, "invalid_query"),
        ({"query": "ok", "maxTokens": 0}, "invalid_max_tokens"),
        ({"query": "ok", "maxTokens": -1}, "invalid_max_tokens"),
        ({"query": "ok", "maxTokens": MAX_MAX_TOKENS + 1}, "invalid_max_tokens"),
    ],
)
def test_search_validation_errors(
    client: TestClient, indexed: str, payload: dict, code: str
) -> None:
    response = client.post("/api/query/search", json={"projectId": indexed, **payload})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


def test_ask_requires_question(client: TestClient, indexed: str) -> None:
    response = client.post("/api/query/ask", json={"projectId": indexed, "question": "  "})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_question"


@pytest.mark.parametrize(
    "endpoint,field", [("/api/query/search", "query"), ("/api/query/ask", "question")]
)
def test_unknown_project_returns_404_envelope(
    client: TestClient, endpoint: str, field: str
) -> None:
    response = client.post(endpoint, json={"projectId": UNKNOWN_PROJECT, field: "任何内容"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"


def test_project_id_can_be_omitted_in_local_mode(
    client: TestClient, engine_manager: EngineManager, indexed: str
) -> None:
    """R37：本地单项目模式下 ``projectId`` 可省略。"""
    body = _search(client, indexed, TARGET_SYMBOL)  # 显式 id
    response = client.post("/api/query/search", json={"query": TARGET_SYMBOL})
    assert response.status_code == 200
    assert response.json()["meta"]["projectId"] == indexed
    assert response.json()["meta"]["evidenceCount"] == body["meta"]["evidenceCount"]


def test_omitted_project_id_with_multiple_projects_is_ambiguous(
    client: TestClient, engine_manager: EngineManager, indexed: str
) -> None:
    client.post("/api/projects/resolve", json={"identityKey": "identity:second"})
    response = client.post("/api/query/search", json={"query": TARGET_SYMBOL})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ambiguous_project"


def test_omitted_project_id_with_no_projects_is_404(client: TestClient) -> None:
    response = client.post("/api/query/search", json={"query": TARGET_SYMBOL})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"


def test_repository_root_is_used_for_contract_files() -> None:
    """自证：测试读的是仓库内的合同文件（不是本机其它路径）。"""
    assert (Path(REPO_ROOT) / "docs" / "contracts" / "openapi.yaml").is_file()
