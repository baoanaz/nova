"""离线 A/B 对比：改检索/组装参数时，用缓存的 query 向量纯本地重算。

**为什么需要它**：调 `CONTEXT_SCORE_RATIO`、rerank 权重这类参数时，如果每次都
重新调 embedding，一轮要花几十秒且消耗额度。而"参数改动"只影响**检索之后**的
融合/rerank/组装——候选池本身不变。所以：**采集一次候选池与 query 向量，之后
改任意参数都能秒级重算**。

用法（在仓库根，先加载 benchmark.env）：

    set -a; source ~/.config/nova/benchmark.env; set +a
    export no_proxy='*'

    # ① 采集：跑一次真实 embedding，落盘候选池（要 key，约数十秒）
    uv run python benches/ratio_bench.py --collect

    # ② 对比：纯本地重算，不需要 key，秒级
    uv run python benches/ratio_bench.py --eval
    uv run python benches/ratio_bench.py --eval --ratio 0.50,0.40,0.35

判读：指标口径与 `benches/run.py` **逐位一致**（复用 `run_golden`）。
低于 `docs/handbook/benchmark/README.md` §4 的基线即为回退。

局限（诚实声明）：
- 只覆盖**候选池之后**的改动。改了解析/切片/召回通道本身 → 必须重新 `--collect`；
- 缓存按 projectId 存 `/tmp/nova-ab-pool-<hash>.pkl`，重建索引后需重新采集。
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from nova_core.cli.eval import load_cases, run_golden  # noqa: E402
from nova_core.embedding.factory import EmbeddingConfig, create_provider  # noqa: E402
from nova_core.engine import Engine  # noqa: E402
from nova_core.retrieval import RecallLimits  # noqa: E402
from nova_core.retrieval import vector as vector_mod  # noqa: E402
from nova_core.retrieval.bm25 import recall_bm25  # noqa: E402
from nova_core.retrieval.exact import parse_explicit, recall_explicit  # noqa: E402
from nova_core.retrieval.literal import extract_literal_phrases, recall_literal  # noqa: E402
from nova_core.retrieval.vector import embed_query, recall_vector  # noqa: E402

CACHE = Path("/tmp/nova-ab-pool.pkl")


def _bench_targets() -> tuple[str, list[tuple[str, str, str]]]:
    """从 targets.json 读数据根与靶场（不硬编码 projectId）。

    只取 role=primary —— internal 靶场需要内网索引，默认不参与。
    """
    spec = json.loads((ROOT / "benches" / "targets.json").read_text(encoding="utf-8"))
    data = os.environ.get("NOVA_BENCH_DATA") or spec.get("data_root")
    if not data:
        # targets.json 没写就退回约定位置
        data = str(Path.home() / ".nova" / "bench" / "voyage-4-lite-d1024")
    targets = []
    for name, t in spec["targets"].items():
        if t.get("role") != "primary":
            continue
        # 跳过历史遗留登记：靶场被替换后旧条目仍在 targets.json 里（如 hello-agents，
        # R50 记录“保留不删但暂停”）。判据：golden 里没有当前格式的用例或已被其他条目录入。
        if name in _SUPERSEDED:
            continue
        gold = ROOT / t["golden"]
        if not gold.is_dir():
            continue
        jsonl = next(iter(sorted(gold.glob("*.jsonl"))), None)
        if jsonl is None:
            continue
        targets.append((name, t["project_id"], str(jsonl.relative_to(ROOT))))
    return data, sorted(targets)


#: 已被新靶场取代的旧登记（targets.json 里保留但不参与基准）。
#: `hello-agents`（commit 4f7682c）由 `helloagents-v1`（commit 93e77ea）取代，
#: 见 docs/contracts/PROCESS.md R50 与 benches/targets-benchmark.md。
_SUPERSEDED = frozenset({"hello-agents"})


def collect(data_root: str, targets: list[tuple[str, str, str]]) -> None:
    """跑一次真实检索，把四通道候选 + query 向量落盘。"""
    provider = create_provider(EmbeddingConfig.from_env())
    engine = Engine.open(data_root, provider=provider)
    collected: list[dict] = []
    try:
        for name, project, golden in targets:
            cases = load_cases(ROOT / golden)
            with engine._open_project(project) as (store, vectors, prov):
                for case in cases:
                    q = case.query
                    limits = RecallLimits()
                    explicit = parse_explicit(q)
                    literal_phrases = extract_literal_phrases(q, covered=explicit.symbols)
                    channels = {
                        "exact": recall_explicit(store, explicit.symbols, limit=limits.explicit),
                        "literal": recall_literal(store, literal_phrases, limit=limits.literal),
                        "bm25": recall_bm25(store, q, limit=limits.bm25),
                        "vector": recall_vector(prov, vectors, q, limit=limits.vector),
                    }
                    ids = [c.chunk_id for cands in channels.values() for c in cands]
                    meta = {c.id: c for c in store.chunks_by_ids(ids)}
                    collected.append(
                        {
                            "target": name,
                            "project": project,
                            "case": case,
                            "channels": channels,
                            "vector": list(embed_query(prov, q)),
                            "meta": meta,
                        }
                    )
                    print(f"  collected {name} {case.id}", flush=True)
    finally:
        provider.close()
    CACHE.write_bytes(pickle.dumps(collected))
    print(f"wrote {CACHE}（{len(collected)} cases）")


def evaluate(data_root: str, cache: list[dict], ratios: list[float]) -> int:
    """对每个 ratio 用缓存向量重算指标。返回 0。"""
    vec_by_q = {c["case"].query: c["vector"] for c in cache}
    orig_embed = vector_mod.embed_query

    def cached_embed(provider, query, cache=None):  # noqa: ANN001, ARG001
        """替身：命中缓存就返回采集时的向量，否则回退真实实现。

        签名必须与 `nova_core.retrieval.vector.embed_query` 一致（含 `cache` 位置参数），
        否则调用方传第三参时会 TypeError。
        """
        return list(vec_by_q[query]) if query in vec_by_q else orig_embed(provider, query)

    header = f"{'ratio':>6s}{'TOT_r5':>9s}{'TOT_r10':>9s}{'TOT_mrr':>9s}{'neg':>7s}"
    per_target_names = sorted({c["target"] for c in cache})
    for n in per_target_names:
        header += f"{n[:9] + '_r5':>12s}{n[:9] + '_mrr':>12s}"
    print(header)

    for ratio in ratios:
        os.environ["NOVA_CONTEXT_SCORE_RATIO"] = str(ratio)
        provider = create_provider(EmbeddingConfig.from_env())
        engine = Engine.open(data_root, provider=provider)
        tot = {"n": 0, "r5": 0, "r10": 0, "mrr": 0.0, "neg": 0, "neg_ok": 0}
        per: dict[str, tuple[float, float]] = {}
        try:
            vector_mod.embed_query = cached_embed
            # 按靶场分组跑：每个靶场用自己 project 的检索闭包
            by_target: dict[str, list] = {}
            for item in cache:
                by_target.setdefault(item["target"], []).append(item)
            for name, items in by_target.items():
                project = items[0]["project"]
                golden = next(
                    (t[2] for t in _bench_targets()[1] if t[0] == name), None
                )
                # 绑定当前 engine 为默认参数：避免闭包捕获循环变量（ruff B023）。
                rep = run_golden(
                    [i["case"] for i in items],
                    lambda q, _p=project, _e=engine: _e.search_with_trace(_p, q),
                    golden=golden,
                    project_id=project,
                )
                pos = [r for r in rep.results if r.case.expected and not r.case.is_negative]
                neg = [r for r in rep.results if r.case.is_negative]
                if not pos:
                    continue
                r5 = sum(1 for r in pos if r.rank and r.rank <= 5)
                r10 = sum(1 for r in pos if r.rank and r.rank <= 10)
                mrr = sum(1.0 / r.rank for r in pos if r.rank) / len(pos)
                per[name] = (r5 / len(pos), mrr)
                tot["n"] += len(pos)
                tot["r5"] += r5
                tot["r10"] += r10
                tot["mrr"] += mrr * len(pos)
                tot["neg"] += len(neg)
                tot["neg_ok"] += sum(1 for r in neg if not r.answerable)
        finally:
            vector_mod.embed_query = orig_embed
            provider.close()

        row = (
            f"{ratio:>6.2f}{tot['r5'] / tot['n']:>9.3f}{tot['r10'] / tot['n']:>9.3f}"
            f"{tot['mrr'] / tot['n']:>9.3f}{tot['neg_ok']:>4d}/{tot['neg']:<2d}"
        )
        for n in per_target_names:
            r5, mrr = per.get(n, (float("nan"), float("nan")))
            row += f"{r5:>12.3f}{mrr:>12.3f}"
        print(row)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--collect", action="store_true", help="采集候选池（要 key，跑一次）")
    g.add_argument("--eval", action="store_true", help="用缓存重算（不需要 key）")
    ap.add_argument(
        "--ratio",
        default=os.environ.get("NOVA_CONTEXT_SCORE_RATIO", "0.40"),
        help="逗号分隔的 CONTEXT_SCORE_RATIO 列表（默认 0.40，可用 env 覆盖）",
    )
    args = ap.parse_args()

    data_root, targets = _bench_targets()
    if args.collect:
        print(f"data_root={data_root}")
        print("targets:", ", ".join(t[0] for t in targets))
        collect(data_root, targets)
        return 0

    if not CACHE.exists():
        print(
            f"缓存不存在：{CACHE}\n"
            "先跑：uv run python benches/ratio_bench.py --collect",
            file=sys.stderr,
        )
        return 2
    ratios = [float(x) for x in args.ratio.split(",")]
    return evaluate(data_root, pickle.loads(CACHE.read_bytes()), ratios)


if __name__ == "__main__":
    raise SystemExit(main())
