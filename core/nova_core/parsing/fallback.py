"""兜底切分（TASK-002 交付物，TASK-006 消费）。

规则来源：docs/design/Module/01 §2.2 末节"兜底（所有语言）"——解析失败 / 不支持的语言
走递归字符切分，800 行上限，chunk 标 ``fallback_block``（evidence tier 天然降低）。

实现口径（TASK-006 消费约定）：

- 分隔符优先级 ``"\\n\\n"`` → ``"\\n"`` → ``" "`` → 硬切；按优先级找到可用分隔符后
  **贪心聚合**至多 800 行一块（不做"一切到底"，避免 1 行 1 块）；单个超限片段用更低优先级
  分隔符继续递归；
- 单块字符上限 ``FALLBACK_MAX_CHARS`` 只为防"单行巨型文件"病态输入，不改变行上限语义；
- 块内容为原文的连续子串（不丢字符、不重排）；行号 1-based 含端点；
- **偏移基准是全原文**：``_split`` 的 ``offset`` 恒为该片段在原文中的绝对字符偏移，
  ``_split_keep`` 返回的相对偏移在进入下一级 ``_group`` 前必须补上本层 ``offset``
  （TASK-018 §A 回归护栏：漏补会让多级递归块的行号回跳到文件开头）；
- 纯函数、无 I/O、确定性：同输入两次调用结果相等。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

#: 兜底块行数上限（Module/01 §2.2）
FALLBACK_MAX_LINES = 800
#: 单块字符上限（病态单行输入的安全阀，非设计约束）
FALLBACK_MAX_CHARS = 40_000

_SEPARATORS = ("\n\n", "\n", " ")


@dataclass(frozen=True, slots=True)
class FallbackBlock:
    """兜底切分块：TASK-006 直接转 ChunkDef（symbol_kind='fallback_block'）。"""

    index: int  # 1-based，块在文件内的顺序
    start_line: int  # 1-based，含
    end_line: int  # 1-based，含
    content: str


def split_fallback(
    content: str,
    max_lines: int = FALLBACK_MAX_LINES,
    max_chars: int = FALLBACK_MAX_CHARS,
) -> tuple[FallbackBlock, ...]:
    """递归字符切分：返回按文件顺序排列的兜底块（空文件 → 空元组）。"""
    if not content:
        return ()

    line_starts = _line_starts(content)
    pieces: list[tuple[int, str]] = []
    _split(content, 0, max_lines, max_chars, pieces, _SEPARATORS)

    blocks: list[FallbackBlock] = []
    for offset, piece in pieces:
        if not piece:
            continue
        blocks.append(
            FallbackBlock(
                index=len(blocks) + 1,
                start_line=_line_number(line_starts, offset),
                end_line=_line_number(line_starts, offset + len(piece) - 1),
                content=piece,
            )
        )
    return tuple(blocks)


def _fits(text: str, max_lines: int, max_chars: int) -> bool:
    return _count_lines(text) <= max_lines and len(text) <= max_chars


def _split(
    text: str,
    offset: int,
    max_lines: int,
    max_chars: int,
    out: list[tuple[int, str]],
    separators: tuple[str, ...],
) -> None:
    if _fits(text, max_lines, max_chars):
        out.append((offset, text))
        return

    for index, separator in enumerate(separators):
        parts = _split_keep(text, separator)
        if len(parts) < 2:
            continue
        # _split_keep 的偏移相对 text，而 text 的起点是绝对偏移 offset，必须补上：
        # 否则 _group 递归到更低优先级分隔符时第二部分行号会回跳（TASK-018 §A）。
        _group(
            [(piece_offset + offset, piece) for piece_offset, piece in parts],
            max_lines,
            max_chars,
            out,
            separators[index + 1 :],
        )
        return

    # 无可用分隔符（单行超长）：按字符硬切；每片字符数严格变小，递归必然终止
    for index in range(0, len(text), max_chars):
        piece = text[index : index + max_chars]
        if piece:
            out.append((offset + index, piece))


def _group(
    parts: list[tuple[int, str]],
    max_lines: int,
    max_chars: int,
    out: list[tuple[int, str]],
    deeper: tuple[str, ...],
) -> None:
    """贪心聚合相邻片段至多一行上限；单片段超限则交给更低优先级分隔符递归。"""
    current_offset = 0
    current = ""
    for part_offset, part in parts:
        if not _fits(part, max_lines, max_chars):
            if current:
                out.append((current_offset, current))
                current = ""
            _split(part, part_offset, max_lines, max_chars, out, deeper)
            continue
        if current and not _fits(current + part, max_lines, max_chars):
            out.append((current_offset, current))
            current = ""
        if not current:
            current_offset = part_offset
        current += part
    if current:
        out.append((current_offset, current))


def _split_keep(text: str, separator: str) -> list[tuple[int, str]]:
    """按分隔符切分并保留分隔符（块拼回 = 原文，不丢字符），返回 (偏移, 片段)。"""
    chunks = text.split(separator)
    result: list[tuple[int, str]] = []
    offset = 0
    for index, chunk in enumerate(chunks):
        piece = chunk + (separator if index < len(chunks) - 1 else "")
        if piece:
            result.append((offset, piece))
        offset += len(piece)
    return result


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


def _line_number(line_starts: list[int], offset: int) -> int:
    """字符偏移 → 1-based 行号（偏移落在该行内）。"""
    return bisect.bisect_right(line_starts, offset)


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)
