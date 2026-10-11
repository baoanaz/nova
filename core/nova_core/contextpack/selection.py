"""有界源码窗口：行号与原文保真，不推断运行时控制流。"""

from __future__ import annotations

import re
from collections.abc import Sequence


def focused_segments(
    content: str, start: int, query: str, path: str, calls: Sequence[int], *, max_lines: int
) -> list[tuple[int, int, str]]:
    lines = content.splitlines() or [""]
    limit = max(1, max_lines)
    if len(lines) <= limit:
        return [(start, start + len(lines) - 1, content)]
    head = min(4, limit)
    selected = set(range(head))
    anchors = [
        int(m.group(1))
        for m in re.finditer(re.escape(path) + r":(\d{1,9})(?!\d)", query)
    ]
    terms = list(dict.fromkeys(
        re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", query)
    ))[:16]
    textual = [
        start + i for i, line in enumerate(lines)
        if i >= head and any(term.lower() in line.lower() for term in terms)
    ][:8]
    for focus in dict.fromkeys([*anchors, *calls[:8], *textual]):
        index = focus - start
        if not head <= index < len(lines):
            continue
        # 中心先入，之后补相邻条件/语句；片段不是完整函数，省略由组装层标明。
        for delta in (0, -1, 1, -2, 2, -3, 3):
            at = index + delta
            if 0 <= at < len(lines) and len(selected) < limit:
                selected.add(at)
    # 没有可靠焦点时沿用头部窗口，而不是任意猜尾部行为。
    if len(selected) == head:
        selected.update(range(min(limit, len(lines))))
    ordered = sorted(selected)
    ranges: list[tuple[int, int, str]] = []
    a = b = ordered[0]
    for index in ordered[1:]:
        if index == b + 1:
            b = index
        else:
            ranges.append((start + a, start + b, "\n".join(lines[a:b + 1])))
            a = b = index
    ranges.append((start + a, start + b, "\n".join(lines[a:b + 1])))
    return ranges
