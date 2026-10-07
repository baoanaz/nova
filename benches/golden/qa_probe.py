#!/usr/bin/env python
"""按题库里**标注的工具**跑分：`search` 走检索，`ask` 走 service 的 grounded 总结。

为什么单独一个脚本：`nova-core eval` 只覆盖 search（检索命中率）；
而题库里有一批题标注为 `ask`（需要跨文件综合解释），必须走
`service/nova_service/answer.py` 的真实 LLM 路径才测得准。判定口径：

- `search` 组：复用 core 的 `first_hit_rank` —— 期望路径出现在**装填顺序** top-k 内即命中；
- `ask` 组：同一条 query 先检索再喂给 LLM，分开记录：
  - `pack_rank`：任一期望证据是否进入包；
  - `pack_expected_coverage` / `pack_complete`：多文件题的期望证据是否完整；
  - `answer_cites_expected`：答案引用是否落到期望证据；
  - `answer_hit`：仅为 v1 兼容，表示正文是否写出期望路径，**不等于答案正确率**。

用法：
    uv run python benches/golden/qa_probe.py \
        --golden benches/golden/langchain/langchain.jsonl \
        --project-id ca2050db0db5b1e2 \
        --data /root/.nova/bench/voyage-4-lite-d1024 \
        --out benches/results/raw/qa-probe/langchain.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "service"))

from nova_core.cli.eval import (  # noqa: E402
    first_hit_rank,
    load_cases,
    ordered_evidence,
    run_golden,
)
from nova_core.embedding.factory import EmbeddingConfig, create_provider  # noqa: E402
from nova_core.engine import Engine  # noqa: E402
from nova_service.answer import AnswerError, answer_question, build_provider  # noqa: E402
from nova_service.config import Settings  # noqa: E402

_CITATION_RE = re.compile(r"\[([EF]\d+)\]")


def metadata_of(golden: Path) -> dict[str, dict[str, str]]:
    """读取 core GoldenCase 尚未承载的 benchmark 扩展字段。"""
    out: dict[str, dict[str, str]] = {}
    for line in golden.read_text(encoding="utf-8").splitlines():
        if line.strip():
            payload = json.loads(line)
            out[str(payload["id"])] = {
                "tool": str(payload.get("tool") or "search"),
                "expected_mode": str(payload.get("expected_mode") or "any"),
            }
    return out


def expected_ranks(items, case, *, top_k: int) -> list[int | None]:
    """逐项计算 expected 的首命中排名；多文件 ask 不能只看任意一项。"""
    return [
        first_hit_rank(items, replace(case, expected=(expectation,)), top_k=top_k)
        for expectation in case.expected
    ]


def expected_evidence_ids(items, case) -> set[str]:
    """返回与任一期望 path+symbol 匹配的 E 编号，供答案引用落点检查。"""
    matched: set[str] = set()
    for item in items:
        if first_hit_rank([item], case, top_k=1) is not None:
            matched.add(item.id)
    return matched


def percentile(values: list[float], ratio: float) -> float | None:
    """Nearest-rank percentile；小样本也保持定义明确、结果可复现。"""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * ratio + 0.999999) - 1))
    return round(ordered[index], 1)


def main() -> int:
    ap = argparse.ArgumentParser(description="按题库标注的工具跑分（search / ask）")
    ap.add_argument("--golden", type=Path, required=True)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--report", type=Path, help="可选：输出一段 Markdown 摘要")
    ap.add_argument("--max-tokens", type=int, default=10_000)
    ap.add_argument("--top-k", type=int, default=10)
    args = ap.parse_args()

    cases = load_cases(args.golden)
    metadata = metadata_of(args.golden)
    search_cases = [c for c in cases if metadata.get(c.id, {}).get("tool", "search") == "search"]
    ask_cases = [c for c in cases if metadata.get(c.id, {}).get("tool") == "ask"]

    provider = create_provider(EmbeddingConfig.from_env())
    engine = Engine.open(args.data, provider=provider)
    payload: dict[str, Any] = {
        "schema": 1,
        "golden": str(args.golden),
        "project_id": args.project_id,
        "data_root": str(args.data),
        "counts": {"total": len(cases), "search": len(search_cases), "ask": len(ask_cases)},
    }
    try:
        # ---- tool=search：走 core 的评估判定 ----
        report = run_golden(
            search_cases,
            lambda q: engine.search_with_trace(args.project_id, q, args.max_tokens),
            golden=str(args.golden),
            project_id=args.project_id,
            max_tokens=args.max_tokens,
            top_k=args.top_k,
        )
        overall = report.overall
        payload["search"] = {
            "cases": len(search_cases),
            "recall_at_5": round(overall.recall_at_5, 4),
            "recall_at_10": round(overall.recall_at_10, 4),
            "mrr": round(overall.mrr, 4),
            "negatives_passed": report.negative_pass_count,
            "negatives_total": len(report.negatives),
            "degraded": report.degraded_count,
            "per_case": [
                {
                    "id": r.case.id,
                    "category": r.case.category,
                    "rank": r.rank,
                    "hit": r.hit,
                    "passed": r.passed,
                    "answerable": r.answerable,
                    "top3": list(r.top),
                    "error": r.error,
                }
                for r in report.results
            ],
        }

        # ---- tool=ask：检索 + LLM（与 service 同一条实现） ----
        settings = Settings.from_env()
        answerer = build_provider(settings)
        ask_rows: list[dict[str, Any]] = []
        for case in ask_cases:
            trace = engine.search_with_trace(args.project_id, case.query, args.max_tokens)
            pack = trace.pack
            items = ordered_evidence(pack)
            expected_paths = [e.path for e in case.expected]
            ranks = expected_ranks(items, case, top_k=args.top_k)
            expected_hits = sum(rank is not None for rank in ranks)
            expected_total = len(ranks)
            expected_ids = expected_evidence_ids(items, case)
            expected_mode = metadata.get(case.id, {}).get("expected_mode", "any")
            row: dict[str, Any] = {
                "id": case.id,
                "category": case.category,
                "question": case.query,
                "expected": expected_paths,
                "expected_mode": expected_mode,
                "pack_rank": first_hit_rank(items, case, top_k=args.top_k),
                "pack_expected_ranks": ranks,
                "pack_expected_hits": expected_hits,
                "pack_expected_total": expected_total,
                "pack_expected_coverage": (
                    round(expected_hits / expected_total, 4) if expected_total else None
                ),
                "pack_complete": (
                    expected_hits == expected_total
                    if expected_mode == "all"
                    else expected_hits > 0
                ),
                "pack_answerable": pack.answerable,
                "evidence_count": len(pack.evidence) + len(pack.docs),
            }
            if not pack.answerable:
                row["status"] = "insufficient_evidence"
                row["reason"] = "pack.answerable=false（按 D-24 短路，不调用 LLM）"
            elif answerer is None:
                row["status"] = "degraded"
                row["reason"] = "ANSWER_* 未配置（走 D-26 降级包，不调 LLM）"
            else:
                try:
                    outcome = answer_question(
                        provider=answerer, settings=settings, pack=pack, question=case.query
                    )
                    cited_ids = set(_CITATION_RE.findall(outcome.answer))
                    answer_path_mentioned = any(p and p in outcome.answer for p in expected_paths)
                    row.update(
                        status="answered",
                        # 兼容 v1；它只表示正文是否写了路径，不能当答案正确率。
                        answer_hit=answer_path_mentioned,
                        answer_path_mentioned=answer_path_mentioned,
                        answer_cites_expected=bool(cited_ids & expected_ids),
                        cited_expected_ids=sorted(cited_ids & expected_ids),
                        latency_ms=round(outcome.latency_ms, 1),
                        answer_tokens=outcome.answer_tokens,
                        valid_citations=outcome.valid_citations,
                        invalid_citations=outcome.invalid_citations,
                        citation_coverage=outcome.citation_coverage,
                        answer=outcome.answer,
                    )
                except AnswerError as exc:
                    row.update(status="degraded", reason=f"{type(exc).__name__}: {exc}"[:300])
            row["pack_answerable"] = bool(row["pack_answerable"])
            ask_rows.append(row)
            print(f"  [{case.id}] {row['status']} pack_rank={row.get('pack_rank')} "
                  f"answer_hit={row.get('answer_hit')}", flush=True)
        payload["ask"] = {
            "cases": len(ask_rows),
            "answered": sum(1 for r in ask_rows if r["status"] == "answered"),
            "short_circuited": sum(
                1 for r in ask_rows if r["status"] == "insufficient_evidence"
            ),
            "answer_hits": sum(1 for r in ask_rows if r.get("answer_hit")),
            "pack_hits": sum(1 for r in ask_rows if r.get("pack_rank")),
            "pack_complete": sum(1 for r in ask_rows if r.get("pack_complete")),
            "answers_citing_expected": sum(
                1 for r in ask_rows if r.get("answer_cites_expected")
            ),
            "latency_p50_ms": percentile(
                [float(r["latency_ms"]) for r in ask_rows if "latency_ms" in r], 0.50
            ),
            "latency_p95_ms": percentile(
                [float(r["latency_ms"]) for r in ask_rows if "latency_ms" in r], 0.95
            ),
            "per_case": ask_rows,
        }
    finally:
        try:
            provider.close()
        except Exception:  # noqa: BLE001
            pass

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    s = payload["search"]
    a = payload.get("ask", {})
    print(f"\nsearch：recall@5={s['recall_at_5']} recall@10={s['recall_at_10']} MRR={s['mrr']} "
          f"负例 {s['negatives_passed']}/{s['negatives_total']}")
    if a:
        print(
            f"ask   ：{a['answered']}/{a['cases']} 有答案，"
            f"pack_hit {a['pack_hits']}/{a['cases']}，"
            f"pack_complete {a['pack_complete']}/{a['cases']}，"
            f"引用命中期望证据 {a['answers_citing_expected']}/{a['cases']}，"
            f"路径点名（兼容指标）{a['answer_hits']}/{a['cases']}"
        )
    print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
