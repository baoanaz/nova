#!/usr/bin/env python3
"""Serial complete-Tool acceptance runs with fixed real-vector source.

Fresh client/server processes and empty runtime stores for every sample. The
fixture and tracked corpus file data are evicted with POSIX_FADV_DONTNEED before
each sample (no system-wide cache drop). No index/vector preparation occurs
before the Tool timer. Post-run logical fingerprints are verification only.
Run in a systemd unit with IPAddressDeny=any, IPAddressAllow=localhost.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from statistics import median


def digest_file(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def evict(paths):
    count = 0
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as f:
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        count += 1
    return count


def fingerprints(out):
    """Compare logical contents, excluding timestamps and surrogate reference IDs."""
    import lancedb
    import numpy as np

    projects = list((out/'index/projects').glob('*/index.db'))
    if len(projects) != 1:
        raise RuntimeError(f'Expected one indexed project, got {len(projects)}')
    db_path = projects[0]
    counts, hashes = {}, {}
    with sqlite3.connect(db_path.as_uri()+'?mode=ro', uri=True) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        for table in ('files', 'chunks', 'symbols', 'edges', 'unresolved_refs',
                      'spec_blocks', 'spec_references'):
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info({table})')
                       if not (table == 'files' and r[1] == 'indexed_at')
                       and not (table == 'unresolved_refs' and r[1] == 'id')]
            names = ','.join('"'+c+'"' for c in columns)
            h = hashlib.sha256()
            n = 0
            for row in conn.execute(f'SELECT {names} FROM {table} ORDER BY {names}'):
                h.update(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode())
                h.update(b'\n')
                n += 1
            counts[table], hashes[table] = n, h.hexdigest()
        h = hashlib.sha256()
        for row in conn.execute(
            'SELECT c.id,f.content_seg,f.signature_seg,f.docstring_seg,f.file_path '
            'FROM chunks_fts f JOIN chunks c ON c.rowid=f.rowid ORDER BY c.id'
        ):
            h.update(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode())
            h.update(b'\n')
        hashes['fts_text_by_chunk'] = h.hexdigest()
        counts['fts_rows'] = conn.execute('SELECT count(*) FROM chunks_fts').fetchone()[0]
    table = lancedb.connect(str(db_path.parent/'vectors')).open_table('chunk_vectors')
    vectors = []
    for batch in table.to_arrow().to_batches(max_chunksize=512):
        ids = batch.column(batch.schema.get_field_index('chunk_id'))
        keys = batch.column(batch.schema.get_field_index('content_hash'))
        values = batch.column(batch.schema.get_field_index('vector'))
        for i in range(batch.num_rows):
            vector = values[i].values.to_numpy(zero_copy_only=False)
            assert len(vector) == 1024 and np.isfinite(vector).all()
            vectors.append((ids[i].as_py(), keys[i].as_py(),
                            hashlib.sha256(vector.astype('<f4').tobytes()).hexdigest()))
    vectors.sort()
    counts['vectors'] = len(vectors)
    hashes['vectors'] = hashlib.sha256(json.dumps(vectors).encode()).hexdigest()
    (out/'vectors-fingerprint.json').write_text(json.dumps(vectors))
    response = json.loads((out/'client.jsonl').read_text())
    hashes['tool_result_raw'] = hashlib.sha256(
        json.dumps(response.get('result'), sort_keys=True).encode()).hexdigest()
    hashes['tool_result'] = canonical_tool_result(out)
    return dict(counts=counts, hashes=hashes)


def canonical_tool_result(out):
    """Exclude only the elapsed-age display; retain freshness, ranking and content."""
    result = json.loads((out/'client.jsonl').read_text()).get('result')
    for item in result.get('content', []):
        if item.get('type') == 'text':
            item['text'] = re.sub(r'index: fresh \(\d+s ago\)',
                                  'index: fresh (<elapsed> ago)', item['text'])
    return hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()


def remove_run_data(out):
    # Only this run's generated stores; audit links before removing anything.
    for directory in (out/'index', out/'client-cache'):
        if not directory.exists():
            continue
        for path in (directory, *directory.rglob('*')):
            if path.is_symlink() or (path.is_file() and path.stat().st_nlink != 1):
                raise RuntimeError(f'Refusing cleanup of linked benchmark artifact: {path}')
        shutil.rmtree(directory)


def resource_snapshot():
    """Untimed boundary counters, not per-operation tracing."""
    result = {}
    try:
        result['meminfo'] = Path('/proc/meminfo').read_text()
        group = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines()
                     if line.startswith('0::'))
        root = Path('/sys/fs/cgroup')/group.lstrip('/')
        for name in ('memory.events', 'memory.stat', 'memory.pressure',
                     'io.pressure', 'cpu.stat'):
            result[name] = (root/name).read_text()
    except (OSError, StopIteration) as error:
        result['unavailable'] = str(error)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--bins', type=Path, required=True)
    parser.add_argument('--binary-prefix', default='baseline')
    parser.add_argument('--modes', default='debug,release')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, NOVA_REPO=str(args.source),
               PYTHONPATH=f'{args.source}/core:{args.source}/service',
               PYTHONHASHSEED='0', NOVA_EMBED_WORKERS='2', NOVA_EMBED_CACHE='on')
    env.pop('LD_PRELOAD', None)
    corpus_commit = subprocess.check_output(
        ['git', '-C', str(args.repo), 'rev-parse', 'HEAD'], text=True).strip()
    if corpus_commit != 'e75dae1f53c99c2b5ddb0c7bb36022c6aea25569':
        raise RuntimeError('Wrong comparison corpus revision')
    tracked = subprocess.check_output(['git', '-C', str(args.repo), 'ls-files', '-z'])
    files = [args.repo/os.fsdecode(p) for p in tracked.split(b'\0') if p]
    fixture_hash = digest_file(args.fixture)
    results = []
    metadata = dict(corpus_commit=corpus_commit, fixture_sha256=fixture_hash,
                    source=str(args.source),
                    commit=subprocess.check_output(
                        ['git', '-C', str(args.source), 'rev-parse', 'HEAD'], text=True).strip(),
                    cache_policy='fresh stores/processes; tracked corpus+fixture fadvise DONTNEED',
                    timing='client process launch through complete Tool response and client exit',
                    instrumentation=False)
    (args.out/'conditions.json').write_text(json.dumps(metadata, indent=2))
    for mode in args.modes.split(','):
        binary = args.bins/f'{args.binary_prefix}-{mode}'
        binary_hash = digest_file(binary)
        for number in range(1, args.rounds+1):
            out = args.out/f'{mode}-{number}'
            evicted = evict([args.fixture, *files])
            started = time.time()
            command = [sys.executable, str(Path(__file__).with_name('sync_probe.py')),
                       '--kind', 'client', '--fixture', str(args.fixture),
                       '--repo', str(args.repo), '--out', str(out), '--client', str(binary)]
            resources_before = resource_snapshot()
            subprocess.run(command, env=env, check=True)
            resources_after = resource_snapshot()
            (out/'resource-boundaries.json').write_text(json.dumps(
                dict(before=resources_before, after=resources_after), indent=2))
            result = json.loads((out/'result.json').read_text())
            assert not result['server']['phases_s'], 'Acceptance run must disable phase patches'
            assert str(args.source) in result['server']['engine_source']
            reports = result['server']['ingests']
            assert len(reports) == 1 and not reports[0]['report']['errors']
            assert reports[0]['report']['files_parsed'] == 2986
            assert reports[0]['report']['vectors_upserted'] == 20931
            subprocess.run([sys.executable, __file__, '--fingerprint-only', str(out)],
                           check=True)
            identity = json.loads((out/'equivalence.json').read_text())
            entry = dict(mode=mode, sample=number, wall_s=result['wall_s'],
                         server_cpu_s=result['server_cpu_s'], binary_sha256=binary_hash,
                         started_at=started, evicted_files=evicted,
                         equivalence=identity, output=str(out))
            results.append(entry)
            (args.out/'samples.json').write_text(json.dumps(results, indent=2))
            print(json.dumps({'mode': mode, 'sample': number,
                              'wall_s': entry['wall_s'],
                              'verified': identity['counts']}), flush=True)
            remove_run_data(out)
    summary = {mode: dict(samples=[r['wall_s'] for r in results if r['mode'] == mode],
                         median_s=median(r['wall_s'] for r in results if r['mode'] == mode))
               for mode in args.modes.split(',')}
    (args.out/'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--fingerprint-only':
        destination = Path(sys.argv[2])
        (destination/'equivalence.json').write_text(
            json.dumps(fingerprints(destination), indent=2))
    else:
        main()
