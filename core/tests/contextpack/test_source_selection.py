from nova_core.contextpack import BudgetConfig, IndexSignals, assemble
from nova_core.contextpack.evidence import EvidenceSet, build_search_pack
from nova_core.contextpack.selection import focused_segments
from nova_core.types import Flow, FlowNode


def test_explicit_target_precedes_higher_scored_neighbours(store, seed_file, sym, cand):
    for name in ("noise", "wanted"):
        seed_file(store, path=f"{name}.py", symbols=[sym(name, name)],
                  bodies={name: f"def {name}():\n    return 1\n"})
    evidence = EvidenceSet(
        query="`wanted`", candidates=(
            cand("noise.py", "noise", 1, score=10),
            cand("wanted.py", "wanted", 1, score=1, channels={"exact": 1}),
        ), flows=(), freshness=store.freshness(), signals=IndexSignals(),
    )
    pack = build_search_pack(store, evidence, config=BudgetConfig(hard_cap=1000))
    assert pack.evidence[0].symbol == "wanted"
    assert pack.budget.used_tokens <= pack.budget.hard_cap


def test_large_body_keeps_tail_query_evidence(store, seed_file, sym, cand):
    lines = ["def run():"] + ["    padding = '" + "x" * 100 + "'" for _ in range(100)]
    lines += ["    if retry_enabled:", "        return commit_request()"]
    seed_file(store, path="run.py", symbols=[sym("run", "run", end=len(lines))],
              bodies={"run": "\n".join(lines)})
    evidence = EvidenceSet(
        query="`run` commit_request", candidates=(
            cand("run.py", "run", 1, end=len(lines), score=1, channels={"exact": 1}),
        ), flows=(), freshness=store.freshness(), signals=IndexSignals(),
    )
    pack = build_search_pack(store, evidence, config=BudgetConfig(
        hard_cap=2000, single_file_tokens=1500, code_floor=0,
    ))
    item = pack.evidence[0]
    assert "return commit_request()" in item.content
    assert "if retry_enabled" in item.content
    assert f"{len(lines)} |" in item.content
    assert item.elided_lines > 0
    assert "完整实现 run.py:1-" in item.reason
    assert pack.budget.used_tokens <= pack.budget.hard_cap


def test_final_flow_cost_cannot_overrun_budget(store, seed_file, sym, cand):
    seed_file(store, path="a.py", symbols=[sym("a", "a")],
              bodies={"a": "def a():\n    return 1"})
    flows = [Flow(id="F1", nodes=[
        FlowNode(symbol="x" * 2000, path="a.py", line=1),
    ])]
    pack = assemble(store, "a", [cand("a.py", "a", 1, score=1)], flows=flows,
                    config=BudgetConfig(hard_cap=400, framework_overhead=0))
    assert pack.budget.used_tokens <= 400
    assert pack.evidence
    assert not pack.flows
    assert pack.budget.truncated


def test_merged_window_preserves_original_tail_elision(store, seed_file, sym, cand):
    prefix = ["def prefix():"] + ["    pass" for _ in range(9)]
    tail = ["def tail():"] + ["    padding = '" + "x" * 100 + "'" for _ in range(189)]
    seed_file(store, path="a.py", symbols=[
        sym("prefix", "prefix", start=1, end=10),
        sym("tail", "tail", start=11, end=200),
    ], bodies={"prefix": "\n".join(prefix), "tail": "\n".join(tail)})
    evidence = EvidenceSet(
        query="`tail` `prefix`", candidates=(
            cand("a.py", "prefix", 1, end=10, score=2, channels={"exact": 1}),
            cand("a.py", "tail", 11, end=200, score=1, channels={"exact": 2}),
        ), flows=(), freshness=store.freshness(), signals=IndexSignals(),
    )
    pack = build_search_pack(store, evidence, config=BudgetConfig(
        hard_cap=2000, single_file_tokens=1500, code_floor=0,
    ))
    item = pack.evidence[0]
    assert item.elided_lines == 174
    assert "完整实现 a.py:11-200" in item.reason
    assert "174" in item.content
    assert pack.budget.used_tokens <= pack.budget.hard_cap


def test_focus_ranges_are_bounded_and_preserve_source_lines():
    source = "\n".join(f"line_{i}" for i in range(200))
    ranges = focused_segments(source, 20, "f.py:190", "f.py", [150], max_lines=16)
    assert sum(b-a+1 for a,b,_ in ranges) <= 16
    assert any(a <= 190 <= b for a,b,_ in ranges)
    for a,b,text in ranges:
        assert text == "\n".join(source.splitlines()[a-20:b-19])
