#!/usr/bin/env python
"""embedding 吞吐标定：在**真实 chunk 文本**上测不同批/并发/截断配置的吞吐与 429 行为。

为什么要单独一个脚本（而不是只跑 `nova-core ingest`）：
- ingest 的 99% 时间花在 embedding，但 ingest 是"一次性"的——无法在同一份数据上快速对比
  4~6 组参数（每组都要重跑解析与入库，且换截断参数还不触发重嵌，会污染对照）；
- 本脚本把**嵌入阶段单独拆出来**：同一份 chunk 样本、同一台机器，唯一变量是配置，
  且用返回的 `usage.prompt_tokens` 记账（与 provider 计费口径一致）。

纪律（AGENTS.md §4 + 用户 2026-09-15 指示）：
- **本脚本串行调用**：一次只跑一组配置，不并发跑多个进程（单 key 独占）；
- 内部并发由 `EMBED_CONCURRENCY` 控制（这是被测变量，不是脚本自己的行为）；
- 限流（429）**只计数不放大**：provider 自带的退避重试照常，脚本不额外重试。

用法：
    uv run python benches/embed-bench/throughput_probe.py \
        --repo /path/to/repo --sample 2000 \
        --concurrency 1 --concurrency 3 --max-input-tokens 8192 \
        --out benches/results/raw/throughput.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

import httpx
from nova_core.embedding.factory import EmbeddingConfig, create_provider  # noqa: E402

# 复用画像脚本的解析/切分逻辑（保证样本与真实 ingest 同源）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from profile_repo import build_tokenizer, profile_repo  # noqa: E402


def machine_info() -> dict:
    def sh(*cmd: str) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    mem_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                mem_kb = int(line.split()[1])
                break
    except OSError:
        pass
    return {
        "cpu_count": os.cpu_count(),
        "mem_total_gib": round(mem_kb / 1024 / 1024, 1),
        "kernel": sh("uname", "-r"),
        "proxy": os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or "(none)",
        "measured_bandwidth_note": "见 TASK-049 §8.1：本机 2.9–4.8 MB/s（走本机代理）",
    }


class CountingClient(httpx.Client):
    """带状态的 httpx 客户端：统计状态码与响应字节（吞吐瓶颈归因用）。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.status_counts: dict[int, int] = {}
        self.response_bytes = 0
        self.request_count = 0

    def send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        response = super().send(request, **kwargs)
        self.request_count += 1
        self.status_counts[response.status_code] = (
            self.status_counts.get(response.status_code, 0) + 1
        )
        try:
            self.response_bytes += len(response.content)
        except Exception:  # noqa: BLE001 - 流式/未读时忽略
            pass
        return response


class TokenCountingProvider:
    """provider 包装：统计实际发出的文本 token 与调用耗时（不改被测实现）。"""

    def __init__(self, inner, tokenizer) -> None:
        self._inner = inner
        self._tokenizer = tokenizer
        self.tokens_sent = 0
        self.calls = 0

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)

    def embed_side(self, texts, side):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.tokens_sent += sum(len(self._tokenizer.encode(t).ids) for t in texts)
        return self._inner.embed_side(texts, side)

    def embed(self, texts):  # type: ignore[no-untyped-def]
        return self.embed_side(texts, "passage")


def chunk_texts_for(repo: Path, tokenizer) -> list[str]:
    """取该仓库的 ``embedding_text()`` 列表（与 ingest 送嵌入的文本一致）。"""
    prof = profile_repo(repo, tokenizer, max_input_tokens=8192)
    # profile_repo 只返回统计；这里重跑一次拿文本（代价 ~秒级，可接受）
    from nova_core.chunking.splitter import embedding_text, split_file
    from nova_core.parsing.registry import detect_language, get_parser
    from nova_core.pipeline.source import DirectorySource
    from nova_core.types import ParsedFile

    source = DirectorySource(repo)
    texts: list[str] = []
    for path in source.list_files():
        try:
            data = source.read(path)
        except Exception:  # noqa: BLE001
            continue
        if b"\x00" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        language = detect_language(path)
        parsed = (
            ParsedFile(path=path, language="fallback", fallback=True)
            if language is None
            else get_parser(language).parse(path, text)
        )
        try:
            texts.extend(embedding_text(c) for c in split_file(parsed, text))
        except Exception:  # noqa: BLE001
            continue
    print(f"  画像: chunks={prof['chunks']} 取回文本={len(texts)}", flush=True)
    return texts


