"""TASK-095：分数相对阈值截断（§A）+ Code 节内部分组渲染（§B）。

覆盖任务卡 DoD 的七项：

1. 分数低于 ``top1 × ratio`` 的候选**不出现在**渲染结果中（§A）；
2. ``code_floor`` / ``spec_floor`` 保底**不受闸门影响**（否则纯文档查询可能被清空）；
3. 测试候选**无论分数**归入 ``#### Tests``；
4. 空组不渲染（无相关候选时不出现 ``#### Related``）；
5. 既有顶层节名与顺序逐字未变（回归保护，见 ``test_render.py`` 的快照用例）；
6. ``[E*]`` 编号 = 装填顺序，**不因分组而重排**；
7. ``CONTEXT_SCORE_RATIO`` 改配置即改行为（含环境变量覆盖与非 spec 参考分口径）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from nova_core.contextpack import (
    CONTEXT_SCORE_RATIO,
    SCORE_RATIO_ENV,
    BudgetConfig,
    assemble,
    budget_for,
    render_evidence_for_prompt,
    render_markdown,
)
from nova_core.types import Flow, FlowNode, SpecBlockDef

SPEC_PATH = "docs/design.md"
#: 本文件一律显式给 hard_cap，避免预算成为"条数变化"的混淆变量。
ROOMY = dict(hard_cap=100_000, framework_overhead=0, single_file_ratio=1.0)


def _code(store, seed_file, sym, path: str, fqn: str, chars: int = 40) -> None:
    seed_file(
        store, path=path, symbols=[sym(fqn, fqn, start=1, end=1)], bodies={fqn: "x" * chars}
    )


def _spec(store, seed_file, *, heading: str = "架构 > 节0", start: int = 10, chars: int = 100):
    """一个文件里落下一个 spec 块（返回它的 ``(heading_path, start)``）。"""
    block = SpecBlockDef(
        path=SPEC_PATH,
        heading=heading.rsplit(" > ", 1)[-1],
        heading_path=heading,
        level=2,
        start_line=start,
        end_line=start + 4,
        content="设计" * (chars // 2),
        doctype="design",
    )
    seed_file(store, path=SPEC_PATH, language="markdown", spec_blocks=[block])
    return block.heading_path, block.start_line


# --------------------------------------------------------------------------- §A 闸门


def test_candidates_below_score_floor_are_not_placed_nor_rendered(store, seed_file, sym, cand):
    """§A：低于 ``top1 × score_ratio`` 的候选不装填，也不出现在 Markdown 里（如实标注缺口）。

    TASK-108：本测试**显式给定** ``score_ratio=0.5``（而非依赖默认值）——它测的是
    “闸门规则本身”，不应随默认值调优而失败。
    """
    scores = (1.0, 0.8, 0.4)
    for index in range(len(scores)):
        _code(store, seed_file, sym, f"src/f{index}.py", f"f{index}")
    candidates = [
        cand(f"src/f{i}.py", f"f{i}", 1, score=score) for i, score in enumerate(scores)
    ]

    pack = assemble(
        store, "q", candidates, config=BudgetConfig(**ROOMY, score_ratio=0.5)
    )
    md = render_markdown(pack)

    assert [item.path for item in pack.evidence] == ["src/f0.py", "src/f1.py"]
    assert "src/f2.py" not in md
    # 不静默丢弃：缺口进 missingEvidence（CF-03 message 为自由文本，不新增字段）。
    message = next(m.message for m in pack.missing_evidence if m.code == "retrieval_truncated")
    assert "低于相对分数阈值" in message and "top1×0.5" in message


def test_threshold_is_relative_to_non_spec_top_not_pool_top(store, seed_file, sym, cand):
    """§A 参考分口径：top1 = **非 spec 候选**的最高分（spec 走 docs_ratio 这条独立路径）。

    若误用池总分（spec 5.0）→ 阈值 2.5 → 两条代码证据全被截掉；正确口径下阈值 0.5 → 都保留。
    """
    _spec(store, seed_file)
    for index in range(2):
        _code(store, seed_file, sym, f"src/c{index}.py", f"c{index}")
    candidates = [
        cand(SPEC_PATH, "架构 > 节0", 10, score=5.0, kind="spec"),
        cand("src/c0.py", "c0", 1, score=1.0),
        cand("src/c1.py", "c1", 1, score=0.6),
    ]

    pack = assemble(store, "q", candidates, config=BudgetConfig(**ROOMY))

    assert [item.path for item in pack.evidence] == ["src/c0.py", "src/c1.py"]


def test_pure_doc_query_is_not_hurt_when_pool_has_no_non_spec_candidate(
    store, seed_file, sym, cand
):
    """纯文档查询（池里没有代码/测试候选）→ 闸门关闭，不被本机制伤害。"""
    blocks = [
        SpecBlockDef(
            path=SPEC_PATH,
            heading=f"节{index}",
            heading_path=f"架构 > 节{index}",
            level=2,
            start_line=10 + index * 20,
            end_line=14 + index * 20,
            content="设计说明" * 10,
            doctype="design",
        )
        for index in range(3)
    ]
    seed_file(store, path=SPEC_PATH, language="markdown", spec_blocks=blocks)
    candidates = [
        cand(SPEC_PATH, block.heading_path, block.start_line, score=1.0 - index / 10,
             kind="spec")
        for index, block in enumerate(blocks)
    ]

    pack = assemble(store, "为什么这样设计", candidates, config=BudgetConfig(**ROOMY))

    assert len(pack.docs) == 3
    assert pack.budget.truncated is False


def test_code_floor_bypasses_the_score_gate(store, seed_file, sym, cand):
    """§A 纪律 2：``code_floor`` 保底**不受闸门约束**（保底是"至少给这些"）。

    构造：池顶是非 spec 的**测试**候选（1.0），唯一的**代码**候选只有 0.2（低于阈值 0.5）
    → 贪心循环在它之前就停了；保底补入仍必须把它装进来。
    """
    _spec(store, seed_file)
    seed_file(store, path="tests/test_top.py", symbols=[sym("t", "t", start=1, end=1)])
    _code(store, seed_file, sym, "src/weak.py", "weak")
    candidates = [
        cand(SPEC_PATH, "架构 > 节0", 10, score=0.9, kind="spec"),
        cand("tests/test_top.py", "t", 1, score=1.0, kind="test"),
        cand("src/weak.py", "weak", 1, score=0.2),
    ]

    pack = assemble(store, "q", candidates, config=BudgetConfig(**ROOMY))

    assert "src/weak.py" in [item.path for item in pack.evidence], (
        "代码保底必须绕过 §A 闸门（否则文档密集查询会一块代码都不剩）"
    )
    assert "tests/test_top.py" in [item.path for item in pack.evidence]


def test_spec_floor_bypasses_the_score_gate(store, seed_file, sym, cand):
    """§A 纪律 2：``spec_floor`` 保底同样不受闸门约束（纯文档答案不被清空）。"""
    _spec(store, seed_file, heading="架构 > 保底节")
    _code(store, seed_file, sym, "src/a.py", "a")
    candidates = [
        cand("src/a.py", "a", 1, score=1.0),
        cand(SPEC_PATH, "架构 > 保底节", 10, score=0.01, kind="spec"),
    ]

    pack = assemble(store, "为什么这样设计", candidates, config=BudgetConfig(**ROOMY))

    assert [item.heading_path for item in pack.docs] == ["架构 > 保底节"]


def test_score_ratio_zero_disables_the_gate(store, seed_file, sym, cand):
    """``score_ratio=0.0`` = 显式关闭闸门（回到旧的贪心填满行为，供对照测量用）。"""
    scores = (1.0, 0.5, 0.0)
    for index in range(len(scores)):
        _code(store, seed_file, sym, f"src/f{index}.py", f"f{index}")
    candidates = [
        cand(f"src/f{i}.py", f"f{i}", 1, score=score) for i, score in enumerate(scores)
    ]

    on = assemble(
        store, "q", candidates, config=BudgetConfig(**ROOMY, code_floor=0, score_ratio=0.5)
    )
    off = assemble(
        store, "q", candidates, config=BudgetConfig(**ROOMY, code_floor=0, score_ratio=0.0)
    )

    assert len(on.evidence) == 2 and len(off.evidence) == 3  # 0.5 恰在阈值上沿，保留
    # TASK-108：``truncated`` 只反映**真的被预算截断**。闸门挡住的候选已计入
    # missingEvidence（上一条测试断言了文案），不再冒充“预算不够”。
    assert off.budget.truncated is False


# --------------------------------------------------------------------------- 配置生效


def test_score_ratio_config_changes_returned_count(store, seed_file, sym, cand):
    """改 ``score_ratio`` → 返回条数变化（证明不是硬编码）。"""
    for index in range(4):
        _code(store, seed_file, sym, f"src/f{index}.py", f"f{index}")
    candidates = [
        cand(f"src/f{i}.py", f"f{i}", 1, score=score)
        for i, score in enumerate((1.0, 0.8, 0.6, 0.4))
    ]

    loose = assemble(store, "q", candidates, config=BudgetConfig(**ROOMY, score_ratio=0.5))
    strict = assemble(store, "q", candidates, config=BudgetConfig(**ROOMY, score_ratio=0.8))

    assert [item.path for item in loose.evidence] == ["src/f0.py", "src/f1.py", "src/f2.py"]
    assert [item.path for item in strict.evidence] == ["src/f0.py", "src/f1.py"]


def test_env_var_overrides_the_default_ratio(monkeypatch) -> None:
    """环境变量 ``NOVA_CONTEXT_SCORE_RATIO`` 覆盖默认值（CF-06 冻结：不暴露为工具参数）。"""
    assert budget_for("fast").score_ratio == CONTEXT_SCORE_RATIO
    assert budget_for("fast").score_ratio == 0.40

    monkeypatch.setenv(SCORE_RATIO_ENV, "0.8")
    assert budget_for("fast").score_ratio == 0.8
    assert budget_for("deep").score_ratio == 0.8  # 两个模式共用同一闸门取值

    monkeypatch.setenv(SCORE_RATIO_ENV, "0")
    assert budget_for("fast").score_ratio == 0.0  # 显式关闭


@pytest.mark.parametrize("bad", ["abc", "1.5", "-0.1", ""])
def test_env_var_falls_back_to_default_on_invalid_value(monkeypatch, bad: str) -> None:
    """非法/越界的环境变量**回落默认值**而不是报错（与 IndexScope.from_env 同一纪律）。"""
    monkeypatch.setenv(SCORE_RATIO_ENV, bad)
    assert budget_for("fast").score_ratio == CONTEXT_SCORE_RATIO


def test_env_var_override_takes_effect_through_engine_budget(
    monkeypatch, store, seed_file, sym, cand
):
    """端到端：环境变量 → ``budget_for`` → ``assemble`` 的装填条数变化。

    ``code_floor=0``：保底会绕过闸门，会把“条数变化”这个信号掩盖掉。
    """
    for index in range(3):
        _code(store, seed_file, sym, f"src/f{index}.py", f"f{index}")
    candidates = [cand(f"src/f{i}.py", f"f{i}", 1, score=1.0 - i / 4) for i in range(3)]
    base = BudgetConfig(**ROOMY, code_floor=0)

    monkeypatch.setenv(SCORE_RATIO_ENV, "0.5")
    loose = assemble(
        store, "q", candidates,
        config=replace(base, score_ratio=budget_for("fast").score_ratio),
    )
    monkeypatch.setenv(SCORE_RATIO_ENV, "0.9")
    strict = assemble(
        store, "q", candidates,
        config=replace(base, score_ratio=budget_for("fast").score_ratio),
    )

    assert budget_for("fast").score_ratio == 0.9
    assert len(loose.evidence) == 3 and len(strict.evidence) == 1


# --------------------------------------------------------------------------- §B 分组渲染


def test_test_candidates_go_to_the_tests_group_regardless_of_score(store, seed_file, sym, cand):
    """§B-2 + §B-1 示例：测试**无论分数多高**都归 ``#### Tests``（渲染层归类，不改 rerank 分）。

    分数直接取自任务卡 §B-1 的真实例子：测试 2.483（100%）→ Tests；
    ``Runtime`` 1.787（72%）→ Core；邻居 1.493（60%）→ Related。
    """
    seed_file(store, path="tests/test_z.py", symbols=[sym("z", "z", start=1, end=1)])
    for index in range(2):
        _code(store, seed_file, sym, f"src/c{index}.py", f"c{index}")
    candidates = [
        cand("tests/test_z.py", "z", 1, score=2.483, kind="test"),  # 分数最高
        cand("src/c0.py", "c0", 1, score=1.787),  # 72% → Core
        cand("src/c1.py", "c1", 1, score=1.493),  # 60% → Related
    ]

    pack = assemble(store, "q", candidates, config=BudgetConfig(**ROOMY))
    md = render_markdown(pack)

    core_at = md.index("#### Core")
    related_at = md.index("#### Related")
    tests_at = md.index("#### Tests")
    assert core_at < related_at < tests_at, "组顺序：Core → Related → Tests"
    assert md.index("[E1] z — tests/test_z.py") > tests_at, "高分测试不得留在 Core"
    assert core_at < md.index("[E2] c0 — src/c0.py") < related_at
    assert related_at < md.index("[E3] c1 — src/c1.py") < tests_at


def test_related_group_holds_candidates_between_the_two_thresholds(store, seed_file, sym, cand):
    """§B-2：``score ≥ top1×0.50``（闸门内）但 ``< top1×0.70`` → ``#### Related``。"""
    for index in range(3):
        _code(store, seed_file, sym, f"src/c{index}.py", f"c{index}")
    candidates = [
        cand("src/c0.py", "c0", 1, score=1.0),
        cand("src/c1.py", "c1", 1, score=0.8),  # 0.80 → Core
        cand("src/c2.py", "c2", 1, score=0.6),  # 0.60 → Related
    ]

    pack = assemble(store, "q", candidates, config=BudgetConfig(**ROOMY))
    md = render_markdown(pack)

    core_at, related_at = md.index("#### Core"), md.index("#### Related")
    assert core_at < md.index("[E2] c1 — src/c1.py") < related_at
    assert md.index("[E3] c2 — src/c2.py") > related_at


