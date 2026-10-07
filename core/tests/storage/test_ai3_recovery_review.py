"""AI3 review: executable findings plus bounded, offline recovery probes.

Known defects assert the desired behavior under strict xfail. Run with
--runxfail to see the actual failures; this file does not change production code.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict
import json

import pytest

from nova_core.chunking import resolve_graph
from nova_core.storage import EdgeTargetUpdate, RefResolution, Store
from nova_core.types import EdgeDef, ParsedFile, UnresolvedRef


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="AI3-1: edge ownership omits source file")
@pytest.mark.parametrize("operation", ["replace", "delete"])
def test_namesake_file_keeps_other_files_edges(store, make_symbol, operation):
    symbol = make_symbol(name="run", fqn="Worker.run")
    # Resolving both files after ingestion can produce both edges initially.
    for path in ("a.py", "b.py"):
        store.apply_file_change(
            ParsedFile(path=path, language="python", symbols=(symbol,)), [], path
        )
    store._conn.executemany(
        "INSERT INTO edges VALUES (?, ?, 'calls', 2, 'parsed')",
        [("Worker.run", "a.target"), ("Worker.run", "b.target")],
    )
    if operation == "delete":
        store.apply_deletions(["b.py"])
    else:
        store.apply_file_change(
            ParsedFile(path="b.py", language="python", symbols=(symbol,),
                       edges=(EdgeDef("Worker.run", "b.changed", "calls", 2),)),
            [], "changed",
        )
    targets = {r[0] for r in store._conn.execute("SELECT target FROM edges")}
    assert "a.target" in targets


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="AI3-2: unresolved deduplication omits file_path")
def test_namesake_refs_resolve_to_each_files_local_symbol(store, make_symbol):
    for path, target in (("a.py", "A.helper"), ("b.py", "B.helper")):
        store.apply_file_change(
            ParsedFile(
                path=path, language="python",
                symbols=(make_symbol(name="run", fqn="Worker.run"),
                         make_symbol(name="helper", fqn=target, start=5)),
                unresolved=(UnresolvedRef("Worker.run", "helper", "call", 2),),
            ), [], path,
        )
    resolve_graph(store, [])
    targets = {r[0] for r in store._conn.execute("SELECT target FROM edges")}
    assert targets == {"A.helper", "B.helper"}


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="AI3-3: retarget swallows non-unique IntegrityError")
def test_retarget_abort_preserves_edge_for_retry(store):
    store._conn.execute("INSERT INTO edges VALUES ('caller','helper','calls',2,'parsed')")
    store._conn.execute(
        "CREATE TRIGGER reject_retarget BEFORE UPDATE ON edges "
        "BEGIN SELECT RAISE(ABORT,'injected non-unique error'); END"
    )
    update = EdgeTargetUpdate("caller", "helper", "calls", 2, "module.helper")
    caught = None
    try:
        store.retarget_edges([update])
    except sqlite3.IntegrityError as exc:
        caught = exc
    assert [r[0] for r in store._conn.execute("SELECT target FROM edges")] == ["helper"]
    assert caught is not None and "injected non-unique error" in str(caught)
    store._conn.execute("DROP TRIGGER reject_retarget")
    assert store.retarget_edges([update]) == 1


@pytest.mark.xfail(strict=True, raises=sqlite3.OperationalError,
                   reason="AI3-4: graph query batches hardcode 500 parameters")
@pytest.mark.parametrize("operation", ["targets", "specs"])
def test_graph_batches_respect_actual_variable_limit(store, operation):
    store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 80)
    if operation == "specs":
        assert store.add_spec_refs([("spec", f"s{i}") for i in range(81)]) == 81
    else:
        store.upsert_unresolved(
            [UnresolvedRef(f"caller{i}", "helper", "call", 2) for i in range(81)],
            "caller.py", "python",
        )
        requests = [RefResolution(r.id, "helper") for r in store.unresolved_refs()]
        assert store.resolve_ref_targets(requests) == 81
        assert store.unresolved_refs() == []


def test_graph_delete_failure_rolls_back_only_inner_savepoint(store):
    store.upsert_unresolved([UnresolvedRef("caller", "helper", "call", 2)],
                            "caller.py", "python")
    row = store.unresolved_refs()[0]
    requests = [RefResolution(row.id, target) for target in ("a.helper", "b.helper")]
    store._conn.execute(
        "CREATE TRIGGER reject_consume BEFORE DELETE ON unresolved_refs "
        "BEGIN SELECT RAISE(ABORT,'consume failed'); END"
    )
    with store.write_batch():
        store.set_config("outer_marker", "kept")
        with pytest.raises(sqlite3.IntegrityError, match="consume failed"):
            store.resolve_ref_targets(requests)
        assert store._conn.execute("SELECT count(*) FROM edges").fetchone()[0] == 0
        assert store.unresolved_refs() == [row]
    assert store.get_config("outer_marker") == "kept"
    store._conn.execute("DROP TRIGGER reject_consume")
    assert store.resolve_ref_targets(requests) == 2
    assert store.resolve_ref_targets(requests) == 0


@pytest.mark.parametrize("phase", ["uncommitted", "committed"])
@pytest.mark.parametrize("operation", ["file", "graph"])
def test_abrupt_process_exit_preserves_committed_state_and_allows_retry(
    tmp_path, make_chunk, phase, operation,
):
    directory = tmp_path / "index"
    parsed = ParsedFile(path="many.py", language="python")
    old = make_chunk(path="many.py", fqn="old", content="oldmarker")
    new = [make_chunk(path="many.py", fqn=f"f{i}", content=f"newmarker{i}")
           for i in range(130)]
    with Store.open(directory) as store:
        store.apply_file_change(parsed, [old], "oldhash")
        store.upsert_unresolved([UnresolvedRef("caller", "helper", "call", 2)],
                                "caller.py", "python")
    # os._exit bypasses Store.close / context manager cleanup. No model or network.
    script = """
