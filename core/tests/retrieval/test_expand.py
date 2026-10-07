"""TASK-011 图扩展与 flows（DoD：配额截断、caller 爆炸、spec 双向、flows 顺序/环/超深）。"""

from __future__ import annotations

from nova_core.retrieval.expand import (
    GRAPH_REASON_PREFIX,
    GRAPH_TIER,
    SYNTHESIZED_REASON,
    ExpansionLimits,
    build_flows,
    expand,
)
from nova_core.retrieval.fusion import KIND_SPEC, make_candidate
from nova_core.types import EdgeDef, SpecBlockDef


def _cand(store, path: str, fqn: str, start: int, *, score: float, tier: int = 1):
    chunk_id = f"{path}:{fqn}:{start}"
    candidate = make_candidate(chunk_id, channel="bm25", rank=1, tier=tier)
    candidate.score = score
    candidate.rrf_score = score
    candidate.symbol_fqn = fqn
    candidate.path = path
    candidate.start_line = start
    candidate.end_line = start + 2
    return candidate


def test_calls_expansion_both_directions(store, seed_file, sym) -> None:
    """calls 1-hop 双向：callers + callees 都入池，tier=3、graph_depth=1。"""
    seed_file(
        store,
        path="src/a.py",
        symbols=[sym("caller", "caller", start=1), sym("target", "target", start=10)],
        edges=[EdgeDef(source_fqn="caller", target_name="target", kind="calls", line=2)],
    )
    seed = _cand(store, "src/a.py", "target", 10, score=0.05)
    result = expand(store, [seed])
    by_id = {c.chunk_id: c for c in result.candidates}
    assert by_id["src/a.py:caller:1"].tier == GRAPH_TIER
    assert by_id["src/a.py:caller:1"].graph_depth == 1
    assert GRAPH_REASON_PREFIX + seed.chunk_id in by_id["src/a.py:caller:1"].reasons


def test_existing_candidates_are_not_duplicated(store, seed_file, sym) -> None:
    seed_file(
        store,
        path="src/a.py",
        symbols=[sym("caller", "caller", start=1), sym("target", "target", start=10)],
        edges=[EdgeDef(source_fqn="caller", target_name="target", kind="calls", line=2)],
    )
    seed = _cand(store, "src/a.py", "target", 10, score=0.05)
    already = _cand(store, "src/a.py", "caller", 1, score=0.04)
    result = expand(store, [seed, already])
    assert result.candidates == []


def test_expansion_quota_is_hard_30(store, seed_file, sym) -> None:
    symbols = [sym(f"f{i}", f"f{i}", start=i * 10 + 1) for i in range(40)]
    edges = [
        EdgeDef(source_fqn=f"f{i}", target_name="seed", kind="calls", line=i + 1)
        for i in range(40)
    ]
    seed_file(store, path="src/big.py", symbols=symbols, edges=edges)
    seed = _cand(store, "src/big.py", "seed", 1, score=0.05)

    result = expand(store, [seed])
    assert len(result.candidates) == 30
    assert all(c.tier == GRAPH_TIER for c in result.candidates)


def test_caller_explosion_truncated_to_20_entry_points_first(store, seed_file, sym) -> None:
    """构造 300 callers → 截断 top 20，且入口点（被导出、无内部调用者）优先。"""
    callers = [
        sym(f"caller{i:03d}", f"caller{i:03d}", start=i + 1, is_exported=(i < 5))
        for i in range(300)
    ]
    edges = [
        EdgeDef(source_fqn=f"caller{i:03d}", target_name="hub", kind="calls", line=i + 1)
        for i in range(300)
    ]
    seed_file(
        store, path="src/hub.py", symbols=[*callers, sym("hub", "hub", start=500)], edges=edges
    )
    seed = _cand(store, "src/hub.py", "hub", 500, score=0.05)

    result = expand(store, [seed], limits=ExpansionLimits(caller_cap=20))
    assert len(result.candidates) == 20
    kept = {c.symbol_fqn for c in result.candidates}
    assert {f"caller{i:03d}" for i in range(5)} <= kept  # 导出的入口点优先保留


def test_below_explosion_threshold_keeps_all_callers(store, seed_file, sym) -> None:
    callers = [sym(f"c{i}", f"c{i}", start=i + 1) for i in range(30)]
    edges = [
        EdgeDef(source_fqn=f"c{i}", target_name="hub", kind="calls", line=i + 1)
        for i in range(30)
    ]
    seed_file(
        store, path="src/hub.py", symbols=[*callers, sym("hub", "hub", start=99)], edges=edges
    )
    seed = _cand(store, "src/hub.py", "hub", 99, score=0.05)
    assert len(expand(store, [seed]).candidates) == 30


def test_spec_references_bidirectional(store, seed_file, sym) -> None:
    """代码 seed → SpecBlock；spec seed → 代码符号（双向 fixture）。"""
    block = SpecBlockDef(
        path="docs/auth.md",
        heading="Token Refresh",
        heading_path="认证 > Token Refresh",
        level=2,
        start_line=30,
        end_line=52,
        content="刷新流程说明\n",
        doctype="design",
        mentioned=("TokenService.refresh",),
    )
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[sym("refresh", "TokenService.refresh", kind="method", start=45)],
    )
    seed_file(store, path="docs/auth.md", language="markdown", spec_blocks=[block])
    code_symbol_id = "src/auth/token_service.py:TokenService.refresh:45"
    spec_id = "docs/auth.md:认证 > Token Refresh:30"
    store.add_spec_refs([(spec_id, code_symbol_id)])

    code_seed = _cand(store, "src/auth/token_service.py", "TokenService.refresh", 45, score=0.05)
    code_result = expand(store, [code_seed])
    assert [c.chunk_id for c in code_result.candidates] == [spec_id]
    assert code_result.candidates[0].kind == KIND_SPEC

    spec_seed = _cand(store, "docs/auth.md", "认证 > Token Refresh", 30, score=0.05)
    spec_seed.kind = KIND_SPEC
    spec_result = expand(store, [spec_seed])
    assert [c.chunk_id for c in spec_result.candidates] == [code_symbol_id]
    assert spec_result.candidates[0].symbol_fqn == "TokenService.refresh"


