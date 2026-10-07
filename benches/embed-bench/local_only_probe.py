#!/usr/bin/env python
"""零网络地板：把 embedding provider 换成瞬时替身，量出 ingest 的**纯本地**耗时。

回答的问题（用户 2026-09-15）："langchain 冷启动久，瓶颈是网速吗？"
- 本脚本不发出任何网络请求（provider.embed 直接返回零向量），因此得到的是
  "解析 + 切分 + SQLite/FTS + 建图 + 向量入库" 的**本地下限**；
- 真实 ingest 墙钟 − 本值 ≈ 网络在飞时间 + 响应体解码（`ingest_probe.py` 的
  `network_busy_s` 给出其中的网络部分）。两者相减即可判定"瓶颈是带宽还是 CPU"。

注意事项：
- 替身向量是共享的 `0.0` 单例，**内存比真实向量低 3~4 倍**（81 B/条 vs 33 KB/条），
  所以本脚本的峰值 RSS 只代表本地阶段的解释器/解析/入库开销，**不能当作真实 ingest 的峰值**；
- 数据根必须为空目录（增量 ingest 会跳过已索引 chunk，就测不到嵌入阶段了）。

用法：
    uv run python benches/embed-bench/local_only_probe.py \
        --repo /path/to/repo --data /tmp/local-only --out benches/results/raw/local-only-vps.json
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

from nova_core.engine import Engine  # noqa: E402
from nova_core.interfaces import EmbeddingProfile  # noqa: E402


class InstantProvider:
    """瞬时替身：不联网、不做任何计算，只占位出 dim 维零向量。"""

    def __init__(self, dim: int, max_input_tokens: int = 32000) -> None:
        self._profile = EmbeddingProfile(
            model_id="stub:instant", dim=dim, max_input_tokens=max_input_tokens
        )

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._profile.dim for _ in texts]

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)


def main() -> int:
    parser = argparse.ArgumentParser(description="零网络地板：ingest 的纯本地耗时")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--data", required=True, help="**空**数据根（临时目录即可）")
    parser.add_argument("--dim", type=int, default=1024)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    data = Path(args.data).resolve()
    data.mkdir(parents=True, exist_ok=True)

    engine = Engine.open(data, provider=InstantProvider(args.dim))
    started = time.perf_counter()
    handle, _ = engine.resolve_repo(repo)
    resolve_s = time.perf_counter() - started
    started = time.perf_counter()
    report = engine.ingest_repo(handle.project_id, repo, full=False)
    ingest_s = time.perf_counter() - started
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    payload = {
        "schema": 1,
        "tag": "local-only-floor",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": {
            "cpu_count": _cpu_count(),
            "mem_total_gib": _mem_total_gib(),
            "kernel": subprocess.run(
                ["uname", "-r"], capture_output=True, text=True
            ).stdout.strip(),
        },
        "repo": str(repo),
        "project_id": handle.project_id,
        "provider": "stub:instant（零网络、零计算）",
        "dim": args.dim,
        "local_ingest_s": round(ingest_s, 2),
        "resolve_s": round(resolve_s, 2),
        "chunks_new": report.chunks_new,
        "vectors_upserted": report.vectors_upserted,
        "edges_retargeted": report.edges_retargeted,
        "spec_refs": report.spec_refs,
        "peak_rss_mb": round(peak_mb, 1),
    }
    if args.out:
        history = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else []
        history.append(payload)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"{repo.name}: 纯本地 {payload['local_ingest_s']}s（resolve {payload['resolve_s']}s）"
        f"｜chunks={report.chunks_new}｜峰值 RSS {payload['peak_rss_mb']} MB"
        + (f" → {args.out}" if args.out else ""),
        flush=True,
    )
    return 0


def _cpu_count() -> int:
    import os

    return os.cpu_count() or 0


def _mem_total_gib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal"):
            return round(int(line.split()[1]) / 1024 / 1024, 1)
    return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
