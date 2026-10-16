"""把一次运行的逐题结果汇总成 Markdown 报告。"""

from __future__ import annotations

import statistics
from collections import defaultdict


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct * (len(ordered) - 1))))]


def summarize(results: list[dict]) -> dict[tuple[str, str], dict]:
    """按 (仓库, 工具) 汇总。"""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in results:
        groups[(item["repo"], item["tool"])].append(item)
    out: dict[tuple[str, str], dict] = {}
    for key, items in groups.items():
        scored = [i for i in items if i.get("score")]
        latency = [i["ms"] for i in items]
        row = {
            "n": len(items),
            "errors": sum(1 for i in items if i.get("error")),
            "p50": statistics.median(latency) if latency else 0.0,
            "p95": _percentile(latency, 0.95),
        }
        if key[1] == "search":
            row["hit5"] = sum(i["score"]["hit5"] for i in scored)
            row["hit10"] = sum(i["score"]["hit10"] for i in scored)
            row["mrr"] = statistics.mean(i["score"]["mrr"] for i in scored) if scored else 0.0
            row["recall10"] = (
                statistics.mean(i["score"]["recall10"] for i in scored) if scored else 0.0
            )
        else:
            row["passed"] = sum(i["score"]["passed"] for i in scored)
            row["answered"] = sum(i["score"]["status"] == "answered" for i in scored)
            row["fact_cov"] = (
                statistics.mean(i["score"]["fact_cov"] for i in scored) if scored else 0.0
            )
            row["cite_cov"] = (
                statistics.mean(i["score"]["cite_cov"] for i in scored) if scored else 0.0
            )
        out[key] = row
    return out


def _rank_text(score: dict) -> str:
    return ",".join("-" if r is None else str(r) for r in score["ranks"])


def render(meta: dict, results: list[dict], index_rows: list[dict] | None = None) -> str:
    summary = summarize(results)
    lines = [f"# NOVA benchmark 报告 · {meta['started']}", ""]
    lines += [f"- 服务：{meta['base']}　题数：{len(results)}　用时：{meta['seconds']:.0f}s", ""]

    if index_rows:
        lines += ["## 索引", "", "| 靶场 | 语言 | 文件 | chunks | 耗时 |", "|---|---|---:|---:|---:|"]
        for r in index_rows:
            lines.append(
                f"| {r['name']} | {r['language']} | {r['files']} | {r['chunks']} | {r['seconds']:.0f}s |"
            )
        lines.append("")

    lines += [
        "## search（期望依据在证据列表里的名次）",
        "",
        "| 仓库 | 题数 | 命中@5 | 命中@10 | MRR | recall@10 | p50 | p95 | 错误 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for (repo, tool), row in sorted(summary.items()):
        if tool != "search":
            continue
        lines.append(
            f"| {repo} | {row['n']} | {row['hit5']}/{row['n']} | {row['hit10']}/{row['n']} "
            f"| {row['mrr']:.3f} | {row['recall10']:.2f} | {row['p50']:.0f}ms | {row['p95']:.0f}ms "
            f"| {row['errors']} |"
        )
    lines += [
        "",
        "## ask（事实覆盖 / 引用覆盖，自动粗筛）",
        "",
        "| 仓库 | 题数 | 通过 | 已回答 | 事实覆盖 | 引用覆盖 | p50 | p95 | 错误 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for (repo, tool), row in sorted(summary.items()):
        if tool != "ask":
            continue
        lines.append(
            f"| {repo} | {row['n']} | {row['passed']}/{row['n']} | {row['answered']}/{row['n']} "
            f"| {row['fact_cov']:.2f} | {row['cite_cov']:.2f} | {row['p50']:.0f}ms "
            f"| {row['p95']:.0f}ms | {row['errors']} |"
        )

    lines += ["", "## 逐题", "", "| 题 | 工具 | 结果 | 详情 | 耗时 |", "|---|---|---|---|---:|"]
    for item in results:
        if item.get("error"):
            verdict, detail = "错误", item["error"][:80]
        elif item["tool"] == "search":
            s = item["score"]
            verdict = "命中" if s["hit10"] else "未命中"
            detail = f"名次 {_rank_text(s)}"
        else:
            s = item["score"]
            verdict = "通过" if s["passed"] else "未通过"
            detail = f"{s['status']} · 事实 {s['fact_cov']:.2f} · 引用 {s['cite_cov']:.2f}"
        lines.append(f"| {item['id']} | {item['tool']} | {verdict} | {detail} | {item['ms']:.0f}ms |")
    lines.append("")
    return "\n".join(lines)
