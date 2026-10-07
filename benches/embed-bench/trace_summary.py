#!/usr/bin/env python3
"""Summarize nested Python/native indexing traces without double-counting.

Usage: python benches/embed-bench/trace_summary.py OUT
Produces summary.json and an additive waterfall.csv.
Start/end are relative to index.job; CPU is the current native thread's clock.
The additive waterfall covers ONLY the indexing thread. Concurrent consumers are
reported separately. Native SQLite/IO children are subtracted from their parents.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import struct
from collections import defaultdict
from pathlib import Path

PY = struct.Struct('<IIIIIiiiQQQQ')
NATIVE = struct.Struct('<QQQQIIIIqq')
OPS = {1: 'fsync', 2: 'fdatasync', 3: 'pwrite', 4: 'pwrite64', 5: 'pread', 6: 'pread64',
       10: 'step', 11: 'reset', 12: 'finalize', 13: 'prepare', 14: 'checkpoint'}
ROLES = {1: 'db', 2: 'wal', 3: 'journal', 4: 'fixture'}
# Event fields: start,end,cpu_start,cpu_end,label,fid,files,chunks,rows,tid,bytes


def records(path, layout):
    with path.open('rb') as f:
        while block := f.read(layout.size * 4096):
            if len(block) % layout.size:
                raise ValueError(f'Truncated trace: {path}')
            yield from layout.iter_unpack(block)


def category(name, ancestors):
    if name.startswith('native.'):
        if name.endswith(('pread', 'pread64')):
            return 'storage.read'
        if name.endswith('checkpoint'):
            return 'sqlite.checkpoint.native'
        if name.endswith(('fsync', 'fdatasync')):
            return 'storage.fsync'
        if name.startswith('native.wal.'):
            return 'storage.wal_write'
        if name.startswith(('native.db.pwrite', 'native.journal.')):
            return 'storage.db_write_checkpoint'
        sql = next((n for n in reversed(ancestors) if n.startswith('sql.')), '')
        if '.commit.' in sql:
            return 'sqlite.commit.native'
        if '.fts.' in sql:
            return 'sqlite.fts.native'
        if '.dml.' in sql:
            return 'sqlite.dml.native'
        if '.transaction.' in sql:
            return 'sqlite.transaction.native'
        if 'close_checkpoint' in sql:
            return 'sqlite.close.native'
        return 'sqlite.read_and_other.native'
    if name.startswith('sql.'):
        if '.commit.' in name:
            return 'sqlite.commit.python_boundary'
        if '.fts.' in name:
            return 'sqlite.fts.python_boundary'
        if '.dml.' in name:
            return 'sqlite.dml.python_boundary'
        if '.transaction.' in name:
            return 'sqlite.transaction.python_boundary'
        if 'close_checkpoint' in name:
            return 'sqlite.close.python_boundary'
        return 'sqlite.read_and_other.python_boundary'
    if name.startswith('tokenizer'):
        return 'tokenizer'
    if name == 'parse' or name == 'chunk.split':
        return name
    if name.startswith('python.'):
        return 'python.objects'
    if name.startswith('queue.drain'):
        return 'vector.drain_wait'
    if name.startswith('lock.'):
        return 'lock.wait'
    if name.startswith('queue.'):
        return 'vector.enqueue_and_pipeline'
    if 'indexer._resolve' in ancestors or name == 'indexer._resolve':
        return 'graph.python'
    if name in ('fts.prepare_and_write', 'store.file') or name.startswith('store._'):
        return 'store.python_loop'
    if name.startswith(('engine.', 'vector.', 'store.')):
        return 'engine.open_count_and_finish'
    return 'indexer.loop_state_and_job'


def scope(name, ancestors):
    chain = ancestors + [name]
    for needle, bucket in (
        ('store.file', 'sqlite_file_change'),
        ('indexer.file', 'file_parse_chunk_and_bookkeeping'),
        ('indexer._resolve', 'graph'),
        ('indexer._collect_inputs', 'collect_inputs'),
        ('queue.submit', 'enqueue'),
        ('store.set_config', 'set_config'),
    ):
        if needle in chain:
            return bucket
    return 'indexer_remainder' if 'indexer.run' in chain else 'engine_and_job'


def empty():
    return dict(wall_ns=0, cpu_ns=0, inclusive_wall_ns=0, inclusive_cpu_ns=0,
                events=0, files=0, chunks=0, rows=0, bytes=0, first_ns=None, last_ns=None)


def add_span(row, event, wall, cpu):
    s, e, c0, c1, _, _, files, chunks, rows, _, size = event
    row['wall_ns'] += wall
    row['cpu_ns'] += cpu
    row['inclusive_wall_ns'] += e-s
    row['inclusive_cpu_ns'] += c1-c0
    row['events'] += 1
    row['files'] += files
    row['chunks'] += chunks
    row['rows'] += rows
    row['bytes'] += size
    row['first_ns'] = min(s, row['first_ns']) if row['first_ns'] is not None else s
    row['last_ns'] = max(e, row['last_ns']) if row['last_ns'] is not None else e


def summarize(events, labels, *, segments=None):
    """Interval tree via a bounded stack. Child time is removed exactly once."""
    events.sort(key=lambda e: (e[0], -e[1]))
    stack = []
    by_name, by_category = defaultdict(empty), defaultdict(empty)
    by_scope = defaultdict(lambda: defaultdict(empty))
    unique_files = defaultdict(set)
    violations = []

    def gap(frame, end, cpu_end):
        event, _, _, cursor, cpu_cursor, cat, _ = frame
        if end > cursor and segments is not None:
            segments.append((cursor, end, cpu_cursor, cpu_end, cat, event[4], event[5]))

    def finish():
        frame = stack.pop()
        event, children_wall, children_cpu, _, _, cat, bucket = frame
        s, e, c0, c1, name, fid, *_ = event
        gap(frame, e, c1)
        wall, cpu = e-s-children_wall, c1-c0-children_cpu
        if wall < 0 or cpu < 0:
            violations.append(dict(name=name, wall_ns=wall, cpu_ns=cpu))
        add_span(by_name[name], event, wall, cpu)
        add_span(by_category[cat], event, wall, cpu)
        add_span(by_scope[bucket][cat], event, wall, cpu)
        if fid:
            unique_files[cat].add(fid)
        if stack:
            stack[-1][1] += e-s
            stack[-1][2] += c1-c0
            stack[-1][3], stack[-1][4] = e, c1

    for event in events:
        while stack and event[0] >= stack[-1][0][1]:
            finish()
        if stack:
            if event[1] > stack[-1][0][1]:
                raise ValueError(f'Crossing events: {event[4]} / {stack[-1][0][4]}')
            gap(stack[-1], event[0], event[2])
        ancestors = [f[0][4] for f in stack]
        cat = category(event[4], ancestors)
        stack.append([event, 0, 0, event[0], event[2], cat, scope(event[4], ancestors)])
    while stack:
        finish()
    for cat, ids in unique_files.items():
        by_category[cat]['unique_files'] = len(ids)
    return dict(by_name=dict(by_name), categories=dict(by_category),
                scopes=dict(by_scope), violations=violations)


def seconds(rows, origin):
    result = {}
    for name, row in rows.items():
        out = dict(row)
        for key in ('wall_ns', 'cpu_ns', 'inclusive_wall_ns', 'inclusive_cpu_ns'):
            out[key.replace('_ns', '_s')] = out.pop(key)/1e9
        out['start_s'] = (out.pop('first_ns')-origin)/1e9
        out['end_s'] = (out.pop('last_ns')-origin)/1e9
        result[name] = out
    return result


def draw_waterfall(path, root, timeline, categories, workers):
    """Standalone SVG: additive budget and real-time main/consumer lanes."""
    palette = {
        '普通 SQL': '#e76f51', 'FTS': '#2a9d8f', 'Tokenizer': '#8b5cf6',
        '解析/切分': '#3b82f6', 'WAL/IO/同步': '#b91c1c',
        '事务/查询': '#e9a23b', 'Python/循环/图': '#64748b',
        '初始化/收尾': '#0f766e', '队列/锁': '#a16207',
    }

    def group(cat):
        if cat.startswith('sqlite.dml'):
            return '普通 SQL'
        if cat.startswith('sqlite.fts'):
            return 'FTS'
        if cat == 'tokenizer':
            return 'Tokenizer'
        if cat in ('parse', 'chunk.split'):
            return '解析/切分'
        if cat.startswith('storage.'):
            return 'WAL/IO/同步'
        if cat.startswith('sqlite.'):
            return '事务/查询'
        if cat.startswith(('vector.', 'lock.')):
            return '队列/锁'
        if cat == 'engine.open_count_and_finish':
            return '初始化/收尾'
        return 'Python/循环/图'

    total = root[1]-root[0]
    budget = defaultdict(float)
    for cat, values in categories.items():
        budget[group(cat)] += values['wall_ns']/1e9
    bins = [defaultdict(int) for _ in range(int(total/100_000_000)+1)]
    for s, e, _, _, cat, *_ in timeline:
        s, e = s-root[0], e-root[0]
        while s < e:
            index = s//100_000_000
            stop = min(e, (index+1)*100_000_000)
            bins[index][group(cat)] += stop-s
            s = stop
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 570">',
             '<rect width="1200" height="570" fill="white"/>',
             '<g font-family="sans-serif" font-size="14" fill="#1f2937">',
             '<text x="30" y="30">离线索引：互斥预算与真实时序（秒）</text>']

    def rect(s, e, y, color, title):
        x, width = 145+1000*s/(total/1e9), 1000*(e-s)/(total/1e9)
        parts.append(f'<rect x="{x:.3f}" y="{y}" width="{width:.3f}" height="24" '
                     f'fill="{color}"><title>{html.escape(title)}</title></rect>')

    cursor = 0
    for name, value in budget.items():
        rect(cursor, cursor+value, 70, palette[name], f'{name}: {value:.6f}s')
        cursor += value
    for index, values in enumerate(bins):
        if values:
            name = max(values, key=values.get)
            rect(index/10, min((index+1)/10, total/1e9), 125, palette[name], name)
    parts.extend(['<text x="20" y="88">互斥耗时预算</text>',
                  '<text x="20" y="143">索引线程时序</text>'])
    for index, (tid, windows) in enumerate(workers.items()):
        y = 185+index*50
        parts.append(f'<text x="20" y="{y+18}">向量线程 {tid}</text>')
        for s, e, _, _, _, _, files, chunks, *_ in windows:
            rect((s-root[0])/1e9, (e-root[0])/1e9, y, '#38bdf8',
                 f'{files} file visits / {chunks} chunks; {(e-s)/1e9:.6f}s')
    for second in range(0, int(total/1e9)+1, 5):
        x = 145+1000*second/(total/1e9)
        parts.append(f'<text x="{x:.2f}" y="55">{second}</text>')
    for index, (name, value) in enumerate(budget.items()):
        x, y = 25+(index%3)*380, 335+(index//3)*38
        parts.append(f'<rect x="{x}" y="{y-14}" width="16" height="16" '
                     f'fill="{palette[name]}"/>')
        parts.append(f'<text x="{x+24}" y="{y}">{name}：{value:.3f}s</text>')
    parts.extend([
        f'<text x="25" y="465">合计 {total/1e9:.6f}s；向量线程与主线程重叠，不重复相加。</text>',
        '<text x="25" y="495">时序按100ms窗口着色；精确起止与线程CPU见waterfall.csv。</text>',
        '<text x="25" y="525">这是带细分计时的样本；探针开销未扣除，不等同于无探针性能。</text>',
        '</g></svg>',
    ])
    path.write_text('\n'.join(parts))


def main(out):
    directory = out / 'trace'
    metadata = json.loads((directory/'metadata.json').read_text())
    names = [x['name'] for x in metadata['labels']]
    root = None
    py_paths = sorted(directory.glob('python-*.bin'))
    for path in py_paths:
        for _, _, label, tid, fid, files, chunks, rows, s, e, c0, c1 in records(path, PY):
            if names[label] == 'index.job':
                if root is not None:
                    raise ValueError('Expected exactly one indexing job')
                root = (s, e, c0, c1, names[label], fid, files, chunks, rows, tid, 0)
    if root is None:
        raise ValueError('No completed indexing job')
    start, end, *_, main_tid, _ = root
    all_threads = defaultdict(list)
    sites = defaultdict(set)
    for item in metadata['labels']:
        if item['site']:
            sites[item['name']].add(item['site'])
    for path in py_paths:
        for _, _, label, tid, fid, files, chunks, rows, s, e, c0, c1 in records(path, PY):
            if s >= start and e <= end:
                all_threads[tid].append((s, e, c0, c1, names[label], fid, files,
                                         chunks, rows, tid, 0))
    for s, e, c0, c1, tid, op, role, _, size, result in records(directory/'native.bin', NATIVE):
        if s >= start and e <= end:
            name = f'native.{ROLES.get(role, role)}.{OPS[op]}'
            all_threads[tid].append((s, e, c0, c1, name, 0, 0, 0,
                                     int(op == 10 and result == 100), tid, max(0, size)))

    timeline = []
    main_result = summarize(all_threads.pop(main_tid), names, segments=timeline)
    total_wall = sum(r['wall_ns'] for r in main_result['categories'].values())
    total_cpu = sum(r['cpu_ns'] for r in main_result['categories'].values())
    assert total_wall == root[1]-root[0], (total_wall, root[1]-root[0])
    assert total_cpu == root[3]-root[2], (total_cpu, root[3]-root[2])
    assert not main_result['violations'], main_result['violations'][:3]
    assert sum(e-s for s, e, *_ in timeline) == total_wall
    timeline.sort()
    for previous, current in zip(timeline, timeline[1:], strict=False):
        assert previous[1] == current[0], (previous, current)

    # Each row is an actual disjoint interval, unlike first/last envelopes in
    # the grouped summary. The full trace contains the concurrent worker lanes.
    with (directory/'waterfall.csv').open('w') as f:
        writer = csv.writer(f)
        writer.writerow(['start_s', 'end_s', 'wall_s', 'thread_cpu_s', 'category',
                         'event', 'file_id'])
        for s, e, c0, c1, cat, name, fid in timeline:
            writer.writerow([(s-start)/1e9, (e-start)/1e9, (e-s)/1e9,
                             (c1-c0)/1e9, cat, name, fid])
    workers = {
        tid: [e for e in events if e[4] == 'vector._process_window']
        for tid, events in all_threads.items()
        if any(e[4] == 'vector._process_window' for e in events)
    }
    draw_waterfall(directory/'waterfall.svg', root, timeline,
                   main_result['categories'], workers)
    summaries = {}
    for tid, events in all_threads.items():
        summary = summarize(events, names)
        summaries[str(tid)] = dict(
            by_name=seconds(summary['by_name'], start),
            categories=seconds(summary['categories'], start),
            violations=summary['violations'],
        )
    output = dict(
        schema=1, root=dict(start_ns=start, end_ns=end, wall_s=total_wall/1e9,
                            thread_cpu_s=total_cpu/1e9, tid=main_tid,
                            files=root[6], chunks=root[7]),
        categories=seconds(main_result['categories'], start),
        by_name=seconds(main_result['by_name'], start),
        scopes={name: seconds(rows, start) for name, rows in main_result['scopes'].items()},
        other_threads=summaries,
        sites={key: sorted(value) for key, value in sites.items()},
        reconciliation=dict(wall_error_ns=0, cpu_error_ns=0, gaps_ns=0),
        caveats=['Exclusive wall/CPU categories are additive on the index thread only.',
                 'Grouped start/end are envelopes; see waterfall.csv for each disjoint interval.',
                 'Python SQL boundary includes adapters, GIL/scheduling waits and tracing.',
                 'Native syscall records include IO only for index.db, WAL and journal.',
                 'Native ROW counts and IO counts are not logical DML rows.',
                 'Thread CPU excludes native worker threads used internally by LanceDB.',
                 'Observer cost stays in caller self-time; compare uninstrumented replay.'],
    )
    (directory/'summary.json').write_text(json.dumps(output, indent=2))
    print(json.dumps({'root': output['root'], 'categories': output['categories'],
                      'reconciliation': output['reconciliation']}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('out', type=Path)
    main(parser.parse_args().out)
