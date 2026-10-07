"""TASK-002：兜底切分测试（800 行上限 / 不丢字符 / 确定性）。"""

from __future__ import annotations

from nova_core.parsing.fallback import (
    FALLBACK_MAX_CHARS,
    FALLBACK_MAX_LINES,
    FallbackBlock,
    split_fallback,
)


def joined(blocks: tuple[FallbackBlock, ...]) -> str:
    return "".join(block.content for block in blocks)


def test_empty_content() -> None:
    assert split_fallback("") == ()


def test_single_block_line_range() -> None:
    content = "a\nb\nc\n"
    blocks = split_fallback(content)
    assert len(blocks) == 1
    block = blocks[0]
    assert (block.index, block.start_line, block.end_line) == (1, 1, 3)
    assert block.content == content


def test_default_budget_constants() -> None:
    assert FALLBACK_MAX_LINES == 800
    assert FALLBACK_MAX_CHARS > FALLBACK_MAX_LINES  # 字符阀只针对单行巨型输入


def test_long_file_respects_line_budget_and_keeps_content() -> None:
    content = "".join(f"line {index}\n" for index in range(2000))
    blocks = split_fallback(content)
    assert len(blocks) >= 3  # 2000 行 / 800 上限
    assert joined(blocks) == content
    assert [block.index for block in blocks] == list(range(1, len(blocks) + 1))
    for block in blocks:
        assert block.end_line - block.start_line + 1 <= FALLBACK_MAX_LINES
    assert blocks[0].start_line == 1
    assert blocks[-1].end_line == 2000


def test_paragraph_boundary_preferred() -> None:
    paragraph_a = "".join(f"a{index}\n" for index in range(500))
    paragraph_b = "".join(f"b{index}\n" for index in range(500))
    paragraph_c = "".join(f"c{index}\n" for index in range(500))
    content = paragraph_a + "\n" + paragraph_b + "\n" + paragraph_c
    blocks = split_fallback(content)
    assert [block.content for block in blocks] == [
        paragraph_a + "\n",
        paragraph_b + "\n",
        paragraph_c,
    ]


def test_oversized_single_line_is_hard_sliced() -> None:
    content = "x" * (FALLBACK_MAX_CHARS * 2 + 10)
    blocks = split_fallback(content)
    assert len(blocks) == 3
    assert joined(blocks) == content
    assert all(len(block.content) <= FALLBACK_MAX_CHARS for block in blocks)
    assert all((block.start_line, block.end_line) == (1, 1) for block in blocks)


def test_custom_budget() -> None:
    content = "".join(f"{index}\n" for index in range(10))
    blocks = split_fallback(content, max_lines=2)
    assert len(blocks) == 5
    assert joined(blocks) == content
    for block in blocks:
        assert block.end_line - block.start_line + 1 <= 2


def test_deterministic() -> None:
    content = "".join(f"line {index}\n" for index in range(1000))
    assert split_fallback(content) == split_fallback(content)


# ---------------------------------------------------------------------------
# TASK-018 §A 回归护栏：多级递归不得丢失基准偏移（行号回跳）
# ---------------------------------------------------------------------------


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


def test_nested_separators_keep_absolute_line_numbers() -> None:
    """最小复现（TASK-018 卡）：``max_lines=5`` 触发 ``"\\n\\n"`` → ``"\\n"`` 两级递归。

    修复前第 3、4 块的行号回跳到文件开头（start=1、7）。
    """
    content = "1\n2\n3\n4\n5\n6\n\n7\n8\n9\n10\n11\n12\n"

    blocks = split_fallback(content, max_lines=5)

    assert [block.start_line for block in blocks] == [1, 6, 8, 13]
    assert joined(blocks) == content


def test_recursive_split_line_ranges_map_back_to_source() -> None:
    """强制多级递归：行号严格递增、块内容 = 原文对应行子串。"""
    paragraphs = ["".join(f"p{para}-{line}\n" for line in range(1, 8)) for para in range(4)]
    content = "\n".join(paragraphs)
    blocks = split_fallback(content, max_lines=5)
    starts = _line_starts(content)

    assert len(blocks) >= 6  # 4 段 × 7 行 / 5 行上限 → 必然多块 + 两级递归
    assert [block.index for block in blocks] == list(range(1, len(blocks) + 1))
    assert joined(blocks) == content
    previous_start = 0
    for block in blocks:
        assert block.start_line > previous_start  # 严格递增（修复前会回跳）
        previous_start = block.start_line
        offset = starts[block.start_line - 1]
        assert content[offset : offset + len(block.content)] == block.content
        assert block.end_line >= block.start_line
        assert block.end_line - block.start_line + 1 <= 5
