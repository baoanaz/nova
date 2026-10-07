"""Benchmark-only nested tracing; production modules are patched in memory.

Events contain monotonic start/end, thread CPU start/end, native thread id,
parent id, source site and files/chunks/rows. Zero counts mean not applicable;
SQL SELECT row counts are recorded on fetch. Files are also identified by a
stable corpus-local id so aggregators need not add repeated file visits.
No SQL parameters, source text or vectors are logged. Python records are
fixed-width binary to avoid JSON/object retention on the 2 GiB test machine.
"""
from __future__ import annotations

import ast
import ctypes
import functools
import inspect
import json
import os
import sqlite3
import struct
import sys
import textwrap
import threading
import time

# id, parent, label, tid, file_id, files, chunks, rows, start, end, cpu_start, cpu_end
RECORD = struct.Struct('<IIIIIiiiQQQQ')
_REC = None


class Span:
    __slots__ = ('rec', 'label', 'files', 'chunks', 'rows', 'fid', 'state', 'id',
                 'parent', 'start', 'cpu', 'enabled')

    def __init__(self, rec, label, files=0, chunks=0, rows=0, fid=None):
        self.rec, self.label = rec, label
        self.files, self.chunks, self.rows, self.fid = files, chunks, rows, fid
        self.enabled = rec.enabled

    def __enter__(self):
        if not self.enabled:
            return self
        self.state = s = self.rec.state()
        self.id = s['next']
        s['next'] += 1
        self.parent = s['stack'][-1] if s['stack'] else 0
        if self.fid is None:
            self.fid = s['fid']
        if self.fid and not self.files:
            self.files = 1
        s['stack'].append(self.id)
        self.start, self.cpu = time.perf_counter_ns(), time.thread_time_ns()
        return self

    def __exit__(self, *exc):
        if not self.enabled:
            return
        cpu, end = time.thread_time_ns(), time.perf_counter_ns()
        self.state['stack'].pop()
        self.state['file'].write(RECORD.pack(
            self.id, self.parent, self.label, self.state['tid'], self.fid,
            self.files, self.chunks, self.rows, self.start, end, self.cpu, cpu,
        ))


class Recorder:
    def __init__(self, out):
        self.out = out
        out.mkdir(exist_ok=False)
        self.enabled = False
        self.local = threading.local()
        self.states = []
        self.labels = []
        self.file_ids = {}
        self.root_counts = {}
        self.sqlite_settings = []
        self.native = ctypes.CDLL(None)
        self.native.nova_trace_enable.argtypes = [ctypes.c_char_p]
        self.native.nova_trace_fd.restype = ctypes.c_int

    def label(self, name, fn=None, site=None):
        if site is None and fn is not None:
            raw = inspect.unwrap(fn)
            site = f'{inspect.getsourcefile(raw)}:{raw.__code__.co_firstlineno}'
        self.labels.append({'name': name, 'site': site})
        return len(self.labels) - 1

    def state(self):
        if not hasattr(self.local, 'state'):
            tid = threading.get_native_id()
            self.local.state = dict(
                tid=tid, next=1, fid=0, stack=[],
                file=(self.out / f'python-{tid}.bin').open('xb', buffering=1024*1024),
            )
            self.states.append(self.local.state)
        return self.local.state

    def span(self, label, **counts):
        return Span(self, label, **counts)

    def evaluate(self, label, call, rows=0, chunks=0):
        with self.span(label, rows=rows, chunks=chunks):
            return call()

    def start(self):
        self.native.nova_trace_enable(os.fsencode(self.out / 'native.bin'))
        if self.native.nova_trace_fd() < 0:
            raise RuntimeError('Native trace could not open its output')
        self.enabled = True

    def finish(self):
        self.enabled = False
        self.native.nova_trace_disable()
        for state in self.states:
            state['file'].flush()
        (self.out / 'metadata.json').write_text(json.dumps({
            'schema': 1, 'labels': self.labels, 'files': self.file_ids,
            'root_counts': self.root_counts, 'sqlite_settings': self.sqlite_settings,
            'record_format': RECORD.format,
            'clock': time.get_clock_info('perf_counter').implementation,
            'counts_note': 'event workload; file_id permits unique counts; rows are logical rows',
        }, indent=2))


