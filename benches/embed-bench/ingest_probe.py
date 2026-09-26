#!/usr/bin/env python
"""持久索引构建 + 计量留档：走**真实 ingest 路径**建索引，同时记录 provider 侧计量。

为什么需要它：``zace-core ingest`` 只打印文件 / chunk / 向量计数，**不记录**
"这次索引实际下载了多少响应体、API 报了多少 token、嵌入阶段墙钟多少"。
而这三项正是 TASK-102 耗时模型的输入（Chunks / Response MB / API→本机 MB/s / Total Tokens），
事后无法从索引反推（``index.db`` 只存向量，不存请求流水）。

做法：不碰 core/service 的实现，只在**进程内**把 provider 包一层——

- ``CountingClient``（httpx 子类）：状态码分布 / 响应字节 / API ``usage.total_tokens``；
- ``TokenCountingProvider``：送出的 text token（按给定 tokenizer 口径）与嵌入阶段墙钟窗口。

再交给 ``Engine.open(provider=...)``。解析 / 切片 / 入库 / 建图路径完全不变。

纪律：
- **串行**：一次只跑一个仓库（单 key 独占，避免互相抢配额污染数据）；
- 包装是"透明"的：除 ``embed_side`` 外一律 ``__getattr__`` 透传，不改变 provider 行为；
- 失败**如实退出非零**，不写半截记录。

用法：
    set -a; source /etc/zace/zace.env; set +a
    uv run python benches/embed-bench/ingest_probe.py \
        --repo /path/to/repo --data /root/.zace/bench/voyage-4-lite-d1024 \
        --out benches/results/raw/ingest-vps/leveldb.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import resource
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# 允许直接以脚本方式运行（无需安装包；与 profile_repo.py 同口径）
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

import httpx  # noqa: E402
from zace_core.chunking.fingerprint import stored_fingerprint  # noqa: E402
from zace_core.embedding.factory import EmbeddingConfig, create_provider  # noqa: E402
from zace_core.engine import Engine  # noqa: E402
from zace_core.pipeline.embedding_sink import embed_window_size  # noqa: E402
from zace_core.storage import Store  # noqa: E402


def union_seconds(intervals: Sequence[tuple[float, float]]) -> float:
    """区间并集：**至少有一个请求在飞**的墙钟时间（并发下不能用求和，会重复计时）。"""
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    total = 0.0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    return total + (cur_end - cur_start)


class UpsertMeter:
    """给 ``VectorStore.upsert`` 计时（进程内打桩，不改 core 实现）。"""

    def __init__(self) -> None:
        self.total_s = 0.0
        self.calls = 0
        self.rows = 0

    def patch(self) -> None:
        from zace_core.vectors import VectorStore  # noqa: PLC0415

        original = VectorStore.upsert
        meter = self

        def timed(target: Any, rows: Sequence[Any]) -> int:
            started = time.perf_counter()
            try:
                return original(target, rows)
            finally:
                meter.total_s += time.perf_counter() - started
                meter.calls += 1
                meter.rows += len(rows)

        VectorStore.upsert = timed  # type: ignore[method-assign]


def machine_info() -> dict[str, Any]:
    """设备指纹：VPS 与公司 WSL 的数据不能混（TASK-102 §8.1）。"""

    def sh(*cmd: str) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    mem_kb = 0
    mem_available_kb = 0
    swap_total_kb = 0
    swap_free_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key == "MemTotal":
                mem_kb = int(rest.split()[0])
            elif key == "MemAvailable":
                mem_available_kb = int(rest.split()[0])
            elif key == "SwapTotal":
                swap_total_kb = int(rest.split()[0])
            elif key == "SwapFree":
                swap_free_kb = int(rest.split()[0])
    except OSError:
        pass
    # 运行时快照：同一配置在这台 2 vCPU 共享 VPS 上实测有 ~20% 波动（本地段），
    # 不记负载/内存就无法判断两次跑批的数字能不能直接比（见 results/baseline-v1.md）。
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = -1.0
    return {
        "cpu_count": os.cpu_count(),
        "mem_total_gib": round(mem_kb / 1024 / 1024, 1),
        "kernel": sh("uname", "-r"),
        "loadavg": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "mem_available_mb": round(mem_available_kb / 1024, 1),
        "swap_used_mb": round((swap_total_kb - swap_free_kb) / 1024, 1),
        "cpu_model": next(
            (
                line.split(":", 1)[1].strip()
                for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith("model name")
            ),
            "",
        ),
    }


def git_head(repo: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


class CountingClient(httpx.Client):
    """带状态的 httpx 客户端：状态码 / 响应字节 / API 计费 token。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.status_counts: dict[int, int] = {}
        self.response_bytes = 0
        self.request_count = 0
        self.embedding_requests = 0
        self.api_tokens = 0
        self.request_sizes: list[dict[str, Any]] = []
        self.embedding_intervals: list[tuple[float, float]] = []

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        started = time.perf_counter()
        response = super().send(request, **kwargs)
        elapsed = time.perf_counter() - started
        self.request_count += 1
        self.status_counts[response.status_code] = (
            self.status_counts.get(response.status_code, 0) + 1
        )
        try:
            payload = len(response.content)
        except Exception:  # noqa: BLE001 - 流式/未读时忽略
            payload = 0
        self.response_bytes += payload
        if request.url.path.endswith("/embeddings") and response.status_code == 200:
            self.embedding_requests += 1
            self.request_sizes.append({"bytes": payload, "elapsed_s": round(elapsed, 3)})
            self.embedding_intervals.append((started, time.perf_counter()))
            try:
                usage = json.loads(response.content).get("usage") or {}
                self.api_tokens += int(usage.get("total_tokens") or 0)
            except Exception:  # noqa: BLE001 - usage 缺失不影响计量主体
                pass
        return response


