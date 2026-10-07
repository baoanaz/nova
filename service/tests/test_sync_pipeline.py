"""Bounded sync protocol tests: no embedding calls, network providers or benchmarks."""
from pathlib import Path

import pytest
from nova_core.pipeline import IngestReport
from nova_service.blobstore import BlobStore
from nova_service.indexer import IndexProgress
from nova_service.sync_state import SyncState

from tests.test_sync_api import encode_blob


@pytest.fixture
def staged(client, engine_manager, monkeypatch):
    project = client.post('/api/projects/resolve', json={
        'identityKey': 'bounded-sync', 'displayName': 'bounded',
    }).json()['projectId']
    calls = []

    def ingest(pid, changes):
        assert pid == project
        calls.append(changes)
        return IngestReport(files_parsed=len(changes.added) + len(changes.modified))

    monkeypatch.setattr(engine_manager, 'ingest', ingest)
    # Control the worker schedule deterministically; exercise real ledger and HTTP.
    starts = []
    monkeypatch.setattr(engine_manager, 'start_sync',
                        lambda pid: starts.append(pid) or IndexProgress(state='running'))
    return project, calls, starts


def upload(client, project, blobs, *, pipeline=True):
    return client.post('/api/sync/batch-upload', json={
        'projectId': project, 'blobs': blobs,
        'deferIndexing': True, 'pipeline': pipeline,
    })


def seal(client, project, hashes):
    return client.post('/api/sync/checkpoint', json={
        'projectId': project, 'blobHashes': hashes, 'seal': True,
    })


def test_upload_ack_precedes_readiness_and_next_batch_applies_backpressure(
    client, engine_manager, staged,
):
    project, calls, starts = staged
    first = encode_blob('a.py', 'def a():\n    return 1\n')
    second = encode_blob('b.py', 'def b():\n    return 2\n')
    response = upload(client, project, [first])
    assert response.status_code == 200
    assert response.json()['accepted'] == [first['blobHash']]
    assert starts == [project]
    assert calls == []
    assert seal(client, project, [first['blobHash']]).status_code == 409
    assert upload(client, project, [second]).status_code == 200
    assert len(calls) == 1
    assert set(engine_manager.sync_state(project).pending_files) == {'b.py'}
    engine_manager.flush_sync(project)
    response = seal(client, project, [first['blobHash'], second['blobHash']])
    assert response.status_code == 200
    assert response.json()['sealed'] is True
    assert response.json()['checkpointId'] in SyncState.load(
        engine_manager.project_dir(project)).checkpoints


def test_disconnected_ack_can_be_replayed_after_ledger_reload(client, engine_manager, staged):
    project, _, _ = staged
    blob = encode_blob('a.py', 'def a():\n    return 1\n')
    # Discard the first response, as if the socket closed after server commit.
    upload(client, project, [blob])
    reloaded = SyncState.load(engine_manager.project_dir(project))
    assert reloaded.pending_files == {'a.py': 'added'}
    assert upload(client, project, [blob]).json()['accepted'] == [blob['blobHash']]
    engine_manager.flush_sync(project)
    assert seal(client, project, [blob['blobHash']]).status_code == 200
    assert len(engine_manager.sync_state(project).files) == 1


def test_failed_batch_does_not_accept_next_batch(client, engine_manager, staged, monkeypatch):
    project, _, _ = staged
    first = encode_blob('a.py', 'def a():\n    return 1\n')
    second = encode_blob('b.py', 'def b():\n    return 2\n')
    assert upload(client, project, [first]).status_code == 200
    original = engine_manager.ingest
    monkeypatch.setattr(engine_manager, 'ingest',
                        lambda *args: IngestReport(errors=('retry me',)))
    assert upload(client, project, [second]).status_code == 503
    assert set(engine_manager.sync_state(project).files) == {'a.py'}
    assert engine_manager.sync_state(project).pending
    monkeypatch.setattr(engine_manager, 'ingest', original)
    assert upload(client, project, [second]).status_code == 200
    engine_manager.flush_sync(project)
    assert seal(client, project, [first['blobHash'], second['blobHash']]).status_code == 200


def test_manifest_seal_rejects_overwrite_and_missing_or_extra_hashes(
    client, engine_manager, staged,
):
    project, _, _ = staged
    old = encode_blob('a.py', 'def a():\n    return 1\n')
    new = encode_blob('a.py', 'def a():\n    return 2\n')
    upload(client, project, [old])
    upload(client, project, [new])
    engine_manager.flush_sync(project)
    for hashes in ([old['blobHash']], [], [new['blobHash'], old['blobHash']]):
        assert seal(client, project, hashes).status_code == 409
    assert seal(client, project, [new['blobHash']]).status_code == 200