def wrap(owner, name, label, counts=None, result_counts=None, file_arg=None):
    original = getattr(owner, name)
    descriptor = inspect.getattr_static(owner, name)
    classmethod_ = isinstance(descriptor, classmethod)
    if classmethod_:
        original = descriptor.__func__
    key = _REC.label(label, original)

    @functools.wraps(original)
    def timed(*args, **kwargs):
        if not _REC.enabled:
            return original(*args, **kwargs)
        values = counts(args, kwargs) if counts else {}
        state = _REC.state()
        old = state['fid']
        if file_arg:
            path = file_arg(args)
            state['fid'] = _REC.file_ids.setdefault(path, len(_REC.file_ids)+1)
        try:
            with _REC.span(key, **values) as event:
                result = original(*args, **kwargs)
                if label == 'indexer.run':
                    _REC.root_counts['chunks'] = result.chunks_new + result.chunks_reused
                if result_counts:
                    for field, value in result_counts(result).items():
                        setattr(event, field, value)
                return result
        finally:
            state['fid'] = old

    setattr(owner, name, classmethod(timed) if classmethod_ else timed)


# All SQLite cursor results remain real sqlite3.Cursor subclasses (including
# iteration, row_factory, lastrowid, transaction and exceptions).
_SQL_LABELS = {}


def sql_label(sql, role):
    key = (sql, role)
    if key not in _SQL_LABELS:
        words = sql.strip().split()
        verb = words[0].upper() if words else 'EMPTY'
        family = ('fts' if 'chunks_fts' in sql.lower() else
                  'commit' if verb in ('COMMIT', 'END') else
                  'checkpoint' if 'wal_checkpoint' in sql.lower() else
                  'transaction' if verb in ('BEGIN', 'SAVEPOINT', 'RELEASE', 'ROLLBACK') else
                  'dml' if verb in ('INSERT', 'UPDATE', 'DELETE', 'REPLACE') else
                  'select' if verb in ('SELECT', 'WITH') else 'other')
        # Do not create one label/cache entry per generated savepoint name.
        display = verb if family == 'transaction' else ' '.join(words)[:160]
        group = (role, family, display)
        if group not in _SQL_LABELS:
            frame = sys._getframe(1)
            while frame and frame.f_code.co_filename == __file__:
                frame = frame.f_back
            site = f'{frame.f_code.co_filename}:{frame.f_lineno}' if frame else None
            _SQL_LABELS[group] = _REC.label(f'sql.{role}.{family}.{display}', site=site)
        label = _SQL_LABELS[group]
        if family != 'transaction':
            _SQL_LABELS[key] = label
        return label
    return _SQL_LABELS[key]


class TraceCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        if not _REC.enabled:
            return super().execute(sql, parameters)
        self.trace_label = sql_label(sql, self.connection.trace_role)
        with _REC.span(self.trace_label) as event:
            result = super().execute(sql, parameters)
            event.rows = max(0, self.rowcount)
            event.chunks = event.rows if 'INTO chunks(' in sql or 'INTO chunks_fts(' in sql else 0
            return result

    def executemany(self, sql, parameters):
        if not _REC.enabled:
            return super().executemany(sql, parameters)
        self.trace_label = sql_label(sql, self.connection.trace_role)
        with _REC.span(self.trace_label) as event:
            result = super().executemany(sql, parameters)
            event.rows = max(0, self.rowcount)
            return result

    def fetchone(self):
        with _REC.span(self.connection.fetch_label) as event:
            row = super().fetchone()
            event.rows = int(row is not None)
            return row

    def fetchall(self):
        with _REC.span(self.connection.fetch_label) as event:
            rows = super().fetchall()
            event.rows = len(rows)
            return rows

    def fetchmany(self, size=None):
        with _REC.span(self.connection.fetch_label) as event:
            rows = super().fetchmany() if size is None else super().fetchmany(size)
            event.rows = len(rows)
            return rows

    def __next__(self):
        with _REC.span(self.connection.fetch_label) as event:
            row = super().__next__()
            event.rows = 1
            return row


class TraceConnection(sqlite3.Connection):
    def __init__(self, database, *args, **kwargs):
        super().__init__(database, *args, **kwargs)
        self.trace_role = 'project' if str(database).endswith('index.db') else 'fixture'
        self.fetch_label = _REC.label(f'sql.{self.trace_role}.fetch_and_materialize')
        self.close_label = _REC.label(f'sql.{self.trace_role}.close_checkpoint')
        if self.trace_role == 'project':
            _REC.sqlite_settings.append({
                key: super(TraceConnection, self).execute(f'PRAGMA {key}').fetchone()[0]
                for key in ('cache_size', 'page_size', 'wal_autocheckpoint')
            })

    def cursor(self, factory=None):
        return super().cursor(factory=factory or TraceCursor)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql):
        with _REC.span(sql_label('SCHEMA script', self.trace_role)):
            return super().executescript(sql)

    def close(self):
        with _REC.span(self.close_label):
            return super().close()

    def commit(self):
        with _REC.span(sql_label('COMMIT', self.trace_role)):
            return super().commit()

    def rollback(self):
        with _REC.span(sql_label('ROLLBACK', self.trace_role)):
            return super().rollback()


