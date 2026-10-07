"""TASK-012 组装（DoD：预算/配额/去重三招/判定矩阵/缺口/性能）。"""

from __future__ import annotations

import math
import time
from dataclasses import replace

import pytest
from nova_core.contextpack import (
    DEEP_BUDGET,
    FAST_BUDGET,
    BudgetConfig,
    IndexSignals,
    assemble,
    budget_for,
    collect_index_signals,
    estimate_render_tokens,
    estimate_tokens,
    evidence_markdown_lines,
    render_markdown,
)
from nova_core.contextpack.assembly import _single_file_cap
from nova_core.types import Flow, FlowNode, Freshness, SpecBlockDef


def _long(store, seed_file, sym, path: str, fqn: str, start: int, chars: int) -> None:
    seed_file(
        store,
        path=path,
        symbols=[sym(fqn, fqn, start=start, end=start)],
        bodies={fqn: "x" * chars},
    )


def _spec_block(path: str, heading: str, *, start: int = 10) -> SpecBlockDef:
    return SpecBlockDef(
        path=path,
        heading=heading,
        heading_path=f"架构 > {heading}",
        level=2,
        start_line=start,
        end_line=start + 5,
        content="设计说明" * 5,
        doctype="design",
    )


# --------------------------------------------------------------------------- 预算


def test_budget_truncates_and_counts_omitted(store, seed_file, sym, cand) -> None:
    for index in range(6):
        _long(store, seed_file, sym, f"src/f{index}.py", f"f{index}", 1, 400)
    candidates = [cand(f"src/f{i}.py", f"f{i}", 1, score=1.0 - i / 10) for i in range(6)]
    config = BudgetConfig(hard_cap=400, framework_overhead=0, single_file_ratio=1.0)

    pack = assemble(store, "q", candidates, config=config)
    assert pack.budget is not None
    assert pack.budget.truncated is True
    assert pack.budget.omitted_count >= 1
    assert pack.budget.used_tokens <= config.hard_cap
    assert len(pack.evidence) < len(candidates)


def test_framework_overhead_is_counted(store, seed_file, sym, cand) -> None:
    """预算账 = 框架开销 + 各条证据的**完整渲染开销**（TASK-096 §A-1）。

    修复前这里断言的是 ``estimate_tokens(item.content)``（只算正文）；现在预算把 header +
    reason + 行号缩进一并计入，所以对照口径必须换成 ``estimate_render_tokens``。
    本用例的语料（``\"x\" * 40``）无 CJK → §A-2 的分类计价不影响它，差异 100% 来自 §A-1。
    """
    _long(store, seed_file, sym, "src/a.py", "f", 1, 40)
    config = BudgetConfig(hard_cap=1_000, framework_overhead=500, single_file_ratio=1.0)
    pack = assemble(store, "q", [cand("src/a.py", "f", 1, score=1.0)], config=config)
    assert pack.budget is not None
    item_tokens = sum(estimate_render_tokens(item) for item in pack.evidence)
    assert pack.budget.used_tokens == 500 + item_tokens
    # 渲染开销 **严格大于** 正文开销（header + reason + 缩进至少十几 token）——修复前二者相等。
    assert item_tokens > sum(estimate_tokens(item.content) for item in pack.evidence)


def test_flow_tokens_count_but_do_not_compete(store, seed_file, sym, cand) -> None:
    _long(store, seed_file, sym, "src/a.py", "f", 1, 40)
    flow = Flow(
        id="F1",
        nodes=(
            FlowNode(symbol="a", path="src/a.py", line=1),
            FlowNode(symbol="b", path="src/b.py", line=2),
        ),
    )
    config = BudgetConfig(hard_cap=10_000, framework_overhead=0, single_file_ratio=1.0)
    pack = assemble(store, "q", [cand("src/a.py", "f", 1, score=1.0)], flows=[flow], config=config)
    assert pack.budget is not None
    assert pack.budget.used_tokens > sum(estimate_tokens(i.content) for i in pack.evidence)
    assert pack.flows == [flow]


def test_budget_defaults_per_mode(store) -> None:
    """TASK-MCP-BUDGET：Fast 14K / Deep 16K（旧值 10K/12K）。"""
    assert FAST_BUDGET.hard_cap == 14_000
    assert DEEP_BUDGET.hard_cap == 16_000
    assert budget_for("deep").hard_cap == 16_000
    assert budget_for("fast").hard_cap == 14_000
    with pytest.raises(ValueError):
        budget_for("turbo")


def test_single_file_cap_is_absolute_in_production_budgets() -> None:
    """单文件上限在**生产预设**里是绝对值（2_500），不随 hard_cap 缩放。

    TASK-MCP-BUDGET：预算 10K→14K 时若仍按 25% 算，单文件上限会 2500→3500，
    一个文件就能吃掉近三分之一包体；新增预算应当给“更多文件”而不是“同一个文件更多行”。
    字段默认 0 时仍走旧比例口径（测试与 param_sweep 靠它）。
    """
    assert _single_file_cap(FAST_BUDGET) == 2_500
    assert _single_file_cap(DEEP_BUDGET) == 2_500  # 不随 16K 变成 4000
    # 未显式设置 → 旧比例口径逐字不变（包括显式关闸的 ratio=1.0）
    assert _single_file_cap(BudgetConfig(hard_cap=10_000)) == 2_500
    assert _single_file_cap(BudgetConfig(hard_cap=100_000, single_file_ratio=1.0)) == 100_000