def test_empty_groups_are_not_rendered(store, seed_file, sym, cand):
    """§B-3：空组不渲染——全是 Core 时不得出现 ``#### Related`` / ``#### Tests``。"""
    _code(store, seed_file, sym, "src/a.py", "a")

    pack = assemble(store, "q", [cand("src/a.py", "a", 1, score=1.0)],
                    config=BudgetConfig(**ROOMY))
    md = render_markdown(pack)

    assert "#### Core" in md
    assert "#### Related" not in md
    assert "#### Tests" not in md


def test_tests_only_pack_renders_only_the_tests_group(store, seed_file, sym, cand):
    """包内全是测试 → 只有 ``#### Tests``（Core/Related 空组不渲染）。"""
    seed_file(store, path="tests/test_a.py", symbols=[sym("a", "a", start=1, end=1)])
    seed_file(store, path="tests/test_b.py", symbols=[sym("b", "b", start=1, end=1)])
    candidates = [
        cand("tests/test_a.py", "a", 1, score=1.0, kind="test"),
        cand("tests/test_b.py", "b", 1, score=0.9, kind="test"),
    ]

    pack = assemble(store, "q", candidates, config=BudgetConfig(**ROOMY))
    md = render_markdown(pack)

    assert "#### Tests" in md
    assert "#### Core" not in md and "#### Related" not in md


