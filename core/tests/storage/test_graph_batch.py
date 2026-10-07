"""Graph batch equivalence, conflict handling, rollback and durable retry."""
from __future__ import annotations

import sqlite3

import pytest
from nova_core.storage import RefResolution, Store
from nova_core.types import UnresolvedRef


def seed(store, sources=('caller',)):
    store.upsert_unresolved(
        [UnresolvedRef(from_fqn=s, name='refresh', kind='call', line=None) for s in sources],
        'caller.py', 'python',
    )
    return store.unresolved_refs(status='pending')


def targets(rows):
    return [RefResolution(ref_id=row.id, target_fqn=target, provenance='synthesized')
            for row in rows for target in ('a.refresh', 'b.refresh')]


def edge_rows(store):
    return [tuple(r) for r in store._conn.execute(
        'SELECT source,target,kind,line,provenance FROM edges '
        'ORDER BY source,target,kind,IFNULL(line,-1)')]


def test_batch_preserves_conflicts_duplicates_missing_refs_and_counts(store):
    rows = seed(store)
    store._conn.execute(
        "INSERT INTO edges VALUES ('caller','a.refresh','calls',NULL,'parsed')")
    requests = targets(rows)
    requests.append(requests[0])
    requests.append(RefResolution(ref_id=999999, target_fqn='absent'))
    assert store.resolve_ref_targets(requests) == 3
    assert edge_rows(store) == [
        ('caller', 'a.refresh', 'calls', None, 'parsed'),
        ('caller', 'b.refresh', 'calls', None, 'synthesized'),
    ]
    assert store.unresolved_refs() == []
    assert store.resolve_ref_targets(requests) == 0


def test_batch_matches_original_seed_and_resolve_algorithm(tmp_path):
    with Store.open(tmp_path/'old') as old, Store.open(tmp_path/'new') as new:
        old_rows, new_rows = seed(old, ('a', 'b')), seed(new, ('a', 'b'))
        expected = 0
        for row in old_rows:
            expected += old.resolve_refs([
                RefResolution(ref_id=row.id, target_fqn='a.refresh', provenance='synthesized')])
            old.upsert_unresolved([
                UnresolvedRef(from_fqn=row.from_symbol, name=row.reference_name,
                              kind=row.reference_kind, line=row.line)], row.file_path, row.language)
            again = old.unresolved_refs(status='pending', file_path=row.file_path)
            replay = next(r for r in reversed(again) if r.from_symbol == row.from_symbol)
            expected += old.resolve_refs([
                RefResolution(ref_id=replay.id, target_fqn='b.refresh', provenance='synthesized')])
        assert new.resolve_ref_targets(targets(new_rows)) == expected
        assert edge_rows(new) == edge_rows(old)
        assert new.unresolved_refs() == old.unresolved_refs() == []


def test_failed_batch_preserves_committed_graph_and_retries_after_reopen(tmp_path):
    directory = tmp_path/'index'
    with Store.open(directory) as store:
        rows = seed(store, ('a', 'b'))
        store._conn.execute("INSERT INTO edges VALUES ('prior','kept','calls',1,'parsed')")
        store._conn.execute(
            "CREATE TRIGGER reject_target BEFORE INSERT ON edges "
            "WHEN new.target='b.refresh' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(sqlite3.IntegrityError, match='injected'):
            store.resolve_ref_targets(targets(rows))
        assert edge_rows(store) == [('prior', 'kept', 'calls', 1, 'parsed')]
        assert [r.id for r in store.unresolved_refs()] == [r.id for r in rows]
    with Store.open(directory) as store:
        assert len(store.unresolved_refs()) == 2
        store._conn.execute('DROP TRIGGER reject_target')
        assert store.resolve_ref_targets(targets(store.unresolved_refs())) == 4
        assert len(edge_rows(store)) == 5
        assert store.unresolved_refs() == []


def test_nested_batch_obeys_outer_rollback(store):
    rows = seed(store)
    with pytest.raises(RuntimeError, match='rollback'):
        with store.write_batch():
            assert store.resolve_ref_targets(targets(rows)) == 2
            raise RuntimeError('rollback')
    assert edge_rows(store) == []
    assert store.unresolved_refs() == rows


def test_batch_handles_more_than_one_sql_parameter_window(store):
    rows = seed(store, tuple(f'caller_{i}' for i in range(601)))
    assert store.resolve_ref_targets(targets(rows)) == 1202
    assert len(edge_rows(store)) == 1202
    assert store.unresolved_refs() == []


def test_spec_batch_retains_stale_and_provenance_and_deduplicates(store):
    store._conn.execute("INSERT INTO spec_references VALUES ('spec','old','legacy',1)")
    requests = [('spec', f'symbol_{i}') for i in range(1100)]
    assert store.add_spec_refs([('spec', 'old'), *requests, *requests]) == 1100
    assert store.add_spec_refs(requests) == 0
    old = store._conn.execute(
        "SELECT provenance,stale FROM spec_references WHERE symbol_id='old'").fetchone()
    assert tuple(old) == ('legacy', 1)
    assert store._conn.execute('SELECT count(*) FROM spec_references').fetchone()[0] == 1101


def test_spec_batch_failure_rolls_back_and_can_retry(store):
    store.add_spec_refs([('prior', 'kept')])
    store._conn.execute(
        "CREATE TRIGGER reject_spec BEFORE INSERT ON spec_references "
        "WHEN new.symbol_id='bad' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        store.add_spec_refs([('spec', 'good'), ('spec', 'bad')])
    assert store._conn.execute('SELECT count(*) FROM spec_references').fetchone()[0] == 1
    store._conn.execute('DROP TRIGGER reject_spec')
    assert store.add_spec_refs([('spec', 'good'), ('spec', 'bad')]) == 2
    with pytest.raises(sqlite3.IntegrityError):
        store.add_spec_refs([('new', 'first'), (None, 'invalid')])
    assert store._conn.execute('SELECT count(*) FROM spec_references').fetchone()[0] == 3
