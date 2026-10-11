from nova_core.retrieval.expand import SYNTHESIZED_REASON, build_flows, expand
from nova_core.types import Candidate


def test_framework_constructor_context_and_incremental_removal(store, ingest_file):
    source = ('from fastapi import FastAPI\n\napp = FastAPI()\n\n'
              '@app.get("/p")\ndef handler():\n    return 1\n')
    ingest_file("routes.py", source)
    row = store.exact_symbols("handler")[0]
    seed = Candidate(row.chunk_id, kind="function", rrf_score=1, score=1,
                     symbol_fqn=row.fqn, path=row.file_path, start_line=row.start_line)
    result = expand(store, [seed])
    related = [c for c in result.candidates if c.chunk_id != row.chunk_id]
    assert any("app = FastAPI()" in store.chunk_by_id(c.chunk_id).content for c in related)
    assert any(SYNTHESIZED_REASON in c.reasons for c in related)
    assert build_flows(store, [seed]) == []
    ingest_file("routes.py", source.replace("app = FastAPI()", "app = Other()"))
    assert not [e for e in store.edges_for("handler") if e.kind == "registers"]
    assert not expand(store, [seed]).candidates