def test_no_candidates_yields_empty_pack(store) -> None:
    pack = assemble(store, "token 刷新", [])
    assert pack.evidence == []
    assert pack.docs == []
    assert pack.answerable is False
    assert pack.confidence == "low"
    assert [item.code for item in pack.missing_evidence] == ["no_context_match"]
    assert "no_context_match" not in pack.next_queries


# --------------------------------------------------------------------------- 配额


def test_single_file_cap_25_percent_lets_other_files_fill(store, seed_file, sym, cand) -> None:
    seed_file(
        store,
        path="src/big.py",
        # 行距 >10 → 不触发相邻区间合并（本测试只验证单文件配额）
        symbols=[sym(f"big{i}", f"big{i}", start=i * 50 + 1) for i in range(3)],
        bodies={f"big{i}": "x" * 396 for i in range(3)},
    )
    _long(store, seed_file, sym, "src/other.py", "other", 1, 196)
    candidates = [
        cand("src/big.py", f"big{index}", index * 50 + 1, score=1.0 - index / 10)
        for index in range(3)
    ] + [cand("src/other.py", "other", 1, score=0.5)]
    config = BudgetConfig(hard_cap=1_000, framework_overhead=0)

    pack = assemble(store, "q", candidates, config=config)
    assert sum(1 for item in pack.evidence if item.path == "src/big.py") == 2
    assert any(item.path == "src/other.py" for item in pack.evidence)  # 其他文件补位
    assert pack.budget is not None and pack.budget.truncated is True


def test_tier3_quota_uses_30_percent_of_used_budget(store, seed_file, sym, cand) -> None:
    _long(store, seed_file, sym, "src/seed.py", "seed", 1, 396)     # 100 token，tier1
    _long(store, seed_file, sym, "src/g1.py", "g1", 1, 396)         # 100 token，tier3
    _long(store, seed_file, sym, "src/g2.py", "g2", 1, 76)          # 20 token，tier3
    candidates = [
        cand("src/seed.py", "seed", 1, score=1.0),
        cand("src/g1.py", "g1", 1, score=0.5, tier=3, channels={}, graph_depth=1),
        cand("src/g2.py", "g2", 1, score=0.4, tier=3, channels={}, graph_depth=1),
    ]
    config = BudgetConfig(hard_cap=10_000, framework_overhead=0, tier3_ratio=0.30)

    # TASK-095：tier3 候选分数（0.5/0.4）低于新闸门（top1×0.50 = 0.5）——
    # 本用例只验**配额**这条腿（分数闸门另有用例），故显式关掉闸门。
    pack = assemble(store, "q", candidates, config=replace(config, score_ratio=0.0))
    paths = [item.path for item in pack.evidence]
    assert "src/seed.py" in paths
    assert "src/g1.py" not in paths       # 100 > 30% × (100)
    assert "src/g2.py" in paths           # 20 ≤ 30% × (100 + 20)
    assert pack.budget is not None and pack.budget.omitted_count == 1


def test_spec_floor_keeps_a_doc_even_when_last(store, seed_file, sym, cand) -> None:
    for index in range(4):
        _long(store, seed_file, sym, f"src/f{index}.py", f"f{index}", 1, 396)
    seed_file(
        store,
        path="docs/auth.md",
        language="markdown",
        spec_blocks=[_spec_block("docs/auth.md", "Token Refresh")],
    )
    candidates = [
        cand(f"src/f{i}.py", f"f{i}", 1, score=1.0 - i / 10) for i in range(4)
    ] + [cand("docs/auth.md", "架构 > Token Refresh", 10, score=0.01, kind="spec")]
    config = BudgetConfig(hard_cap=1_200, framework_overhead=0, single_file_ratio=1.0)

    pack = assemble(store, "为什么这样设计", candidates, config=config)
    assert len(pack.docs) >= 1
    assert pack.docs[0].heading_path == "架构 > Token Refresh"


# --------------------------------------------------------------------------- 去重三招


def test_dedup_adjacent_interval_merge_overlap(store, seed_file, sym, cand) -> None:
    seed_file(
        store,
        path="src/a.py",
        symbols=[sym("one", "one", start=40, end=70), sym("two", "two", start=65, end=90)],
    )
    candidates = [
        cand("src/a.py", "one", 40, score=1.0, end=70),
        cand("src/a.py", "two", 65, score=0.9, end=90),
    ]
    config = BudgetConfig(hard_cap=10_000, framework_overhead=0, single_file_ratio=1.0)
    pack = assemble(store, "q", candidates, config=config)
    assert len(pack.evidence) == 1
    item = pack.evidence[0]
    assert item.lines == (40, 90)
    assert item.elided_lines == 0
    assert "相邻区间合并" in item.reason


