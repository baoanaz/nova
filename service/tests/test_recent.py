"""会话级去重：重复返回的代码块只留占位，窗口外/换用户/换项目不受影响。"""

from __future__ import annotations

from nova_core.types import ContextPack, EvidenceItem
from nova_service.recent import STUB_NOTE, RecentEvidence


def _item(id_: str, path: str, lines: tuple[int, int], type_: str = "code") -> EvidenceItem:
    return EvidenceItem(
        id=id_, type=type_, path=path, content="1 | body", score=1.0, evidence_tier=1,
        reason="", symbol="f", lines=lines,
    )


def _pack(*items: EvidenceItem) -> ContextPack:
    pack = ContextPack.__new__(ContextPack)
    pack.evidence = list(items)
    return pack


def test_second_search_replaces_repeated_blocks_with_a_stub() -> None:
    recent = RecentEvidence()
    first = _pack(_item("E1", "a.py", (1, 5)), _item("E2", "b.py", (1, 5)))
    assert recent.dedupe("u", "p", first) == 0
    second = _pack(_item("E1", "a.py", (1, 5)), _item("E2", "c.py", (9, 12)))
    assert recent.dedupe("u", "p", second) == 1
    assert second.evidence[0].content == STUB_NOTE
    assert second.evidence[0].path == "a.py" and second.evidence[0].lines == (1, 5)
    assert second.evidence[1].content == "1 | body"


def test_other_user_project_and_expired_records_are_not_deduped() -> None:
    now = [0.0]
    recent = RecentEvidence(ttl_s=10, clock=lambda: now[0])
    recent.dedupe("u", "p", _pack(_item("E1", "a.py", (1, 5))))
    for owner, project in (("v", "p"), ("u", "q")):
        again = _pack(_item("E1", "a.py", (1, 5)))
        assert recent.dedupe(owner, project, again) == 0
    now[0] = 11.0
    expired = _pack(_item("E1", "a.py", (1, 5)))
    assert recent.dedupe("u", "p", expired) == 0
    assert expired.evidence[0].content == "1 | body"


def test_docs_and_blocks_without_lines_are_never_stubbed() -> None:
    recent = RecentEvidence()
    spec = _item("E1", "README.md", (1, 5), type_="spec")
    recent.dedupe("u", "p", _pack(spec))
    again = _pack(spec)
    assert recent.dedupe("u", "p", again) == 0
