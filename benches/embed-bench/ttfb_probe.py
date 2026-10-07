#!/usr/bin/env python
"""单请求时延分解：把"API→本机"拆成 TTFB（服务端算完首字节）与纯下载两段。

为什么单独一个脚本（WSL 报告 §2.4 的 VPS 对应物）：
- `ingest` 的墙钟里网络只占一部分，剩下的既有本地解析/入库、也有**服务端算力与 RTT**；
  只看端到端 MB/s 无法回答"带宽到底是不是瓶颈"；
- 本脚本在**同一份真实 chunk 文本**上，只发 N 个固定批量请求（无解析、无入库），
  流式读取响应体，逐请求记录 TTFB / 纯下载耗时 / 连接内下载速率；
- 并发数可扫（`--concurrency`），用来观察"聚合速率随并发是否线性增长"——
  若增长远慢于线性，说明限制在服务端或单连接 TCP，而不是本机出口带宽。

零落盘、零索引副作用：只调 `/v1/embeddings`，不写任何索引目录。

用法：
    uv run python benches/embed-bench/ttfb_probe.py \
        --repo /path/to/repo --requests 8 --batch-size 500 \
        --concurrency 1 --concurrency 4 --concurrency 8 \
        --out benches/results/raw/ttfb-vps.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from profile_repo import build_tokenizer  # noqa: E402
from throughput_probe import chunk_texts_for, machine_info  # noqa: E402

PLACEHOLDER_KEY = "missing-EMBED_API_KEY"


def one_request(
    url: str,
    key: str,
    model: str,
    batch: list[str],
    *,
    timeout_s: float = 120.0,
) -> dict:
    """一次批量嵌入：流式读，分别记录 TTFB（首字节）与纯下载耗时。"""
    payload = {"model": model, "input": batch, "input_type": "document"}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    started = time.perf_counter()
    with httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10.0)) as client:
        with client.stream("POST", url, json=payload, headers=headers) as response:
            ttfb = time.perf_counter() - started
            nbytes = 0
            for part in response.iter_bytes():
                nbytes += len(part)
            total = time.perf_counter() - started
            status = response.status_code
    transfer = max(total - ttfb, 1e-9)
    return {
        "items": len(batch),
        "status": status,
        "bytes": nbytes,
        "ttfb_s": round(ttfb, 3),
        "total_s": round(total, 3),
        "transfer_s": round(transfer, 3),
        "transfer_MB_per_s": round(nbytes / 1024 / 1024 / transfer, 2),
    }


def run_concurrency(
    *,
    url: str,
    key: str,
    model: str,
    batches: list[list[str]],
    concurrency: int,
) -> dict:
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        rows = list(pool.map(lambda b: one_request(url, key, model, b), batches))
    wall = time.perf_counter() - started
    total_mb = sum(r["bytes"] for r in rows) / 1024 / 1024
    ok = [r for r in rows if r["status"] == 200]
    return {
        "config": {"concurrency": concurrency, "batch_size": len(batches[0])},
        "requests": len(rows),
        "chunks": sum(r["items"] for r in rows),
        "response_mb": round(total_mb, 2),
        "wall_s": round(wall, 2),
        "aggregate_MB_per_s": round(total_mb / wall, 2),
        "ttfb_s_median": round(statistics.median(r["ttfb_s"] for r in ok), 3) if ok else None,
        "transfer_s_median": (
            round(statistics.median(r["transfer_s"] for r in ok), 3) if ok else None
        ),
        "connection_MB_per_s_median": (
            round(statistics.median(r["transfer_MB_per_s"] for r in ok), 2) if ok else None
        ),
        "status_counts": {
            str(s): sum(1 for r in rows if r["status"] == s) for s in {r["status"] for r in rows}
        },
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="单请求时延分解（TTFB vs 纯下载）")
    parser.add_argument("--repo", required=True, help="靶场 checkout（只用来取真实 chunk 文本）")
    parser.add_argument("--requests", type=int, default=8, help="每个并发档发多少个请求")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--concurrency", type=int, action="append", default=None)
    parser.add_argument("--model", default="voyage-4-lite")
    parser.add_argument("--base-url", default="https://api.voyageai.com")
    parser.add_argument("--tokenizer", default="BAAI/bge-m3")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    import os

    key = os.environ.get("EMBED_API_KEY", "")
    if not key or key == PLACEHOLDER_KEY:
        print(
            "缺少 EMBED_API_KEY（示例：set -a; source /etc/nova/nova.env; set +a）", file=sys.stderr
        )
        return 2

    repo = Path(args.repo).resolve()
    tokenizer = build_tokenizer(args.tokenizer)
    texts = chunk_texts_for(repo, tokenizer)
    batches = [
        texts[i : i + args.batch_size]
        for i in range(0, args.requests * args.batch_size, args.batch_size)
    ]
    batches = [b for b in batches if b]
    print(f"  样本: {sum(len(b) for b in batches)} chunk / {len(batches)} 请求", flush=True)

    url = args.base_url.rstrip("/") + "/v1/embeddings"
    results = []
    for concurrency in args.concurrency or [4]:
        print(f"  → 并发 {concurrency}", flush=True)
        result = run_concurrency(
            url=url, key=key, model=args.model, batches=batches, concurrency=concurrency
        )
        results.append(result)
        print(
            f"     {result['response_mb']} MB / {result['wall_s']}s"
            f" → 聚合 {result['aggregate_MB_per_s']} MB/s"
            f"（TTFB 中位 {result['ttfb_s_median']}s，纯下载中位 {result['transfer_s_median']}s，"
            f"单连接中位 {result['connection_MB_per_s_median']} MB/s）",
            flush=True,
        )

    payload = {
        "schema": 1,
        "tag": "ttfb-decomposition",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": machine_info(),
        "repo": str(repo),
        "model": args.model,
        "base_url": args.base_url,
        "requests_per_config": len(batches),
        "batch_size": args.batch_size,
        "tokenizer": args.tokenizer,
        "results": results,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  → 已写入 {args.out}", flush=True)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
