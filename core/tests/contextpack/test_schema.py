"""TASK-012 schema 校验（DoD：``jsonschema`` 对 ``to_json()`` 输出按合同文件本体校验）。

契约来源：``docs/contracts/contextpack.schema.json``（CF-03 / D-21）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from nova_core.contextpack import (
    BudgetConfig,
    IndexSignals,
    assemble,
    collect_index_signals,
    to_json,
)
from nova_core.types import Flow, FlowNode, Freshness, SpecBlockDef

SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "contracts" / "contextpack.schema.json"
)


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _spec_block(path: str = "docs/design/auth.md") -> SpecBlockDef:
    return SpecBlockDef(
        path=path,
        heading="Token Refresh",
        heading_path="认证 > Token Refresh",
        level=2,
        start_line=30,
        end_line=32,
        content="刷新流程说明\n\n使用 refresh_token",
        doctype="design",
    )


def _rich_pack(store, seed_file, sym, cand):
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[sym("refresh", "TokenService.refresh", kind="method", start=45, end=46)],
        bodies={"TokenService.refresh": "def refresh(self):\n    return 1\n"},
    )
    seed_file(
        store,
        path="core/tests/test_token.py",
        symbols=[sym("t", "test_refresh", start=1, end=2)],
        bodies={"test_refresh": "def test_refresh():\n    assert True\n"},
    )
    seed_file(store, path="docs/design/auth.md", language="markdown",
              spec_blocks=[_spec_block()])
    store.add_spec_refs(
        [("docs/design/auth.md:认证 > Token Refresh:30",
          "src/auth/token_service.py:TokenService.refresh:45")]
    )
    store.apply_deletions(["src/gone.py"])

    candidates = [
        cand("src/auth/token_service.py", "TokenService.refresh", 45, score=3.5, tier=0,
             channels={"exact": 1, "bm25": 1, "vector": 1}, reasons=["explicit symbol x"], end=46),
        cand("core/tests/test_token.py", "test_refresh", 1, score=2.0, kind="test",
             channels={"bm25": 2, "vector": 2}, reasons=["bm25 -1.0"], end=2),
        cand("docs/design/auth.md", "认证 > Token Refresh", 30, score=1.0, kind="spec",
             channels={"bm25": 3}, reasons=["bm25 -2.0"]),
    ]
    flow = Flow(
        id="F1",
        nodes=(
            FlowNode(symbol="AuthMiddleware.verify", path="src/mw.py", line=88),
            FlowNode(symbol="TokenService.refresh", path="src/auth/token_service.py", line=45),
        ),
        truncated=True,
    )
    signals = collect_index_signals(store, candidates)
    return assemble(
        store,
        "token 过期后在哪里刷新？",
        candidates,
        flows=[flow],
        freshness=Freshness(indexed_at=1_760_000_000, stale_files=("src/old.py",)),
        signals=IndexSignals(
            stale_doc_refs=signals.stale_doc_refs
            or {"docs/design/auth.md:认证 > Token Refresh:30": ("Legacy.rotate",)},
            unresolved_count=1,
        ),
    )


def test_rich_pack_validates(store, seed_file, sym, cand, validator) -> None:
    pack = _rich_pack(store, seed_file, sym, cand)
    payload = to_json(pack)
    validator.validate(payload)
    assert payload["evidence"] and payload["docs"] and payload["flows"]
    assert payload["budget"]["hardCap"] == 14_000
    assert payload["mode"] == "fast"


def test_empty_pack_validates(store, validator) -> None:
    payload = to_json(assemble(store, "nothing", []))
    validator.validate(payload)
    assert payload["evidence"] == []
    assert payload["docs"] == []
    assert payload["nextQueries"] == []
    assert payload["missingEvidence"][0]["code"] == "no_context_match"


def test_deep_mode_and_truncated_pack_validates(store, seed_file, sym, cand, validator) -> None:
    seed_file(
        store,
        path="src/huge.py",
        symbols=[sym("huge", "huge", start=1, end=400)],
        bodies={"huge": "\n".join(f"line {i}" for i in range(400))},
    )
    for index in range(4):
        seed_file(
            store,
            path=f"src/f{index}.py",
            symbols=[sym(f"f{index}", f"f{index}", start=1, end=1)],
            bodies={f"f{index}": "x" * 400},
        )
    candidates = [cand("src/huge.py", "huge", 1, score=1.0, end=400)] + [
        cand(f"src/f{i}.py", f"f{i}", 1, score=0.9 - i / 10, end=1) for i in range(4)
    ]
    pack = assemble(
        store,
        "q",
        candidates,
        mode="deep",
        config=BudgetConfig(hard_cap=300, framework_overhead=0, single_file_ratio=1.0),
    )
    payload = to_json(pack)
    validator.validate(payload)
    assert payload["mode"] == "deep"
    assert payload["budget"]["truncated"] is True
    assert payload["evidence"][0]["elidedLines"] > 0


def test_spec_doc_item_has_no_score_field(store, seed_file, sym, cand, validator) -> None:
    """CF-03：docItem 只有 id/type/path/headingPath/doctype/content/reason（无 score/tier）。"""
    seed_file(store, path="docs/design/x.md", language="markdown",
              spec_blocks=[_spec_block("docs/design/x.md")])
    pack = assemble(
        store, "为什么这样设计",
        [cand("docs/design/x.md", "认证 > Token Refresh", 30, score=1.0, kind="spec")],
    )
    payload = to_json(pack)
    validator.validate(payload)
    doc = payload["docs"][0]
    assert "score" not in doc and "evidenceTier" not in doc
    assert doc["doctype"] == "design"
    assert doc["headingPath"] == "认证 > Token Refresh"
