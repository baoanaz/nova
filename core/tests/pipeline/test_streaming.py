"""Ordering regressions: full rebuild must stream, including an incomplete final window."""
from threading import Event

from zace_core.pipeline import DirectorySource, Indexer

from .conftest import write_repo


def test_full_rebuild_embeds_before_parsing_last_file(
    store, vectors, embedding, repo, change_set, monkeypatch,
):
    embedding.batch_size = 1
    embedding.concurrency = 1
    started = Event()
    original_embed = embedding.embed

    def embed(texts):
        started.set()
        return original_embed(texts)

    monkeypatch.setattr(embedding, 'embed', embed)
    indexer = Indexer(store, embedding, vectors, DirectorySource(repo), workers=1)
    original_index_file = indexer._index_file

    def index_file(item, *args):
        if item.path == 'z.py':
            assert started.wait(5), 'Embedding must start before all files have been parsed'
        return original_index_file(item, *args)

    monkeypatch.setattr(indexer, '_index_file', index_file)
    files = {'a.py': 'def alpha():\n    return 1\n',
             'z.py': 'def zeta():\n    return 2\n'}
    write_repo(repo, files)
    report = indexer.ingest(change_set(added=files))
    assert not report.errors
    assert report.files_parsed == 2
    assert report.vectors_upserted == vectors.count() == store.counts()['chunks']


def test_graph_resolution_overlaps_incomplete_embedding_window(
    store, vectors, embedding, repo, change_set, monkeypatch,
):
    embedding.batch_size = 1000
    embedding.concurrency = 1
    started, graph_started = Event(), Event()
    original_embed = embedding.embed

    def embed(texts):
        started.set()
        assert graph_started.wait(5), 'Graph resolution must not wait for vector drain'
        return original_embed(texts)

    monkeypatch.setattr(embedding, 'embed', embed)
    indexer = Indexer(store, embedding, vectors, DirectorySource(repo), workers=1)
    original_resolve = indexer._resolve

    def resolve(*args):
        assert started.wait(5), 'The tail window must be dispatched before graph resolution'
        graph_started.set()
        return original_resolve(*args)

    monkeypatch.setattr(indexer, '_resolve', resolve)
    files = {'a.py': 'def alpha():\n    return 1\n'}
    write_repo(repo, files)
    report = indexer.ingest(change_set(added=files))
    assert not report.errors
    assert report.vectors_upserted == vectors.count() == store.counts()['chunks']