class TraceLock:
    def __init__(self, lock, label):
        self.lock, self.label = lock, label

    def acquire(self, *args, **kwargs):
        with _REC.span(self.label):
            return self.lock.acquire(*args, **kwargs)

    def release(self):
        return self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


def transform_objects(owner, name, *, sql_params=False, payload=False, tolist=False):
    """Instrument only expressions/blocks in the original function, in memory.

    No algorithm is copied or changed. Line numbers remain the original ones;
    compile/inspect failures abort before the benchmark, never silently degrade.
    """
    original = getattr(owner, name)
    raw = inspect.unwrap(original)
    lines, line = inspect.getsourcelines(raw)
    source = inspect.getsourcefile(raw)
    tree = ast.parse(textwrap.dedent(''.join(lines)))
    tree.body[0].decorator_list = []

    class Rewrite(ast.NodeTransformer):
        def visit_Call(self, node):
            self.generic_visit(node)
            if (sql_params and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ('execute', 'executemany') and len(node.args) > 1):
                key = _REC.label('python.sql_parameters', site=f'{source}:{line+node.lineno-1}')
                node.args[1] = self.evaluation(key, node.args[1])
            if tolist and isinstance(node.func, ast.Attribute) and node.func.attr == 'tolist':
                key = _REC.label('python.float32_to_list', site=f'{source}:{line+node.lineno-1}')
                return ast.copy_location(self.evaluation(key, node, chunks=1), node)
            return node

        def evaluation(self, key, node, chunks=0):
            return ast.Call(
                func=ast.Attribute(value=ast.Name(id='_PHASE_REC', ctx=ast.Load()),
                                   attr='evaluate', ctx=ast.Load()),
                args=[ast.Constant(key), ast.Lambda(args=ast.arguments(
                    posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                    body=node)],
                keywords=[ast.keyword(arg='chunks', value=ast.Constant(chunks))],
            )

        def visit_For(self, node):
            self.generic_visit(node)
            if payload and isinstance(node.target, (ast.Name, ast.Tuple)):
                # upsert: for row in rows; cache.put: for digest, vector in vectors.items().
                is_payload = (isinstance(node.iter, ast.Name) and node.iter.id == 'rows') or (
                    isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Attribute)
                    and isinstance(node.iter.func.value, ast.Name)
                    and node.iter.func.value.id == 'vectors'
                )
                if is_payload:
                    key = _REC.label('python.vector_payload', site=f'{source}:{line+node.lineno-1}')
                    ctx = ast.Call(func=ast.Attribute(
                        value=ast.Name(id='_PHASE_REC', ctx=ast.Load()),
                        attr='span', ctx=ast.Load()), args=[ast.Constant(key)], keywords=[])
                    return ast.copy_location(ast.With(
                        items=[ast.withitem(context_expr=ctx)], body=[node]), node)
            return node

    tree = ast.fix_missing_locations(Rewrite().visit(tree))
    ast.increment_lineno(tree, line-1)
    namespace = dict(raw.__globals__, _PHASE_REC=_REC)
    exec(compile(tree, source, 'exec'), namespace)
    replacement = namespace[name]
    # Keep the older aggregate meter, if present, around the transformed function.
    if original is not raw:
        from coldstart_probe import meter
        old_keys = {'upsert': 'lancedb.upsert', 'apply_file_change': 'sqlite.apply_file_change'}
        setattr(owner, name, replacement)
        if name not in old_keys:
            raise RuntimeError(f'Unknown existing timing wrapper: {name}')
        meter.wrap_class(owner, name, old_keys[name])
    else:
        setattr(owner, name, replacement)


