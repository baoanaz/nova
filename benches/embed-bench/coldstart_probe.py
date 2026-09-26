#!/usr/bin/env python
"""冷启动索引耗时分解探针（研究工具；不改仓库代码，全部用运行时打桩）。

来源：`/root/.zace/bench/probe-2026-09-25/coldstart_probe.py`（2026-09-25 调研版），
TASK-114 收进仓库并适配新结构（`_embed_new` → `EmbeddingSink._process_window` /
`EmbeddingPipeline.submit`），使性能证据可复现。用法与口径见
`docs/plan/index-perf-plan.md` §5。


输出：每个阶段累计墙钟 / 计数 / HTTP 请求明细（TTFB、下载、响应体字节）/ 内存与 CPU 采样。
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import resource
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve()
ZACE = Path(os.environ.get("ZACE_REPO", str(Path(__file__).resolve().parents[2])))
sys.path.insert(0, str(ZACE / "core"))

import httpx  # noqa: E402
import zace_core.embedding.api as api_mod  # noqa: E402
import zace_core.pipeline.indexer as indexer_mod  # noqa: E402
from zace_core.embedding.factory import EmbeddingConfig, create_provider  # noqa: E402
from zace_core.engine import Engine  # noqa: E402
from zace_core.storage import Store  # noqa: E402
from zace_core.vectors import VectorStore  # noqa: E402


class Meter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.phases: dict[str, float] = defaultdict(float)
        self.counts: dict[str, int] = defaultdict(int)

    def add(self, key: str, dt: float, n: int = 1) -> None:
        with self.lock:
            self.phases[key] += dt
            self.counts[key] += n

    def record(self, key: str, value: float) -> None:
        with self.lock:
            self.raw.setdefault(key, []).append(value)

    raw: dict[str, list[float]] = {}  # type: ignore[assignment]

    def wrap_instance(self, obj, name: str, key: str, *, missing_ok: bool = False):
        try:
            orig = getattr(obj, name)
        except AttributeError:
            if missing_ok:
                return None
            raise

        @functools.wraps(orig)
        def timed(*a, **k):
            t0 = time.perf_counter()
            try:
                return orig(*a, **k)
            finally:
                self.add(key, time.perf_counter() - t0, 0)

        setattr(obj, name, timed)
        return orig

    def wrap_class(self, cls, name: str, key: str):
        orig = getattr(cls, name)

        @functools.wraps(orig)
        def timed(self_, *a, **k):
            t0 = time.perf_counter()
            try:
                return orig(self_, *a, **k)
            finally:
                meter.add(key, time.perf_counter() - t0, 0)

        # wrap_class 用在实例方法上，这里统一用闭包 meter
        setattr(cls, name, timed)
        return orig


meter = Meter()
meter.raw = defaultdict(list)


class TimingClient(httpx.Client):
    """记录每个请求的 request/headers(TTFB)/end 三个时间点与响应体量。"""

    def __init__(self, *a, **k):
        super().__init__(
            *a, event_hooks={"request": [self._on_req], "response": [self._on_resp]}, **k
        )
        self._starts: dict[int, float] = {}
        self._ttfb: dict[int, float] = {}
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.inflight: list[tuple[float, float]] = []

    def _on_req(self, request: httpx.Request) -> None:
        now = time.perf_counter()
        with self.lock:
            self._starts[id(request)] = now
            self.inflight.append((now, now))

    def _on_resp(self, response: httpx.Response) -> None:
        req = response.request
        now = time.perf_counter()
        with self.lock:
            self._ttfb[id(req)] = now
            self.inflight[-1] = (self.inflight[-1][0], now)

    def send(self, request: httpx.Request, **kwargs):
        t0 = time.perf_counter()
        response = super().send(request, **kwargs)
        t1 = time.perf_counter()
        with self.lock:
            start = self._starts.pop(id(request), t0)
            ttfb = self._ttfb.pop(id(request), t1)
            try:
                nbytes = len(response.content)
            except Exception:
                nbytes = 0
            try:  # 只扫尾部取 usage，避免对 251MB 响应做第二次全量 JSON 解析（测量污染）
                tail = bytes(response.content[-256:])
                match = re.search(rb'"total_tokens"\s*:\s*(\d+)', tail)
                tokens = int(match.group(1)) if match else 0
            except Exception:
                tokens = 0
            self.requests.append(
                {
                    "api_tokens": tokens,
                    "status": response.status_code,
                    "req_bytes": int(request.headers.get("content-length") or 0),
                    "resp_bytes": nbytes,
                    "start": start,
                    "ttfb_s": ttfb - start,
                    "download_s": t1 - ttfb,
                    "total_s": t1 - start,
                }
            )
            self.inflight[-1] = (self.inflight[-1][0], t1)
        return response


ZERO_VEC_CACHE: dict[int, list[float]] = {}


def make_offline_post(provider, meter: Meter):
    """零网络替身：只替换 `_post`（socket 那一段），其余本地路径全部保留。"""
    dim = provider.profile.dim

    def offline_post(payload):
        t0 = time.perf_counter()
        n = len(payload.get("input") or [])
        zero = ZERO_VEC_CACHE.setdefault(dim, [0.0] * dim)
        body = {"data": [{"index": i, "embedding": zero} for i in range(n)]}
        meter.add("embed.synth_reply_offline", time.perf_counter() - t0, 0)
        return body

    provider._post = offline_post


def sample_rss(stop: threading.Event, out: list) -> None:
    while not stop.wait(0.05):
        try:
            with open("/proc/self/statm") as fh:
                pages = int(fh.read().split()[1])
            out.append(pages * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024)
        except Exception:
            return


def install_patches(provider, meter: Meter) -> None:
    import zace_core.storage.store as store_mod

    for fn_name, key in (
        ("segment", "sqlite.jieba_segment"),
        ("_insert_fts_row", "sqlite.fts_insert"),
        ("_delete_fts_for_file", "sqlite.fts_delete"),
        ("_apply_spec_ref_cascade", "sqlite.spec_ref_cascade"),
        ("_insert_unresolved_rows", "sqlite.unresolved_rows"),
        ("_chunk_insert_sql", "sqlite.noop"),
    ):
        if hasattr(store_mod, fn_name):
            setattr(store_mod, fn_name, _wrap_fn(getattr(store_mod, fn_name), key))
    for meth, key in (
        ("_apply_spec_ref_cascade", "sqlite.spec_ref_cascade"),
        ("_insert_unresolved_rows", "sqlite.unresolved_rows"),
    ):
        meter.wrap_class(store_mod.Store, meth, key)

    for name, key in (
        ("_prepare", "embed.prepare_truncate"),
        ("_estimate_tokens", "embed.estimate_tokens"),
        ("_ensure_tokenizer", "embed.tokenizer_load"),
        ("_embed_batch", "embed.batch_thread_wall"),
        ("_decode", "embed.json_decode"),
        ("embed_side", "embed.embed_side_total"),
    ):
        meter.wrap_instance(provider, name, key, missing_ok=True)

    api_mod._as_float_list = _wrap_fn(api_mod._as_float_list, "embed.float_convert")
    api_mod.l2_normalize = _wrap_fn(api_mod.l2_normalize, "embed.l2_normalize")

    meter.wrap_class(VectorStore, "upsert", "lancedb.upsert")
    meter.wrap_class(Store, "apply_file_change", "sqlite.apply_file_change")
    meter.wrap_class(Store, "apply_deletions", "sqlite.apply_deletions")
    meter.wrap_class(Store, "set_config", "sqlite.set_config")

    indexer_mod.split_file = _wrap_fn(indexer_mod.split_file, "chunk.split_file")
    meter.wrap_class(indexer_mod.Indexer, "_parse", "parse.tree_sitter")
    meter.wrap_class(indexer_mod.Indexer, "_run", "ingest.run")
    meter.wrap_class(indexer_mod.Indexer, "_index_file", "index.file_total")
    meter.wrap_class(indexer_mod.Indexer, "_collect_inputs", "scan.collect_inputs")
    meter.wrap_class(indexer_mod.Indexer, "_resolve", "graph.resolve_phase2")
    import zace_core.pipeline.embedding_sink as sink_mod

    # TASK-114：向量阶段拆成 EmbeddingSink（同步消费者）+ EmbeddingPipeline（后台线程）。
    meter.wrap_class(sink_mod.EmbeddingSink, "_process_window", "embed.stage_window")
    meter.wrap_class(sink_mod.EmbeddingPipeline, "submit", "embed.pipeline_backpressure_wait")
    meter.wrap_class(indexer_mod.Indexer, "_rebuild_vectors", "embed.stage_rebuild")

    import zace_core.engine as engine_mod

    engine_mod.plan_scan = _wrap_fn(engine_mod.plan_scan, "scan.plan_scan")
    meter.wrap_class(engine_mod.Engine, "_ingest", "engine.ingest_inner")
    meter.wrap_class(engine_mod.Engine, "write_manifest", "engine.write_manifest")
    meter.wrap_class(engine_mod.Engine, "_displacement_warning", "engine.displacement_check")

def _wrap_fn(fn, key):
    @functools.wraps(fn)
    def timed(*a, **k):
        t0 = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            meter.add(key, time.perf_counter() - t0, 0)

    return timed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--mode", choices=["full", "incremental", "local-only"], default="full")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    data = Path(args.data).expanduser().resolve()

    cfg = EmbeddingConfig.from_env()
    client = TimingClient(timeout=httpx.Timeout(60.0, connect=10.0))
    provider = create_provider(cfg, client=client)
    install_patches(provider, meter)
    if args.mode == "local-only":
        make_offline_post(provider, meter)

    rss_series: list[float] = []
    stop = threading.Event()
    sampler = threading.Thread(target=sample_rss, args=(stop, rss_series), daemon=True)
    sampler.start()

    t_wall0 = time.perf_counter()
    cpu0 = resource.getrusage(resource.RUSAGE_SELF)
    try:
        t0 = time.perf_counter()
        engine = Engine.open(data, provider=provider)
        meter.add("engine.open", time.perf_counter() - t0)

        t0 = time.perf_counter()
        handle, identity = engine.resolve_repo(repo)
        meter.add("engine.resolve_repo", time.perf_counter() - t0)

        t0 = time.perf_counter()
        report = engine.ingest_repo(handle.project_id, repo, full=args.mode != "incremental")
        ingest_s = time.perf_counter() - t0
        meter.add("ingest.total", ingest_s)

        t0 = time.perf_counter()
        with Store.open(engine.project_dir(handle.project_id)) as store:
            from zace_core.chunking.fingerprint import stored_fingerprint

            stored_fingerprint(store)
        meter.add("finalize.fingerprint_read", time.perf_counter() - t0)
    finally:
        stop.set()
        try:
            provider.close()
        finally:
            client.close()

    wall_s = time.perf_counter() - t_wall0
    cpu1 = resource.getrusage(resource.RUSAGE_SELF)
    cpu_s = (cpu1.ru_utime - cpu0.ru_utime) + (cpu1.ru_stime - cpu0.ru_stime)

    reqs = client.requests
    resp_bytes = sum(r["resp_bytes"] for r in reqs)
    api_tokens = sum(r["api_tokens"] for r in reqs)
    req_bytes = sum(r["req_bytes"] for r in reqs)
    # 网络在飞并集
    intervals = sorted((r["start"], r["start"] + r["total_s"]) for r in reqs)
    union = 0.0
    cur_s = cur_e = None
    for s, e in intervals:
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s > cur_e:
            union += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_s is not None:
        union += cur_e - cur_s
    if reqs:
        t_first = min(r["start"] for r in reqs)
        t_last = max(r["start"] + r["total_s"] for r in reqs)
    else:
        t_first = t_last = 0.0

    payload = {
        "schema": 1,
        "tag": args.tag,
        "mode": args.mode,
        "repo": str(repo),
        "data": str(data),
        "project_id": handle.project_id,
        "machine": {
            "cpu_count": os.cpu_count(),
            "loadavg": list(os.getloadavg()),
            "kernel": os.uname().release,
            "mem_total_mb": round(
                os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e6, 1
            ),
        },
        "config": {
            "model": cfg.model,
            "base_url": cfg.base_url,
            "batch_size": getattr(provider, "batch_size", None),
            "batch_token_budget": getattr(provider, "batch_token_budget", None),
            "concurrency": getattr(provider, "concurrency", None),
            "embed_workers": os.environ.get("ZACE_EMBED_WORKERS", "default(2)"),
            "max_input_tokens": cfg.max_input_tokens,
        },
        "wall_s": round(wall_s, 3),
        "ingest_wall_s": round(ingest_s, 3),
        "cpu_user_s": round(cpu1.ru_utime - cpu0.ru_utime, 3),
        "cpu_sys_s": round(cpu1.ru_stime - cpu0.ru_stime, 3),
        "cpu_total_s": round(cpu_s, 3),
        "cpu_util_pct_of_ncpu": round(100 * cpu_s / wall_s / (os.cpu_count() or 1), 1),
        "phases_s": {
            k: round(v, 4) for k, v in sorted(meter.phases.items(), key=lambda kv: -kv[1])
        },
        "peak_rss_mb": round(max(rss_series), 1) if rss_series else None,
        "rss_series_mb": [round(v, 1) for v in rss_series],
        "network": {
            "api_total_tokens": api_tokens,
            "requests": len(reqs),
            "status_counts": _status_counts(reqs),
            "request_bytes": req_bytes,
            "response_bytes": resp_bytes,
            "mb_per_req": round(resp_bytes / 1024 / 1024 / max(1, len(reqs)), 3),
            "network_busy_s": round(union, 3),
            "api_window_from_first_req_to_last_end_s": round(t_last - t_first, 3),
            "mb_per_s_network_busy": round(resp_bytes / 1024 / 1024 / union, 3) if union else None,
            "ttfb_s_list": [round(r["ttfb_s"], 3) for r in reqs],
            "download_s_list": [round(r["download_s"], 3) for r in reqs],
            "total_s_list": [round(r["total_s"], 3) for r in reqs],
            "resp_mb_list": [round(r["resp_bytes"] / 1024 / 1024, 3) for r in reqs],
            "req_kb_list": [round(r["req_bytes"] / 1024, 1) for r in reqs],
        },
        "report": {
            "added": report.added,
            "modified": report.modified,
            "deleted": report.deleted,
            "chunks_new": report.chunks_new,
            "chunks_reused": report.chunks_reused,
            "chunks_deduped": report.chunks_deduped,
            "files_parsed": report.files_parsed,
            "vectors_upserted": report.vectors_upserted,
            "invalidation": str(report.invalidation),
            "errors": list(report.errors)[:5],
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    print(f"[{args.tag}] wall={wall_s:.2f}s ingest={ingest_s:.2f}s cpu={cpu_s:.1f}s "
          f"peakRSS={payload['peak_rss_mb']}MB reqs={len(reqs)} respMB={resp_bytes/1048576:.1f}")
    for k, v in sorted(meter.phases.items(), key=lambda kv: -kv[1])[:18]:
        print(f"    {k:<32} {v:8.3f}s  ({100*v/wall_s:5.1f}%)")
    return 0


def _status_counts(reqs):
    out: dict[int, int] = {}
    for r in reqs:
        out[r["status"]] = out.get(r["status"], 0) + 1
    return out


if __name__ == "__main__":
    raise SystemExit(main())