def test_dedup_adjacent_interval_merge_counts_gap(store, seed_file, sym, cand) -> None:
    seed_file(
        store,
        path="src/a.py",
        symbols=[sym("one", "one", start=40, end=70), sym("two", "two", start=80, end=90)],
    )
    candidates = [
        cand("src/a.py", "one", 40, score=1.0, end=70),
        cand("src/a.py", "two", 80, score=0.9, end=90),
    ]
    config = BudgetConfig(hard_cap=10_000, framework_overhead=0, single_file_ratio=1.0)
    pack = assemble(store, "q", candidates, config=config)
    assert len(pack.evidence) == 1
    assert pack.evidence[0].lines == (40, 90)
    assert pack.evidence[0].elided_lines == 9  # 71..79


def test_dedup_far_apart_chunks_are_not_merged(store, seed_file, sym, cand) -> None:
    seed_file(
        store,
        path="src/a.py",
        symbols=[sym("one", "one", start=40, end=70), sym("two", "two", start=200, end=210)],
    )
    candidates = [
        cand("src/a.py", "one", 40, score=1.0, end=70),
        cand("src/a.py", "two", 200, score=0.9, end=210),
    ]
    config = BudgetConfig(hard_cap=10_000, framework_overhead=0, single_file_ratio=1.0)
    assert len(assemble(store, "q", candidates, config=config).evidence) == 2


def test_dedup_same_symbol_prefers_cpp_definition_over_declaration(
    store, seed_file, sym, cand
) -> None:
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
            ),
        },
    )
    candidates = [
        cand("db/db_impl.h", "DBImpl::Get", 10, score=1.0, end=11),
        cand("db/db_impl.cc", "DBImpl::Get", 100, score=0.9, end=104),
    ]
    config = BudgetConfig(hard_cap=10_000, framework_overhead=0, single_file_ratio=1.0)

    pack = assemble(store, "Get 读取顺序", candidates, config=config)

    assert len(pack.evidence) == 1
    assert pack.evidence[0].path == "db/db_impl.cc"
    assert pack.evidence[0].lines == (100, 104)
    assert "同符号聚合×1" in pack.evidence[0].reason
    assert pack.budget is not None and pack.budget.omitted_count == 1


def test_dedup_cpp_declarations_keep_score_order(store, seed_file, sym, cand) -> None:
    for path in ("include/db_impl.h", "include/db_impl_compat.h"):
        seed_file(
            store,
            path=path,
            language="cpp",
            symbols=[sym("Get", "DBImpl::Get", kind="method", start=10, end=11)],
            bodies={"DBImpl::Get": "Status Get() override;"},
        )
    candidates = [
        cand("include/db_impl.h", "DBImpl::Get", 10, score=1.0, end=11),
        cand("include/db_impl_compat.h", "DBImpl::Get", 10, score=0.9, end=11),
    ]
    config = BudgetConfig(hard_cap=10_000, framework_overhead=0, single_file_ratio=1.0)

    pack = assemble(store, "Get", candidates, config=config)

    assert len(pack.evidence) == 1
    assert pack.evidence[0].path == "include/db_impl.h"
    assert pack.budget is not None and pack.budget.omitted_count == 1


def test_dedup_same_symbol_aggregation(store, seed_file, sym, cand) -> None:
    seed_file(
        store,
        path="src/a.py",
        symbols=[
            sym("run", "Foo.run", start=10, end=20),
            sym("run2", "Foo.run", start=200, end=210),
        ],
    )
    candidates = [
        cand("src/a.py", "Foo.run", 10, score=1.0, end=20),
        cand("src/a.py", "Foo.run", 200, score=0.9, end=210),
    ]
    config = BudgetConfig(hard_cap=10_000, framework_overhead=0, single_file_ratio=1.0)
    pack = assemble(store, "q", candidates, config=config)
    assert len(pack.evidence) == 1
    assert "同符号聚合×1" in pack.evidence[0].reason
    assert pack.budget is not None and pack.budget.omitted_count == 1


def test_dedup_skeleton_degradation_elides_lines(store, seed_file, sym, cand) -> None:
    body = "\n".join(f"line {index}" for index in range(400))
    seed_file(
        store,
        path="src/huge.py",
        symbols=[sym("huge", "huge", start=1, end=400)],
        bodies={"huge": body},
    )
    candidates = [cand("src/huge.py", "huge", 1, score=1.0, end=400)]
    config = BudgetConfig(hard_cap=1_000, framework_overhead=0, single_file_ratio=1.0)

    pack = assemble(store, "q", candidates, config=config)
    item = pack.evidence[0]
    assert item.elided_lines > 0
    assert item.lines == (1, 16)
    assert len(item.content.splitlines()) <= 20


def test_skeleton_degradation_not_triggered_when_budget_allows(store, seed_file, sym, cand) -> None:
    body = "\n".join(f"line {index}" for index in range(400))
    seed_file(
        store,
        path="src/huge.py",
        symbols=[sym("huge", "huge", start=1, end=400)],
        bodies={"huge": body},
    )
    candidates = [cand("src/huge.py", "huge", 1, score=1.0, end=400)]
    config = BudgetConfig(hard_cap=100_000, framework_overhead=0, single_file_ratio=1.0)
    item = assemble(store, "q", candidates, config=config).evidence[0]
    assert item.elided_lines == 0
    assert item.lines == (1, 400)