def test_pipeline_rejects_excess_count_without_writing(client, engine_manager, staged):
    project, _, _ = staged
    blobs = [encode_blob(f'{i}.py', '') for i in range(65)]
    assert upload(client, project, blobs).status_code == 413
    assert engine_manager.sync_state(project).files == {}


def test_restart_backlog_is_drained_in_bounded_batches_and_failure_keeps_tail(
    client, engine_manager, staged, monkeypatch,
):
    project, _, _ = staged
    blobs = [encode_blob(f'{i:03}.py', '') for i in range(130)]
    assert upload(client, project, blobs, pipeline=False).status_code == 200
    sizes = []

    def ingest(pid, changes):
        size = len(changes.added) + len(changes.modified)
        sizes.append(size)
        if len(sizes) == 2:
            return IngestReport(errors=('failed second batch',))
        return IngestReport(files_parsed=size)

    monkeypatch.setattr(engine_manager, 'ingest', ingest)
    assert engine_manager.flush_sync(project).errors
    # Disk, not the manager's memory, is authoritative after restart.
    reloaded = SyncState.load(engine_manager.project_dir(project))
    assert len(reloaded.pending_files) == 66
    assert sizes == [64, 64]
    report = engine_manager.flush_sync(project)
    assert not report.errors
    assert report.files_parsed == 66
    assert sizes == [64, 64, 64, 2]
    assert not SyncState.load(engine_manager.project_dir(project)).pending


def test_batch_bytes_are_bounded(client, engine_manager, staged, monkeypatch):
    project, _, _ = staged
    for i in range(3):
        assert upload(client, project, [encode_blob(f'{i}.txt', 'x' * 600_000)],
                      pipeline=False).status_code == 200
    sizes = []

    def ingest(pid, changes):
        sizes.append(sum(len(item.content) for item in (*changes.added, *changes.modified)))
        return IngestReport()

    monkeypatch.setattr(engine_manager, 'ingest', ingest)
    engine_manager.flush_sync(project)
    assert sizes == [600_000] * 3


def test_checkpoint_save_failure_never_confirms_seal(client, engine_manager, staged, monkeypatch):
    project, _, _ = staged
    blob = encode_blob('a.py', '')
    upload(client, project, [blob])
    engine_manager.flush_sync(project)
    original = SyncState.save

    def fail(self):
        raise OSError('disk full')

    monkeypatch.setattr(SyncState, 'save', fail)
    assert seal(client, project, [blob['blobHash']]).status_code == 507
    monkeypatch.setattr(SyncState, 'save', original)
    assert seal(client, project, [blob['blobHash']]).json()['sealed'] is True


def test_blob_failed_publish_leaves_no_partial_target(tmp_path, monkeypatch):
    store = BlobStore.open(tmp_path)
    blob = encode_blob('a.py', 'content')
    original = Path.replace

    def fail(self, target):
        raise OSError('interrupted publish')

    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError):
        store.put('a.py', blob['blobHash'], b'content')
    assert not store.exists(blob['blobHash'])
    monkeypatch.setattr(Path, 'replace', original)
    assert store.put('a.py', blob['blobHash'], b'content')
    assert store.get(blob['blobHash']) == b'content'


def begin(client, project, session):
    response = client.post('/api/sync/flush', json={
        'projectId': project, 'sessionId': session, 'begin': True,
    })
    assert response.status_code == 200, response.text
    assert response.json()['sessionId'] == session


def session_upload(client, project, session, blob):
    return client.post('/api/sync/batch-upload', json={
        'projectId': project, 'sessionId': session, 'pipeline': True,
        'deferIndexing': True, 'blobs': [blob],
    })


def close_input(client, project, session, hashes):
    return client.post('/api/sync/flush', json={
        'projectId': project, 'sessionId': session, 'blobHashes': hashes,
    })