def test_grouping_does_not_renumber_or_reorder_evidence_ids(store, seed_file, sym, cand):
    """§B-3：``[E*]`` 编号 = **装填顺序**，不因分组而重排（测试排最高分也不改编号）。"""
    seed_file(store, path="tests/test_z.py", symbols=[sym("z", "z", start=1, end=1)])
    for index in range(2):
        _code(store, seed_file, sym, f"src/c{index}.py", f"c{index}")
    candidates = [
        cand("tests/test_z.py", "z", 1, score=1.2, kind="test"),
        cand("src/c0.py", "c0", 1, score=1.0),
        cand("src/c1.py", "c1", 1, score=0.9),  # 均在闸门（1.2×0.50=0.6）之上
    ]

    pack = assemble(store, "q", candidates, config=BudgetConfig(**ROOMY))

    # 装填顺序（= 分数降序）决定编号：E1 = 最高分的测试。
    assert [(item.id, item.path) for item in pack.evidence] == [
        ("E1", "tests/test_z.py"),
        ("E2", "src/c0.py"),
        ("E3", "src/c1.py"),
    ]
    # 分组只重排**渲染位置**（Core → Related → Tests），不动编号。
    md = render_markdown(pack)
    assert md.index("[E2] c0") < md.index("[E3] c1") < md.index("[E1] z")
    assert [item.id for item in pack.evidence] == ["E1", "E2", "E3"]


