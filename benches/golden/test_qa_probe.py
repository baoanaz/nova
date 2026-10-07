"""qa_probe 的纯指标逻辑测试；不联网、不打开真实索引。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from nova_core.cli.eval import Expectation, GoldenCase
from nova_core.types import EvidenceItem

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "benches" / "golden") not in sys.path:
    sys.path.insert(0, str(ROOT / "benches" / "golden"))

from qa_probe import expected_evidence_ids, expected_ranks, metadata_of, percentile  # noqa: E402


def _item(identifier: str, path: str, symbol: str) -> EvidenceItem:
    return EvidenceItem(
        id=identifier,
        type="code",
        path=path,
        symbol=symbol,
        lines=(1, 2),
        content="1 | body",
        score=1.0,
        evidence_tier=1,
        reason="test",
    )


def test_expected_metrics_keep_each_required_item_separate() -> None:
    case = GoldenCase(
        id="q",
        query="q",
        lang="zh",
        category="behavior",
        expected=(
            Expectation("a.py", "A.run"),
            Expectation("b.py", "B.load"),
        ),
    )
    items = [_item("E1", "a.py", "A.run"), _item("E2", "other.py", "Other")]

    assert expected_ranks(items, case, top_k=10) == [1, None]
    assert expected_evidence_ids(items, case) == {"E1"}


def test_metadata_defaults_and_expected_mode(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"id": "a"}),
                json.dumps({"id": "b", "tool": "ask", "expected_mode": "all"}),
            ]
        ),
        encoding="utf-8",
    )

    assert metadata_of(path) == {
        "a": {"tool": "search", "expected_mode": "any"},
        "b": {"tool": "ask", "expected_mode": "all"},
    }


def test_percentile_uses_nearest_rank() -> None:
    assert percentile([], 0.5) is None
    assert percentile([4.0, 1.0, 3.0, 2.0], 0.5) == 2.0
    assert percentile([4.0, 1.0, 3.0, 2.0], 0.95) == 4.0
