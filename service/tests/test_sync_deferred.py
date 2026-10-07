"""Deferred sync must be durable, coalesced, retryable, and observable before query."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from zace_core.engine import Engine
from zace_core.pipeline import IngestReport
from zace_service.runtime import EngineManager

from tests.conftest import DeterministicBigramEmbedding
from tests.test_sync_api import encode_blob, store_counts


@pytest.fixture
def project(client):
    return client.post('/api/projects/resolve', json={
        'identityKey': 'deferred-sync', 'displayName': 'deferred',
    }).json()['projectId']


def stage(client, project, path):
    response = client.post('/api/sync/batch-upload', json={
        'projectId': project, 'deferIndexing': True,
        'blobs': [encode_blob(path, f'def {path[0]}():\n    return 1\n')],
    })
    assert response.status_code == 200, response.text
    assert response.json()['indexingDeferred'] is True
    assert response.json()['report'] is None


def status(client, project):
    response = client.get(f'/api/sync/status/{project}')
    assert response.status_code == 200, response.text
    return response.json()


def flush(client, manager, project):
    response = client.post('/api/sync/flush', json={'projectId': project})
    assert response.status_code == 200, response.text
    manager._indexers[project].join(20)
    assert not manager._indexers[project].running
    return status(client, project)


def test_batches_coalesce_and_duplicate_flush_does_not_enqueue(
    client, engine_manager, project, monkeypatch,
):
    stage(client, project, 'a.py')
    stage(client, project, 'b.py')
    before = status(client, project)
    assert before['pendingJobs'] == 1
    assert before['filesIndexed'] == 0
    entered, release = Event(), Event()
    calls = []
    original = engine_manager.ingest

    def blocked(pid, changes):
        calls.append(changes)
        entered.set()
        assert release.wait(5)
        return original(pid, changes)

    monkeypatch.setattr(engine_manager, 'ingest', blocked)
    try:
        assert client.post('/api/sync/flush', json={'projectId': project}).status_code == 200
        assert entered.wait(5)
        assert status(client, project)['indexProgress']['state'] == 'running'
        assert client.post('/api/sync/flush', json={'projectId': project}).status_code == 200
        assert len(calls) == 1
    finally:
        release.set()
        engine_manager._indexers[project].join(20)
        assert not engine_manager._indexers[project].running
    after = status(client, project)
    assert after['pendingJobs'] == 0
    assert after['filesIndexed'] == 2
    assert len(calls[0].added) == 2
    flush(client, engine_manager, project)
    assert len(calls) == 1, 'An empty flush must not reindex the project'


@pytest.mark.parametrize('partial_report', [False, True])
def test_failed_ingest_retains_pending_and_can_retry(
    client, engine_manager, project, monkeypatch, partial_report,
):
    stage(client, project, 'a.py')
    original = engine_manager.ingest

    def fail(*args):
        if partial_report:
            return IngestReport(errors=('parse failed',))
        raise RuntimeError('provider unavailable')

    monkeypatch.setattr(engine_manager, 'ingest', fail)
    failed = flush(client, engine_manager, project)
    assert failed['pendingJobs'] == 1
    assert failed['indexProgress']['error']
    assert engine_manager.sync_state(project).pending
    monkeypatch.setattr(engine_manager, 'ingest', original)
    succeeded = flush(client, engine_manager, project)
    assert succeeded['pendingJobs'] == 0
    assert succeeded['indexProgress']['error'] is None
    assert succeeded['filesIndexed'] == 1


def test_pending_survives_manager_restart(client, engine_manager, project):
    stage(client, project, 'a.py')
    restarted = EngineManager(engine_manager.data_root, Engine.open(
        engine_manager.data_root, provider=DeterministicBigramEmbedding(),
    ))
    try:
        assert restarted.sync_state(project).pending
        report = restarted.flush_sync(project)
        assert report.files_parsed == 1
        assert not restarted.sync_state(project).pending
        assert store_counts(restarted, project)['files'] == 1
    finally:
        restarted.close()


def test_deletion_before_flush_never_resurrects_file(client, engine_manager, project):
    stage(client, project, 'a.py')
    stage(client, project, 'b.py')
    response = client.post('/api/sync/deletions', json={
        'projectId': project, 'paths': ['a.py'], 'deferIndexing': True,
    })
    assert response.status_code == 200
    assert response.json()['indexingDeferred'] is True
    after = flush(client, engine_manager, project)
    assert after['filesIndexed'] == 1
    assert set(engine_manager.sync_state(project).files) == {'b.py'}
    assert not engine_manager.sync_state(project).pending


def test_concurrent_uploads_preserve_both_ledger_entries(client, engine_manager, project):
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(stage, client, project, path) for path in ('a.py', 'b.py')]
        for future in futures:
            future.result(timeout=5)
    assert set(engine_manager.sync_state(project).pending_files) == {'a.py', 'b.py'}
    assert flush(client, engine_manager, project)['filesIndexed'] == 2


def test_deferred_skips_are_available_after_flush(client, engine_manager, project):
    response = client.post('/api/sync/batch-upload', json={
        'projectId': project, 'deferIndexing': True,
        'blobs': [encode_blob('blob.bin', b'\x00binary')],
    })
    assert response.status_code == 200
    after = flush(client, engine_manager, project)
    assert after['pendingJobs'] == 0
    assert after['skippedFiles'] == ['blob.bin']


def test_progress_poll_does_not_open_index_or_scan_blobs(
    client, engine_manager, project, monkeypatch,
):
    stage(client, project, 'a.py')

    def forbidden(*args, **kwargs):
        raise AssertionError('Readiness polling must not open the index or enumerate blobs')

    monkeypatch.setattr(engine_manager.engine, 'sync_status', forbidden)
    monkeypatch.setattr(engine_manager, 'blob_store', forbidden)
    response = client.get(f'/api/sync/status/{project}?progressOnly=true')
    assert response.status_code == 200, response.text
    assert response.json()['pendingJobs'] == 1
    assert response.json()['indexProgress']['state'] == 'idle'


@pytest.mark.parametrize('delete_before_initialize', [False, True])
def test_flush_racing_project_deletion_does_not_recreate_directory(
    client, engine_manager, project, monkeypatch, delete_before_initialize,
):
    stage(client, project, 'a.py')
    directory = engine_manager.project_dir(project)
    original_initialize = engine_manager._initialize_project

    def race(pid):
        if delete_before_initialize:
            assert engine_manager.delete_project(pid)
            original_initialize(pid)
        else:
            original_initialize(pid)
            assert engine_manager.delete_project(pid)

    monkeypatch.setattr(engine_manager, '_initialize_project', race)
    response = client.post('/api/sync/flush', json={'projectId': project})
    assert response.status_code == 404
    assert not directory.exists()
    assert project not in engine_manager._indexers


def test_deletion_cannot_be_bypassed_by_starting_another_worker(
    client, engine_manager, project, monkeypatch,
):
    from zace_service.errors import ApiError

    stage(client, project, 'a.py')
    original_stop = engine_manager._stop_indexer

    def stop(pid):
        original_stop(pid)
        with pytest.raises(ApiError) as raised:
            engine_manager.start_sync(pid)
        assert raised.value.status == 404

    monkeypatch.setattr(engine_manager, '_stop_indexer', stop)
    directory = engine_manager.project_dir(project)
    assert engine_manager.delete_project(project)
    assert not directory.exists()


def test_legacy_sync_repair_clears_old_background_failure(
    client, engine_manager, project, monkeypatch,
):
    stage(client, project, 'a.py')
    original = engine_manager.ingest

    def fail(*args):
        raise RuntimeError('provider unavailable')

    monkeypatch.setattr(engine_manager, 'ingest', fail)
    assert flush(client, engine_manager, project)['indexProgress']['error']
    monkeypatch.setattr(engine_manager, 'ingest', original)
    response = client.post('/api/sync/batch-upload', json={
        'projectId': project, 'blobs': [encode_blob('b.py', 'def b():\n    return 2\n')],
    })
    assert response.status_code == 200, response.text
    after = client.get(f'/api/sync/status/{project}?progressOnly=true').json()
    assert after['pendingJobs'] == 0
    assert after['indexProgress']['error'] is None




def test_readiness_poll_avoids_core_counts_and_blob_scan(
    client, engine_manager, project, monkeypatch,
):
    stage(client, project, 'a.py')

    def forbidden(*args, **kwargs):
        raise AssertionError('Readiness polling must not open the index or scan blob storage')

    monkeypatch.setattr(engine_manager.engine, 'sync_status', forbidden)
    monkeypatch.setattr(engine_manager, 'blob_store', forbidden)
    response = client.get(f'/api/sync/status/{project}?progressOnly=true')
    assert response.status_code == 200
    assert response.json()['pendingJobs'] == 1
    assert 'filesIndexed' not in response.json()