# --------------------------------------------------------------------------- 判定矩阵


def _pool(cand, *, explicit: int, consensus: int, plain: int, specs: int = 0):
    candidates = []
    for index in range(explicit):
        candidates.append(
            cand(f"src/e{index}.py", f"e{index}", 1, score=1.0,
                 channels={"exact": 1, "bm25": 1, "vector": 1})
        )
    for index in range(consensus):
        candidates.append(
            cand(f"src/c{index}.py", f"c{index}", 1, score=0.8, channels={"bm25": 1, "vector": 1})
        )
    for index in range(plain):
        candidates.append(
            cand(f"src/p{index}.py", f"p{index}", 1, score=0.5, channels={"bm25": 1})
        )
    for index in range(specs):
        candidates.append(
            cand(f"docs/s{index}.md", f"h{index}", 1, score=0.4, kind="spec",
                 channels={"bm25": 1})
        )
    return candidates


@pytest.mark.parametrize(
    "explicit,consensus,plain,specs,structural,graph_boundary,answerable,confidence",
    [
        (1, 3, 0, 0, False, False, True, "high"),      # explicit + ≥3 共识
        (1, 1, 0, 0, False, False, True, "low"),       # explicit 但共识不足 → 其余
        (1, 3, 0, 0, False, True, True, "low"),        # graph_boundary 阻断 high
        # R22/TASK-022 口径更新：共识候选只覆盖 1 个文件 + 无 explicit → 不可回答
        (0, 1, 1, 0, False, False, False, "low"),
        (0, 0, 2, 0, False, False, False, "low"),      # 仅单通道
        # R22/TASK-022：双通道共识跨 2 个文件且最强候选被双通道命中 → 仍算有据
        (0, 2, 0, 0, False, False, True, "medium"),
        (0, 0, 0, 0, True, False, True, "low"),        # 结构路由非空（接口预留）
    ],
)
def test_answerable_confidence_matrix(
    store, seed_file, sym, cand,
    explicit, consensus, plain, specs, structural, graph_boundary, answerable, confidence,
) -> None:
    for index in range(max(explicit, consensus, plain)):
        _long(store, seed_file, sym, f"src/x{index}.py", f"x{index}", 1, 40)
    candidates = _pool(cand, explicit=explicit, consensus=consensus, plain=plain, specs=specs)
    config = BudgetConfig(hard_cap=100_000, framework_overhead=0, single_file_ratio=1.0)
    pack = assemble(
        store, "q", candidates, config=config,
        structural_result=structural, graph_boundary=graph_boundary,
    )
    assert pack.answerable is answerable
    assert pack.confidence == confidence


def test_spec_only_hit_is_not_answerable_and_reports_low_confidence(
    store, seed_file, sym, cand
) -> None:
    """单条 spec（单通道、单文件）→ 不可回答，且 confidence 随之为 low（R22/TASK-022 口径）。

    旧口径下这种包是 `answerable=False` 但 `confidence=medium`（spec-only → medium）；
    TASK-022 要求 `answerable=False` 时不得用中等把握掩盖不可回答，故同步降为 low。
    """
    seed_file(
        store,
        path="docs/auth.md",
        language="markdown",
        spec_blocks=[_spec_block("docs/auth.md", "Token Refresh")],
    )
    candidates = [cand("docs/auth.md", "架构 > Token Refresh", 10, score=0.4, kind="spec")]
    pack = assemble(store, "q", candidates)
    assert pack.answerable is False
    assert pack.confidence == "low"


# --------------------------------------------------------------------------- 缺失证据与 nextQueries


def test_missing_evidence_from_freshness_and_signals(store, seed_file, sym, cand) -> None:
    _long(store, seed_file, sym, "src/a.py", "f", 1, 40)
    freshness = Freshness(indexed_at=100, stale_files=("src/a.py",), indexing_files=("src/b.py",))
    pack = assemble(
        store,
        "q",
        [cand("src/a.py", "f", 1, score=1.0)],
        freshness=freshness,
        signals=IndexSignals(unresolved_count=3),
    )
    codes = [item.code for item in pack.missing_evidence]
    assert codes == ["index_stale", "indexing_pending", "unresolved_reference"]
    assert "缺" in pack.missing_evidence[0].message or "落后" in pack.missing_evidence[0].message


def test_missing_evidence_stale_doc_reference(store, seed_file, sym, cand) -> None:
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[sym("refresh", "TokenService.refresh", kind="method", start=45)],
    )
    seed_file(
        store,
        path="docs/auth.md",
        language="markdown",
        spec_blocks=[_spec_block("docs/auth.md", "Token Refresh")],
    )
    store.add_spec_refs(
        [
            (
                "docs/auth.md:架构 > Token Refresh:10",
                "src/auth/token_service.py:TokenService.refresh:45",
            )
        ]
    )
    store.apply_deletions(["src/auth/token_service.py"])

    spec = cand("docs/auth.md", "架构 > Token Refresh", 10, score=0.4, kind="spec")
    signals = collect_index_signals(store, [spec])
    assert signals.stale_doc_refs == {
        "docs/auth.md:架构 > Token Refresh:10": ("TokenService.refresh",)
    }
    pack = assemble(store, "q", [spec], signals=signals)
    codes = [item.code for item in pack.missing_evidence]
    assert "stale_doc_reference" in codes
    stale = next(item for item in pack.missing_evidence if item.code == "stale_doc_reference")
    assert stale.symbol == "TokenService.refresh"
    assert pack.docs[0].stale_refs == ("TokenService.refresh",)


