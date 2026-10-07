#!/usr/bin/env python3
"""TASK-015A：embedding bake-off 运行器（**只做模型选型**）。

纪律（卡内 + R29/R30，脚本本身也不许违反）：

- **唯一变量是 embedding provider**：rerank 权重 / FTS 列权重 / 前缀匹配 / ``RecallLimits`` /
  ``docs_ratio`` / ``CONSENSUS_SCORE_RATIO`` 一律使用代码默认值，本脚本不传、不改、不覆盖；
- 仓库、golden 用例、切分参数同样固定（切分参数不在本脚本内）；
- **每个模型独立数据根**：``<data-root-base>/<key>/``，其中 ``key = <slug>__t<max_input_tokens>``；
- 中间结果逐 (模型, 仓库) 落 JSON，重跑同一命令会跳过已完成步骤（``--force`` / ``--force-ingest``
  可强制重来）——模型下载与建索引都是分钟到小时级，重复劳动不可接受；
- 报告必须能追溯：每条 JSON 记录完整命令、数据根、仓库 commit、**索引范围摘要**
  （scan manifest 的路径→content_hash 摘要，用于证明各模型索引的是同一份文件集）。

子命令：

```bash
# 候选清单（含缓存命中情况）
uv run python benches/bakeoff/embed_compare.py list

# 预下载模型（记录：下载了什么、多大、放在哪、耗时多少）
uv run python benches/bakeoff/embed_compare.py fetch --model all

# 建索引 + 跑 golden（每个 (模型, 仓库) 一次；可断点续跑）
uv run python benches/bakeoff/embed_compare.py run \
  --model multilingual-e5-small --repo . --repo-name nova \
  --golden benches/golden/nova

# 汇总报告（纯数字部分，报告正文由 benches/results/phase2-bakeoff.md 撰写引用）
uv run python benches/bakeoff/embed_compare.py aggregate --report /tmp/bakeoff-raw.md
```

默认路径（可用参数覆盖）：

- 数据根基目录：``~/.cache/nova-bakeoff``（每 key 一个子目录）；
- 中间结果目录：``~/.cache/nova-bakeoff/results``（**故意落在仓库外**：``benches/**`` 在 dogfood
  索引范围内，把含 query 原文的原始报告写进仓库会污染负例口径，见 R17 / phase1-baseline §4.2）；
- 模型缓存目录：``~/.cache/nova-embedding-cache``。

**为什么不用 ``/tmp``**（2026-09-11 实测，写入任务卡执行记录）：本机在 2026-09-11 00:07:55 重启，
``/tmp`` 被整体清空——任务卡给出的三个现成数据根（``/tmp/nova-aibox`` 等）与模型缓存
（``/tmp/nova-embedding-cache``，含已下载的 e5-small/arctic-xs）全部丢失，一轮已完成的
13 分钟索引也一并作废。bake-off 是小时级任务，中间产物必须放在重启后仍存在的路径上。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

#: 仓库根（本文件的上一级的上上级）。
ROOT = Path(__file__).resolve().parents[2]

#: 默认数据根基目录（每个 key 一个独立数据根）。
#: 注意：**不要放 /tmp**——2026-09-11 00:07 本机重启把 /tmp 清空，一轮 13 分钟的索引作废。
DEFAULT_DATA_ROOT_BASE = Path.home() / ".cache" / "nova-bakeoff"
#: 默认中间结果目录（仓库外，避免污染 dogfood 索引范围）。
DEFAULT_RESULTS_DIR = DEFAULT_DATA_ROOT_BASE / "results"
#: 默认模型缓存目录（重启后幸存；体积 ~1.2GB，重下载代价高）。
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "nova-embedding-cache"
#: 检索预算（与 ``nova-core eval`` 的 ``DEFAULT_MAX_TOKENS`` 一致，一处都不许改）。
MAX_TOKENS = 10_000
#: 查询侧嵌入微基准：采样查询数 × 重复次数。
QUERY_EMBED_SAMPLES = 8
QUERY_EMBED_REPEATS = 3


# ---------------------------------------------------------------------------
# 候选模型表（不做结论，只声明"要测什么"）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """一个候选模型（本地 ONNX）。

    ``registered=False`` 的候选不在 ``nova_core.embedding.registry`` 里（本卡不得改注册表，
    只在报告里给结论）——脚本用 ``LocalModelSpec`` 就地构造，走同一个本地实现。
    """

    slug: str
    title: str
    repo_id: str
    onnx_file: str
    dim: int
    pooling: str
    max_input_tokens: int = 512
    tokenizer_file: str = "tokenizer.json"
    query_prefix: str = ""
    passage_prefix: str = ""
    registered: bool = True
    notes: str = ""


CANDIDATES: tuple[Candidate, ...] = (
    Candidate(
        slug="multilingual-e5-small",
        title="multilingual-e5-small（暂定默认，基线）",
        repo_id="intfloat/multilingual-e5-small",
        onnx_file="onnx/model.onnx",
        dim=384,
        pooling="mean",
        query_prefix="query: ",
        passage_prefix="passage: ",
        notes="TASK-008 暂定默认；多语言，118M 参数",
    ),
    Candidate(
        slug="bge-small-zh-v1.5",
        title="bge-small-zh-v1.5（中文定位）",
        repo_id="Xenova/bge-small-zh-v1.5",
        onnx_file="onnx/model.onnx",
        dim=512,
        pooling="cls",
        notes="中文检索强；官方 BAAI 无 ONNX 导出，用 transformers.js 镜像",
    ),
    Candidate(
        slug="arctic-embed-xs",
        title="arctic-embed-xs（对照，英文）",
        repo_id="Snowflake/snowflake-arctic-embed-xs",
        onnx_file="onnx/model.onnx",
        dim=384,
        pooling="cls",
        notes="英文对照基线；22M 参数（Background/04 §3 的本地 ONNX 先例）",
    ),
    Candidate(
        slug="bge-m3-int8",
        title="bge-m3-int8（上界参考，代价优先）",
        repo_id="Xenova/bge-m3",
        onnx_file="onnx/model_quantized.onnx",
        dim=1024,
        pooling="cls",
        max_input_tokens=8192,
        tokenizer_file="tokenizer.json",
        registered=False,
        notes=(
            "多语言大模型（568M 参数）；fp32 导出为 0.6MB onnx + 2266MB 外部数据，"
            "本脚本取 int8（569MB 单文件）以把代价控制在可测范围；不在注册表中（本卡不改注册表）"
        ),
    ),
)

_BY_SLUG = {candidate.slug: candidate for candidate in CANDIDATES}


def candidate_for(slug: str) -> Candidate:
    try:
        return _BY_SLUG[slug]
    except KeyError:
        known = ", ".join(sorted(_BY_SLUG))
        raise SystemExit(f"未知候选 {slug!r}；可用：{known}") from None


def spec_for(candidate: Candidate, max_input_tokens: int) -> Any:
    """构造 ``LocalModelSpec``（已登记模型读注册表后覆盖截断值；未登记模型就地构造）。"""
    from nova_core.embedding import LocalModelSpec, get_local_spec

    if candidate.registered:
        return replace(get_local_spec(candidate.slug), max_input_tokens=max_input_tokens)
    return LocalModelSpec(
        slug=candidate.slug,
        repo_id=candidate.repo_id,
        onnx_file=candidate.onnx_file,
        dim=candidate.dim,
        pooling=candidate.pooling,
        max_input_tokens=max_input_tokens,
        query_prefix=candidate.query_prefix,
        passage_prefix=candidate.passage_prefix,
        tokenizer_file=candidate.tokenizer_file,
        notes=candidate.notes,
    )


def run_key(slug: str, max_input_tokens: int) -> str:
    return f"{slug}__t{max_input_tokens}"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _now() -> int:
    return int(time.time())


def _json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _json_load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _percentile(values: Sequence[float], fraction: float) -> float:
    """最近秩百分位（样本足够大的墙钟延迟，不需要插值）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(-(-len(ordered) * fraction // 1)) - 1))
    return ordered[index]


def _dir_bytes(path: Path) -> int:
    """文件或目录的字节数（目录递归；文件直接取 size）。"""
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:  # pragma: no cover
            return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:  # pragma: no cover - 并发删除/权限
            continue
    return total


def _git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return ""
    return out.stdout.strip()


def _scope_digest(manifest_path: Path) -> tuple[str, int]:
    """索引范围摘要 = scan manifest 的 ``路径:content_hash`` 排序后 sha256。

    用于证明"各模型索引的是同一份文件集"（模型之间唯一变量是 provider）。
    """
    payload = _json_load(manifest_path) or {}
    files = payload.get("files")
    if not isinstance(files, dict):
        return "", 0
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(f"{name}\x00{files[name]}\n".encode())
    return digest.hexdigest(), len(files)


def _command_line() -> str:
    """记录**实际**执行命令（含真实解释器路径；报告里同时给等价的 ``uv run`` 形式）。"""
    return f"{sys.executable} {Path(sys.argv[0]).name} " + " ".join(sys.argv[1:])


def _machine() -> dict[str, Any]:
    import platform

    info: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
    }
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("model name"):
                    info["cpu_model"] = line.split(":", 1)[1].strip()
                    break
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal"):
                    info["mem_total_kb"] = int(line.split()[1])
                    break
    except OSError:  # pragma: no cover - 非 Linux
        pass
    try:
        import onnxruntime

        info["onnxruntime"] = onnxruntime.__version__
        info["providers"] = list(onnxruntime.get_available_providers())
    except Exception:  # pragma: no cover - 依赖缺失
        info["onnxruntime"] = "unavailable"
    return info


# ---------------------------------------------------------------------------
# 计时 provider（唯一"侵入"实现层的地方：纯包装，不改任何检索/装填参数）
# ---------------------------------------------------------------------------


class TimingProvider:
    """``EmbeddingProvider`` 包装：统计嵌入耗时 / 文本数 / token 长度分布。

    只转发 ``profile`` / ``embed`` / ``embed_query``（CF-09 面），因此对索引与检索链完全透明；
    不参与任何排序或装填决策。
    """

    def __init__(self, inner: Any, *, measure_tokens: bool = True) -> None:
        self._inner = inner
        self._measure_tokens = measure_tokens
        self.calls: dict[str, int] = {"passage": 0, "query": 0}
        self.texts: dict[str, int] = {"passage": 0, "query": 0}
        self.seconds: dict[str, float] = {"passage": 0.0, "query": 0.0}
        self.per_call_seconds: list[float] = []
        self.token_samples: list[int] = []
        self.summary: dict[str, Any] = {}

    @property
    def inner(self) -> Any:
        return self._inner

    @property
    def profile(self) -> Any:
        return self._inner.profile

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self._call("passage", texts)

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        return self._call("query", texts)

    def _call(self, side: str, texts: Sequence[str]) -> list[list[float]]:
        items = list(texts)
        self._measure_tokens_for(items)
        started = time.perf_counter()
        vectors = self._inner.embed(texts) if side == "passage" else self._inner.embed_query(texts)
        elapsed = time.perf_counter() - started
        self.calls[side] += 1
        self.texts[side] += len(items)
        self.seconds[side] += elapsed
        self.per_call_seconds.append(elapsed)
        return vectors

    def _measure_tokens_for(self, texts: Sequence[str]) -> None:
        if not self._measure_tokens or not texts:
            return
        if getattr(self._inner, "_tokenizer", None) is None:
            ensure = getattr(self._inner, "ensure_loaded", None)
            if callable(ensure):
                ensure()  # 首批也要统计（tokenizer 懒加载）
        tokenizer = getattr(self._inner, "_tokenizer", None)
        if tokenizer is None:  # pragma: no cover - 未加载出 tokenizer 时不统计
            return
        try:
            encodings = tokenizer.encode_batch(list(texts), add_special_tokens=True)
        except Exception:  # pragma: no cover - 统计失败不影响测量
            self._measure_tokens = False
            return
        self.token_samples.extend(len(encoding.ids) for encoding in encodings)

    def finish(self) -> dict[str, Any]:
        """收尾统计（token 长度分布 + 吞吐）。"""
        samples = self.token_samples
        payload: dict[str, Any] = {
            "calls": dict(self.calls),
            "texts": dict(self.texts),
            "seconds": {side: round(value, 3) for side, value in self.seconds.items()},
            "throughput_per_s": {
                side: round(self.texts[side] / self.seconds[side], 3)
                if self.seconds[side] > 0
                else 0.0
                for side in ("passage", "query")
            },
        }
        if samples:
            truncated: dict[str, int] = {}
            for limit in (128, 256, 512, 1024, 2048):
                truncated[str(limit)] = sum(1 for value in samples if value > limit)
            payload["tokens"] = {
                "count": len(samples),
                "max": max(samples),
                "p50": _percentile(samples, 0.50),
                "p90": _percentile(samples, 0.90),
                "p99": _percentile(samples, 0.99),
                "over": truncated,
            }
        self.summary = payload
        return payload


# ---------------------------------------------------------------------------
# list / fetch
# ---------------------------------------------------------------------------


def _model_cache_report(candidate: Candidate, cache_dir: Path) -> dict[str, Any]:
    """报告缓存命中情况（两种布局都看：``cache_dir/<repo>`` 与 ``cache_dir/hub/<repo>``）。"""
    found: dict[str, Any] = {"onnx": None, "tokenizer": None}
    roots = [cache_dir / f"models--{candidate.repo_id.replace('/', '--')}", cache_dir / "hub"]
    for root in roots:
        if not root.exists():
            continue
        if root.name == "hub":
            root = root / f"models--{candidate.repo_id.replace('/', '--')}"
        snapshots = root / "snapshots"
        if not snapshots.is_dir():
            continue
        for snapshot in sorted(snapshots.iterdir()):
            for name, key in (
                (candidate.onnx_file, "onnx"),
                (candidate.tokenizer_file, "tokenizer"),
            ):
                path = snapshot / name
                if path.is_file() and found[key] is None:
                    found[key] = {"path": str(path), "bytes": path.stat().st_size}
    found["cached"] = bool(found["onnx"] and found["tokenizer"])
    return found


def cmd_list(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir)
    rows = []
    for candidate in CANDIDATES:
        report = _model_cache_report(candidate, cache_dir)
        rows.append(
            {
                "slug": candidate.slug,
                "title": candidate.title,
                "repo_id": candidate.repo_id,
                "onnx_file": candidate.onnx_file,
                "dim": candidate.dim,
                "pooling": candidate.pooling,
                "registered": candidate.registered,
                "cached": report["cached"],
                "onnx_bytes": (report["onnx"] or {}).get("bytes", 0),
                "tokenizer_bytes": (report["tokenizer"] or {}).get("bytes", 0),
            }
        )
    width = max(len(row["slug"]) for row in rows)
    for row in rows:
        size = (row["onnx_bytes"] + row["tokenizer_bytes"]) / 1e6
        print(
            f"{row['slug']:<{width}}  dim={row['dim']:<5} pooling={row['pooling']:<4} "
            f"registered={str(row['registered']):<5} cached={'yes' if row['cached'] else 'no ':<3} "
            f"files={size:7.1f}MB  {row['title']}"
        )
    print(f"\ncache_dir: {cache_dir}")
    return 0


def _download(candidate: Candidate, filename: str, cache_dir: Path) -> dict[str, Any]:
    """下载单个模型文件（命中缓存时为 0 秒）；返回路径 / 体积 / 耗时 / 是否新下载。"""
    from huggingface_hub import hf_hub_download

    started = time.perf_counter()
    path = Path(
        hf_hub_download(
            repo_id=candidate.repo_id,
            filename=filename,
            cache_dir=str(cache_dir),
        )
    )
    elapsed = time.perf_counter() - started
    stat = path.stat()
    return {
        "file": filename,
        "path": str(path),
        "bytes": stat.st_size,
        "download_seconds": round(elapsed, 2),
        # hf_hub_download 的"缓存命中"判定：文件 mtime 早于本次调用开始（>2s 余量）。
        "cache_hit": (time.time() - stat.st_mtime) > (elapsed + 2.0),
    }


def cmd_fetch(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache_dir).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    results_dir = Path(args.results_dir).expanduser()
    targets = (
        list(CANDIDATES)
        if args.model == "all"
        else [candidate_for(slug) for slug in args.model.split(",")]
    )
    for candidate in targets:
        payload = {
            "slug": candidate.slug,
            "repo_id": candidate.repo_id,
            "onnx_file": candidate.onnx_file,
            "cache_dir": str(cache_dir),
            "fetched_at": _now(),
            "command": _command_line(),
            "machine": _machine(),
            "files": [],
        }
        for filename in (candidate.tokenizer_file, candidate.onnx_file):
            info = _download(candidate, filename, cache_dir)
            payload["files"].append(info)
            print(
                f"[fetch] {candidate.slug}: {filename} {info['bytes'] / 1e6:.1f}MB "
                f"{info['download_seconds']:.1f}s cache_hit={info['cache_hit']}"
            )
        payload["total_bytes"] = sum(item["bytes"] for item in payload["files"])
        payload["total_download_seconds"] = round(
            sum(item["download_seconds"] for item in payload["files"]), 2
        )
        payload["new_bytes"] = sum(
            item["bytes"] for item in payload["files"] if not item["cache_hit"]
        )
        _json_dump(results_dir / f"{candidate.slug}.model.json", payload)
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _project_paths(project_dir: Path) -> dict[str, int]:
    return {
        "vectors_bytes": _dir_bytes(project_dir / "vectors"),
        "index_db_bytes": _dir_bytes(project_dir / "index.db"),
        "project_bytes": _dir_bytes(project_dir),
    }


def _resume_ok(
    payload: dict[str, Any] | None,
    model_id: str,
    max_input_tokens: int,
    commit: str,
    *,
    force: bool,
) -> bool:
    """断点续跑判定：产物存在且与当前 (模型, 截断值, 仓库 commit) 一致。

    模型文件与建索引都是分钟到小时级，所以判定必须严：**模型换了、截断值换了、
    仓库 commit 变了、或显式 --force** 都不允许复用旧索引（否则报告里的数字对不上）。
    """
    if payload is None or force:
        return False
    fingerprint = payload.get("fingerprint") or {}
    repo_info = payload.get("repo") or {}
    return (
        fingerprint.get("model_id") == model_id
        and fingerprint.get("max_input_tokens") == max_input_tokens
        and repo_info.get("commit") == commit
    )


def _stored_fingerprint(project_dir: Path) -> dict[str, Any]:
    """读库内 ``index_config`` 指纹（判断该数据根是不是同一个模型建的）。"""
    from nova_core.storage import Store

    info_path = project_dir / "index.db"
    if not info_path.is_file():
        return {}
    with Store.open(project_dir) as store:
        return {
            "embedding_model": store.get_config("embedding_model"),
            "embedding_dim": store.get_config("embedding_dim"),
            "parser_config_hash": store.get_config("parser_config_hash"),
            "counts": store.counts(),
        }


def cmd_run(args: argparse.Namespace) -> int:
    from nova_core.embedding import LocalOnnxEmbeddingProvider
    from nova_core.engine import Engine

    candidate = candidate_for(args.model)
    max_input_tokens = args.max_input_tokens or candidate.max_input_tokens
    key = run_key(candidate.slug, max_input_tokens)
    spec = spec_for(candidate, max_input_tokens)
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        raise SystemExit(f"--repo 不是目录：{repo}")
    repo_name = args.repo_name or repo.name
    golden = Path(args.golden).expanduser().resolve()
    if not golden.exists():
        raise SystemExit(f"--golden 不存在：{golden}")

    results_dir = Path(args.results_dir).expanduser()
    data_root = Path(args.data_root_base).expanduser() / key
    cache_dir = Path(args.cache_dir).expanduser()
    index_path = results_dir / f"{key}__{repo_name}.index.json"
    eval_path = results_dir / f"{key}__{repo_name}.eval.json"
    raw_path = results_dir / f"{key}__{repo_name}.eval.raw.md"

    provider = LocalOnnxEmbeddingProvider(
        spec,
        cache_dir=cache_dir,
        batch_size=args.batch_size,
    )
    timing = TimingProvider(provider, measure_tokens=not args.no_measure_tokens)
    machine = _machine()

    with Engine.open(data_root, provider=timing) as engine:
        handle, identity = engine.resolve_repo(repo)
        project_dir = engine.project_dir(handle.project_id)
        stored = _stored_fingerprint(project_dir)
        fingerprint_ok = stored.get("embedding_model") == spec.model_id and str(
            stored.get("embedding_dim")
        ) == str(spec.dim)

        index_payload = _json_load(index_path) if not args.force_ingest else None
        resume_ok = bool(
            index_payload
            and fingerprint_ok
            and _resume_ok(
                index_payload,
                spec.model_id,
                max_input_tokens,
                _git(repo, "rev-parse", "HEAD"),
                force=args.force,
            )
        )

        if resume_ok:
            print(f"[run] {key} @ {repo_name}: 索引已存在且指纹一致，跳过建索引（断点续跑）")
        else:
            if index_payload and not fingerprint_ok and not args.force_ingest:
                print(f"[run] {key} @ {repo_name}: 数据根指纹不匹配，重跑建索引")
            started = time.perf_counter()
            report = engine.ingest_repo(handle.project_id, repo, full=True)
            elapsed = time.perf_counter() - started
            stats = timing.finish()
            status = engine.sync_status(handle.project_id)
            scope_digest, scope_files = _scope_digest(project_dir / "scan_manifest.json")
            index_payload = {
                "key": key,
                "command": _command_line(),
                "generated_at": _now(),
                "machine": machine,
                "repo": {
                    "name": repo_name,
                    "path": str(repo),
                    "project_id": handle.project_id,
                    "identity_remote": identity.remote_url,
                    "identity_path": identity.repo_path,
                    "commit": _git(repo, "rev-parse", "HEAD"),
                    "commit_subject": _git(repo, "log", "-1", "--pretty=%s"),
                    "dirty_files": len(_git(repo, "status", "--porcelain").splitlines()),
                },
                "golden": str(golden),
                "data_root": str(data_root),
                "cache_dir": str(cache_dir),
                "fingerprint": {
                    "model_id": spec.model_id,
                    "dim": spec.dim,
                    "max_input_tokens": spec.max_input_tokens,
                    "pooling": spec.pooling,
                    "query_prefix": spec.query_prefix,
                    "passage_prefix": spec.passage_prefix,
                },
                "scope": {"digest": scope_digest, "files": scope_files},
                "index_seconds": round(elapsed, 2),
                "embed": stats,
                "ingest_report": {
                    "invalidation": report.invalidation.value,
                    "files_added": report.added,
                    "files_modified": report.modified,
                    "files_deleted": report.deleted,
                    "files_parsed": report.files_parsed,
                    "chunks_new": report.chunks_new,
                    "chunks_reused": report.chunks_reused,
                    "chunks_removed": report.chunks_removed,
                    "vectors_upserted": report.vectors_upserted,
                    "vectors_deleted": report.vectors_deleted,
                    "skipped_files": len(report.skipped_files),
                    "errors": len(report.errors),
                },
                "project_stats": {
                    "files": status.files_indexed,
                    "chunks": status.chunks,
                    "symbols": status.symbols,
                    "edges": status.edges,
                },
                "disk": _project_paths(project_dir),
                "existing_index_before": bool(stored.get("counts")),
            }
            _json_dump(index_path, index_payload)
            print(
                f"[run] {key} @ {repo_name}: 索引完成 {elapsed:.1f}s "
                f"chunks={status.chunks} embed={stats['seconds']['passage']:.1f}s "
                f"vectors={index_payload['disk']['vectors_bytes'] / 1e6:.1f}MB"
            )

        if args.skip_eval:
            return 0

        existing_eval = None if args.force else _json_load(eval_path)
        if existing_eval and existing_eval.get("scope", {}).get("digest") == index_payload.get(
            "scope", {}
        ).get("digest"):
            print(f"[run] {key} @ {repo_name}: golden 评估已完成，跳过（断点续跑）")
            return 0

        from nova_core.cli.eval import load_cases, run_golden, write_report

        cases = load_cases(golden)
        if not cases:
            raise SystemExit(f"golden 集为空：{golden}")
        latencies: list[float] = []

        def search(query: str) -> Any:
            started = time.perf_counter()
            trace = engine.search_with_trace(handle.project_id, query, MAX_TOKENS)
            latencies.append(time.perf_counter() - started)
            return trace

        # 预热（lancedb / jieba / ONNX 会话的首次成本不计入延迟统计）。
        search("warmup")

        golden_report = run_golden(
            cases,
            search,
            golden=str(golden),
            repo=str(repo),
            project_id=handle.project_id,
            max_tokens=MAX_TOKENS,
        )
        write_report(golden_report, raw_path)
        search_latency = [value for value in latencies[1:]]  # 去掉 warmup
        query_embed = _query_embed_latency(provider, cases)

        overall = golden_report.overall
        eval_payload = {
            "key": key,
            "command": _command_line(),
            "generated_at": _now(),
            "machine": machine,
            "repo": {"name": repo_name, "path": str(repo), "project_id": handle.project_id},
            "golden": str(golden),
            "data_root": str(data_root),
            "raw_report": str(raw_path),
            "scope": index_payload["scope"],
            "fingerprint": index_payload["fingerprint"],
            "cases": [
                {
                    "id": result.case.id,
                    "lang": result.case.lang,
                    "category": result.case.category,
                    "is_negative": result.case.is_negative,
                    "rank": result.rank,
                    "answerable": result.answerable,
                    "degraded": result.degraded,
                    "error": result.error,
                }
                for result in golden_report.results
            ],
            "metrics": {
                "positives": overall.count,
                "recall_at_5": round(overall.recall_at_5, 4),
                "recall_at_10": round(overall.recall_at_10, 4),
                "mrr": round(overall.mrr, 4),
                "negative_pass": golden_report.negative_pass_count,
                "negative_total": len(golden_report.negatives),
                "degraded_cases": golden_report.degraded_count,
                "by_lang": {
                    name: _metrics_payload(metrics)
                    for name, metrics in golden_report.by("lang").items()
                },
                "by_category": {
                    name: _metrics_payload(metrics)
                    for name, metrics in golden_report.by("category").items()
                },
            },
            "latency": {
                "search_count": len(search_latency),
                "search_p50": round(_percentile(search_latency, 0.50), 4),
                "search_p95": round(_percentile(search_latency, 0.95), 4),
                "search_mean": round(statistics.fmean(search_latency), 4)
                if search_latency
                else 0.0,
                "search_max": round(max(search_latency), 4) if search_latency else 0.0,
                "query_embed": query_embed,
            },
        }
        _json_dump(eval_path, eval_payload)
        print(
            f"[run] {key} @ {repo_name}: r@5={overall.recall_at_5:.3f} "
            f"r@10={overall.recall_at_10:.3f} MRR={overall.mrr:.3f} "
            f"neg={golden_report.negative_pass_count}/{len(golden_report.negatives)} "
            f"search_p50={eval_payload['latency']['search_p50']:.3f}s"
        )
    return 0


def _metrics_payload(metrics: Any) -> dict[str, Any]:
    return {
        "count": metrics.count,
        "recall_at_5": round(metrics.recall_at_5, 4),
        "recall_at_10": round(metrics.recall_at_10, 4),
        "mrr": round(metrics.mrr, 4),
    }


def _query_embed_latency(provider: Any, cases: Iterable[Any]) -> dict[str, Any]:
    """查询侧嵌入微基准（采样若干 golden query，各重复若干次）。"""
    queries: list[str] = []
    for case in cases:
        if not case.is_negative:
            queries.append(case.query)
        if len(queries) >= QUERY_EMBED_SAMPLES:
            break
    samples: list[float] = []
    provider.ensure_loaded()
    for query in queries:
        for _ in range(QUERY_EMBED_REPEATS):
            started = time.perf_counter()
            provider.embed_query([query])
            samples.append(time.perf_counter() - started)
    return {
        "queries": len(queries),
        "repeats": QUERY_EMBED_REPEATS,
        "count": len(samples),
        "p50": round(_percentile(samples, 0.50), 4),
        "p95": round(_percentile(samples, 0.95), 4),
        "mean": round(statistics.fmean(samples), 4) if samples else 0.0,
    }


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------


def _load_artifacts(results_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    indexes = [
        payload
        for path in sorted(results_dir.glob("*.index.json"))
        if (payload := _json_load(path))
    ]
    evals = [
        payload for path in sorted(results_dir.glob("*.eval.json")) if (payload := _json_load(path))
    ]
    return indexes, evals


def _merged_metrics(evals: Sequence[dict[str, Any]], *, key: str | None = None) -> dict[str, Any]:
    """把多仓库的用例级结果合并成一组指标（正例 recall/MRR + 负例通过）。"""
    selected = [item for item in evals if key is None or item.get("key") == key]
    positives: list[int | None] = []
    negatives: list[bool] = []
    degraded = 0
    for item in selected:
        for case in item.get("cases", []):
            if case.get("error"):
                continue
            if case.get("is_negative"):
                negatives.append(bool(case.get("answerable") is False))
            else:
                positives.append(case.get("rank"))
            degraded += 1 if case.get("degraded") else 0
    hits5 = sum(1 for rank in positives if rank and rank <= 5)
    hits10 = sum(1 for rank in positives if rank and rank <= 10)
    mrr = sum(1.0 / rank for rank in positives if rank) / len(positives) if positives else 0.0
    return {
        "count": len(positives),
        "recall_at_5": round(hits5 / len(positives), 3) if positives else 0.0,
        "recall_at_10": round(hits10 / len(positives), 3) if positives else 0.0,
        "mrr": round(mrr, 3),
        "negative_pass": sum(1 for passed in negatives if passed),
        "negative_total": len(negatives),
        "degraded": degraded,
    }


def _by_group(evals: Sequence[dict[str, Any]], field_name: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[int | None]] = {}
    for item in evals:
        for case in item.get("cases", []):
            if case.get("is_negative") or case.get("error"):
                continue
            groups.setdefault(str(case.get(field_name)), []).append(case.get("rank"))
    result: dict[str, dict[str, Any]] = {}
    for name, ranks in sorted(groups.items()):
        hits5 = sum(1 for rank in ranks if rank and rank <= 5)
        hits10 = sum(1 for rank in ranks if rank and rank <= 10)
        result[name] = {
            "count": len(ranks),
            "recall_at_5": round(hits5 / len(ranks), 3) if ranks else 0.0,
            "recall_at_10": round(hits10 / len(ranks), 3) if ranks else 0.0,
            "mrr": round(sum(1.0 / rank for rank in ranks if rank) / len(ranks), 3)
            if ranks
            else 0.0,
        }
    return result


def _models_meta(results_dir: Path) -> dict[str, dict[str, Any]]:
    meta: dict[str, dict[str, Any]] = {}
    for path in sorted(results_dir.glob("*.model.json")):
        payload = _json_load(path)
        if payload:
            meta[payload["slug"]] = payload
    return meta


def cmd_aggregate(args: argparse.Namespace) -> int:
    results_dir = Path(args.results_dir).expanduser()
    indexes, evals = _load_artifacts(results_dir)
    if not indexes and not evals:
        raise SystemExit(f"结果目录里没有 JSON 产物：{results_dir}")
    models = _models_meta(results_dir)
    keys = sorted({item["key"] for item in [*indexes, *evals]})

    lines: list[str] = []
    lines.append("# TASK-015A bake-off 原始数字（脚本产出，勿手改）")
    lines.append("")
    machine = (indexes[0] if indexes else evals[0]).get("machine", {})
    lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 结果目录：`{results_dir}`")
    lines.append(
        f"- 机器：{machine.get('cpu_model', '?')}｜{machine.get('cpu_count', '?')} 逻辑核｜"
        f"内存 {machine.get('mem_total_kb', 0) / 1024 / 1024:.1f} GiB｜"
        f"onnxruntime {machine.get('onnxruntime', '?')}"
    )
    lines.append(f"- 检索预算 maxTokens={MAX_TOKENS}（与 `nova-core eval` 默认一致）")
    lines.append("")

    # 模型文件
    lines.append("## 模型文件与下载")
    lines.append("")
    lines.append("| slug | repo | onnx | dim | pooling | 文件体积 | 下载耗时 | 缓存目录 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for candidate in CANDIDATES:
        payload = models.get(candidate.slug)
        if payload:
            size = payload["total_bytes"] / 1e6
            seconds = payload["total_download_seconds"]
            cache = payload["cache_dir"]
        else:
            size, seconds, cache = 0.0, 0.0, "-"
        lines.append(
            f"| `{candidate.slug}` | {candidate.repo_id} | `{candidate.onnx_file}` "
            f"| {candidate.dim} | {candidate.pooling} | {size:.1f} MB "
            f"| {seconds:.1f} s | `{cache}` |"
        )
    lines.append("")

    # 索引代价
    lines.append("## 索引代价（每模型 × 仓库）")
    lines.append("")
    lines.append(
        "| key | repo | commit | 索引范围文件数 | 范围摘要 | 索引耗时 s | 嵌入耗时 s | chunks "
        "| 嵌入吞吐 chunk/s | 向量库 MB | 字节/chunk | index.db MB | 项目总计 MB |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for item in sorted(indexes, key=lambda value: (value["key"], value["repo"]["name"])):
        embed = item["embed"]
        chunks = item["project_stats"]["chunks"] or 1
        disk = item["disk"]
        tokens = embed.get("tokens", {})
        over = tokens.get("over", {})
        lines.append(
            f"| `{item['key']}` | {item['repo']['name']} | `{item['repo']['commit'][:8]}` "
            f"| {item['scope']['files']} | `{item['scope']['digest'][:8]}` "
            f"| {item['index_seconds']:.1f} | {embed['seconds']['passage']:.1f} "
            f"| {item['project_stats']['chunks']} "
            f"| {embed['throughput_per_s']['passage']:.2f} "
            f"| {disk['vectors_bytes'] / 1e6:.1f} | {disk['vectors_bytes'] / chunks:.0f} "
            f"| {disk['index_db_bytes'] / 1e6:.1f} | {disk['project_bytes'] / 1e6:.1f} |"
        )
        if over:
            lines.append(
                f"| ↳ token 分布 |  |  |  |  |  |  |  |  |  |  |  | "
                f"p50={tokens.get('p50')} p90={tokens.get('p90')} p99={tokens.get('p99')} "
                f"max={tokens.get('max')}（>512: {over.get('512')}，>2048: {over.get('2048')}） |"
            )
    lines.append("")

    # 质量
    lines.append("## 检索质量（每模型 × 仓库，② e2e 装填序口径）")
    lines.append("")
    lines.append(
        "| key | repo | 正例 | recall@5 | recall@10 | MRR | 负例通过 "
        "| 向量降级 | 查询 P50 s | 查询 P95 s | 查询嵌入 P50 s |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for item in sorted(evals, key=lambda value: (value["key"], value["repo"]["name"])):
        metrics = item["metrics"]
        latency = item["latency"]
        lines.append(
            f"| `{item['key']}` | {item['repo']['name']} | {metrics['positives']} "
            f"| {metrics['recall_at_5']:.3f} | {metrics['recall_at_10']:.3f} "
            f"| {metrics['mrr']:.3f} "
            f"| {metrics['negative_pass']}/{metrics['negative_total']} "
            f"| {metrics['degraded_cases']} | {latency['search_p50']:.3f} "
            f"| {latency['search_p95']:.3f} | {latency['query_embed']['p50']:.3f} |"
        )
    lines.append("")

    # 合并
    lines.append("## 三仓库合并（按 key）")
    lines.append("")
    lines.append("| key | 正例 | recall@5 | recall@10 | MRR | 负例通过 | 向量降级 |")
    lines.append("|---|---|---|---|---|---|---|")
    for key in keys:
        merged = _merged_metrics(evals, key=key)
        if not merged["count"] and not merged["negative_total"]:
            continue
        lines.append(
            f"| `{key}` | {merged['count']} | {merged['recall_at_5']:.3f} "
            f"| {merged['recall_at_10']:.3f} | {merged['mrr']:.3f} "
            f"| {merged['negative_pass']}/{merged['negative_total']} | {merged['degraded']} |"
        )
    lines.append("")

    for field_name, title in (("lang", "按语言（合并）"), ("category", "按类别（合并）")):
        lines.append(f"### {title}")
        lines.append("")
        groups = {}
        for key in keys:
            groups[key] = _by_group([item for item in evals if item["key"] == key], field_name)
        names = sorted({name for value in groups.values() for name in value})
        lines.append("| 分组 | " + " | ".join(f"`{key}`" for key in keys) + " |")
        lines.append("|---" * (len(keys) + 1) + "|")
        for name in names:
            cells = []
            for key in keys:
                metrics = groups[key].get(name)
                cells.append(
                    f"{metrics['count']}/{metrics['recall_at_5']:.3f}/"
                    f"{metrics['recall_at_10']:.3f}/{metrics['mrr']:.3f}"
                    if metrics
                    else "-"
                )
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
        lines.append("")
        lines.append("（单元格 = 正例数 / recall@5 / recall@10 / MRR）")
        lines.append("")

    # 逐模型胜负（同 key 组内比较）
    lines.append("## 逐用例名次对比（同仓库同 key）")
    lines.append("")
    for key in keys:
        lines.append(f"### `{key}`")
        lines.append("")
        per_repo = {}
        for item in evals:
            if item["key"] != key:
                continue
            per_repo[item["repo"]["name"]] = {
                case["id"]: case.get("rank") for case in item["cases"]
            }
        for repo_name, ranks in sorted(per_repo.items()):
            hits = [value for value in ranks.values() if value]
            lines.append(
                f"- {repo_name}: {len(ranks)} 条，命中 {len(hits)} 条，"
                f"未命中 {len(ranks) - len(hits)} 条"
            )
        lines.append("")

    report = Path(args.report).expanduser()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nraw report: {report}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """两个 key 的逐用例名次对比（截断 A/B 用）。"""
    results_dir = Path(args.results_dir).expanduser()
    _, evals = _load_artifacts(results_dir)
    left = [item for item in evals if item["key"] == args.left]
    right = [item for item in evals if item["key"] == args.right]
    if not left or not right:
        raise SystemExit(f"缺少产物：{args.left} / {args.right}（在 {results_dir}）")

    def ranks(items: Sequence[dict[str, Any]]) -> dict[str, int | None]:
        return {case["id"]: case.get("rank") for item in items for case in item["cases"]}

    left_ranks, right_ranks = ranks(left), ranks(right)
    lines = [
        f"# 逐用例名次对比：`{args.left}` → `{args.right}`",
        "",
        f"- 左：{_merged_metrics(left)}",
        f"- 右：{_merged_metrics(right)}",
        "",
        "| id | 左名次 | 右名次 | 变化 |",
        "|---|---|---|---|",
    ]
    changed = 0
    for case_id in sorted(left_ranks):
        before, after = left_ranks[case_id], right_ranks.get(case_id)
        if before != after:
            changed += 1
            lines.append(
                f"| {case_id} | {before if before else '-'} | {after if after else '-'} | 变化 |"
            )
    lines.append("")
    lines.append(f"名次变化用例数：{changed} / {len(left_ranks)}")
    print("\n".join(lines))
    if args.report:
        target = Path(args.report).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nwritten: {target}")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    """删除某个 key 的数据根（只删本脚本自己的数据根基目录下的内容）。"""
    base = Path(args.data_root_base).expanduser().resolve()
    target = (base / args.key).resolve()
    if base not in target.parents:
        raise SystemExit(f"拒绝删除数据根之外的路径：{target}")
    if target.exists():
        shutil.rmtree(target)
        print(f"removed: {target}")
    else:
        print(f"not found: {target}")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embed_compare.py",
        description="TASK-015A embedding bake-off（只比模型，不改任何排序/装填参数）",
    )
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="{list,fetch,run,aggregate,compare,clean}"
    )

    def add_paths(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--cache-dir",
            default=str(DEFAULT_CACHE_DIR),
            help=f"模型缓存目录（默认 {DEFAULT_CACHE_DIR}）",
        )
        target.add_argument(
            "--results-dir",
            default=str(DEFAULT_RESULTS_DIR),
            help=f"中间结果 JSON 目录（默认 {DEFAULT_RESULTS_DIR}，故意在仓库外）",
        )

    listing = sub.add_parser("list", help="列出候选模型与缓存命中情况")
    listing.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    listing.set_defaults(handler=cmd_list)

    fetch = sub.add_parser("fetch", help="预下载模型文件（记录体积与耗时）")
    add_paths(fetch)
    fetch.add_argument("--model", default="all", help="slug / 逗号分隔的 slug 列表 / all")
    fetch.set_defaults(handler=cmd_fetch)

    run = sub.add_parser("run", help="建索引 + 跑 golden（幂等，可断点续跑）")
    add_paths(run)
    run.add_argument("--model", required=True)
    run.add_argument("--repo", required=True)
    run.add_argument("--repo-name", default=None, help="报告中的仓库名（默认取目录名）")
    run.add_argument("--golden", required=True)
    run.add_argument("--data-root-base", default=str(DEFAULT_DATA_ROOT_BASE))
    run.add_argument(
        "--max-input-tokens", type=int, default=None, help="覆盖候选的默认截断值（A2 截断 A/B 用）"
    )
    run.add_argument("--batch-size", type=int, default=16)
    run.add_argument("--force", action="store_true", help="忽略已有产物，全部重跑")
    run.add_argument("--force-ingest", action="store_true", help="只强制重建索引")
    run.add_argument("--skip-eval", action="store_true", help="只建索引")
    run.add_argument(
        "--no-measure-tokens",
        action="store_true",
        help="关闭 token 长度分布统计（省一次分词的耗时）",
    )
    run.set_defaults(handler=cmd_run)

    aggregate = sub.add_parser("aggregate", help="汇总 JSON → Markdown 原始数字")
    aggregate.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    aggregate.add_argument("--report", required=True)
    aggregate.set_defaults(handler=cmd_aggregate)

    compare = sub.add_parser("compare", help="两个 key 的逐用例名次对比")
    compare.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR))
    compare.add_argument("--left", required=True)
    compare.add_argument("--right", required=True)
    compare.add_argument("--report", default=None)
    compare.set_defaults(handler=cmd_compare)

    clean = sub.add_parser("clean", help="删除某个 key 的数据根")
    clean.add_argument("--data-root-base", default=str(DEFAULT_DATA_ROOT_BASE))
    clean.add_argument("--key", required=True)
    clean.set_defaults(handler=cmd_clean)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if str(ROOT / "core") not in sys.path:
        sys.path.insert(0, str(ROOT / "core"))
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