def test_restart_recovers_orphan_upload_and_seals_authoritative_manifest(
    client, engine_manager, staged, monkeypatch,
):
    from nova_service.runtime import EngineManager

    project, calls, _ = staged
    orphan = encode_blob('orphan.py', '')
    kept = encode_blob('kept.py', '')
    begin(client, project, 'disconnected')
    assert session_upload(client, project, 'disconnected', orphan).status_code == 200
    # A fresh manager can recover persisted session and work without any queue.
    restarted = EngineManager(engine_manager.data_root, engine_manager.engine)
    monkeypatch.setattr(restarted, 'ingest', engine_manager.ingest)
    assert restarted.sync_state(project).session_id == 'disconnected'
    assert restarted.flush_sync(project).files_parsed == 1
    begin(client, project, 'retry')
    assert session_upload(client, project, 'retry', kept).status_code == 200
    assert close_input(client, project, 'retry', [kept['blobHash']]).status_code == 200
    assert restarted.sync_state(project).input_closed
    assert restarted.sync_state(project).pending_deleted == ('orphan.py',)
    restarted.flush_sync(project)
    assert any('orphan.py' in change.deleted for change in calls)
    assert set(restarted.sync_state(project).files) == {'kept.py'}
    response = client.post('/api/sync/checkpoint', json={
        'projectId': project, 'sessionId': 'retry', 'seal': True,
        'blobHashes': [kept['blobHash']],
    })
    assert response.json()['sealed'] is True
    # Lost close/checkpoint responses are replayable, but new input is fenced.
    assert close_input(client, project, 'retry', [kept['blobHash']]).status_code == 200
    assert session_upload(client, project, 'retry', orphan).status_code == 409


def test_session_takeover_fences_old_upload_delete_and_seal(client, engine_manager, staged):
    project, _, _ = staged
    blob = encode_blob('a.py', '')
    begin(client, project, 'old')
    assert session_upload(client, project, 'old', blob).status_code == 200
    begin(client, project, 'new')
    assert session_upload(client, project, 'old', blob).status_code == 409
    assert close_input(client, project, 'old', []).status_code == 409
    response = client.post('/api/sync/deletions', json={
        'projectId': project, 'sessionId': 'old', 'paths': ['a.py'],
    })
    assert response.status_code == 409
    engine_manager.flush_sync(project)
    response = client.post('/api/sync/checkpoint', json={
        'projectId': project, 'sessionId': 'old', 'seal': True,
        'blobHashes': [blob['blobHash']],
    })
    assert response.status_code == 409
    assert 'a.py' in engine_manager.sync_state(project).files


def test_legacy_write_invalidates_active_session(client, staged):
    project, _, _ = staged
    begin(client, project, 'modern')
    blob = encode_blob('a.py', '')
    assert upload(client, project, [blob]).status_code == 200
    assert close_input(client, project, 'modern', [blob['blobHash']]).status_code == 409


def test_close_rejects_missing_blob_without_deleting_existing_files(client, engine_manager, staged):
    project, _, _ = staged
    begin(client, project, 'current')
    blob = encode_blob('a.py', '')
    session_upload(client, project, 'current', blob)
    assert close_input(client, project, 'current', ['missing']).status_code == 409
    state = engine_manager.sync_state(project)
    assert not state.input_closed
    assert 'a.py' in state.files


def test_open_input_is_not_ready_even_after_last_batch_has_finished(client, engine_manager, staged):
    project, _, _ = staged
    begin(client, project, 'scanning')
    blob = encode_blob('a.py', '')
    session_upload(client, project, 'scanning', blob)
    engine_manager.flush_sync(project)
    status = client.get(f'/api/sync/status/{project}?progressOnly=true').json()
    assert status['pendingJobs'] == 1
    assert status['inputClosed'] is False
    response = client.post('/api/sync/checkpoint', json={
        'projectId': project, 'sessionId': 'scanning', 'seal': True,
        'blobHashes': [blob['blobHash']],
    })
    assert response.status_code == 409
    assert close_input(client, project, 'scanning', [blob['blobHash']]).status_code == 200
    status = client.get(f'/api/sync/status/{project}?progressOnly=true').json()
    assert status['pendingJobs'] == 0
    assert status['inputClosed'] is True


def test_empty_manifest_deletes_all_previous_input(client, engine_manager, staged):
    project, calls, _ = staged
    upload(client, project, [encode_blob('gone.py', '')])
    engine_manager.flush_sync(project)
    begin(client, project, 'empty')
    assert close_input(client, project, 'empty', []).status_code == 200
    engine_manager.flush_sync(project)
    assert calls[-1].deleted == ('gone.py',)
    assert not engine_manager.sync_state(project).files
    response = client.post('/api/sync/checkpoint', json={
        'projectId': project, 'sessionId': 'empty', 'seal': True, 'blobHashes': [],
    })
    assert response.json()['sealed'] is True