def test_missing_evidence_retrieval_truncated(store, seed_file, sym, cand) -> None:
    for index in range(3):
        _long(store, seed_file, sym, f"src/f{index}.py", f"f{index}", 1, 400)
    candidates = [cand(f"src/f{i}.py", f"f{i}", 1, score=1.0 - i / 10) for i in range(3)]
    config = BudgetConfig(hard_cap=200, framework_overhead=0, single_file_ratio=1.0)
    pack = assemble(store, "q", candidates, config=config)
    assert "retrieval_truncated" in [item.code for item in pack.missing_evidence]


def test_next_queries_are_deterministic_and_bounded(store, seed_file, sym, cand) -> None:
    """无缺口 → 走路径兼底；确定性且 ≤3 条（TASK-096 §B：不再从 pool[:3] 取符号）。"""
    _long(store, seed_file, sym, "src/auth/token_service.py", "TokenService.refresh", 1, 40)
    candidates = [cand("src/auth/token_service.py", "TokenService.refresh", 1, score=1.0)]
    first = assemble(store, "q", candidates)
    second = assemble(store, "q", candidates)
    assert first.next_queries == second.next_queries
    assert 1 <= len(first.next_queries) <= 3
    # 本用例无任何缺口（无 stale / unresolved / truncated）→ 退回"用路径构造"的兼底。
    # 旧口径会取 pool top-1 的符号（"refresh 的调用方有哪些"）——那正是 §B 要修的行为。
    assert first.next_queries == ["src/auth/token_service.py 里还有哪些与查询相关的符号"]


def test_e_numbering_is_shared_between_evidence_and_docs(store, seed_file, sym, cand) -> None:
    seed_file(store, path="src/a.py", symbols=[sym("f", "f", start=1)])
    seed_file(
        store,
        path="docs/auth.md",
        language="markdown",
        spec_blocks=[_spec_block("docs/auth.md", "Token Refresh")],
    )
    candidates = [
        cand("src/a.py", "f", 1, score=1.0),
        cand("docs/auth.md", "架构 > Token Refresh", 10, score=0.5, kind="spec"),
    ]
    pack = assemble(store, "q", candidates)
    ids = [item.id for item in pack.evidence] + [item.id for item in pack.docs]
    assert ids == ["E1", "E2"]
    assert pack.docs[0].id == "E2"


def test_index_signals_without_store_inputs(store, seed_file, sym, cand) -> None:
    _long(store, seed_file, sym, "src/a.py", "f", 1, 40)
    signals = collect_index_signals(store, [cand("src/a.py", "f", 1, score=1.0)])
    assert signals.unresolved_count == 0
    assert dict(signals.stale_doc_refs) == {}


# --------------------------------------------------------------------------- 性能


def test_assembly_under_200ms_for_200_candidates(store, seed_file, sym, cand) -> None:
    for index in range(20):
        seed_file(
            store,
            path=f"src/mod{index}.py",
            symbols=[sym(f"f{index}_{j}", f"f{index}_{j}", start=j * 20 + 1)
                     for j in range(10)],
        )
    candidates = [
        cand(f"src/mod{index}.py", f"f{index}_{j}", j * 20 + 1, score=1.0 - (index * 10 + j) / 1000)
        for index in range(20)
        for j in range(10)
    ]
    assert len(candidates) == 200

    started = time.perf_counter()
    pack = assemble(store, "token 刷新", candidates, config=BudgetConfig(hard_cap=10_000))
    elapsed_ms = (time.perf_counter() - started) * 1000
    # 阈值 50ms 在多泳道并发跑测试时会误报（编排者实测 3 泳道并发失败 1 次，TASK-017）；
    # 放宽到 200ms 仍保留性能下限回归保护（断言不删）。
    assert elapsed_ms < 200, f"组装耗时 {elapsed_ms:.1f}ms ≥ 200ms"
    assert pack.evidence


def _placed(pack):
    """包内全部证据（evidence + docs），按 E 编号顺序。"""
    return sorted([*pack.evidence, *pack.docs], key=lambda item: int(item.id[1:]))


# --------------------------------------------------------------------------- TASK-096 §A 预算计量


