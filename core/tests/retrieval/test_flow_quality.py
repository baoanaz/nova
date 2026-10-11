from dataclasses import replace

import pytest
from nova_core.retrieval.expand import _symbol_row, build_flows, expand
from nova_core.types import Candidate, EdgeDef


@pytest.mark.parametrize("provenance", ["synthesized", "inferred"])
def test_weak_edge_is_not_rendered_as_flow(store, seed_file, sym, provenance):
    seed_file(store, path="a.py", symbols=[sym("a", "a", start=1), sym("b", "b", start=10)],
              edges=[EdgeDef("a", "b", "calls", 2, provenance)])
    seed = Candidate("a.py:a:1", kind="function", rrf_score=1, score=1,
                     symbol_fqn="a", path="a.py")
    assert build_flows(store, [seed]) == []
    assert any(c.symbol_fqn == "b" for c in expand(store, [seed]).candidates)


def test_unsliced_duplicate_js_identity_is_still_ambiguous(store, seed_file, sym, monkeypatch):
    seed_file(store, path="a.ts", symbols=[sym("run", "a.ts::run")])
    row = store.exact_symbols("a.ts::run")[0]
    other = replace(row, id=row.id + ":other", chunk_id=None, start_line=20)
    original = store.exact_symbols
    monkeypatch.setattr(store, "exact_symbols", lambda name, limit=None:
                        [row, other] if name == row.fqn else original(name, limit))
    assert _symbol_row(store, row.fqn) is None