import json, os, sys
from nova_core.storage import Store, RefResolution
from nova_core.types import ParsedFile, ChunkDef
payload = json.loads(sys.stdin.read())
store = Store.open(payload['directory'])
if payload['phase'] == 'uncommitted':
    store._conn.execute('BEGIN IMMEDIATE')
if payload['operation'] == 'file':
    store.apply_file_change(ParsedFile(path='many.py', language='python'),
                            [ChunkDef(**c) for c in payload['chunks']], 'newhash')
else:
    row = store.unresolved_refs()[0]
    store.resolve_ref_targets([RefResolution(row.id, 'a.helper'),
                               RefResolution(row.id, 'b.helper')])
os._exit(73)
"""
    child = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps({"directory": str(directory), "phase": phase,
                          "operation": operation, "chunks": [asdict(c) for c in new]}),
        text=True, capture_output=True, timeout=20, env=os.environ.copy(),
    )
    assert child.returncode == 73, child.stderr
    with Store.open(directory) as store:
        if operation == "file":
            expected = new if phase == "committed" else [old]
            rows = store._conn.execute(
                "SELECT c.id, c.content, f.content_seg FROM chunks c "
                "JOIN chunks_fts f ON f.rowid=c.rowid"
            ).fetchall()
            assert {r["id"] for r in rows} == {c.id for c in expected}
            assert all(r["content"] == r["content_seg"] for r in rows)
            assert store._conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == len(expected)
            expected_hash = "newhash" if phase == "committed" else "oldhash"
            assert store._conn.execute("SELECT content_hash FROM files").fetchone()[0] == expected_hash
            store.apply_file_change(parsed, new, "newhash")
            assert store.counts()["chunks"] == 130
            assert store._conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == 130
        else:
            refs = store.unresolved_refs()
            assert len(refs) == (1 if phase == "uncommitted" else 0)
            assert store.counts()["edges"] == (0 if phase == "uncommitted" else 2)
            if refs:
                requests = [RefResolution(refs[0].id, target)
                            for target in ("a.helper", "b.helper")]
                assert store.resolve_ref_targets(requests) == 2
                assert store.resolve_ref_targets(requests) == 0
            assert store.counts()["edges"] == 2
            assert store.unresolved_refs() == []
        assert store._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
