"""ask 多轮调研：规划 → 定向补检 → 合并去重重编号；失败时退回原证据。"""

from __future__ import annotations

import json

from nova_core.types import ContextPack, EvidenceItem, Flow, FlowNode, Freshness
from nova_service.research import merge_into, research


def _item(eid: str, path: str, lines: tuple[int, int], content: str = "1 | x", elided: int = 0):
    return EvidenceItem(
        id=eid, type="code", path=path, content=content, score=1.0, evidence_tier=1,
        reason="test", symbol=None, lines=lines, elided_lines=elided,
    )


def _pack(*items: EvidenceItem, flows: list[Flow] | None = None) -> ContextPack:
    return ContextPack(
        query="q", mode="deep", answerable=True, confidence="high", freshness=Freshness(),
        evidence=list(items), flows=list(flows or []),
    )


class ScriptedProvider:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []

    def complete(self, *, system: str, user: str, max_tokens: int, temperature: float) -> str:
        self.calls.append(user)
        return self.replies.pop(0)


def test_merge_dedupes_overlaps_and_renumbers() -> None:
    base = _pack(_item("E1", "a.py", (1, 10)), _item("E2", "b.py", (1, 5)))
    extra = _pack(
        _item("E1", "a.py", (5, 8)),            # 与 E1 重叠 → 丢弃
        _item("E2", "c.py", (1, 3)),            # 新文件 → 追加
        flows=[Flow(id="F1", nodes=(FlowNode("f", "a.py", 1), FlowNode("g", "c.py", 1)))],
    )
    merged, added, _ = merge_into(base, [extra], budget=10_000)
    assert added == 1
    assert [(e.id, e.path) for e in merged.evidence] == [
        ("E1", "a.py"), ("E2", "b.py"), ("E3", "c.py"),
    ]
    assert [f.id for f in merged.flows] == ["F1"]


def test_merge_replaces_elided_excerpt_with_fuller_version() -> None:
    base = _pack(_item("E1", "a.py", (1, 40), content="1 | a", elided=20))
    extra = _pack(_item("E9", "a.py", (1, 40), content="1 | a\n2 | b\n3 | c", elided=0))
    merged, added, _ = merge_into(base, [extra], budget=10_000)
    assert added == 0
    assert merged.evidence[0].elided_lines == 0 and merged.evidence[0].id == "E1"


def test_research_searches_planned_queries_and_stops_when_enough() -> None:
    provider = ScriptedProvider(
        json.dumps({"enough": False, "gaps": ["缺 g 的实现"], "queries": ["g", "h"]}),
        json.dumps({"enough": True, "gaps": [], "queries": []}),
    )
    searched: list[str] = []

    def search(query: str) -> ContextPack:
        searched.append(query)
        return _pack(_item("E1", f"{query}.py", (1, 3)))

    pack, trace = research(provider, search, "问题", _pack(_item("E1", "a.py", (1, 3))), rounds=3)
    assert sorted(searched) == ["g", "h"]
    assert [e.path for e in pack.evidence] == ["a.py", "g.py", "h.py"]
    assert trace.added_items == 2 and len(trace.rounds) == 2
    assert "g; h" in provider.calls[1]  # 第二轮规划看得到已查过的检索词


def test_research_falls_back_on_bad_plan() -> None:
    provider = ScriptedProvider("not json at all")
    original = _pack(_item("E1", "a.py", (1, 3)))
    pack, trace = research(provider, lambda q: original, "问题", original, rounds=2)
    assert pack is original
    assert trace.rounds == [{"error": "ValueError"}]


def test_hedged_returns_backup_when_first_call_hangs() -> None:
    import threading

    from nova_service.research import hedged

    release = threading.Event()
    calls: list[int] = []

    def call() -> str:
        calls.append(1)
        if len(calls) == 1:
            release.wait(5)
            return "slow"
        return "fast"

    assert hedged(call, hedge_after_s=0.05) == "fast"
    release.set()
    assert len(calls) == 2


def test_hedged_times_out_and_propagates_errors() -> None:
    import time

    import pytest
    from nova_service.research import hedged

    with pytest.raises(TimeoutError):
        hedged(lambda: time.sleep(1) or "x", hedge_after_s=0.05, timeout_s=0.2)

    def boom() -> str:
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        hedged(boom, hedge_after_s=0.05)