def test_framework_render_overhead_is_inside_used_tokens(store, seed_file, sym, cand) -> None:
    """§A-1 DoD：框架开销占比很高的 pack 里，``used_tokens`` 必须含 header/reason/行号开销。

    构造方式：很多**极短** chunk（每条正文 8 字符）→ header+reason 的占比极高。
    修复前 ``_Slot.tokens`` 只算 ``content``，``used_tokens`` 会等于正文之和（断言失败）。
    """
    for index in range(6):
        _long(store, seed_file, sym, f"src/s{index}.py", f"s{index}", 1, 8)
    candidates = [cand(f"src/s{i}.py", f"s{i}", 1, score=1.0 - i / 10) for i in range(6)]
    config = BudgetConfig(hard_cap=10_000, framework_overhead=0, single_file_ratio=1.0)

    pack = assemble(store, "q", candidates, config=config)
    assert pack.budget is not None
    rendered = sum(estimate_render_tokens(item) for item in _placed(pack))
    content_only = sum(estimate_tokens(item.content) for item in _placed(pack))

    assert pack.budget.used_tokens == rendered
    # 短 chunk 下框架开销占大头：正文之外的 header/reason/缩进必须被记账
    assert rendered > content_only * 2, (rendered, content_only)


def test_render_accounting_matches_rendered_evidence(store, seed_file, sym, cand) -> None:
    """§A-1 一致性锁：``evidence_markdown_lines`` 与 render.py 的真实输出**逐行一致**。

    预算账用的格式副本若与渲染漂移，预算就又变成假账。本用例把两个口径钉在一起
    （``render.py`` 属 TASK-095 领地，本卡不改它，靠本测试防漂移）。
    """
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[sym("refresh", "TokenService.refresh", kind="method", start=45, end=46)],
        bodies={"TokenService.refresh": "def refresh(self):\n    return self.store.rotate()"},
    )
    pack = assemble(
        store,
        "q",
        [cand("src/auth/token_service.py", "TokenService.refresh", 45, score=1.0, end=46)],
        freshness=Freshness(indexed_at=100),
    )
    rendered = render_markdown(pack)
    for item in pack.evidence:
        assert all(line in rendered.splitlines() for line in evidence_markdown_lines(item))


def test_estimate_tokens_charges_cjk_higher_than_ascii() -> None:
    """§A-2 DoD：中文文本的估算**显著高于** ``chars/4``（旧口径低估约 2.7 倍）。"""
    chinese = "输入准入是运行时校验来源与绑定会话的第一道闸门" * 4
    assert all("\u4e00" <= char <= "\u9fff" for char in chinese), "样本必须是纯汉字"
    # 旧口径 chars/4 只会给 len/4；新口径至少翻倍（纯 CJK 按 1.5 字符/token）。
    assert estimate_tokens(chinese) >= 2 * math.ceil(len(chinese) / 4)

    # 纯 ASCII 结果与旧口径完全一致（英文代码不被误伤）
    ascii_text = "def refresh(self):\n    return self.store.rotate()\n"
    assert estimate_tokens(ascii_text) == max(1, math.ceil(len(ascii_text) / 4))
    assert estimate_tokens("") == 0


def test_cjk_budget_shrinks_the_pack(store, seed_file, sym, cand) -> None:
    """§A-2 行为面：中文正文在旧口径下\"看起来很便宜\"，新口径应装得更少。"""
    body = "中文注释与说明" * 100  # 800 字符，旧口径 200 token，新口径 ≥ 500
    seed_file(store, path="src/zh.py", symbols=[sym("zh", "zh", start=1, end=1)],
              bodies={"zh": body})
    config = BudgetConfig(hard_cap=600, framework_overhead=0, single_file_ratio=1.0)
    pack = assemble(store, "q", [cand("src/zh.py", "zh", 1, score=1.0)], config=config)
    assert pack.budget is not None
    assert pack.budget.used_tokens > 400
    assert pack.budget.used_tokens <= config.hard_cap


# --------------------------------------------------------------------------- TASK-096 §B 自愈查询

def _unresolved_pack(store, seed_file, sym, cand, *, answerable: bool):
    """构造“池顶是测试函数 + 缺口里有真符号”的 pack（§B-1 的真实场景缩影）。"""
    _long(store, seed_file, sym, "tests/test_gateway.py", "test_user_input_reaches_runtime", 1, 40)
    _long(store, seed_file, sym, "src/runtime.py", "Runtime.accept", 1, 40)
    candidates = [
        cand("tests/test_gateway.py", "test_user_input_reaches_runtime", 1, score=1.0),
        cand("src/runtime.py", "Runtime.accept", 1, score=0.9),
    ]
    signals = IndexSignals(unresolved_count=3, unresolved_symbols=("runtime_input",))
    return assemble(store, "输入准入", candidates, signals=signals)


def test_next_queries_come_from_gaps_not_pool_top_symbols(store, seed_file, sym, cand) -> None:
    """§B-1 DoD：生成源是 ``missing_evidence``，**不是** pool top-3 符号。

    修复前：top-1 是测试函数 → 建议\"查这个测试函数的调用方\"（真实靶场实测的噪音）。
    修复后：应给出缺口里的真符号（``runtime_input``），且不得出现测试函数名。
    """
    pack = _unresolved_pack(store, seed_file, sym, cand, answerable=False)
    assert "unresolved_reference" in [item.code for item in pack.missing_evidence]
    joined = " ".join(pack.next_queries)
    assert "runtime_input" in joined
    assert "test_user_input_reaches_runtime" not in joined, "不得再建议去查测试函数"
    assert "Runtime.accept" not in joined, "也不得从 pool 取符号"
    assert len(pack.next_queries) <= 3


