"""D-50：旧组装器的可变候选操作不能污染共享 EvidenceSet。"""

from copy import deepcopy

import pytest
from nova_core.contextpack import BudgetConfig, IndexSignals
from nova_core.contextpack import evidence as evidence_module
from nova_core.contextpack.evidence import EvidenceSet, build_ask_evidence, build_search_pack
from nova_core.types import ContextPack, Freshness


@pytest.mark.parametrize("ask_first", [False, True])
def test_cpp_definition_selection_is_isolated(store, seed_file, sym, cand, ask_first) -> None:
    seed_file(
        store,
        path="db/db_impl.h",
        language="cpp",
        symbols=[sym("Get", "DBImpl::Get", kind="method", start=10, end=11)],
        bodies={"DBImpl::Get": "Status Get() override;"},
    )
    seed_file(
        store,
        path="db/db_impl.cc",
        language="cpp",
        symbols=[sym("Get", "DBImpl::Get", kind="method", start=100, end=104)],
        bodies={
            "DBImpl::Get": (
                "Status DBImpl::Get() {\n"
                "  read_memtable();\n"
                "  read_immutable();\n"
                "  return read_version();\n"
                "}"
            )
        },
    )
    evidence = EvidenceSet(
        query="Get 读取顺序",
        candidates=(
            cand("db/db_impl.h", "DBImpl::Get", 10, score=1.0, end=11),
            cand("db/db_impl.cc", "DBImpl::Get", 100, score=0.9, end=104),
        ),
        flows=(),
        freshness=store.freshness(),
        signals=IndexSignals(),
    )
    before = deepcopy(evidence)
    config = BudgetConfig(hard_cap=10_000, framework_overhead=0, single_file_ratio=1.0)
    builders = (
        (build_ask_evidence, build_search_pack)
        if ask_first
        else (build_search_pack, build_ask_evidence)
    )

    for build in builders:
        pack = build(store, evidence, config=config)
        assert len(pack.evidence) == 1
        assert pack.evidence[0].path == "db/db_impl.cc"
        assert pack.evidence[0].lines == (100, 104)
        assert pack.evidence[0].reason.count("同符号聚合×1") == 1
        assert "read_memtable" in pack.evidence[0].content
        assert evidence == before, "定义代表选择不能向共享 reasons 追加聚合说明"


def test_all_mutable_fields_and_backfill_are_isolated(store, cand, monkeypatch) -> None:
    candidate = cand("a.py", "run", 1, score=1.0)
    evidence = EvidenceSet(
        query="run",
        candidates=(candidate,),
        flows=(),
        freshness=Freshness(),
        signals=IndexSignals(),
        backfill=((candidate, "gap backfill: test"),),
        repair_triggered=True,
    )
    before = deepcopy(evidence)

    def mutating_assembler(store, query, candidates, **kwargs):
        copied = candidates[0]
        assert copied is not candidate
        assert copied is kwargs["backfill"][0][0], "副本内保留候选与 backfill 的共享身份"
        copied.reasons.append("local aggregation")
        copied.channel_ranks["local"] = 1
        kwargs["backfill"][0][0].reasons.append("local backfill")
        return ContextPack(
            query=query,
            mode=kwargs["mode"],
            answerable=False,
            confidence="low",
            freshness=kwargs["freshness"],
        )

    monkeypatch.setattr(evidence_module, "assemble", mutating_assembler)
    for build in (build_search_pack, build_ask_evidence):
        build(store, evidence, config=BudgetConfig())
        assert evidence == before