def test_stale_spec_reference_is_not_expanded(store, seed_file, sym) -> None:
    block = SpecBlockDef(
        path="docs/auth.md",
        heading="Token Refresh",
        heading_path="认证 > Token Refresh",
        level=2,
        start_line=30,
        end_line=52,
        content="刷新流程说明\n",
        doctype="design",
    )
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[sym("refresh", "TokenService.refresh", kind="method", start=45)],
    )
    seed_file(store, path="docs/auth.md", language="markdown", spec_blocks=[block])
    store.add_spec_refs([("docs/auth.md:认证 > Token Refresh:30",
                          "src/auth/token_service.py:TokenService.refresh:45")])
    # 删掉符号所在文件 → spec_references 置 stale=1
    store.apply_deletions(["src/auth/token_service.py"])

    seed = _cand(store, "docs/auth.md", "认证 > Token Refresh", 30, score=0.05)
    seed.kind = KIND_SPEC
    assert expand(store, [seed]).candidates == []


def test_synthesized_edge_is_marked(store, seed_file, sym) -> None:
    seed_file(
        store,
        path="src/a.py",
        symbols=[sym("caller", "caller", start=1), sym("target", "target", start=10)],
        edges=[
            EdgeDef(
                source_fqn="caller",
                target_name="target",
                kind="calls",
                line=2,
                provenance="synthesized",
            )
        ],
    )
    seed = _cand(store, "src/a.py", "target", 10, score=0.05)
    candidate = expand(store, [seed]).candidates[0]
    assert SYNTHESIZED_REASON in candidate.reasons


def test_seeds_are_top_20_by_score(store, seed_file, sym) -> None:
    symbols = [sym(f"f{i}", f"f{i}", start=i * 10 + 1) for i in range(25)]
    edges = [
        EdgeDef(source_fqn=f"f{i}", target_name=f"f{i}", kind="calls", line=1)
        for i in range(25)
    ]
    seed_file(store, path="src/many.py", symbols=symbols, edges=edges)
    candidates = [
        _cand(store, "src/many.py", f"f{i}", i * 10 + 1, score=float(i)) for i in range(25)
    ]
    result = expand(store, candidates)
    assert len(result.seed_chunk_ids) == 20
    assert result.seed_chunk_ids[0] == "src/many.py:f24:241"


# --------------------------------------------------------------------------- flows


def _chain(store, seed_file, sym, n: int) -> None:
    symbols = [sym(f"s{i}", f"s{i}", start=i * 100 + 1) for i in range(n)]
    edges = [
        EdgeDef(source_fqn=f"s{i}", target_name=f"s{i + 1}", kind="calls", line=i * 100 + 5)
        for i in range(n - 1)
    ]
    seed_file(store, path="src/chain.py", symbols=symbols, edges=edges)


def test_flow_nodes_order_and_lines(store, seed_file, sym) -> None:
    _chain(store, seed_file, sym, 5)
    seed = _cand(store, "src/chain.py", "s0", 1, score=0.05)
    flows = build_flows(store, [seed])
    assert [flow.id for flow in flows] == ["F1"]
    flow = flows[0]
    assert [node.symbol for node in flow.nodes] == ["s0", "s1", "s2"]
    assert [node.line for node in flow.nodes] == [1, 101, 201]
    assert flow.truncated is True


def test_flow_shorter_than_two_nodes_is_skipped(store, seed_file, sym) -> None:
    seed_file(store, path="src/leaf.py", symbols=[sym("leaf", "leaf", start=1)])
    seed = _cand(store, "src/leaf.py", "leaf", 1, score=0.05)
    assert build_flows(store, [seed]) == []


def test_flow_cycle_is_truncated_without_exploding(store, seed_file, sym) -> None:
    seed_file(
        store,
        path="src/cycle.py",
        symbols=[sym("a", "a", start=1), sym("b", "b", start=10)],
        edges=[
            EdgeDef(source_fqn="a", target_name="b", kind="calls", line=2),
            EdgeDef(source_fqn="b", target_name="a", kind="calls", line=11),
        ],
    )
    seed = _cand(store, "src/cycle.py", "a", 1, score=0.05)
    flow = build_flows(store, [seed])[0]
    assert [node.symbol for node in flow.nodes] == ["a", "b"]
    assert flow.truncated is True


def test_flows_only_for_top_3_seeds(store, seed_file, sym) -> None:
    _chain(store, seed_file, sym, 5)
    seeds = [_cand(store, "src/chain.py", f"s{i}", i * 100 + 1, score=1.0 - i) for i in range(4)]
    flows = build_flows(store, seeds)
    assert len(flows) == 3
    assert flows[0].nodes[0].symbol == "s0"


def test_expand_returns_flows_for_final_path(store, seed_file, sym) -> None:
    _chain(store, seed_file, sym, 3)
    seed = _cand(store, "src/chain.py", "s0", 1, score=0.05)
    result = expand(store, [seed])
    assert [node.symbol for node in result.flows[0].nodes] == ["s0", "s1", "s2"]
