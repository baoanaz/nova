#!/usr/bin/env python
"""靶场画像：解析 + 切分 + **精确 token 统计**（不调用任何 embedding API）。

为什么需要它：embedding 索引耗时的自变量是**送进 API 的 token 总量**，不是文件数或 chunk 数。
本脚本复用流水线自身的解析/切分实现（``DirectorySource`` + ``parsing`` + ``split_file`` +
``embedding_text``），因此统计口径与真实 ingest 一致，但**零 API 成本、零限流风险**。

用法：
    uv run python benches/embed-bench/profile_repo.py \
        --repo /path/to/repo [--repo ...] --out benches/results/raw/repo-profile.json

token 口径：``tokenizers`` 加载 ``BAAI/bge-m3`` 的 ``tokenizer.json``（与 API provider 的
截断口径一致）。拿不到 tokenizer 时**报错退出**——字节数估计会让"2048 截断省多少 token"
这类结论失真，宁可不跑。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# 允许直接以脚本方式运行（无需安装包）
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

from nova_core.chunking.splitter import embedding_text, split_file  # noqa: E402
from nova_core.parsing.registry import detect_language, get_parser  # noqa: E402
from nova_core.pipeline.ignore import (  # noqa: E402
    SKIP_REASON_BINARY,
    IndexScope,
    binary_reason,
)
from nova_core.pipeline.source import DirectorySource  # noqa: E402
from nova_core.types import ParsedFile  # noqa: E402

H_EXTENSION = ".h"


def build_tokenizer(repo_id: str):
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    path = hf_hub_download(repo_id=repo_id, filename="tokenizer.json")
    return Tokenizer.from_file(str(path))


def percentile(sorted_values: list[int], pct: float) -> int:
    if not sorted_values:
        return 0
    idx = min(len(sorted_values) - 1, max(0, int(round(pct / 100.0 * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def profile_repo(repo: Path, tokenizer, *, max_input_tokens: int) -> dict:
    scope = IndexScope.from_env({})  # 默认 128 KB / 10%（与 CLI ingest 同口径）
    source = DirectorySource(repo)
    paths = list(source.list_files())

    # R1 仓库级语言抬升：C++ 仓库里 ``.h`` 按 ``cpp`` 解析（与 indexer 同口径，
    # 否则 .h 走兜底切分 → chunk 数与 token 数都会偏低）。
    repo_is_cpp = any(detect_language(p) == "cpp" for p in paths)

    files_read = files_parsed = 0
    skipped: dict[str, int] = {}
    errors: list[str] = []
    chunk_texts: list[str] = []
    languages: dict[str, int] = {}

    for path in paths:
        try:
            size = (repo / path).stat().st_size
        except OSError as exc:  # pragma: no cover - 竞态
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        readable, reason = scope.should_read(path, size)
        if not readable:
            key = (reason or "oversize").split(":")[0]
            skipped[key] = skipped.get(key, 0) + 1
            continue
        try:
            data = source.read(path)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        files_read += 1
        ok, bin_reason = scope.check_bytes(data)
        if not ok:
            key = (bin_reason or SKIP_REASON_BINARY).split(":")[0]
            skipped[key] = skipped.get(key, 0) + 1
            continue
        if b"\x00" in data:
            skipped[binary_reason().split(":")[0]] = (
                skipped.get(binary_reason().split(":")[0], 0) + 1
            )
            continue
        text = data.decode("utf-8", errors="replace")
        language = (
            "cpp" if (repo_is_cpp and path.lower().endswith(H_EXTENSION)) else detect_language(path)
        )
        if language is None:
            parsed = ParsedFile(path=path, language="fallback", fallback=True)
        else:
            try:
                parsed = get_parser(language).parse(path, text)
            except Exception as exc:  # noqa: BLE001 - 与 indexer._parse 的降级一致
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
                parsed = ParsedFile(
                    path=path, language=language, parse_errors=(str(exc),), fallback=True
                )
        try:
            chunks = split_file(parsed, text)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path}: split: {type(exc).__name__}: {exc}")
            continue
        files_parsed += 1
        languages[parsed.language] = languages.get(parsed.language, 0) + 1
        for chunk in chunks:
            chunk_texts.append(embedding_text(chunk))

    encoded = [tokenizer.encode(t).ids for t in chunk_texts]
    lengths = sorted(len(ids) for ids in encoded)

    def truncated_total(limit: int) -> dict:
        total = sum(min(n, limit) for n in lengths)
        affected = sum(1 for n in lengths if n > limit)
        return {
            "limit": limit,
            "tokens": total,
            "chunks_truncated": affected,
            "chunks_truncated_pct": round(100.0 * affected / max(1, len(lengths)), 2),
            "tokens_saved_vs_max": sum(lengths) - total,
        }

    total_tokens = sum(lengths)
    return {
        "repo": str(repo),
        "files_listed": len(paths),
        "files_read": files_read,
        "files_parsed": files_parsed,
        "chunks": len(chunk_texts),
        "tokens_total": total_tokens,
        "tokens_per_chunk_mean": round(total_tokens / max(1, len(chunk_texts)), 1),
        "token_p50": percentile(lengths, 50),
        "token_p90": percentile(lengths, 90),
        "token_p99": percentile(lengths, 99),
        "token_max": lengths[-1] if lengths else 0,
        "skipped": skipped,
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
        "parse_errors": len(errors),
        "parse_error_samples": errors[:5],
        "truncation": [
            truncated_total(max_input_tokens),
            truncated_total(8192),
            truncated_total(2048),
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="靶场画像（免 API 的 token 统计）")
    ap.add_argument("--repo", action="append", required=True, help="仓库路径（可重复）")
    ap.add_argument("--tokenizer", default="BAAI/bge-m3", help="tokenizer 的 HF repo id")
    ap.add_argument("--max-input-tokens", type=int, default=8192)
    ap.add_argument("--out", type=Path, help="结果 JSON 输出路径")
    args = ap.parse_args()

    t0 = time.time()
    tokenizer = build_tokenizer(args.tokenizer)
    print(f"tokenizer 加载完成（{args.tokenizer}）耗时 {time.time() - t0:.1f}s", flush=True)

    results = []
    for repo in args.repo:
        path = Path(repo).expanduser().resolve()
        t1 = time.time()
        prof = profile_repo(path, tokenizer, max_input_tokens=args.max_input_tokens)
        prof["wall_seconds_local_only"] = round(time.time() - t1, 1)
        results.append(prof)
        print(
            f"\n{prof['repo']}\n"
            f"  文件 listed/parsed = {prof['files_listed']} / {prof['files_parsed']}  "
            f"skipped = {prof['skipped']}\n"
            f"  chunks = {prof['chunks']}  tokens = {prof['tokens_total']:,} "
            f"(mean {prof['tokens_per_chunk_mean']}/chunk)\n"
            f"  token p50/p90/p99/max = {prof['token_p50']}/{prof['token_p90']}/"
            f"{prof['token_p99']}/{prof['token_max']}\n"
            f"  本地解析+切分+计数耗时 = {prof['wall_seconds_local_only']}s",
            flush=True,
        )
        for t in prof["truncation"]:
            print(
                f"  截断到 {t['limit']:>5}: {t['tokens']:>12,} token  "
                f"({t['chunks_truncated']} chunk 被截, {t['chunks_truncated_pct']}%, "
                f"省 {t['tokens_saved_vs_max']:,})",
                flush=True,
            )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