def run_config(
    texts: list[str],
    tokenizer,
    *,
    model: str,
    base_url: str,
    api_key: str,
    batch_size: int,
    token_budget: int,
    concurrency: int,
    max_input_tokens: int,
    repeats: int,
) -> dict:
    client = CountingClient(timeout=httpx.Timeout(60.0, connect=10.0))
    cfg = EmbeddingConfig(
        mode="api",
        model=model,
        base_url=base_url,
        api_key=api_key,
        batch_size=batch_size,
        batch_token_budget=token_budget,
        concurrency=concurrency,
        max_input_tokens=max_input_tokens,
    )
    provider = create_provider(cfg, client=client)
    counted = TokenCountingProvider(provider, tokenizer)

    wall = []
    error = None
    for _ in range(repeats):
        client.status_counts.clear()
        client.response_bytes = 0
        client.request_count = 0
        counted.tokens_sent = 0
        t0 = time.perf_counter()
        try:
            vectors = counted.embed(texts)
            assert len(vectors) == len(texts), f"向量数 {len(vectors)} != 输入 {len(texts)}"
        except Exception as exc:  # noqa: BLE001 - 429 等失败要如实记录
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
            wall.append(time.perf_counter() - t0)
            break
        wall.append(time.perf_counter() - t0)

    elapsed = min(wall) if wall else 0.0
    tokens = counted.tokens_sent
    effective_tokens = sum(min(len(tokenizer.encode(t).ids), max_input_tokens) for t in texts)
    result = {
        "config": {
            "batch_size": batch_size,
            "token_budget": token_budget,
            "concurrency": concurrency,
            "max_input_tokens": max_input_tokens,
        },
        "chunks": len(texts),
        "tokens_raw": tokens,
        "tokens_effective": effective_tokens,
        "elapsed_s": round(elapsed, 2),
        "walls_s": [round(w, 2) for w in wall],
        "m_tok_per_min": round(effective_tokens / elapsed / 1e6 * 60, 3) if elapsed else None,
        "chunk_per_s": round(len(texts) / elapsed, 1) if elapsed else None,
        "requests": client.request_count,
        "response_mb": round(client.response_bytes / 1024 / 1024, 2),
        "response_MB_per_s": (
            round(client.response_bytes / 1024 / 1024 / elapsed, 2) if elapsed else None
        ),
        "status_counts": dict(sorted(client.status_counts.items())),
        "error": error,
    }
    provider.close()
    client.close()
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="embedding 吞吐标定（真实 chunk 文本）")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--sample", type=int, default=2000, help="抽样 chunk 数（0 = 全量）")
    ap.add_argument("--seed", type=int, default=20260915)
    ap.add_argument("--model", default="bge-m3")
    ap.add_argument("--base-url", default="https://api.siliconflow.cn")
    ap.add_argument("--tokenizer", default="BAAI/bge-m3")
    ap.add_argument("--batch-size", type=int, action="append", default=None)
    ap.add_argument("--token-budget", type=int, action="append", default=None)
    ap.add_argument("--concurrency", type=int, action="append", default=None)
    ap.add_argument("--max-input-tokens", type=int, action="append", default=None)
    ap.add_argument("--repeats", type=int, default=1, help="每组重复次数（取最快一次）")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    api_key = os.environ.get("EMBED_API_KEY")
    if not api_key:
        print("错误：需要 EMBED_API_KEY（set -a; source .env; set +a）", file=sys.stderr)
        return 2

    repo = Path(args.repo).expanduser().resolve()
    batch_sizes = args.batch_size or [256]
    budgets = args.token_budget or [75000]
    concurrencies = args.concurrency or [1]
    limits = args.max_input_tokens or [8192]

    t0 = time.time()
    tokenizer = build_tokenizer(args.tokenizer)
    print(f"tokenizer 就绪（{time.time() - t0:.1f}s）", flush=True)
    all_texts = chunk_texts_for(repo, tokenizer)
    if args.sample and args.sample < len(all_texts):
        import random

        rng = random.Random(args.seed)
        texts = rng.sample(all_texts, args.sample)
        print(f"抽样 {len(texts)} / {len(all_texts)} chunk（seed={args.seed}）", flush=True)
    else:
        texts = all_texts
    # 固定顺序（可复现；批边界由 provider 自己按预算切）
    tokens_all = sum(len(tokenizer.encode(t).ids) for t in texts)
    print(f"样本 token 总量 = {tokens_all:,}", flush=True)

    results = []
    for limit in limits:
        for conc in concurrencies:
            for bs in batch_sizes:
                for budget in budgets:
                    label = f"maxTok={limit} conc={conc} batch={bs} budget={budget}"
                    print(f"\n[跑] {label}", flush=True)
                    r = run_config(
                        texts,
                        tokenizer,
                        model=args.model,
                        base_url=args.base_url,
                        api_key=api_key,
                        batch_size=bs,
                        token_budget=budget,
                        concurrency=conc,
                        max_input_tokens=limit,
                        repeats=args.repeats,
                    )
                    r["label"] = label
                    results.append(r)
                    if r["error"]:
                        print(f"  ✗ 失败: {r['error']}", flush=True)
                    else:
                        print(
                            f"  ✓ {r['elapsed_s']}s  {r['m_tok_per_min']} M tok/min  "
                            f"req={r['requests']}  resp={r['response_mb']} MB "
                            f"({r['response_MB_per_s']} MB/s)  status={r['status_counts']}",
                            flush=True,
                        )

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": machine_info(),
        "repo": str(repo),
        "sample": len(texts),
        "sample_tokens_total": tokens_all,
        "model": args.model,
        "base_url": args.base_url,
        "results": results,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
