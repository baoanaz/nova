#!/usr/bin/env python3
"""CPU and direct HTTPS bandwidth measurements; no credentials or source upload."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import os
import platform
import re
import statistics
import subprocess
import time
from pathlib import Path


def cpu_probe(threads: int, seconds: int) -> dict:
    command = [
        "sysbench", "cpu", "--cpu-max-prime=20000",
        f"--threads={threads}", f"--time={seconds}", "run",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=seconds + 30)
    if result.returncode:
        raise RuntimeError("sysbench failed: " + result.stderr.strip())
    match = re.search(r"events per second:\s+([\d.]+)", result.stdout)
    if not match:
        raise RuntimeError("sysbench did not report events per second")
    return {"threads": threads, "seconds": seconds, "max_prime": 20000,
            "events_per_second": float(match.group(1))}


def transfer(url: str, expected_bytes: int, *, upload: bool = False) -> dict:
    command = [
        "curl", "--silent", "--show-error", "--fail", "--noproxy", "*",
        "--connect-timeout", "10", "--max-time", "60",
        "--output", os.devnull, "--write-out", "%{json}",
    ]
    if upload:
        command += ["--header", "Content-Type: application/octet-stream",
                    "--data-binary", "@-"]
    result = subprocess.run(
        command + [url], input=b"\0" * expected_bytes if upload else None,
        capture_output=True, timeout=70,
    )
    if result.returncode:
        raise RuntimeError("curl failed: " + result.stderr.decode(errors="replace").strip())
    row = json.loads(result.stdout)
    status = int(row["http_code"])
    size_key = "size_upload" if upload else "size_download"
    transferred = int(row[size_key])
    if status != 200 or transferred != expected_bytes:
        raise RuntimeError(f"Invalid transfer: HTTP {status}, {transferred}/{expected_bytes} bytes")
    return {
        "http_status": status, "bytes": transferred,
        "total_s": row["time_total"], "ttfb_s": row["time_starttransfer"],
        "mbps_including_latency": round(transferred * 8 / row["time_total"] / 1e6, 3),
    }


def download_probe(byte_count: int, connections: int, download_url: str | None = None) -> dict:
    url = download_url or "https://proof.ovh.net/files/100Mb.dat"
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=connections) as pool:
        rows = list(pool.map(lambda _: transfer(url, byte_count), range(connections)))
    wall = time.perf_counter() - started
    return {
        "connections": connections, "bytes_per_connection": byte_count,
        "total_bytes": sum(row["bytes"] for row in rows), "wall_s": round(wall, 6),
        "aggregate_mbps": round(sum(row["bytes"] for row in rows) * 8 / wall / 1e6, 3),
        "connection_mbps_median": statistics.median(row["mbps_including_latency"] for row in rows),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--download-bytes", type=int, default=104_857_600)
    parser.add_argument("--upload-bytes", type=int, default=16_000_000)
    parser.add_argument(
        "--download-url", default="https://proof.ovh.net/files/100Mb.dat",
        help="Public HTTPS file; set --download-bytes to its size.",
    )
    parser.add_argument("--skip-cpu", action="store_true")
    parser.add_argument("--skip-network", action="store_true")
    args = parser.parse_args()
    if min(args.seconds, args.download_bytes, args.upload_bytes) < 1:
        parser.error("measurement sizes and duration must be positive")
    if args.download_url and not args.download_url.startswith("https://"):
        parser.error("--download-url must use HTTPS")
    cpu_model = next(
        (line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
         if line.startswith("model name")), platform.machine(),
    )
    payload = {
        "schema": 1, "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "machine": {"cpu_model": cpu_model, "cpu_count": os.cpu_count(),
                    "kernel": platform.release(), "loadavg_before": list(os.getloadavg())},
        "network": {"download_endpoint": args.download_url,
                    "upload_endpoint": "https://speed.cloudflare.com", "proxy": "disabled",
                    "scope": "throughput to this endpoint, not a guaranteed port speed",
                    "download": []},
        "cpu": [], "errors": [],
    }
    if not args.skip_cpu:
        for threads in dict.fromkeys((1, min(2, os.cpu_count() or 1))):
            try:
                row = cpu_probe(threads, args.seconds)
                payload["cpu"].append(row)
                print(json.dumps(row), flush=True)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                payload["errors"].append(str(exc))
                break
    if not args.skip_network:
        for connections in (1, 4):
            try:
                row = download_probe(args.download_bytes, connections, args.download_url)
                payload["network"]["download"].append(row)
                print(
                    f"download connections={connections}: {row['aggregate_mbps']} Mbps", flush=True,
                )
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                payload["errors"].append(str(exc))
                break
        try:
            payload["network"]["upload"] = transfer(
                "https://speed.cloudflare.com/__up", args.upload_bytes, upload=True,
            )
            print("upload: " + json.dumps(payload["network"]["upload"]), flush=True)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            payload["errors"].append(str(exc))
    payload["machine"]["loadavg_after"] = list(os.getloadavg())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    for error in payload["errors"]:
        print(error, flush=True)
    return 1 if payload["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
