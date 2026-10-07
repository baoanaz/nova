#!/usr/bin/env python3
"""Voyage burst/paced transport probe. Public corpus only; no vector JSON decoding.

Rates use successful HTTP response bytes and provider usage.total_tokens. A paced
run reuses the calibrated batches for >=60 s. It does not infer an account quota
from the configured client limit. Credentials and response bodies are never saved.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import resource
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))


def cpu_s():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def union_s(intervals):
    end = -float("inf")
    total = 0.0
    for start, stop in sorted(intervals):
        total += max(0, stop - max(start, end))
        end = max(end, stop)
    return total


def run(texts, batch_size, concurrency, *, calibrated=None, seconds=0, tpm=16_000_000):
    batches = [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]
    stop = threading.Event()
    lock = threading.Lock()
    bins = defaultdict(int)
    rows = []
    peak_rss = 0
    samples = []
    started = time.perf_counter()
    cpu0 = cpu_s()

    def sample():
        nonlocal peak_rss
        while not stop.wait(0.1):
            rss = int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
            peak_rss = max(peak_rss, rss)
            samples.append([round(time.perf_counter() - started, 3), round(cpu_s() - cpu0, 3), rss])

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    with httpx.Client(
        timeout=httpx.Timeout(90, connect=10), limits=limits, trust_env=False
    ) as client:
        def request(index):
            if stop.is_set():
                return
            batch = batches[index]
            t0 = time.perf_counter()
            tail = b''
            size = 0
            local_bins = defaultdict(int)
            try:
                with client.stream('POST', 'https://api.voyageai.com/v1/embeddings',
                    headers={'Authorization': 'Bearer ' + os.environ['EMBED_API_KEY']},
                    json={'model': 'voyage-4-lite', 'input': batch, 'input_type': 'document',
                          'output_dimension': 1024}) as response:
                    headers_at = time.perf_counter()
                    status = response.status_code
                    first_byte = None
                    for part in response.iter_bytes():
                        now = time.perf_counter()
                        if first_byte is None:
                            first_byte = now
                        size += len(part)
                        tail = (tail + part)[-512:]
                        local_bins[int(now - started)] += len(part)
                    ended = time.perf_counter()
                    match = re.search(rb'"total_tokens"\s*:\s*(\d+)', tail)
                    tokens = int(match[1]) if match else None
                    safe_headers = {k: v for k, v in response.headers.items()
                                    if (k.startswith('x-ratelimit-') or k == 'retry-after')
                                    and re.fullmatch(r'[0-9.,: TZ+\-a-z]+', v)}
                row = dict(batch=index, items=len(batch), status=status, tokens=tokens,
                           bytes=size, wire_body_bytes=response.num_bytes_downloaded,
                           content_encoding=response.headers.get('content-encoding', 'identity'),
                           start_s=t0-started, end_s=ended-started,
                           headers_s=headers_at-t0,
                           first_byte_s=(first_byte-t0 if first_byte else None),
                           body_s=ended-headers_at, rate_headers=safe_headers)
                if status == 200 and tokens is None:
                    row['accounting_error'] = 'usage_missing_cannot_safely_pace'
                if status != 200 or tokens is None:
                    stop.set()
                with lock:
                    if status == 200 and tokens is not None:
                        for second, value in local_bins.items():
                            bins[second] += value
                    rows.append(row)
            except Exception as exc:
                with lock:
                    rows.append(dict(batch=index, status=0, error_type=type(exc).__name__,
                                     start_s=t0-started, end_s=time.perf_counter()-started))
                stop.set()

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            if calibrated is None:
                list(pool.map(request, range(len(batches))))
            else:
                costs = {row['batch']: row['tokens'] for row in calibrated['rows']}
                planned = 0
                inflight = deque()
                recent = deque()
                recent_tokens = 0
                index = 0
                while not stop.is_set():
                    batch_index = index % len(batches)
                    cost = costs[batch_index]
                    if planned + cost > tpm * seconds / 60:
                        break
                    scheduled = started + (planned + cost) / (tpm / 60)
                    while time.perf_counter() < scheduled and not stop.is_set():
                        stop.wait(min(0.1, scheduled - time.perf_counter()))
                    if stop.is_set():
                        break
                    while recent and recent[0][0] <= time.perf_counter() - 60:
                        _, old_cost = recent.popleft()
                        recent_tokens -= old_cost
                    if recent_tokens + cost > tpm:
                        stop.wait(max(0, recent[0][0] + 60 - time.perf_counter()))
                        continue
                    if len(inflight) >= concurrency:
                        inflight.popleft().result()
                    planned += cost
                    recent.append((time.perf_counter(), cost))
                    recent_tokens += cost
                    inflight.append(pool.submit(request, batch_index))
                    index += 1
                for future in inflight:
                    future.result()
    failed = stop.is_set()
    stop.set()
    sampler.join()
    wall = time.perf_counter() - started
    cpu = cpu_s() - cpu0
    ok = [r for r in rows if r.get('status') == 200 and r.get('tokens') is not None]
    tokens = sum(r['tokens'] for r in ok)
    size = sum(r['bytes'] for r in ok)
    wire_size = sum(r['wire_body_bytes'] for r in ok)
    peak_issued_tokens_60s = max((
        sum(r['tokens'] for r in ok if first['start_s'] <= r['start_s'] < first['start_s'] + 60)
        for first in ok
    ), default=0)
    return dict(batch_size=batch_size, concurrency=concurrency,
                mode='paced' if calibrated else 'burst',
                target_tpm=tpm if calibrated else None, requested_seconds=seconds,
                requests=len(rows), status_counts=dict(Counter(str(r['status']) for r in rows)),
                failed=failed, wall_s=wall, cpu_s=cpu, cpu_cores=cpu/wall,
                peak_rss_mib=peak_rss/1048576, tokens=tokens, response_bytes=size,
                achieved_tpm=tokens/wall*60, mbps=size*8/wall/1e6,
                wire_body_bytes=wire_size, wire_body_mbps=wire_size*8/wall/1e6,
                peak_issued_tokens_60s=peak_issued_tokens_60s,
                bytes_per_token=size/tokens if tokens else None,
                network_busy_s=union_s([(r['start_s'], r['end_s']) for r in ok]),
                header_wait_median_s=statistics.median(r['headers_s'] for r in ok) if ok else None,
                body_median_s=statistics.median(r['body_s'] for r in ok) if ok else None,
                response_bytes_per_second=dict(sorted(bins.items())), samples=samples, rows=rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--paced-seconds', type=float, default=65)
    args = parser.parse_args()
    if args.paced_seconds < 60:
        parser.error('paced measurement requires at least 60 seconds')
    if args.out.exists():
        raise SystemExit('Output exists; choose a new file.')
    if not os.environ.get('EMBED_API_KEY'):
        raise SystemExit('Missing external EMBED_API_KEY.')
    from profile_repo import build_tokenizer
    from throughput_probe import chunk_texts_for
    texts = chunk_texts_for(args.repo, build_tokenizer('BAAI/bge-m3'))
    random.Random(20261007).shuffle(texts)
    texts = [t.encode('utf-8')[:32000].decode('utf-8', errors='ignore') for t in texts[:16000]]
    output = dict(schema=1, model='voyage-4-lite', dim=1024, corpus='public LangChain e75dae1f',
                  sample_count=len(texts),
                  sample_note='Fixed shuffled chunk_texts_for sample, not full-ingest input set',
                  proxy=False, encoding='JSON float', decode_vectors=False, results=[])
    args.out.parent.mkdir(parents=True, exist_ok=True)

    def save(result):
        output['results'].append(result)
        args.out.write_text(json.dumps(output, indent=2)+'\n')
        print(json.dumps({k:result[k] for k in ('mode','batch_size','concurrency','wall_s','mbps',
                         'tokens','achieved_tpm','cpu_cores','peak_rss_mib','status_counts',
                         'failed')}), flush=True)

    baseline = None
    window = []
    for batch_size, concurrency in ((500,4), (500,8), (500,16), (500,32), (1000,16)):
        estimate = (baseline['tokens'] * 1.05) if baseline else 0
        while window and sum(n for _,n in window)+estimate > 15_500_000:
            delay = max(0, window[0][0]+61-time.monotonic())
            if delay:
                print('Waiting for token budget window.', flush=True)
                time.sleep(min(delay, 60))
            window = [(t,n) for t,n in window if t > time.monotonic()-61]
        result = run(texts, batch_size, concurrency)
        save(result)
        window.append((time.monotonic(), result['tokens']))
        if result['failed']:
            return 1
        if baseline is None:
            baseline = result
    print('Cooling down the previous requests before the sustained 16M TPM run.', flush=True)
    for _ in range(61):
        time.sleep(1)
    result = run(texts, 500, 16, calibrated=baseline, seconds=args.paced_seconds)
    save(result)
    return int(result['failed'])


if __name__ == '__main__':
    raise SystemExit(main())
