#!/usr/bin/env python3
"""Export/replay existing real vectors without any provider or network fallback.

The fixture is a local SQLite file keyed by SHA-256 of the exact embedding input.
Replay includes real float32 materialization and indexing, but excludes remote
inference, transfer and JSON decoding. Never compare it directly with live API time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import numpy as np
from zace_core.chunking.splitter import embedding_text
from zace_core.interfaces import EmbeddingProfile
from zace_core.types import ChunkDef


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class ReplayEmbedding:
    batch_size = 500
    concurrency = 4

    def __init__(self, fixture: Path):
        self.fixture = fixture.resolve(strict=True)
        with closing(self._connect()) as db:
            self.metadata = dict(db.execute('SELECT key, value FROM metadata'))
        if self.metadata.get('schema') != '1':
            raise ValueError('Unsupported replay fixture schema')
        self.profile = EmbeddingProfile(
            model_id=self.metadata['model_id'], dim=int(self.metadata['dim']),
            max_input_tokens=int(self.metadata['max_input_tokens']),
        )

    def _connect(self):
        return sqlite3.connect(self.fixture.as_uri() + '?mode=ro&immutable=1', uri=True)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        db = self._connect()
        try:
            for text in texts:
                row = db.execute(
                    'SELECT vector FROM embeddings WHERE key=?', (text_key(text),)
                ).fetchone()
                if row is None:
                    raise RuntimeError(
                        'Offline embedding fixture miss; network fallback is forbidden'
                    )
                vector = np.frombuffer(row[0], dtype='<f4')
                if len(vector) != self.profile.dim:
                    raise ValueError('Replay vector dimension mismatch')
                vectors.append(vector.tolist())
        finally:
            db.close()
        return vectors

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        # Benchmark query must be an explicitly exported exact input; no fake fallback.
        return self.embed(texts)

    def close(self):
        pass


def export_fixture(project: Path, out: Path, *, corpus_commit: str, max_input_tokens=32000):
    import lancedb

    if out.exists():
        raise FileExistsError('Refusing to overwrite a replay fixture')
    source = sqlite3.connect((project / 'index.db').resolve().as_uri() + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    config = dict(source.execute('SELECT key, value FROM index_config'))
    dim = int(config['embedding_dim'])
    table = lancedb.connect(project / 'vectors').open_table('chunk_vectors')
    out.parent.mkdir(parents=True, exist_ok=True)
    metadata = dict(schema='1', model_id=config['embedding_model'], dim=str(dim),
                    max_input_tokens=str(max_input_tokens), corpus_commit=corpus_commit,
                    input_key='sha256(exact embedding_text UTF-8)',
                    source='existing indexed float32 vectors; no API calls',
                    query_note='probe_query uses a passage vector; not a quality benchmark')
    exported = 0
    duplicate_min_cosine = 1.0
    try:
        with sqlite3.connect(out) as db:
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE embeddings(key TEXT PRIMARY KEY, vector BLOB NOT NULL)')
            # Arrow buffers keep the export bounded; avoid a whole-table Python float list.
            for batch in table.to_arrow().to_batches(max_chunksize=256):
                ids = batch.column('chunk_id').to_pylist()
                vectors = batch.column('vector')
                for i, chunk_id in enumerate(ids):
                    row = source.execute('SELECT * FROM chunks WHERE id=?', (chunk_id,)).fetchone()
                    if row is None:
                        raise ValueError('Vector has no corresponding source chunk')
                    chunk = ChunkDef(**{name: row[name] for name in ChunkDef.__dataclass_fields__})
                    text = embedding_text(chunk)
                    vector = np.asarray(vectors[i].as_py(), dtype='<f4')
                    if vector.size != dim or not np.isfinite(vector).all():
                        raise ValueError('Invalid source vector')
                    key = text_key(text)
                    prior = db.execute(
                        'SELECT vector FROM embeddings WHERE key=?', (key,)
                    ).fetchone()
                    if prior is not None:
                        previous = np.frombuffer(prior[0], dtype='<f4')
                        cosine = float(np.dot(previous, vector))
                        duplicate_min_cosine = min(duplicate_min_cosine, cosine)
                        if cosine < 0.999:
                            raise ValueError(f'Conflicting recorded vectors: cosine={cosine}')
                        # Keep one recorded vector per exact input. Provider floating-point
                        # variation is not reproduced; both comparison runs use this same fixture.
                    else:
                        db.execute('INSERT INTO embeddings VALUES (?,?)', (key, vector.tobytes()))
                        exported += 1
                    if 'probe_query' not in metadata and 20 <= len(text) <= 150:
                        metadata['probe_query'] = text
            metadata['vectors'] = str(exported)
            metadata['duplicate_min_cosine'] = str(duplicate_min_cosine)
            db.executemany('INSERT INTO metadata VALUES (?,?)', metadata.items())
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    print(json.dumps({'vectors': exported, 'dim': dim, 'bytes': out.stat().st_size,
                      'network_requests': 0}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--corpus-commit', required=True)
    args = parser.parse_args()
    export_fixture(args.project, args.out, corpus_commit=args.corpus_commit)


if __name__ == '__main__':
    main()
