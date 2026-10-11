#!/usr/bin/env python3
"""Offline cold-start comparison through either core or the real Rust MCP client.

A new output directory is mandatory. The server uses only ReplayEmbedding;
external Python socket connections are rejected. Run under a systemd unit with
IPAddressDeny=any and IPAddressAllow=localhost to also contain native/client IO.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import resource
import select
import subprocess
import sys
import time
from pathlib import Path

from coldstart_probe import install_patches, meter
from nova_core.engine import Engine
from replay import ReplayEmbedding


def deny_external(event, args):
    if event == 'socket.connect':
        address = args[1]
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise RuntimeError('Offline benchmark forbids external network connections')


def _live_children_cpu_s():
    """CPU of live descendant processes (e.g. parse workers): RUSAGE_* misses unreaped ones."""
    tick = os.sysconf('SC_CLK_TCK')
    pending, total = [os.getpid()], 0.0
    while pending:
        pid = pending.pop()
        try:
            for task in os.listdir(f'/proc/{pid}/task'):
                with open(f'/proc/{pid}/task/{task}/children') as handle:
                    pending.extend(int(child) for child in handle.read().split())
            if pid != os.getpid():
                with open(f'/proc/{pid}/stat') as handle:
                    fields = handle.read().rsplit(')', 1)[1].split()
                total += (int(fields[11]) + int(fields[12])) / tick
        except OSError:
            continue
    return total


def snapshot():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {'cpu_s': usage.ru_utime + usage.ru_stime,
            'children_cpu_s': _live_children_cpu_s(),
            'peak_rss_mib': usage.ru_maxrss / 1024,
            'phases_s': dict(meter.phases)}


def serve(args, provider):
    import uvicorn
    from nova_service.app import create_app
    from nova_service.config import Settings
    from nova_service.runtime import EngineManager

    try:  # Same startup preload as `nova-service serve`; absent on older source trees.
        from nova_core.preload import preload_runtime
    except ImportError:
        preload_runtime = None
    disabled = os.environ.get('NOVA_PRELOAD', '').strip().lower() in ('0', 'false', 'no', 'off')
    preload = preload_runtime() if preload_runtime is not None and not disabled else None
    app = create_app(Settings(data_root=args.out / 'index', local_mode=True))
    manager = EngineManager(args.out / 'index', Engine.open(args.out / 'index', provider=provider))
    app.state.engine_manager = manager
    prefetcher = getattr(manager.engine, 'prefetcher', None)
    if prefetcher is not None:  # Same as a long-running service: start the worker before timing.
        prefetcher.warm()
    reports = []
    original = manager.ingest

    def ingest(*a, **kw):
        t0 = time.perf_counter()
        report = original(*a, **kw)
        end = time.perf_counter()
        reports.append({'wall_s': end-t0, 'start_s': t0, 'end_s': end,
                        'report': dataclasses.asdict(report)})
        return report

    manager.ingest = ingest

    @app.get('/bench/metrics')
    def metrics():
        return {**snapshot(), 'ingests': reports, 'preload_s': preload,
                'engine_source': sys.modules[Engine.__module__].__file__,
                'replay_delays_s': {'batch_wave': provider.batch_delay_s,
                                    'query': provider.query_delay_s}}

    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


def run_client(args, provider):
    import httpx

    root = Path(__file__).resolve().parents[2]
    server_command = [
        sys.executable, __file__, '--kind', 'serve', '--out', str(args.out),
        '--fixture', str(args.fixture), '--repo', str(args.repo), '--port', str(args.port),
        '--replay-batch-delay-s', str(args.replay_batch_delay_s),
        '--replay-query-delay-s', str(args.replay_query_delay_s),
    ]
    if args.phase_timings:
        server_command.append('--phase-timings')
    client = None
    server_started = time.perf_counter()
    with (args.out / 'service.log').open('w') as log:
        process = subprocess.Popen(server_command, stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{args.port}', trust_env=False) as http:
                for _ in range(200):
                    if process.poll() is not None:
                        raise RuntimeError('Benchmark server exited; see service.log')
                    try:
                        http.get('/bench/metrics').raise_for_status()
                        break
                    except httpx.ConnectError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('Benchmark server did not become ready')
                startup_s = time.perf_counter() - server_started
                initialized_at = time.perf_counter()
                with (args.out / 'client.log').open('wb') as client_log:
                    client = subprocess.Popen(
                        [str(args.client or root / 'client/target/debug/nova-client'),
                         '--base-url', f'http://127.0.0.1:{args.port}',
                         '--cache-root', str(args.out / 'client-cache')],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=client_log,
                        bufsize=0,
                        env={k: v for k, v in os.environ.items()
                             if k not in ('NOVA_API_TOKEN', 'NOVA_BASE_URL')},
                    )
                    buffer = b''

                    def send(frame):
                        pending = memoryview((json.dumps(frame)+'\n').encode())
                        while pending:
                            written = client.stdin.write(pending)
                            if not written:
                                raise RuntimeError('MCP stdin closed')
                            pending = pending[written:]

                    def receive(expected_id):
                        nonlocal buffer
                        deadline = time.perf_counter() + 600
                        while True:
                            while b'\n' not in buffer:
                                remaining = deadline - time.perf_counter()
                                if remaining <= 0 or not select.select(
                                    [client.stdout], [], [], remaining
                                )[0]:
                                    raise TimeoutError('MCP response timeout')
                                data = os.read(client.stdout.fileno(), 65536)
                                if not data:
                                    raise RuntimeError('MCP stdout closed')
                                buffer += data
                            raw, buffer = buffer.split(b'\n', 1)
                            response = json.loads(raw)
                            if response.get('jsonrpc') != '2.0':
                                raise RuntimeError('Invalid JSON-RPC response')
                            if 'id' not in response:
                                continue  # Notifications are not a Tool result.
                            if response['id'] != expected_id:
                                raise RuntimeError('MCP response ID mismatch')
                            if 'result' not in response and 'error' not in response:
                                raise RuntimeError('Missing MCP result/error')
                            return response, raw, time.perf_counter()

                    send({'jsonrpc': '2.0', 'id': 10, 'method': 'initialize', 'params': {
                        'protocolVersion': '2025-11-25', 'capabilities': {},
                        'clientInfo': {'name': 'nova-offline-probe', 'version': '1'},
                    }})
                    initialized, _, _ = receive(10)
                    if 'error' in initialized:
                        raise RuntimeError('MCP initialization failed')
                    send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
                    send({'jsonrpc': '2.0', 'id': 11, 'method': 'tools/list'})
                    listed, _, _ = receive(11)
                    if not any(t['name'] == 'search_context'
                               for t in listed.get('result', {}).get('tools', [])):
                        raise RuntimeError('search_context is not advertised')
                    init_s = time.perf_counter() - initialized_at
                    before = http.get('/bench/metrics').raise_for_status().json()
                    frame = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                             'params': {'name': 'search_context', 'arguments': {
                                 'query': provider.metadata['probe_query'],
                                 'project_root': str(args.repo),
                             }}}
                    t0 = time.perf_counter()
                    send(frame)
                    response, raw, finished = receive(1)
                    wall = finished - t0
                    (args.out / 'client.jsonl').write_bytes(raw+b'\n')
                    after = http.get('/bench/metrics').raise_for_status().json()
                    # Exit and metric collection are strictly outside Tool timing.
                    client.stdin.close()
                    exit_code = client.wait(timeout=15)
                payload = {
                    'wall_s': wall, 'tool_started_s': t0, 'tool_finished_s': finished,
                    'server_startup_s': startup_s, 'mcp_initialization_s': init_s,
                    'server_cpu_s': after['cpu_s']-before['cpu_s'],
                    'server_children_cpu_s': after.get('children_cpu_s', 0.0)
                    - before.get('children_cpu_s', 0.0),
                    'server': after, 'client_exit': exit_code,
                    'client_error': response.get('error')
                    or response.get('result', {}).get('isError', False),
                }
                (args.out / 'result.json').write_text(json.dumps(payload, default=str, indent=2))
                if exit_code or payload['client_error']:
                    raise RuntimeError('Client benchmark failed; see client.jsonl and service.log')
                print(json.dumps({'wall_s': wall, 'server_cpu_s': payload['server_cpu_s'],
                                  'server_children_cpu_s': payload['server_children_cpu_s'],
                                  'ingests': len(after['ingests']),
                                  'peak_rss_mib': after['peak_rss_mib']}))
        finally:
            if client is not None and client.poll() is None:
                client.kill()
                client.wait()
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=('core', 'client', 'serve'), required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18973)
    parser.add_argument('--client', type=Path, help='Explicit frozen debug/release binary')
    parser.add_argument('--phase-timings', action='store_true',
                        help='Opt-in aggregate instrumentation; disabled for acceptance runs')
    parser.add_argument('--replay-batch-delay-s', type=float, default=0.0)
    parser.add_argument('--replay-query-delay-s', type=float, default=0.0)
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.repo = args.repo.resolve()
    args.fixture = args.fixture.resolve()
    if args.kind != 'serve':
        args.out.mkdir(parents=True, exist_ok=False)
    sys.addaudithook(deny_external)
    provider = ReplayEmbedding(args.fixture, batch_delay_s=args.replay_batch_delay_s,
                               query_delay_s=args.replay_query_delay_s)
    if args.phase_timings:
        install_patches(provider, meter)
    if args.kind == 'serve':
        serve(args, provider)
    elif args.kind == 'client':
        run_client(args, provider)
    else:
        before = snapshot()
        t0 = time.perf_counter()
        engine = Engine.open(args.out / 'index', provider=provider)
        handle, _ = engine.resolve_repo(args.repo)
        report = engine.ingest_repo(handle.project_id, args.repo, full=True)
        after = snapshot()
        payload = {**after, 'wall_s': time.perf_counter()-t0,
                   'cpu_s': after['cpu_s']-before['cpu_s'], 'report': dataclasses.asdict(report)}
        (args.out / 'result.json').write_text(json.dumps(payload, default=str, indent=2))
        if report.errors:
            raise RuntimeError(f'Index benchmark failed: {report.errors[:3]}')
        print(json.dumps({k: v for k, v in payload.items() if k not in ('phases_s', 'report')}))


if __name__ == '__main__':
    main()