class TokenCountingProvider:
    """provider 包装：统计送出的 text token 与**嵌入阶段墙钟窗口**（不改被测实现）。"""

    def __init__(self, inner: Any, tokenizer: Any) -> None:
        self._inner = inner
        self._tokenizer = tokenizer
        self.tokens_sent = 0
        self.batches = 0
        self.started: float | None = None
        self.finished: float | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def embed_side(self, texts: Sequence[str], side: str) -> list[list[float]]:
        now = time.perf_counter()
        if self.started is None:
            self.started = now
        if self._tokenizer is not None:
            self.tokens_sent += sum(len(self._tokenizer.encode(t).ids) for t in texts)
        self.batches += 1
        try:
            return self._inner.embed_side(texts, side)
        finally:
            self.finished = time.perf_counter()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed_side(texts, "passage")

    @property
    def window_s(self) -> float:
        if self.started is None or self.finished is None:
            return 0.0
        return self.finished - self.started


def load_tokenizer(repo_id: str) -> tuple[Any, str | None]:
    """拿 bge-m3 tokenizer（与 profile_repo / TASK-102 的 token 口径一致）。

    拿不到**不致命**：只是少一列 token 口径，API 侧 ``usage`` 仍然有效。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from profile_repo import build_tokenizer  # noqa: PLC0415

        return build_tokenizer(repo_id), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def report_to_dict(report: Any) -> dict[str, Any]:
    try:
        return dataclasses.asdict(report)
    except Exception:  # noqa: BLE001
        return {"repr": repr(report)}


def main() -> int:
    ap = argparse.ArgumentParser(description="持久索引构建 + 计量留档（真实 ingest 路径）")
    ap.add_argument("--repo", required=True, help="靶场 checkout（只读，不在其中写文件）")
    ap.add_argument("--data", required=True, help="持久数据根（{data}/projects/{projectId}/）")
    ap.add_argument("--out", type=Path, help="计量 JSON 输出路径")
    ap.add_argument("--tokenizer", default="BAAI/bge-m3", help="token 口径用的 tokenizer")
    ap.add_argument("--tag", default="", help="运行标签（进报告，便于区分批次）")
    ap.add_argument(
        "--incremental",
        action="store_true",
        help="默认全量（冷启动口径）；给了则走增量，可用来证明『复用不重嵌』",
    )
    args = ap.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"错误：仓库路径不是目录：{repo}", file=sys.stderr)
        return 2
    data_root = Path(args.data).expanduser().resolve()

    tokenizer, tokenizer_error = load_tokenizer(args.tokenizer)
    cfg = EmbeddingConfig.from_env()
    client = CountingClient(timeout=httpx.Timeout(60.0, connect=10.0))
    provider = create_provider(cfg, client=client)
    counted = TokenCountingProvider(provider, tokenizer)
    upsert_meter = UpsertMeter()
    upsert_meter.patch()
    engine = Engine.open(data_root, provider=counted)

    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        handle, identity = engine.resolve_repo(repo)
        wall0 = time.perf_counter()
        report = engine.ingest_repo(handle.project_id, repo, full=not args.incremental)
        wall_s = time.perf_counter() - wall0
        with Store.open(engine.project_dir(handle.project_id)) as store:
            fingerprint = stored_fingerprint(store)
    except Exception as exc:  # noqa: BLE001 - 失败必须如实退出
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            provider.close()
        finally:
            client.close()

    profile = provider.profile
    transport = provider.transport
    response_mb = client.response_bytes / 1024 / 1024
    payload = {
        "schema": 1,
        "tag": args.tag,
        "started_at": started_at,
        "machine": machine_info(),
        "repo": str(repo),
        "repo_commit": git_head(repo),
        "identity": {
            "identity_key": identity.identity_key,
            "display_name": identity.display_name,
            "remote_url": identity.remote_url,
            "repo_path": identity.repo_path,
        },
        "project_id": handle.project_id,
        "project_created": handle.created,
        "data_root": str(data_root),
        "index_dir": str(engine.project_dir(handle.project_id)),
        "fingerprint": (
            {
                "parser_config_hash": fingerprint.parser_config_hash,
                "embedding_model": fingerprint.embedding_model,
                "embedding_dim": fingerprint.embedding_dim,
                "embedding_profile": fingerprint.embedding_profile,
            }
            if fingerprint
            else None
        ),
        "embedding": {
            "mode": cfg.mode,
            "model": profile.model_id,
            "dim": profile.dim,
            "base_url": transport.base_url,
            "transport": transport.provider,
            "batch_size": provider.batch_size,
            "batch_token_budget": provider.batch_token_budget,
            "concurrency": provider.concurrency,
            "max_input_tokens": profile.max_input_tokens,
        },
        "ingest": {
            "mode": "incremental" if args.incremental else "full",
            "wall_s": round(wall_s, 2),
            "report": report_to_dict(report),
        },
        "provider_meter": {
            "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
            "embed_window_chunks": embed_window_size(provider),
            "embedding_window_s": round(counted.window_s, 2),
            "network_busy_s": round(union_seconds(client.embedding_intervals), 2),
            "upsert_total_s": round(upsert_meter.total_s, 2),
            "upsert_calls": upsert_meter.calls,
            "batches": counted.batches,
            "requests_total": client.request_count,
            "requests_embeddings": client.embedding_requests,
            "status_counts": dict(sorted(client.status_counts.items())),
            "response_mb": round(response_mb, 2),
            "response_kb_per_chunk": (
                round(response_mb * 1024 / report.chunks_new, 2) if report.chunks_new else None
            ),
            "api_total_tokens": client.api_tokens,
            "tokens_sent_tokenizer": counted.tokens_sent if tokenizer is not None else None,
            "tokenizer": args.tokenizer if tokenizer is not None else None,
            "tokenizer_error": tokenizer_error,
            "api_mb_per_s_network_busy": (
                round(response_mb / union_seconds(client.embedding_intervals), 2)
                if client.embedding_intervals
                else None
            ),
            "api_mb_per_s_over_wall": round(response_mb / wall_s, 2) if wall_s else None,
            "api_tokens_per_min_over_wall": (
                round(client.api_tokens / wall_s * 60, 0) if wall_s else None
            ),
        },
    }

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"已写入 {args.out}")

    m = payload["provider_meter"]
    fp = payload["fingerprint"]
    profile_line = fp["embedding_profile"] if fp else "?"
    wall_s = payload["ingest"]["wall_s"]
    net_share = 100 * m["network_busy_s"] / wall_s if wall_s else 0
    parser_line = fp["parser_config_hash"][:12] if fp else "?"
    print(
        f"\n{repo.name}\n"
        f"  project        = {payload['project_id']}"
        f"{' (created)' if payload['project_created'] else ''}\n"
        f"  fingerprint    = {profile_line}  parser={parser_line}\n"
        f"  files parsed   = {report.files_parsed}  chunks new/reused = "
        f"{report.chunks_new}/{report.chunks_reused}  vectors = {report.vectors_upserted}\n"
        f"  ingest 墙钟    = {payload['ingest']['wall_s']}s"
        f"（嵌入窗口 {m['embedding_window_s']}s）\n"
        f"  请求 / 状态    = {m['requests_embeddings']} / {m['status_counts']}\n"
        f"  进程峰值       = {m['peak_rss_mb']} MB"
        f"（窗口 {m['embed_window_chunks']} chunk/次）\n"
        f"  响应体         = {m['response_mb']} MB"
        f"（{m['response_kb_per_chunk']} KB/chunk）\n"
        f"  API 计费 token = {m['api_total_tokens']:,}\n"
        f"  网络在飞       = {m['network_busy_s']}s（占比 {net_share:.0f}%）"
        f"  →  API→本机 {m['api_mb_per_s_network_busy']} MB/s\n"
        f"  入库(upsert)   = {m['upsert_total_s']}s / {m['upsert_calls']} 次\n",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
