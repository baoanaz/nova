"""Initial-build page-cache scope preserves transaction and recovery behavior."""
from __future__ import annotations

import subprocess
import sys

import pytest
from nova_core.storage import Store
from nova_core.types import ParsedFile


def pragma(store, name):
    return store._conn.execute(f'PRAGMA {name}').fetchone()[0]


@pytest.mark.parametrize('fail', [False, True])
def test_cache_restores_after_commit_or_rollback(store, fail):
    original = {name: pragma(store, name) for name in
                ('cache_size', 'synchronous', 'journal_mode', 'wal_autocheckpoint')}
    try:
        with store.write_batch():
            assert pragma(store, 'cache_size') == -32768
            with store.write_batch():
                assert pragma(store, 'cache_size') == -32768
                store.apply_file_change(ParsedFile(path='a.py', language='python'), [], 'hash')
            assert pragma(store, 'cache_size') == -32768
            if fail:
                raise RuntimeError('rollback')
    except RuntimeError as error:
        assert fail and str(error) == 'rollback'
    assert not store._conn.in_transaction
    assert {name: pragma(store, name) for name in original} == original
    assert store.counts()['files'] == (0 if fail else 1)


@pytest.mark.parametrize('existing_cache', [-65536, 20000])
def test_larger_configured_cache_is_preserved(store, existing_cache):
    store._conn.execute(f'PRAGMA cache_size = {existing_cache}')
    with store.write_batch():
        assert pragma(store, 'cache_size') == existing_cache
    assert pragma(store, 'cache_size') == existing_cache


@pytest.mark.parametrize('remaining', ['file', 'edge', 'fts'])
def test_partial_or_existing_index_never_uses_empty_build_cache(store, remaining):
    if remaining == 'file':
        store.apply_file_change(ParsedFile(path='a.py', language='python'), [], 'hash')
    elif remaining == 'edge':
        store._conn.execute("INSERT INTO edges VALUES ('s','t','calls',1,'parsed')")
    else:
        store._conn.execute("INSERT INTO chunks_fts(rowid,content_seg) VALUES (1,'kept')")
    original = pragma(store, 'cache_size')
    with store.write_batch():
        assert pragma(store, 'cache_size') == original


def test_process_exit_before_commit_discards_batch_and_allows_replay(tmp_path):
    directory = tmp_path/'crashed'
    program = '''
import os, sys
from nova_core.storage import Store
from nova_core.types import ParsedFile
with Store.open(sys.argv[1]) as store:
    with store.write_batch():
        assert store._conn.execute('PRAGMA cache_size').fetchone()[0] == -32768
        store.apply_file_change(ParsedFile(path='a.py', language='python'), [], 'hash')
        os._exit(42)
'''
    result = subprocess.run([sys.executable, '-c', program, str(directory)], check=False)
    assert result.returncode == 42
    with Store.open(directory) as store:
        assert store.counts()['files'] == 0
        with store.write_batch():
            store.apply_file_change(ParsedFile(path='a.py', language='python'), [], 'hash')
    with Store.open(directory) as store:
        assert store.counts()['files'] == 1
        assert store._conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
