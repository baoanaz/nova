"""参数敏感性扫描（ablation）：离线重算，回答"这个参数到底值不值得调"。

**为什么需要它**：Module/02 §7 与 R29/R30 都写着"rerank 分值/配额需 benchmark 校准"，但
**没有任何脚本回答过"这些参数在当前测试集上到底有没有信号"**。本脚本做的就是这件事：
对每个参数取一组候选值，量出 ΔMRR；**若 Δ 落在噪声内，那这个参数现在就不该调**
（基于 105 条靶场拟合出来的值，换一批题就会反向）。

用法（仓库根，先加载 benchmark.env）：

    set -a; source ~/.config/nova/benchmark.env; set +a
    export no_proxy='*'
    uv run python benches/param_sweep.py            # 全量扫描
    uv run python benches/param_sweep.py --axis score_ratio

**与 `ratio_bench.py` 的分工**：那个是"改一个参数、看靶场分"的**调参**工具（只覆盖
`CONTEXT_SCORE_RATIO`）；本脚本是"问参数有没有信号"的**判定**工具，覆盖 rerank 权重 /
配额 / 图扩展上限等多个轴，输出 Δ 而非绝对值。两者口径一致（同一 `first_hit_rank`）。

口径与可信度（重要）：

- 命中判定复用 `nova_core.cli.eval`，指标与 `benches/run.py` **逐位一致**——脚本自带
  `--verify` 会断言离线复算的基线等于官方四靶场报告的合并值，不符即报错（防止"离线口径悄悄漂移"）。
- embedding 只算一次（真实调用），之后每个配置**用 `copy.deepcopy` 的干净候选重算**。

**为什么必须 deepcopy**（踩过的坑，勿删）：`expand` / `rerank` / `assemble` 会**就地改写**
候选对象（`reasons` 只追加、`score` 直接覆盖）。实测同一批候选进 `assemble` 前后
`reasons` 从 262 条涨到 389 条；**复用被改过的对象跑第二组配置，`used_tokens` 从 5841 变成
5830**——于是每一组配置都得到同一个假的 Δ。这种污染很隐蔽（数字看着完全正常）。
`ratio_bench.py` 不受影响：它每个参数值都重新跑 `search_with_trace`（召回侧产出新对象）。

局限（诚实声明）：

- 只覆盖**候选池之后**；改解析/切片/召回通道本身必须重建索引；
- 105 条正例（4 靶场）下，**单条用例的排名变化即可移动 MRR 约 0.002**，因此
  |ΔMRR| < 0.005 一律按噪声处理，不当作改进；
- 靶场是**人工出题**，不代表真实分布（这正是 TASK-093 要补的）。
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from nova_core.cli.eval import first_hit_rank, load_cases, ordered_evidence  # noqa: E402
from nova_core.contextpack import (  # noqa: E402
    MODE_FAST,
    assemble,
    budget_for,
    collect_index_signals,
)
from nova_core.engine import Engine  # noqa: E402
from nova_core.retrieval import RecallLimits, recall  # noqa: E402
from nova_core.retrieval import rerank as rerank_mod  # noqa: E402
from nova_core.retrieval.expand import ExpansionLimits, expand  # noqa: E402
from nova_core.retrieval.gap import GapLimits  # noqa: E402
from nova_core.retrieval.qcache import PersistentQueryVectorCache  # noqa: E402
from nova_core.retrieval.rerank import RerankWeights, collect_signals, rerank  # noqa: E402

#: 靶场（name, golden 目录, project_id）。含 internal 的 cockpit——它是本卡两个真实失败
#: 用例的来源，缺它则样本只剩 57 条。
TARGETS: tuple[tuple[str, str, str], ...] = (
    ("cockpit", "benches/golden/cockpit-agents-py", "8e69da62f37e5783"),
    ("leveldb", "benches/golden/leveldb", "3ed886ce58bc0e47"),
    ("HelloAgents", "benches/golden/HelloAgents", "06078cc80c7ce7d7"),
    ("langchain", "benches/golden/langchain", "ca2050db0db5b1e2"),
)

#: 官方四靶场的合并基线（实测于 `main @ 20346f8`，`company-wsl`）。
#: 正例数 36 / 19 / 19 / 31 = 105。
#: `--verify` 用它守住"离线复算 = 官方口径"。
#:
#: **必须配合侧车缓存**：provider 的 query 向量不是逐位可复现的（实测 `api:voyage-4-lite`
#: 165 条 query 跨进程只有 65 条逐位相同，最大绝对差 5.6e-3），不缓存则 R@5 在
#: 0.8857 / 0.8952 之间摆动，超过下面的 0.002 容差。本值是在侧车文件上测得的。
#:
#: **口径变更史（勿删，否则下次漂移又无人察觉）**：
#: - `main @ 5b60fc4`：`(0.892, 0.925, 0.684, 93)`，langchain 20 条；
#: - `6836deb` / `7167388` / `83d877a` 三次提交把 langchain 扩到 26 / 29 / 32 条，
#:   正例数 93 → 105，MRR 0.6844 → 0.6678（−0.0166，超噪声阈值）；
#: - 2026-09-17 更新为此值，并在同一轮复核中发现 `vector_rank_top=0` 的信号
#:   从 −0.0334 衰减到 −0.0138（见 `docs/core-architecture-runtime-review-assessment.md` §2）。
OFFICIAL_BASELINE = (0.8857, 0.9238, 0.6678, 105)

#: 噪声上限：105 条正例下改 1 条用例的排名即可动 ~0.002 MRR。
NOISE = 0.005

#: `--verify` 的基线容差。
#:
#: **为什么不是 0.002**：那是理论上的“1 条用例排名变化”，但 provider 本身的
#: query 向量噪声就能造成多条用例各掉一位——实测同一批 query 重新预热一次，
#: 合并 R@5 在 0.8857 / 0.8952 之间摆动（差 0.0095）。容差比 provider 噪底还紧，
#: 自检就会变成随机的假警报（这正是 2026-09-17 复核时发现 --verify 间歇性失败的原因）。
#: 侧车缓存能把**同机**跑分锁成逐位一致，但换机器重新预热仍然会落到噪声带的另一头，
#: 因此容差必须高于噪底。
VERIFY_TOLERANCE = 0.012


def default_data_root() -> str:
    """索引根：`NOVA_BENCH_DATA` 优先，否则用约定位置（复用持久索引，不要重建）。"""
    return os.environ.get("NOVA_BENCH_DATA") or str(
        Path.home() / ".nova" / "bench" / "voyage-4-lite-d1024"
    )


def default_vector_cache(data_root: str) -> Path:
    """侧车文件默认路径：索引根下的 ``query-vectors.json``。"""
    return Path(data_root) / "query-vectors.json"


def precompute(data_root: str, cache: PersistentQueryVectorCache) -> dict[str, list]:
    """真实 embedding 跑一次：每题只做召回，留下候选池供后续任意重算。

    **query 向量必须过侧车缓存**（否则基线不可复现）：实测 `api:voyage-4-lite` 的
    ``embed_query()`` 同一输入会间歇返回**略有差异**的向量（165 条 query 跨进程只有 65 条
    逐位相同，最大绝对差 5.6e-3），足以让个别用例的排名掉一位——同一命令连跑四次，
    合并 R@5 在 0.8857 / 0.8952 之间摆动（3 用例的差），超过 ``--verify`` 的 0.002 容差。
    缓存后命中即逐位复用，抖动消失；未命中才调 provider 并写回。
    """
    pre: dict[str, list] = {}
    bound = False
    for name, golden, project in TARGETS:
        engine = Engine.open(data_root)
        pre[name] = []
        with engine._open_project(project) as (store, vectors, provider):
            if not bound:
                # 侧车自证字段（model/dim）：不绑定则文件里是 null，
                # 将来拿别个模型的向量来跑也无从察觉（qcache 的 identity 校验会退化为空操作）。
                profile = provider.profile
                cache.bind_identity(model=profile.model_id, dim=profile.dim)
                bound = True
            for case in load_cases(ROOT / golden):
                result = recall(
                    store,
                    case.query,
                    provider=provider,
                    vector_store=vectors,
                    limits=RecallLimits(),
                    cache=cache,
                )
                pre[name].append((case, result.candidates))
        print(f"  预计算 {name}: {len(pre[name])} 题", flush=True)
    return pre


def _run_one(
    store,
    engine,
    case,
    candidates,
    *,
    exp_limits: ExpansionLimits,
    weights: RerankWeights | None,
    budget,
    base_scale: float | None,
    gap_on: bool,
):
    """单题：候选 →（expand → rerank → assemble → [gap] → assemble）→ ContextPack。"""
    expansion = expand(store, candidates, limits=exp_limits)
    pool = [*candidates, *expansion.candidates]
    signals = collect_signals(store, case.query, pool)
    original = rerank_mod.RRF_BASE_SCALE
    if base_scale is not None:
        rerank_mod.RRF_BASE_SCALE = base_scale
    try:
        ranked = rerank(pool, signals, weights)
    finally:
        rerank_mod.RRF_BASE_SCALE = original
    pack = assemble(
        store,
        case.query,
        ranked,
        flows=expansion.flows,
        freshness=store.freshness(),
        mode=MODE_FAST,
        config=budget,
        signals=collect_index_signals(store, ranked),
    )
    if gap_on:
        plan, backfill, reranked = engine._backfill_gaps(
            store, case.query, ranked, pack, limits=GapLimits()
        )
        if plan.triggered:
            pack = assemble(
                store,
                case.query,
                reranked,
                flows=expansion.flows,
                freshness=store.freshness(),
                mode=MODE_FAST,
                config=budget,
                signals=collect_index_signals(store, reranked),
                backfill=backfill,
            )
    return pack


def evaluate(
    data_root: str,
    pre: dict[str, list],
    *,
    exp_limits: ExpansionLimits | None = None,
    weights: RerankWeights | None = None,
    budget=None,
    base_scale: float | None = None,
    gap_on: bool = True,
) -> tuple[float, float, float, int]:
    """一组参数 → ``(R@5, R@10, MRR, 正例数)``（每题用 deepcopy 的干净候选）。"""
    hits5 = hits10 = 0
    mrr = 0.0
    total = 0
    for name, _golden, project in TARGETS:
        engine = Engine.open(data_root)
        with engine._open_project(project) as (store, _vectors, _provider):
            for case, candidates in pre[name]:
                if case.is_negative:
                    continue
                pack = _run_one(
                    store,
                    engine,
                    case,
                    copy.deepcopy(candidates),
                    exp_limits=exp_limits or ExpansionLimits(),
                    weights=weights,
                    budget=budget or budget_for(MODE_FAST),
                    base_scale=base_scale,
                    gap_on=gap_on,
                )
                rank = first_hit_rank(ordered_evidence(pack), case, top_k=10)
                total += 1
                if rank:
                    mrr += 1.0 / rank
                if rank and rank <= 5:
                    hits5 += 1
                if rank and rank <= 10:
                    hits10 += 1
    return hits5 / total, hits10 / total, mrr / total, total


def axes() -> list[tuple[str, str, dict]]:
    """扫描轴：(轴名, 人类可读的取值, 覆盖参数)。"""
    fast = budget_for(MODE_FAST)
    return [
        # --- 图扩展：种子数与扩展总量 ---
        ("seeds/expanded", "20/30（基线）", {}),
        ("seeds", "seeds 10", {"exp_limits": ExpansionLimits(seeds=10)}),
        ("seeds", "seeds 40", {"exp_limits": ExpansionLimits(seeds=40)}),
        ("expanded", "expanded 15", {"exp_limits": ExpansionLimits(max_expanded=15)}),
        ("expanded", "expanded 60", {"exp_limits": ExpansionLimits(max_expanded=60)}),
        # --- rerank 基准分缩放 ---
        ("base_scale", "×10", {"base_scale": 10.0}),
        ("base_scale", "×50", {"base_scale": 50.0}),
        ("base_scale", "×100", {"base_scale": 100.0}),
        # --- rerank 特征：语义相关性档位 ---
        (
            "vector_weight",
            "vector_top=0（关）",
            {"weights": replace(RerankWeights(), vector_rank_top=0.0)},
        ),
        (
            "vector_weight",
            "vector_top=0.75",
            {"weights": replace(RerankWeights(), vector_rank_top=0.75)},
        ),
        (
            "vector_weight",
            "vector_top=3.0",
            {"weights": replace(RerankWeights(), vector_rank_top=3.0)},
        ),
        (
            "test_fixture",
            "test 惩罚=0（不抑制）",
            {"weights": replace(RerankWeights(), test_fixture=0.0)},
        ),
        (
            "test_fixture",
            "test 惩罚=-3.0",
            {"weights": replace(RerankWeights(), test_fixture=-3.0)},
        ),
        # --- 装填闸门与配额（R29/R30 冻结区） ---
        ("score_ratio", "0.15", {"budget": replace(fast, score_ratio=0.15)}),
        ("score_ratio", "0.25", {"budget": replace(fast, score_ratio=0.25)}),
        ("score_ratio", "0.55", {"budget": replace(fast, score_ratio=0.55)}),
        ("docs_ratio", "0.05", {"budget": replace(fast, docs_ratio=0.05)}),
        ("docs_ratio", "0.20", {"budget": replace(fast, docs_ratio=0.20)}),
        ("tier3_ratio", "0.45", {"budget": replace(fast, tier3_ratio=0.45)}),
        ("single_file_ratio", "0.40", {"budget": replace(fast, single_file_ratio=0.40)}),
        # --- Repair 回路本身 ---
        ("gap", "Repair 关闭", {"gap_on": False}),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default=default_data_root(), help="索引根（默认复用持久索引）")
    ap.add_argument("--axis", default=None, help="只跑某个轴（默认全跑）")
    ap.add_argument("--verify", action="store_true", help="只校验离线复算 = 官方基线")
    ap.add_argument(
        "--vector-cache",
        type=Path,
        default=None,
        help=(
            "query 向量侧车文件（默认 <索引根>/query-vectors.json）。**必须持久化**："
            "provider 的 query 向量不是逐位可复现的，不缓存则基线会随机漂移，"
            "--verify 将间歇性失败。首次跑会联网预热并写回，之后完全离线。"
        ),
    )
    args = ap.parse_args()

    if not (Path(args.data) / "projects").is_dir():
        print(f"索引根不可用：{args.data}（复用持久索引，不要重建）", file=sys.stderr)
        return 2

    print(f"索引根：{args.data}")
    print("预计算召回（真实 embedding，只做一次）…", flush=True)
    cache_path = args.vector_cache or default_vector_cache(args.data)
    cache = PersistentQueryVectorCache(cache_path)  # __init__ 内部已 load
    print(f"query 向量侧车：{cache_path}（已有 {len(cache)} 条）", flush=True)
    pre = precompute(args.data, cache)
    written = cache.put_all()
    print(f"query 向量侧车已落盘：{written} 条", flush=True)

    base = evaluate(args.data, pre)
    print(
        f"\n基线（当前全部参数）：R@5={base[0]:.3f} R@10={base[1]:.3f} "
        f"MRR={base[2]:.4f}  n={base[3]}"
    )
    expected = OFFICIAL_BASELINE
    drift = max(abs(base[0] - expected[0]), abs(base[2] - expected[2]))
    if drift > VERIFY_TOLERANCE:
        print(
            f"!! 离线复算与官方基线不符：得到 {base[:3]}，期望 {expected[:3]}。\n"
            "   说明离线口径已漂移或索引/代码已变——本脚本的结论此时不可用。",
            file=sys.stderr,
        )
        return 1
    print("   与官方四靶场报告一致（离线口径未漂移）")
    if args.verify:
        return 0

    # 确定性自检：同一配置连跑两次必须逐位一致（防 deepcopy 漏掉的污染）。
    again = evaluate(args.data, pre)
    if again != base:
        print(f"!! 非确定性：{base} vs {again}", file=sys.stderr)
        return 1
    print("   确定性自检通过（重复测量逐位一致）\n")

    print(f"{'轴':18}{'取值':24}{'R@5':>7}{'R@10':>7}{'MRR':>8}{'ΔMRR':>9}  判定")
    print("-" * 84)
    rows = [r for r in axes() if args.axis is None or r[0] == args.axis]
    for axis, label, overrides in rows:
        result = evaluate(args.data, pre, **overrides)
        delta = result[2] - base[2]
        if axis == "seeds/expanded":
            verdict = "（基线）"
        elif abs(delta) < NOISE:
            verdict = "噪声内，不值得调"
        elif delta > 0:
            verdict = "微正（<1% 提升，需更多数据确认）"
        else:
            verdict = "变差"
        print(
            f"{axis:18}{label:24}{result[0]:7.3f}{result[1]:7.3f}"
            f"{result[2]:8.4f}{delta:+9.4f}  {verdict}"
        )
    print(
        f"\n判读纪律：{OFFICIAL_BASELINE[3]} 条正例下改 1 条用例的排名即可动约 0.002 MRR；"
        f"|ΔMRR| < {NOISE} 一律按噪声处理（见模块 docstring 与 "
        "benches/results/param-sensitivity-2026-09-15.md）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
