"""面向 Agent 的证据理由：把内部打分痕迹压成一行能读的标签。

``EvidenceItem.reason`` 是排查用的原始痕迹（``bm25 -34.2976 + bm25 rank 14 + vector 0.7046 …``），
历史页和诊断工具要它；但 Agent 看不懂也用不上这些数字，实测占输出 12% 左右。
渲染给 Agent 时只保留"为什么这条在这里"的语义：命中方式、公开 API、关联来源、风险标记。
纯排名/分数片段全部丢弃；没有可读标签时整行不渲染。
"""

from __future__ import annotations

import re

__all__ = ["agent_reason"]

_MAX_TAGS = 4

_NUMERIC = re.compile(r"^(bm25|bm\d+|vector|literal|inferred|rrf)( rank)?\s*-?\d+(\.\d+)?$|"
                      r"^(bm25|vector|literal|inferred) rank \d+$")
_EXPLICIT = re.compile(r"^explicit literal (.+?) \(\d+ hits?\)")
_EXPLICIT_SYMBOL = re.compile(r"^explicit (?:symbol|path) (.+)$")
_ROOT = re.compile(r"^literal root (.+?) \((symbol name|reference|extension|writable)")
_SOURCE = re.compile(r"^(?:graph-expanded from|gap backfill:\s*)(.+)$")


def _tag(part: str) -> str | None:
    part = re.sub(r"\s*[+-]\d+(\.\d+)?$", "", part.strip())
    if not part or _NUMERIC.match(part):
        return None
    if match := _EXPLICIT.match(part):
        return f'exact match "{match.group(1)}"'
    if match := _ROOT.match(part):
        return f'name match "{match.group(1)}"'
    if match := _EXPLICIT_SYMBOL.match(part):
        return f'exact match "{match.group(1)}"'
    if part.startswith("query symbol =="):
        return "symbol matches query"
    if part.startswith("vector top"):
        return "semantic match"
    if part.startswith("entry point"):
        return "public API"
    if "-channel consensus" in part:
        return "multi-signal match"
    if part.startswith("graph connected"):
        return "linked to top result"
    if match := _SOURCE.match(part):
        return f"related: {match.group(1).strip()[:60]}"
    if part.startswith("example /") or part.startswith("changelog"):
        return part.split(" N")[0].split()[0] + " (low priority)"
    if part.startswith("test fixture"):
        return "test (reference only)"
    if part.startswith("fallback_block"):
        return "unstructured block"
    if part.startswith("high-value doctype"):
        return "key document"
    return None  # 同符号聚合/相邻区间合并/inferred edge 等内部痕迹


def agent_reason(raw: str) -> str:
    """原始 reason → 一行短标签（``a · b · c``）；没有可读内容返回空串。"""
    parts = [p for p in raw.split(" + ") if p.strip()]
    tags: list[str] = []
    if any(p.strip().startswith("bm25 ") and "rank" not in p for p in parts):
        tags.append("keyword match")
    if any(re.match(r"^vector -?\d", p.strip()) for p in parts):
        tags.append("semantic match")
    for part in parts:
        tag = _tag(part)
        if tag and tag not in tags:
            tags.append(tag)
    # 同时有关键词与语义命中时合并成一个标签，更短也更好懂。
    if "keyword match" in tags and "semantic match" in tags:
        rest = [t for t in tags if t not in ("keyword match", "semantic match")]
        tags = ["keyword + semantic match", *rest]
    return " · ".join(tags[:_MAX_TAGS])