def install(out):
    global _REC
    _REC = rec = Recorder(out / 'trace')
    import nova_core.engine as eng
    import nova_core.pipeline.embedding_sink as sink
    import nova_core.pipeline.indexer as idx
    import nova_core.storage.store as st
    import nova_core.text.segmenter as seg
    import nova_core.vectors.cache as cache
    import nova_core.vectors.store as vs
    import nova_service.indexer as job
    from replay import ReplayEmbedding

    transform_objects(st.Store, 'apply_file_change', sql_params=True)
    transform_objects(vs.VectorStore, 'upsert', payload=True)
    transform_objects(cache.EmbeddingCache, 'put', payload=True)
    transform_objects(ReplayEmbedding, 'embed', tolist=True)

    original_connect = sqlite3.connect

    def connect(*args, **kwargs):
        if 'factory' in kwargs or len(args) >= 6:
            raise RuntimeError('Detailed benchmark requires its tracing SQLite factory')
        return original_connect(*args, factory=TraceConnection, **kwargs)
    sqlite3.connect = connect

    wrap(st, 'segment', 'tokenizer', counts=lambda a, k: dict(chunks=1))
    for method, label in (('_ascii_segment', 'tokenizer.ascii'),
                          ('_jieba_segment', 'tokenizer.jieba'),
                          ('_load_jieba', 'tokenizer.load')):
        wrap(seg, method, label)
    wrap(st, '_insert_fts_row', 'fts.prepare_and_write', counts=lambda a, k: dict(chunks=1))
    wrap(st.Store, 'apply_file_change', 'store.file',
         counts=lambda a, k: dict(files=1, chunks=len(a[2])))
    for method in ('_apply_spec_ref_cascade', '_insert_unresolved_rows',
                   'counts', 'set_config', 'close'):
        wrap(st.Store, method, f'store.{method}')
    for name in ('_symbol_from_row', '_edge_from_row', '_chunk_from_row'):
        wrap(st, name, 'python.row_object', counts=lambda a, k: dict(rows=1))
    wrap(idx.Indexer, '_run', 'indexer.run',
         result_counts=lambda r: dict(files=r.files_parsed, chunks=r.chunks_new+r.chunks_reused))
    wrap(idx.Indexer, '_index_file', 'indexer.file', counts=lambda a, k: dict(files=1),
         file_arg=lambda a: a[1].path,
         result_counts=lambda r: dict(chunks=len(r.chunks) if r else 0))
    wrap(idx.Indexer, '_parse', 'parse', counts=lambda a, k: dict(files=1))
    wrap(idx, 'split_file', 'chunk.split', result_counts=lambda r: dict(chunks=len(r)))
    for method in ('_collect_inputs', '_resolve', '_rebuild_vectors', 'fingerprint', '__init__'):
        wrap(idx.Indexer, method, f'indexer.{method}')
    for method in ('_decode', 'file_content_hash', 'is_generated'):
        wrap(idx, method, f'python.{method}')
    wrap(idx._Accumulator, 'report', 'indexer.report')
    for method in ('_ingest', '_embedding_cache', '_mark_index_state'):
        wrap(eng.Engine, method, f'engine.{method}')
    for method in ('open', 'close', 'lookup', 'put', 'count'):
        wrap(cache.EmbeddingCache, method, f'vector.cache.{method}')
    for method in ('open', 'close', 'upsert', '_merge_batch',
                   'get_hashes', 'get_vectors_by_hash', 'count'):
        wrap(vs.VectorStore, method, f'vector.{method}')
    for method in ('_process_window', '_embed_unique'):
        wrap(sink.EmbeddingSink, method, f'vector.{method}',
             counts=lambda a, k: dict(chunks=len(a[1]), files=len({c.file_path for c in a[1]})))
    wrap(ReplayEmbedding, 'embed', 'vector.replay', counts=lambda a, k: dict(chunks=len(a[1])))
    wrap(sink.EmbeddingPipeline, 'submit', 'queue.submit',
         counts=lambda a, k: dict(chunks=len(a[1])))
    for method in ('flush_pending', 'close', '_consume', '__init__'):
        wrap(sink.EmbeddingPipeline, method, f'queue.{method}')
    wrap(sink.Queue, 'put', 'queue.enqueue',
         counts=lambda a, k: dict(chunks=len(a[1]) if isinstance(a[1], list) else 0))
    wrap(sink.Queue, 'get', 'queue.consumer_wait')
    wrap(threading.Thread, 'join', 'queue.drain_join')

    lock_label = rec.label('lock.vector.wait', vs.VectorStore.__init__)
    original_init = vs.VectorStore.__init__

    def vector_init(self, *a, **k):
        original_init(self, *a, **k)
        self._lock = TraceLock(self._lock, lock_label)
    vs.VectorStore.__init__ = vector_init

    job_init = job.ProjectIndexer.__init__
    lock_job = rec.label('lock.project.wait', job.ProjectIndexer._run)
    lock_guard = rec.label('lock.progress.wait', job.ProjectIndexer._run)

    def traced_job_init(self, *a, **k):
        job_init(self, *a, **k)
        self._lock = TraceLock(self._lock, lock_job)
        self._guard = TraceLock(self._guard, lock_guard)
    job.ProjectIndexer.__init__ = traced_job_init
    for method in ('_count_files', '_finish', '_notify_finish'):
        wrap(job.ProjectIndexer, method, f'job.{method}')
    root_key = rec.label('index.job', job.ProjectIndexer._run)
    original_job = job.ProjectIndexer._run

    def root(self):
        rec.start()
        try:
            with rec.span(root_key) as event:
                original_job(self)
                event.files = self._progress.processed_files
                event.chunks = rec.root_counts.get('chunks', 0)
                rec.root_counts.update(files=event.files, state=self._progress.state,
                                       error=self._progress.error)
        finally:
            rec.finish()
    job.ProjectIndexer._run = root
    return rec
