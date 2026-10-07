"""TASK-019（R15）：spec 保底块重复装填修复的回归测试。

**准确根因**（实测校正，见任务卡执行记录）：`_Slot` 是**非 frozen** dataclass，Python 会生成
`__eq__`（值比较），因此 `reserved not in slots` 在"贪心路径原样装填预留候选"时为 **False**
（不会重复）；但只要贪心路径装下的那个 `_Slot` **被后续改动**——去重第 1 招的
`_try_merge`（把后到的相邻块并入它，segments/elision 变化）或 `_degrade`（skeleton 降级）——
值就不再生效相等，判定翻转为 True，保底分支于是把**同一 chunk 的原始 span** 再装一次
（真实复现：`E1(173-232)` 合并块 + `E30(175-197)` 预留块，重复约 1.4K token）。

修复：保底分支与贪心循环、合并路径共用 `placed_ids`（**按 chunk_id** 判重），
且预留块一旦被装填（含并入既有块）预留预算立即**归还**。

本文件覆盖任务卡 DoD 四项：
1. 去重：`test_reserved_spec_chunk_merged_into_neighbor_is_not_placed_again`（真实复现路径，
   修复前失败）与 `test_reserved_spec_chunk_is_placed_only_once_when_greedy_takes_it_first`
   （朴素路径 + E 编号连续性）；
2. 预算不变量：`test_used_tokens_equals_framework_overhead_plus_evidence_tokens`
   （TASK-096 §A 起口径 = 框架开销 + 各条证据的**完整渲染开销**，不再是只算正文）；
3. 保底仍在：`test_spec_floor_still_places_a_spec_when_code_candidates_fill_budget`（防修过头）；
4. 回归：`test_reserved_capacity_is_returned_once_the_spec_is_placed`（预留预算归还，
   修复前失败）。
"""

from __future__ import annotations

from dataclasses import replace

from nova_core.contextpack import (
    BudgetConfig,
    assemble,
    estimate_render_tokens,
)
from nova_core.types import SpecBlockDef

SPEC_PATH = "docs/design.md"
SPEC_HEADING = "架构 > Token Refresh"