def test_docs_and_flow_sections_are_not_grouped(store, seed_file, sym, cand):
    """§B-3：``### Docs`` 与 ``### Flow`` 不分组（文档本就是独立语义）。"""
    _spec(store, seed_file)
    _code(store, seed_file, sym, "src/a.py", "a")
    flow = Flow(
        id="F1",
        nodes=(FlowNode(symbol="a", path="src/a.py", line=1),),
        truncated=False,
    )
    candidates = [
        cand("src/a.py", "a", 1, score=1.0),
        cand(SPEC_PATH, "架构 > 节0", 10, score=0.6, kind="spec"),
    ]

    md = render_markdown(assemble(store, "q", candidates, flows=[flow],
                                  config=BudgetConfig(**ROOMY)))

    docs_at = md.index("### Docs")
    flow_at = md.index("### Flow")
    assert "#### " not in md.split("### Docs")[1].split("### Missing Evidence")[0]
    assert "#### " not in md.split("### Flow")[1].split("### Docs")[0]
    assert docs_at < flow_at or flow_at < docs_at  # 两节都在（顺序由 render_markdown 固定）


def test_render_evidence_for_prompt_is_grouped_too(store, seed_file, sym, cand):
    """prompt 侧复用同一 formatter → 也带分组标题（两处绝不各写一套渲染逻辑）。"""
    seed_file(store, path="tests/test_z.py", symbols=[sym("z", "z", start=1, end=1)])
    _code(store, seed_file, sym, "src/c0.py", "c0")
    candidates = [
        cand("tests/test_z.py", "z", 1, score=2.483, kind="test"),
        cand("src/c0.py", "c0", 1, score=1.787),  # 72% → Core
    ]

    prompt_block = render_evidence_for_prompt(
        assemble(store, "q", candidates, config=BudgetConfig(**ROOMY))
    )

    assert "### Code" in prompt_block
    assert "#### Core" in prompt_block and "#### Tests" in prompt_block


