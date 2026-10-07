"""TASK-017（R12）：合并区间的行序单调性、elidedLines 语义与原文保真回归。

场景刻意让**分数顺序与行序相反**（L9-14 分最高、L18-24 居中、L1-6 最低），复现编排者实测的
行号回跳（9→6→14→1）：修复后 ``item.content`` 的行号必须严格递增，``elidedLines`` 必须等于
真实省略行数，且各片段正文与源 chunk 逐字一致。
"""

from __future__ import annotations

import re

from nova_core.contextpack import BudgetConfig, assemble, render_markdown

NUMBERED = re.compile(r"^\s*(\d+) \| (.*)$")
ELISION = re.compile(r"^\.\.\. （省略 (\d+) 行）$")

#: 三段区间（同行同文件、两两行距 ≤ ADJACENT_GAP_LINES=10 → 全部合并）
SPANS = ((1, 6), (9, 14), (18, 24))
#: 分数与行序刻意相反：L9-14（最高）→ L18-24 → L1-6（最低）
SCORES = {1: 0.4, 9: 1.0, 18: 0.7}
#: 真实省略行数 = 声明区间 1..24（24 行） − Σ片段行数（6+6+7=19） = 5（7..8 与 15..17）
EXPECTED_ELIDED = 5
#: TASK-095：本文件验的是**合并后的行序与省略标注**，不是分数闸门——
#: 三段分数跨越 0.4~1.0（低于默认闸门 top1×0.50），故显式关掉闸门（闸门另有用例）。
CONFIG = BudgetConfig(
    hard_cap=100_000, framework_overhead=0, single_file_ratio=1.0, score_ratio=0.0
)


def _body(start: int, end: int) -> str:
    """与符号声明区间等长的正文（真实 chunk 的 content 与 [start_line, end_line] 等长）。"""
    return "\n".join(f"L{line}" for line in range(start, end + 1))


def _seed(store, seed_file, sym) -> None:
    seed_file(
        store,
        path="src/a.py",
        symbols=[sym(f"s{start}", f"s{start}", start=start, end=end) for start, end in SPANS],
        bodies={f"s{start}": _body(start, end) for start, end in SPANS},
    )


def _candidates(cand) -> list:
    return [
        cand("src/a.py", f"s{start}", start, score=SCORES[start], end=end)
        for start, end in SPANS
    ]


def _numbers(content: str) -> list[int]:
    """从证据块正文里解析行号（``     9 | L9`` → 9）。"""
    numbers = []
    for line in content.splitlines():
        matched = NUMBERED.match(line)
        if matched is not None:
            numbers.append(int(matched.group(1)))
    return numbers


def _merged_item(store, seed_file, sym, cand):
    _seed(store, seed_file, sym)
    pack = assemble(store, "q", _candidates(cand), config=CONFIG)
    assert len(pack.evidence) == 1, "三段行距均 ≤10，应合并为一个证据块"
    item = pack.evidence[0]
    assert item.lines == (1, 24)
    assert "相邻区间合并" in item.reason
    return pack, item


# --------------------------------------------------------------------------- 行序


def test_merged_content_line_numbers_are_strictly_increasing(store, seed_file, sym, cand) -> None:
    _pack, item = _merged_item(store, seed_file, sym, cand)
    numbers = _numbers(item.content)
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)  # 严格递增（无重复）
    assert numbers == [*range(1, 7), *range(9, 15), *range(18, 25)]


def test_rendered_block_line_numbers_are_monotonic(store, seed_file, sym, cand) -> None:
    pack, _item = _merged_item(store, seed_file, sym, cand)
    text = render_markdown(pack)
    numbers = _numbers(text)
    assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)
    # 省略标注落在被省略的那一段之后（位置正确），两处共 2+3=5 行，无重复的尾部总计
    assert "     6 | L6\n     ... （省略 2 行）\n     9 | L9" in text
    assert "     14 | L14\n     ... （省略 3 行）\n     18 | L18" in text
    assert len([line for line in text.splitlines() if ELISION.match(line.strip())]) == 2
    # 证据块内的省略标注恰好 2 处。**不用** ``text.count("省略")``：``### Missing Evidence``
    # 里可能出现别的“省略 N 个候选”（TASK-095 的闸门上报就是这种），全文计数会把它算进来。
    assert len([line for line in text.splitlines() if ELISION.match(line.strip())]) == 2


# --------------------------------------------------------------------------- elided 计数


def test_elided_lines_equals_real_elided_count(store, seed_file, sym, cand) -> None:
    _pack, item = _merged_item(store, seed_file, sym, cand)
    assert item.elided_lines == EXPECTED_ELIDED  # 声明区间 1..24 之外的 7..8 与 15..17
    notes = [int(m.group(1)) for line in item.content.splitlines() if (m := ELISION.match(line))]
    assert notes == [2, 3]
    assert sum(notes) == item.elided_lines


# --------------------------------------------------------------------------- 原文保真


def test_merged_content_keeps_each_chunk_verbatim(store, seed_file, sym, cand) -> None:
    _pack, item = _merged_item(store, seed_file, sym, cand)
    source = {
        line: f"L{line}" for start, end in SPANS for line in range(start, end + 1)
    }
    seen: dict[int, str] = {}
    for line in item.content.splitlines():
        matched = NUMBERED.match(line)
        if matched is None:
            continue
        number, body = int(matched.group(1)), matched.group(2)
        assert number in source, f"渲染出区间外的行号 {number}"
        assert body == source[number], f"第 {number} 行正文被改写：{body!r}"
        seen[number] = body
    assert seen == source  # 三段原文全在，未截断、未重排


# --------------------------------------------------------------------------- 无合并回归


def test_single_candidate_line_order_and_elision_unchanged(store, seed_file, sym, cand) -> None:
    seed_file(
        store,
        path="src/a.py",
        symbols=[sym("s9", "s9", start=9, end=14)],
        bodies={"s9": _body(9, 14)},
    )
    pack = assemble(
        store, "q", [cand("src/a.py", "s9", 9, score=1.0, end=14)], config=CONFIG
    )
    item = pack.evidence[0]
    assert item.lines == (9, 14)
    assert item.elided_lines == 0
    assert item.content == "\n".join(f"{line} | L{line}" for line in range(9, 15))
    assert "省略" not in render_markdown(pack)
