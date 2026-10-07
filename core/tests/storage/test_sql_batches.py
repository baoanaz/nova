"""Multi-row SQL keeps FTS identities and file rollback across statement windows."""
from __future__ import annotations

import sqlite3

import pytest
from nova_core.storage import store as store_module
from nova_core.text.segmenter import segment
from nova_core.types import ParsedFile


@pytest.mark.parametrize('supports_returning', [True, False])
def test_parameter_windows_and_unordered_returning(
    store, make_chunk, monkeypatch, supports_returning,
):
    if not supports_returning:
        monkeypatch.setattr(sqlite3, 'sqlite_version_info', (3, 34, 0))
    store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 80)
    original = store_module._insert_rows

    def reversed_returning(*args, **kwargs):
        result = original(*args, **kwargs)
        return list(reversed(result)) if kwargs.get('returning') else result

    monkeypatch.setattr(store_module, '_insert_rows', reversed_returning)
    chunks = [make_chunk(path='many.py', fqn=f'f{i}', content=f'marker_{i}')
              for i in range(300)]
    store.apply_file_change(ParsedFile(path='many.py', language='python'), chunks, 'hash')
    rows = store._conn.execute(
        'SELECT c.id,c.content,f.content_seg FROM chunks c '
        'JOIN chunks_fts f ON c.rowid=f.rowid').fetchall()
    assert len(rows) == 300
    assert {r['id'] for r in rows} == {c.id for c in chunks}
    assert all(r['content_seg'] == segment(r['content']) for r in rows)


def test_constraint_failure_in_later_statement_restores_old_file_then_retries(store, make_chunk):
    parsed = ParsedFile(path='many.py', language='python')
    old = make_chunk(path='many.py', fqn='old', content='oldmarker')
    store.apply_file_change(parsed, [old], 'oldhash')
    store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 80)
    chunks = [make_chunk(path='many.py', fqn=f'f{i}', content=f'marker_{i}')
              for i in range(300)]
    with pytest.raises(sqlite3.IntegrityError):
        store.apply_file_change(parsed, [*chunks, chunks[0]], 'newhash')
    assert store._conn.execute('SELECT content_hash FROM files').fetchone()[0] == 'oldhash'
    assert store._conn.execute('SELECT id FROM chunks').fetchall()[0][0] == old.id
    assert store._conn.execute('SELECT content_seg FROM chunks_fts').fetchall()[0][0] == 'oldmarker'
    delta = store.apply_file_change(parsed, chunks, 'newhash')
    assert delta.removed_chunk_ids == (old.id,)
    assert store.counts()['chunks'] == 300
    assert store._conn.execute('SELECT count(*) FROM chunks_fts').fetchone()[0] == 300