def test_next_queries_empty_when_answerable(store, seed_file, sym, cand) -> None:
    """§B-2 DoD：``answerable=true`` → ``next_queries == []``（证据够了就不打扰）。"""
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[sym("refresh", "TokenService.refresh", kind="method", start=45, end=46)],
    )
    pack = assemble(
        store,
        "q",
        [
            cand(
                "src/auth/token_service.py",
                "TokenService.refresh",
                45,
                score=1.0,
                reasons=["explicit symbol TokenService.refresh"],
                channels={"exact": 1, "bm25": 1},
                end=46,
            )
        ],
    )
    assert pack.answerable is True
    assert pack.next_queries == []
    assert "Suggested Next Queries" not in render_markdown(pack)


def test_next_queries_still_generated_when_not_answerable(store, seed_file, sym, cand) -> None:
    """防修过头：``answerable=false`` 时仍生成（走兜底路径也不能为空）。"""
    pack = _unresolved_pack(store, seed_file, sym, cand, answerable=False)
    assert pack.answerable is False
    assert pack.next_queries


def test_retrieval_truncated_generates_no_query(store, seed_file, sym, cand) -> None:
    """§B-1 DoD：``retrieval_truncated`` 不生成查询（预算不够，改问帮不上）。"""
    for index in range(3):
        _long(store, seed_file, sym, f"src/f{index}.py", f"f{index}", 1, 400)
    candidates = [cand(f"src/f{i}.py", f"f{i}", 1, score=1.0 - i / 10) for i in range(3)]
    config = BudgetConfig(hard_cap=200, framework_overhead=0, single_file_ratio=1.0)

    pack = assemble(store, "q", candidates, config=config)
    codes = [item.code for item in pack.missing_evidence]
    assert "retrieval_truncated" in codes
    assert pack.next_queries == [], "只有 retrieval_truncated 时不得凭空造查询"
    assert all("预算" not in query for query in pack.next_queries)


def test_stale_doc_gap_asks_where_the_symbol_is_now(store, seed_file, sym, cand) -> None:
    """§B-1 模板表：``stale_doc_reference`` → \"{symbol} 现在在哪里实现\"。"""
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[sym("refresh", "TokenService.refresh", kind="method", start=45)],
    )
    seed_file(
        store,
        path="docs/auth.md",
        language="markdown",
        spec_blocks=[_spec_block("docs/auth.md", "Token Refresh")],
    )
    store.add_spec_refs(
        [
            (
                "docs/auth.md:架构 > Token Refresh:10",
                "src/auth/token_service.py:TokenService.refresh:45",
            )
        ]
    )
    store.apply_deletions(["src/auth/token_service.py"])

    spec = cand("docs/auth.md", "架构 > Token Refresh", 10, score=0.4, kind="spec")
    pack = assemble(store, "q", [spec], signals=collect_index_signals(store, [spec]))

    assert pack.answerable is False, "只有文档、无代码命中 → 不可回答"
    stale = next(item for item in pack.missing_evidence if item.code == "stale_doc_reference")
    assert f"{stale.symbol} 现在在哪里实现" in pack.next_queries


def test_consensus_without_structured_code_is_not_answerable(store, seed_file, sym, cand) -> None:
    """TASK-106：双通道共识全落在文档/前导段上时不得判可答。

    真实反例（HelloAgents H-20）：问“仓库里 Qdrant 向量库的实现在哪”而该能力**不存在**，
    README 与 ``.env.example`` 同时被 BM25/Vector 命中，旧规则因此判 ``answerable=True``。
    新口径要求共识候选里至少有一个结构化代码切片（``kind=code/test``）。
    """
    seed_file(
        store,
        path="src/config.py",
        language="python",
        symbols=[sym("cfg", "cfg", kind="fallback_block", start=1)],
    )
    seed_file(
        store,
        path="docs/readme.md",
        language="markdown",
        spec_blocks=[_spec_block("docs/readme.md", "Overview")],
    )
    consensus_fallback = cand("src/config.py", "(module)", 1, score=1.0, kind="fallback")
    consensus_fallback.channel_ranks = {"bm25": 1, "vector": 1}
    consensus_spec = cand("docs/readme.md", "Overview", 1, score=0.9, kind="spec")
    consensus_spec.channel_ranks = {"bm25": 2, "vector": 2}

    pack = assemble(store, "q", [consensus_fallback, consensus_spec])

    assert pack.answerable is False
    assert pack.confidence == "low"


def test_consensus_with_structured_code_stays_answerable(store, seed_file, sym, cand) -> None:
    """防修过头：共识里有真实代码切片时仍可答（不能把所有多通道查询都压掉）。"""
    seed_file(
        store,
        path="src/store.py",
        symbols=[sym("get", "Store.get", kind="method", start=10)],
    )
    seed_file(
        store,
        path="docs/readme.md",
        language="markdown",
        spec_blocks=[_spec_block("docs/readme.md", "Overview")],
    )
    code = cand("src/store.py", "Store.get", 10, score=0.8, kind="code")
    code.channel_ranks = {"bm25": 3, "vector": 3}
    doc = cand("docs/readme.md", "Overview", 1, score=1.0, kind="spec")
    doc.channel_ranks = {"bm25": 1, "vector": 1}

    pack = assemble(store, "q", [doc, code])

    assert pack.answerable is True