def _spec_block(path: str, heading_path: str, *, start: int, end: int, chars: int) -> SpecBlockDef:
    """构造指定行区间与正文长度的 spec 块（正文 = ``chars`` 个字符）。"""
    return SpecBlockDef(
        path=path,
        heading=heading_path.rsplit(" > ", 1)[-1],
        heading_path=heading_path,
        level=2,
        start_line=start,
        end_line=end,
        content="设计" * (chars // 2),
        doctype="design",
    )


def _code_file(store, seed_file, sym, path: str, fqn: str, chars: int) -> None:
    seed_file(store, path=path, symbols=[sym(fqn, fqn, start=1, end=1)], bodies={fqn: "x" * chars})


def _placed(pack):
    return [*pack.evidence, *pack.docs]


def test_reserved_spec_chunk_merged_into_neighbor_is_not_placed_again(
    store, seed_file, sym, cand
) -> None:
    """去重（真实复现路径）：预留 spec 被贪心装填并**并入相邻块** → 不得再装原始 span。

    修复前：贪心路径就地改动该 slot（`_try_merge` 扩段）→ `reserved not in slots` 由 False 翻成
    True → 保底分支把同一 chunk 的原始区间再装一次（真实仓库的 `E1(173-232)` + `E30(175-197)`）。
    """
    seed_file(
        store,
        path=SPEC_PATH,
        language="markdown",
        spec_blocks=[
            _spec_block(SPEC_PATH, SPEC_HEADING, start=10, end=15, chars=400),
            _spec_block(SPEC_PATH, "架构 > 相邻一节", start=16, end=25, chars=400),
        ],
    )
    for index in range(3):
        _code_file(store, seed_file, sym, f"src/f{index}.py", f"f{index}", 400)
    candidates = [cand(SPEC_PATH, SPEC_HEADING, 10, score=1.0, kind="spec")] + [
        cand(f"src/f{i}.py", f"f{i}", 1, score=0.5 - i / 10) for i in range(3)
    ]
    candidates.append(cand(SPEC_PATH, "架构 > 相邻一节", 16, score=0.4, kind="spec"))
    config = BudgetConfig(hard_cap=10_000, framework_overhead=0, single_file_ratio=1.0)

    pack = assemble(store, "为什么这样设计", candidates, config=config)

    assert len(pack.docs) == 1  # 同一 chunk 的原始 span 不得再装一次
    assert pack.docs[0].lines == (10, 25)
    assert "相邻区间合并" in pack.docs[0].reason
    ids = sorted((item.id for item in _placed(pack)), key=lambda value: int(value[1:]))
    assert ids == [f"E{index}" for index in range(1, len(ids) + 1)]  # E 编号连续唯一（D-21）
    assert pack.budget.used_tokens == config.framework_overhead + sum(
        estimate_render_tokens(item) for item in _placed(pack)
    )


def test_reserved_spec_chunk_is_placed_only_once_when_greedy_takes_it_first(
    store, seed_file, sym, cand
) -> None:
    """朴素路径：预留候选原本就能装下（slot 未被改动）→ 只装一次，E 编号仍按装填顺序。"""
    seed_file(
        store,
        path=SPEC_PATH,
        language="markdown",
        spec_blocks=[_spec_block(SPEC_PATH, SPEC_HEADING, start=10, end=15, chars=400)],
    )
    for index in range(4):
        _code_file(store, seed_file, sym, f"src/f{index}.py", f"f{index}", 400)
    candidates = [cand(SPEC_PATH, SPEC_HEADING, 10, score=1.0, kind="spec")] + [
        cand(f"src/f{i}.py", f"f{i}", 1, score=0.5 - i / 10) for i in range(4)
    ]
    config = BudgetConfig(hard_cap=1_400, framework_overhead=0, single_file_ratio=1.0)

    # TASK-095：本用例验的是“同一 chunk 只装一次”的去重与 E 编号连续性；代码候选分数
    # 0.5/0.4/0.3/0.2 会被默认分数闸门（top1×0.50）截掉，故显式关掉闸门。
    pack = assemble(
        store, "为什么这样设计", candidates, config=replace(config, score_ratio=0.0)
    )

    assert [item.path for item in pack.docs] == [SPEC_PATH]
    assert len(pack.evidence) == 4 and len(pack.docs) == 1
    assert pack.docs[0].id == "E1"  # 最高分 spec 先装（编号 = 装填顺序）
    assert pack.budget.used_tokens <= config.hard_cap
    assert pack.budget.used_tokens == sum(estimate_render_tokens(item) for item in _placed(pack))


def test_used_tokens_equals_framework_overhead_plus_evidence_tokens(
    store, seed_file, sym, cand
) -> None:
    """预算不变量：`usedTokens ≤ hardCap`，且 `usedTokens` = 框架开销 + 各项证据 token 之和。"""
    seed_file(
        store,
        path="src/a.py",
        symbols=[sym("one", "one", start=40, end=70), sym("two", "two", start=65, end=90)],
    )
    seed_file(
        store,
        path=SPEC_PATH,
        language="markdown",
        spec_blocks=[_spec_block(SPEC_PATH, SPEC_HEADING, start=10, end=15, chars=400)],
    )
    candidates = [
        cand("src/a.py", "one", 40, score=1.0, end=70),
        cand("src/a.py", "two", 65, score=0.9, end=90),  # 与 one 相邻 → 合并（R12）
        cand(SPEC_PATH, SPEC_HEADING, 10, score=0.5, kind="spec"),
    ]
    config = BudgetConfig(hard_cap=10_000, framework_overhead=500, single_file_ratio=0.25)

    pack = assemble(store, "q", candidates, config=config)

    placed = _placed(pack)
    assert pack.budget.used_tokens <= config.hard_cap
    assert pack.budget.used_tokens == config.framework_overhead + sum(
        estimate_render_tokens(item) for item in placed
    )
    assert len({item.id for item in placed}) == len(placed)  # E 编号唯一
    assert len({(item.path, item.lines) for item in placed}) == len(placed)  # 无重复块


def test_spec_floor_still_places_a_spec_when_code_candidates_fill_budget(
    store, seed_file, sym, cand
) -> None:
    """保底语义未被破坏：代码候选占满预算时，仍至少装填 1 块 spec（防修过头）。"""
    for index in range(4):
        _code_file(store, seed_file, sym, f"src/f{index}.py", f"f{index}", 1_000)
    seed_file(
        store,
        path=SPEC_PATH,
        language="markdown",
        spec_blocks=[_spec_block(SPEC_PATH, SPEC_HEADING, start=10, end=15, chars=1_000)],
    )
    candidates = [
        cand(f"src/f{i}.py", f"f{i}", 1, score=1.0 - i / 10) for i in range(4)
    ] + [cand(SPEC_PATH, SPEC_HEADING, 10, score=0.01, kind="spec")]
    # TASK-096 §A 重标定：code(1000) 渲染约 262、spec(1000) 约 683。
    # hard_cap=945：预留 spec 后硬顶=262 → 贪心恰好只装 1 块代码
    # （第 2 块超顶 break，故 truncated）；保底补入 spec 后 262+683=945 ≤ hard_cap。
    config = BudgetConfig(hard_cap=945, framework_overhead=0, single_file_ratio=1.0, spec_floor=1)

    pack = assemble(store, "为什么这样设计", candidates, config=config)

    assert len(pack.docs) == 1
    assert pack.docs[0].type == "spec"
    assert pack.docs[0].path == SPEC_PATH
    assert pack.budget.truncated is True  # 预算确实被代码候选挤满
    assert pack.budget.omitted_count >= 1
    assert pack.budget.used_tokens <= config.hard_cap


def test_reserved_capacity_is_returned_once_the_spec_is_placed(
    store, seed_file, sym, cand
) -> None:
    """回归：保底候选已被贪心装填 → 预留预算归还，后续候选恢复完整 hardCap，不被预留挤掉。"""
    _code_file(store, seed_file, sym, "src/a.py", "a", 1_000)
    _code_file(store, seed_file, sym, "src/b.py", "b", 1_500)
    seed_file(
        store,
        path=SPEC_PATH,
        language="markdown",
        spec_blocks=[_spec_block(SPEC_PATH, SPEC_HEADING, start=10, end=15, chars=1_000)],
    )
    candidates = [
        cand(SPEC_PATH, SPEC_HEADING, 10, score=1.0, kind="spec"),
        cand("src/a.py", "a", 1, score=0.9),
        cand("src/b.py", "b", 1, score=0.8),
    ]
    # TASK-096 §A 重标定：spec 683 + a 262 + b 387 = 1332 ≤ 1400（旧口径同语料只需 1000）。
    config = BudgetConfig(hard_cap=1_400, framework_overhead=0, single_file_ratio=1.0)

    pack = assemble(store, "为什么这样设计", candidates, config=config)

    assert [item.path for item in pack.evidence] == ["src/a.py", "src/b.py"]  # 未被预留额度挤掉
    assert len(pack.docs) == 1
    assert pack.budget.truncated is False
    assert pack.budget.used_tokens == sum(estimate_render_tokens(item) for item in _placed(pack))
    assert pack.budget.used_tokens <= config.hard_cap
