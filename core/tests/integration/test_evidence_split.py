"""D-50：真实索引与确定性 embedding 下的 EvidenceSet 分流回归。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from nova_core import engine as engine_module
from nova_core.contextpack import MODE_DEEP, MODE_FAST, budget_for, to_json
from nova_core.contextpack.evidence import build_ask_evidence, build_search_pack
from nova_core.engine import Engine, EngineError
from nova_core.storage import Store

from .conftest import (
    DESIGN_DOC,
    MODULE_PATH,
    QUERY,
    SPEC_PATH,
    TARGET_SYMBOL,
    TOKEN_MODULE,
    DeterministicBigramEmbedding,
    make_change_set,
)


@pytest.fixture
def indexed_engine(tmp_path: Path):
    with Engine.open(tmp_path, provider=DeterministicBigramEmbedding()) as engine:
        project_id = engine.resolve_project("identity:evidence-split").project_id
        report = engine.apply_changes(
            project_id,
            make_change_set(modified={MODULE_PATH: TOKEN_MODULE, SPEC_PATH: DESIGN_DOC}),
        )
        assert not report.errors
        yield engine, project_id


def test_small_search_retains_unpacked_candidates(indexed_engine) -> None:
    engine, project_id = indexed_engine
    trace = engine.search_with_trace(project_id, QUERY, 600)
    evidence = trace.evidence_set

    assert evidence is not None
    assert evidence.candidates == trace.candidates
    assert len(evidence.candidates) > len(trace.pack.evidence) + len(trace.pack.docs)
    assert trace.pack.budget.hard_cap == 600
    assert trace.pack.budget.truncated
    assert any(c.symbol_fqn == TARGET_SYMBOL for c in evidence.candidates)


def test_builders_use_full_set_and_independent_budgets(indexed_engine) -> None:
    engine, project_id = indexed_engine
    evidence = engine.search_with_trace(project_id, QUERY, 600).evidence_set
    before = deepcopy(evidence)

    with Store.open(engine.project_dir(project_id)) as store:
        small = build_search_pack(
            store, evidence, config=replace(budget_for(MODE_FAST), hard_cap=600)
        )
        wide = build_ask_evidence(store, evidence, config=budget_for(MODE_DEEP))

    assert small.mode == MODE_FAST
    assert wide.mode == MODE_DEEP
    assert small.budget.hard_cap == 600
    assert wide.budget.hard_cap == 16_000
    assert wide.budget.used_tokens > small.budget.used_tokens
    assert len(wide.evidence) + len(wide.docs) > len(small.evidence) + len(small.docs)
    assert any(
        item.path == MODULE_PATH
        and "def refresh_token" in item.content
        and 'return "old"' in item.content
        for item in wide.evidence
    ), "类级聚合可以改变 symbol，但目标方法正文必须仍在 Deep 证据中"
    assert evidence == before, "组装不得原地改写共享候选或补检来源"


def test_ask_does_not_call_search_or_search_builder(indexed_engine, monkeypatch) -> None:
    engine, project_id = indexed_engine

    def forbidden(*args, **kwargs):
        pytest.fail("ask 不得依赖 search 或其返回包")

    monkeypatch.setattr(engine, "search", forbidden)
    monkeypatch.setattr(engine, "search_with_trace", forbidden)
    monkeypatch.setattr(engine_module, "build_search_pack", forbidden)

    trace = engine.ask_with_trace(project_id, TARGET_SYMBOL)
    assert trace.pack.mode == MODE_DEEP
    assert trace.pack.answerable
    assert trace.evidence_set is not None
    assert any(item.symbol == TARGET_SYMBOL for item in trace.pack.evidence)


def test_ask_preserves_legacy_deep_output(indexed_engine) -> None:
    engine, project_id = indexed_engine
    legacy = engine.search_with_trace(project_id, QUERY, 16_000, deep=True)
    dedicated = engine.ask_with_trace(project_id, QUERY)

    assert to_json(dedicated.pack) == to_json(legacy.pack)
    assert dedicated.channels_used == legacy.channels_used
    assert dedicated.gap_kinds == legacy.gap_kinds
    assert dedicated.backfilled == legacy.backfilled


@pytest.mark.parametrize("deep", [False, True])
def test_discovery_runs_once_and_repair_is_bounded(indexed_engine, monkeypatch, deep) -> None:
    engine, project_id = indexed_engine
    recall = Mock(wraps=engine_module.recall)
    expand = Mock(wraps=engine_module.expand)
    repair = Mock(wraps=engine._backfill_gaps)
    monkeypatch.setattr(engine_module, "recall", recall)
    monkeypatch.setattr(engine_module, "expand", expand)
    monkeypatch.setattr(engine, "_backfill_gaps", repair)

    trace = (
        engine.ask_with_trace(project_id, QUERY)
        if deep
        else engine.search_with_trace(project_id, QUERY, 14_000)
    )
    assert recall.call_count == 1
    assert expand.call_count == 1
    assert repair.call_count == 1
    limits = repair.call_args.kwargs["limits"]
    if deep:
        assert limits == engine_module.DEEP_GAP_LIMITS
    else:
        assert limits != engine_module.DEEP_GAP_LIMITS
    assert trace.candidates == trace.evidence_set.candidates


def test_repair_metadata_survives_repacking(indexed_engine, monkeypatch) -> None:
    engine, project_id = indexed_engine
    reason = "gap backfill: 同容器成员 TokenService"

    def repaired(store, query, ranked, pack, *, limits):
        return SimpleNamespace(triggered=True, kinds=("container_members",)), [
            (ranked[0], reason)
        ], ranked

    monkeypatch.setattr(engine, "_backfill_gaps", repaired)
    trace = engine.ask_with_trace(project_id, QUERY)

    assert trace.gap_kinds == ("container_members",)
    assert trace.backfilled == 1
    assert trace.evidence_set.repair_triggered
    assert trace.evidence_set.backfill == ((trace.candidates[0], reason),)


@pytest.mark.parametrize("question,budget", [(" ", 16_000), (QUERY, 0), (QUERY, -1)])
def test_ask_validates_before_retrieval(indexed_engine, monkeypatch, question, budget) -> None:
    engine, project_id = indexed_engine
    recall = Mock(side_effect=AssertionError("无效输入不得触发召回"))
    monkeypatch.setattr(engine_module, "recall", recall)

    with pytest.raises(EngineError):
        engine.ask_with_trace(project_id, question, budget)
    recall.assert_not_called()