def test_existing_top_level_sections_are_unchanged(store, seed_file, sym, cand):
    """回归保护：既有的顶层节（``### ``）**不新增、不重排**（§B 只加四级标题）。"""
    seed_file(store, path="tests/test_z.py", symbols=[sym("z", "z", start=1, end=1)])
    _code(store, seed_file, sym, "src/c0.py", "c0")
    candidates = [
        cand("tests/test_z.py", "z", 1, score=2.0, kind="test"),
        cand("src/c0.py", "c0", 1, score=1.0),
        cand("src/c9.py", "c9", 1, score=0.1),  # 被闸门截掉 → 产生 Missing Evidence
    ]

    md = render_markdown(assemble(store, "q", candidates, config=BudgetConfig(**ROOMY)))

    canonical = [
        "### Code",
        "### Flow",
        "### Docs",
        "### Missing Evidence",
        "### Suggested Next Queries",
        "### Meta",
    ]
    # 在场者必须严格按 canonical 顺序出现，且不得有 canonical 之外的新顶层节。
    assert [line for line in md.splitlines() if line.startswith("### ")] == [
        heading for heading in canonical if heading in md
    ]
    assert md.startswith("## Relevant Context\n"), "首行节名逐字未变"
    positions = [md.index(heading) for heading in canonical if heading in md]
    assert positions == sorted(positions)
    assert "### Missing Evidence" in md, "语料前提：闸门确实截掉了 c9"