def test_gap_message_lists_unresolved_symbol_names(store, seed_file, sym, cand) -> None:
    """缺口 message 里的符号名来自 ``unresolved_symbols``（标识符形态过滤，不含表达式）。"""
    _long(store, seed_file, sym, "src/a.py", "f", 1, 40)
    pack = assemble(
        store,
        "q",
        [cand("src/a.py", "f", 1, score=1.0)],
        signals=IndexSignals(
            unresolved_count=5,
            unresolved_symbols=("getattr", "execute", "workflow_id", "extra_one"),
        ),
    )
    message = next(m.message for m in pack.missing_evidence if m.code == "unresolved_reference")
    # 最多列 3 个（余下用"等"带过），避免 message 自身膨胀；`symbol` 取首个。
    assert "getattr, execute, workflow_id 等" in message
    assert "5 个符号引用无法解析" in message


def test_first_backfill_candidate_is_not_skeleton_degraded(store, seed_file, sym, cand) -> None:
    """补检首位候选不被 skeleton 降级（TASK-MCP-BUDGET）。

    实测来源（cockpit-agents-server，2026-09-16）：``StreamingService.process_with_streaming``
    199 行 / 2016 token，是**补检池首位**、也是唯一能回答"流式完整链路"的证据。但补检的单成员
    公平上限（``backfill_single_ratio`` 0.25 × ``backfill_ratio`` 0.35 × hard_cap）在 16K 下
    只有 1400 token，它被强制降级成"签名 + 前 15 行"——恰好丢掉了问题要问的那段正文
    （memory_query / preference_updated / complete 三个事件与知识检索调用点）。

    公平上限的原始目的是"防止一个大调度器独吞配额、让后面 13 个成员无处可放"
    （cockpit-0035 的 ``_invoke``）——那个场景里目标是**多个小成员**；当首位目标本身就是
    唯一答案时，让位换不来任何东西。故只豁免首位，其余候选仍按原上限排队。
    """
    # 首位：一个超长方法（远超 backfill_skeleton_lines，也超单成员 token 上限）
    # 声明多行（``_long`` 默认 start=end，无法触发按行数降级）
    seed_file(
        store,
        path="src/big.py",
        symbols=[sym("huge_method", "huge_method", start=1, end=220)],
        bodies={"huge_method": "x" * 6000},
    )
    # 后面：若干小成员，证明豁免没有让它们全部落空
    for i in range(4):
        _long(store, seed_file, sym, f"src/small{i}.py", f"small{i}", 1, 40)
    # 主循环先装一个种子，其余（含 huge_method）走补检
    _long(store, seed_file, sym, "src/seed.py", "seed", 1, 40)
    config = BudgetConfig(hard_cap=14_000, single_file_tokens=2_500, docs_tokens=950)
    pack = assemble(
        store,
        "q",
        [cand("src/seed.py", "seed", 1, score=1.0)],
        config=config,
        backfill=[(cand("src/big.py", "huge_method", 1, score=0.3), "gap backfill: G1")],
    )
    items = [*pack.evidence, *pack.docs]
    huge = next(item for item in items if item.symbol == "huge_method")
    # 未被降级：省略行数为 0（降级后是 6000 行正文只留 15 行）
    assert huge.elided_lines == 0, f"首位补检候选被降级了（elided={huge.elided_lines}）"
    assert huge.lines is not None and huge.lines[1] - huge.lines[0] + 1 > 100
    assert "gap backfill" in huge.reason  # 来源标注仍在


def test_non_first_backfill_candidate_respects_single_cap(
    store, seed_file, sym, cand
) -> None:
    """非首位补检候选仍受单成员上限约束（豁免只给首位，别把防独吞闸门拆掉）。"""
    _long(store, seed_file, sym, "src/seed.py", "seed", 1, 40)
    _long(store, seed_file, sym, "src/first.py", "first_small", 1, 40)
    seed_file(
        store,
        path="src/huge.py",
        symbols=[sym("huge_method", "huge_method", start=1, end=220)],
        bodies={"huge_method": "x" * 6000},
    )
    config = BudgetConfig(hard_cap=14_000, single_file_tokens=2_500, docs_tokens=950)
    pack = assemble(
        store,
        "q",
        [cand("src/seed.py", "seed", 1, score=1.0)],
        config=config,
        backfill=[
            (cand("src/first.py", "first_small", 1, score=0.4), "gap backfill: G1"),
            (cand("src/huge.py", "huge_method", 1, score=0.3), "gap backfill: G1"),
        ],
    )
    items = [*pack.evidence, *pack.docs]
    huge = next((item for item in items if item.symbol == "huge_method"), None)
    if huge is not None:
        # 作为第二位候选不得免检：要么被降级，要么被放弃；不能整段进包
        assert huge.elided_lines > 0, "非首位候选不应享受首位豁免"
