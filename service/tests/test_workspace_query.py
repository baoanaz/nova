from dataclasses import replace
from types import SimpleNamespace

from nova_core.types import Freshness
from nova_service.errors import ApiError
from nova_service.routers import query
from nova_service.workspace import combine_evidence

from tests.conftest import SAMPLE_FILES, TARGET_SYMBOL, upload_files


def prepare(manager, count=2):
    ids = [manager.resolve_project("workspace:" + str(i), str(i)).project_id for i in range(count)]
    for project in ids:
        upload_files(manager, project, SAMPLE_FILES)
    return ids


def test_workspace_uses_one_synthesis_with_unique_sources(client, engine_manager, monkeypatch):
    ids = prepare(engine_manager)
    calls = []
    monkeypatch.setattr(query, "_ask_provider", lambda request: object())

    def answer(**kwargs):
        pack = kwargs["pack"]
        calls.append(pack)
        return SimpleNamespace(answer="已核对 [E1]", citation_coverage=1,
                               latency_ms=1, answer_tokens=1)

    monkeypatch.setattr(query, "answer_question", answer)
    response = client.post("/api/query/ask-workspace",
                           json={"projectIds": ids, "question": TARGET_SYMBOL})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "answered"
    assert len(calls) == 1
    pack = calls[0]
    items = [*pack.evidence, *pack.docs, *pack.flows]
    assert len({i.id for i in items}) == len(items)
    assert {v["projectId"] for v in body["meta"]["sources"].values()} == set(ids)
    assert pack.budget.used_tokens <= 16_000
    assert all(len({n.path.split("::")[0] for n in f.nodes}) == 1 for f in pack.flows)


def test_unauthorized_project_never_reaches_retrieval(client, engine_manager, monkeypatch):
    ids = prepare(engine_manager)
    original = query.require_project_id
    searched = []
    retrieve = engine_manager.ask_evidence

    def authorize(request, project):
        if project == ids[1]:
            raise ApiError("project_not_found", "project not found", 404)
        return original(request, project)

    def evidence(project, *args):
        searched.append(project)
        return retrieve(project, *args)

    monkeypatch.setattr(query, "require_project_id", authorize)
    monkeypatch.setattr(engine_manager, "ask_evidence", evidence)
    response = client.post("/api/query/ask-workspace",
                           json={"projectIds": ids, "question": TARGET_SYMBOL})
    assert response.status_code == 200
    assert searched == [ids[0]]
    body = response.json()
    assert "P2" in str(body["meta"]["coverageGaps"])
    assert all(s["projectId"] != ids[1] for s in body["meta"]["sources"].values())


def test_stored_evidence_needs_no_version_handshake(client, engine_manager):
    project = prepare(engine_manager)[0]
    body = client.post("/api/query/ask", json={
        "projectId": project, "question": TARGET_SYMBOL,
    }).json()
    assert body["status"] == "degraded"  # 未配置模型，但当前索引证据照常返回
    assert body["meta"]["evidenceCount"] > 0
    assert "sourceVersions" not in body["meta"]
    assert "源码版本核验" not in body["answer"]


def test_combining_does_not_mutate_source_pack(engine_manager):
    project = prepare(engine_manager)[0]
    pack = engine_manager.ask_evidence(project, TARGET_SYMBOL).pack
    before = [(i.id, i.path, i.symbol) for i in (*pack.evidence, *pack.docs)]
    combined, _ = combine_evidence("q", [("P1", project, pack), ("P2", project, pack)])
    assert before == [(i.id, i.path, i.symbol) for i in (*pack.evidence, *pack.docs)]
    assert all(i.path.startswith(("P1::", "P2::")) for i in combined.evidence)
    huge = replace(pack.evidence[0], content="x" * 100_000)
    oversized = replace(pack, evidence=[huge], docs=[], flows=[])
    result, origins = combine_evidence("q", [("P1", project, oversized)])
    assert result.budget.used_tokens <= 16_000
    assert not result.answerable and not origins
    noisy = replace(pack, freshness=Freshness(stale_files=tuple("x" * 1000 for _ in range(1000))))
    result, _ = combine_evidence("q", [("P1", project, noisy)])
    assert result.budget.used_tokens <= 16_000
    assert result.budget.truncated


def test_workspace_accepts_five_projects_and_deduplicates(client, engine_manager, monkeypatch):
    ids = prepare(engine_manager, count=5)
    retrieve = engine_manager.ask_evidence
    searched = []

    def evidence(project, *args):
        searched.append(project)
        return retrieve(project, *args)

    monkeypatch.setattr(engine_manager, "ask_evidence", evidence)
    response = client.post("/api/query/ask-workspace",
                           json={"projectIds": ids + ids[:1], "question": TARGET_SYMBOL})
    assert response.status_code == 200, response.text
    assert searched == ids
    assert response.json()["meta"]["budget"]["hardCap"] == 16_000


def test_workspace_does_not_rescan_local_sources(client, engine_manager, monkeypatch):
    project = prepare(engine_manager)[0]

    def unexpected_rescan(*args):
        raise AssertionError("workspace query must use stored index")

    monkeypatch.setattr(query, "_rescan_before_query", unexpected_rescan)
    response = client.post("/api/query/ask-workspace",
                           json={"projectIds": [project], "question": TARGET_SYMBOL})
    assert response.status_code == 200
    assert response.json()["meta"]["evidenceCount"] > 0
    assert not response.json()["meta"]["coverageGaps"]


def test_one_bad_project_does_not_reject_the_workspace(client, engine_manager):
    project = prepare(engine_manager)[0]
    response = client.post("/api/query/ask-workspace",
                           json={"projectIds": ["bad", project], "question": TARGET_SYMBOL})
    assert response.status_code == 200
    assert response.json()["meta"]["evidenceCount"] > 0
    assert response.json()["meta"]["coverageGaps"]
