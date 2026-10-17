"""命令行：list / index / run。

    uv run python bench/run.py list
    uv run python bench/run.py index [--repo muduo ...]
    uv run python bench/run.py run [--repo X] [--tool search|ask] [--ids A,B] [--label 说明]

服务由 ``bench/stack.sh up`` 启动（本地模式、无界面）。结果落 ``bench/results/<时间戳>/``。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import report, scoring
from .api import Api, ApiFailure
from .targets import (
    BENCH_DIR,
    BenchError,
    Question,
    Target,
    load_questions,
    load_targets,
    repos_root,
)

DEFAULT_BASE = "http://127.0.0.1:8892"
RESULTS_DIR = BENCH_DIR / "results"
INDEX_TIMEOUT_S = 3 * 3600


def _select_targets(names: list[str] | None) -> list[Target]:
    targets = load_targets()
    chosen = names or list(targets)
    unknown = [n for n in chosen if n not in targets]
    if unknown:
        raise BenchError(f"未知靶场：{unknown}；可选 {list(targets)}")
    return [targets[n] for n in chosen]


def _check_checkout(target: Target) -> Path:
    path = target.path(repos_root())
    if not path.is_dir():
        raise BenchError(f"{target.name}: 靶场目录不存在 {path}，见 docs/handbook/benchmark/targets.md")
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if head != target.commit:
        raise BenchError(f"{target.name}: HEAD={head[:12]} 与清单固定的 {target.commit[:12]} 不一致")
    return path


def cmd_list(_: argparse.Namespace) -> int:
    for target in load_targets().values():
        questions = load_questions(target.name)
        search = sum(q.tool == "search" for q in questions)
        print(
            f"{target.name:10} {target.language:11} {target.commit[:12]}  "
            f"search {search} / ask {len(questions) - search}"
        )
    return 0


def _project_ids(api: Api) -> dict[str, str]:
    """靶场名 → projectId。服务端会给显示名加分支后缀，所以按挂载目录匹配。"""
    projects, _ = api.get("/api/projects")
    by_root = {p.get("attachedRoot"): p["projectId"] for p in projects}
    root = repos_root()
    return {
        name: by_root[str(target.path(root))]
        for name, target in load_targets().items()
        if str(target.path(root)) in by_root
    }


def _attach_and_wait(api: Api, target: Target, *, quiet: bool = False) -> dict:
    """挂载靶场并等索引完成（幂等：已索引则只做增量扫描）。返回索引行。"""
    path = _check_checkout(target)
    started = time.perf_counter()
    attached, _ = api.post("/api/projects/attach", {"root": str(path), "displayName": target.name})
    pid = attached["projectId"]
    if not quiet:
        print(f"[{target.name}] 已接入 {pid}，等待索引…", flush=True)
    last_print = 0.0
    while True:
        project, _ = api.get(f"/api/projects/{pid}")
        progress = project["indexProgress"]
        if progress["state"] in ("done", "failed"):
            break
        if time.perf_counter() - started > INDEX_TIMEOUT_S:
            raise BenchError(f"{target.name}: 索引超时")
        if not quiet and time.perf_counter() - last_print > 15:
            print(f"  {progress['state']} {progress['processedFiles']}/{progress['totalFiles']}", flush=True)
            last_print = time.perf_counter()
        time.sleep(2)
    if progress["state"] == "failed":
        raise BenchError(f"{target.name}: 索引失败：{progress.get('error')}")
    sync = project.get("sync", {})
    return {
        "name": target.name,
        "language": target.language,
        "projectId": pid,
        "files": sync.get("filesIndexed", progress["processedFiles"]),
        "chunks": sync.get("chunks", 0),
        "seconds": time.perf_counter() - started,
    }


def cmd_index(args: argparse.Namespace) -> int:
    api = Api(args.base)
    rows: list[dict] = []
    for target in _select_targets(args.repo):
        row = _attach_and_wait(api, target)
        rows.append(row)
        print(f"[{target.name}] 完成：{row['files']} 文件 / {row['chunks']} chunks / {row['seconds']:.0f}s")
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "last-index.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def _digest(pack: dict | None) -> list[dict]:
    return [
        {k: item.get(k) for k in ("id", "path", "symbol", "lines", "score", "evidence_tier", "reason")}
        for item in scoring.ordered_evidence(pack)
    ]


def _run_one(api: Api, project_id: str, question: Question) -> dict:
    record: dict = {"id": question.id, "repo": question.repo, "tool": question.tool, "query": question.query}
    try:
        if question.tool == "search":
            body = {"projectId": project_id, "query": question.query, "includePack": True}
            response, ms = api.post("/api/query/search", body)
            pack = response["meta"].get("pack")
            record.update(ms=ms, score=scoring.score_search(question, pack).to_json())
            record["evidence"] = _digest(pack)
            record["meta"] = {k: response["meta"].get(k) for k in ("answerable", "confidence", "degraded", "candidateCount", "channelsUsed", "budget", "evidenceCount")}
        else:
            body = {"projectId": project_id, "question": question.query}
            response, ms = api.post("/api/query/ask", body, timeout=300)
            record.update(ms=ms, score=scoring.score_ask(question, response).to_json())
            record["answer"] = response.get("answer", "")
            record["status"] = response.get("status")
            record["evidenceSummary"] = response.get("evidenceSummary", [])
            record["meta"] = {k: response.get("meta", {}).get(k) for k in ("llmModel", "llmLatencyMs", "answerTokens", "citationCoverage")}
    except (ApiFailure, OSError, KeyError) as exc:
        record.update(ms=record.get("ms", 0.0), error=str(exc))
    record.setdefault("ms", 0.0)
    return record


def _line(index: int, total: int, record: dict) -> str:
    if record.get("error"):
        return f"[{index}/{total}] {record['id']:16} 错误 {record['error'][:80]}"
    score = record["score"]
    if record["tool"] == "search":
        verdict = "命中" if score["hit10"] else "未命中"
        detail = f"名次 {','.join('-' if r is None else str(r) for r in score['ranks'])}"
    else:
        verdict = "通过" if score["passed"] else "未通过"
        detail = f"{score['status']} 事实 {score['fact_cov']:.2f} 引用 {score['cite_cov']:.2f}"
    return f"[{index}/{total}] {record['id']:16} {verdict} {detail} {record['ms']:.0f}ms"


def cmd_run(args: argparse.Namespace) -> int:
    api = Api(args.base)
    selected = _select_targets(args.repo)
    # 服务重启后只记得种子仓库的挂载目录：这里统一重新挂载（索引是持久的，只做增量扫描）。
    for target in selected:
        _attach_and_wait(api, target, quiet=True)
    ids = _project_ids(api)
    wanted_ids = set(args.ids.split(",")) if args.ids else None
    plan: list[tuple[str, Question]] = []
    for target in selected:
        if target.name not in ids:
            raise BenchError(f"{target.name}: 服务里没有该项目，请先运行 index")
        for question in load_questions(target.name):
            if args.tool and question.tool != args.tool:
                continue
            if wanted_ids and question.id not in wanted_ids:
                continue
            plan.append((ids[target.name], question))
    if not plan:
        raise BenchError("没有匹配的题目")

    started_at = datetime.now()
    started = time.perf_counter()
    results: list[dict] = []
    for index, (project_id, question) in enumerate(plan, 1):
        record = _run_one(api, project_id, question)
        results.append(record)
        print(_line(index, len(plan), record), flush=True)

    out = RESULTS_DIR / started_at.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    meta = {
        "started": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "base": args.base,
        "label": args.label,
        "seconds": time.perf_counter() - started,
    }
    index_file = RESULTS_DIR / "last-index.json"
    index_rows = json.loads(index_file.read_text(encoding="utf-8")) if index_file.is_file() else None
    (out / "results.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n", encoding="utf-8"
    )
    (out / "report.md").write_text(report.render(meta, results, index_rows), encoding="utf-8")
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告：{out / 'report.md'}")
    return 0


def _load_results(dirs: list[str]) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for name in dirs:
        path = Path(name)
        path = path if path.is_absolute() else RESULTS_DIR / name
        for line in (path / "results.jsonl").read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            merged[record["id"]] = record
    return merged


def _verdict(record: dict | None) -> str:
    if record is None:
        return "—"
    if record.get("error"):
        return "错误"
    score = record["score"]
    if record["tool"] == "search":
        first = score["first_rank"]
        return f"@{first}" if first else "未中"
    return "通过" if score["passed"] else f"{score['status'][:4]}/{score['fact_cov']:.2f}"


def cmd_compare(args: argparse.Namespace) -> int:
    """逐题对比两次运行（目录名可多个，按题 id 合并）。"""
    base, new = _load_results(args.base), _load_results(args.new)
    changed = 0
    print(f"{'题':16} {'基线':>10} {'本次':>10}")
    for qid in sorted(set(base) | set(new)):
        before, after = _verdict(base.get(qid)), _verdict(new.get(qid))
        mark = "" if before == after else "  ← 变化"
        changed += before != after
        print(f"{qid:16} {before:>10} {after:>10}{mark}")
    for label, records in (("基线", base), ("本次", new)):
        summary = report.summarize(list(records.values()))
        search = [v for (_, t), v in summary.items() if t == "search"]
        ask = [v for (_, t), v in summary.items() if t == "ask"]
        n_s, n_a = sum(v["n"] for v in search), sum(v["n"] for v in ask)
        text = f"{label}: search 命中@10 {sum(v['hit10'] for v in search)}/{n_s}"
        if n_s:
            text += f" @5 {sum(v['hit5'] for v in search)}/{n_s}"
        if n_a:
            text += f"；ask 通过 {sum(v['passed'] for v in ask)}/{n_a}"
        print(text)
    print(f"共 {changed} 题有变化")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    """逐个期望依据报告：是否在候选池里、最终名次、命中通道。需要服务在跑（用来取 projectId）。"""
    import os

    from . import diagnose

    diagnose.load_embed_env()
    from nova_core.engine import Engine  # 延后导入：只有诊断才需要 core

    api = Api(args.base)
    selected = _select_targets(args.repo)
    for target in selected:
        _attach_and_wait(api, target, quiet=True)
    ids = _project_ids(api)
    data_root = os.environ.get("NOVA_BENCH_DATA", str(Path.home() / ".nova" / "bench-data"))
    engine = Engine(data_root)
    wanted = set(args.ids.split(",")) if args.ids else None
    for target in selected:
        for question in load_questions(target.name):
            if question.tool != "search" or (wanted and question.id not in wanted):
                continue
            info = diagnose.explain(engine, ids[target.name], question)
            print(f"{question.id}（候选池 {info['pool']}）")
            for row in info["rows"]:
                if row["rank"]:
                    print(f"  ✓ {row['expected']}  名次 {row['rank']}  通道 {row['channels']}")
                else:
                    print(f"  ✗ {row['expected']}  不在候选池；同文件候选 {row['same_path']}")
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    """LLM 审核：随机抽函数出题 → 调真实 search/ask → LLM 打分。需要服务在跑。"""
    import os

    from . import diagnose, judge

    diagnose.load_embed_env()
    from nova_core.engine import Engine

    cfg = judge.load_llm()
    api = Api(args.base)
    selected = _select_targets(args.repo)
    for target in selected:
        _attach_and_wait(api, target, quiet=True)
    ids = _project_ids(api)
    data_root = os.environ.get("NOVA_BENCH_DATA", str(Path.home() / ".nova" / "bench-data"))
    engine = Engine(data_root)
    tools = args.tools.split(",")
    cases: list[dict] = []
    out_dir = RESULTS_DIR / f"judge-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for target in selected:
        pid = ids[target.name]
        with engine._open_project(pid) as (store, _vectors, _provider):  # noqa: SLF001 - 诊断工具
            picks = judge.sample_chunks(store._conn, args.n, args.seed)  # noqa: SLF001
        for index, pick in enumerate(picks):
            lang = "中文" if index % 2 == 0 else "英文"
            generated = judge.chat(cfg, judge.GEN_PROMPT.format(
                lang=lang, path=pick["file_path"], code=pick["content"][:6000]))
            question = generated.get("question")
            if not question:
                print(f"[{target.name}] 出题失败：{generated}")
                continue
            facts = generated.get("facts") or []
            symbol = pick["symbol_fqn"]
            for tool in tools:
                case = {"repo": target.name, "tool": tool, "question": question, "lang": lang,
                        "path": pick["file_path"], "symbol": symbol, "facts": facts}
                try:
                    if tool == "search":
                        response, ms = api.post("/api/query/search", {"projectId": pid, "query": question})
                        output = response["markdown"]
                        case["tokens"] = (response["meta"].get("budget") or {}).get("usedTokens", 0)
                        prompt = judge.SEARCH_JUDGE_PROMPT.format(
                            question=question, path=pick["file_path"], symbol=symbol,
                            code=pick["content"][:4000], limit=16000, output=output[:16000])
                    else:
                        response, ms = api.post("/api/query/ask", {"projectId": pid, "question": question}, timeout=300)
                        output = response.get("answer") or response.get("bestEffortContext") or ""
                        case["status"] = response.get("status")
                        prompt = judge.ASK_JUDGE_PROMPT.format(
                            question=question, path=pick["file_path"], symbol=symbol,
                            code=pick["content"][:4000], facts="；".join(facts), output=output[:8000])
                    case["ms"] = ms
                    case["output"] = output
                    case["judge"] = judge.chat(cfg, prompt)
                except Exception as exc:  # noqa: BLE001
                    case["error"] = f"{type(exc).__name__}: {exc}"
                cases.append(case)
                print(f"[{target.name}] {tool:6} {question[:50]} → {case.get('judge', case.get('error'))}")
    (out_dir / "cases.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n", encoding="utf-8")
    meta = {"model": cfg["ANSWER_MODEL"], "seed": args.seed}
    (out_dir / "report.md").write_text(judge.render_report(cases, meta), encoding="utf-8")
    print(f"报告：{out_dir / 'report.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench")
    parser.add_argument("--base", default=DEFAULT_BASE, help="nova-service 地址")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出靶场与题数").set_defaults(func=cmd_list)
    index = sub.add_parser("index", help="接入靶场并等待索引完成（持久，增量）")
    index.add_argument("--repo", action="append")
    index.set_defaults(func=cmd_index)
    run = sub.add_parser("run", help="跑题并生成报告")
    run.add_argument("--repo", action="append")
    run.add_argument("--tool", choices=("search", "ask"))
    run.add_argument("--ids")
    run.add_argument("--label", default="", help="本次运行的说明（改了什么），写入 meta")
    run.set_defaults(func=cmd_run)
    diag = sub.add_parser("diagnose", help="诊断 search 题的期望依据在哪个通道丢了")
    diag.add_argument("--repo", action="append")
    diag.add_argument("--ids")
    diag.set_defaults(func=cmd_diagnose)
    judge_p = sub.add_parser("judge", help="LLM 出题并评审 search/ask 的返回")
    judge_p.add_argument("--repo", action="append")
    judge_p.add_argument("--n", type=int, default=4, help="每个仓库抽多少个函数出题")
    judge_p.add_argument("--seed", type=int, default=1)
    judge_p.add_argument("--tools", default="search,ask")
    judge_p.set_defaults(func=cmd_judge)
    compare = sub.add_parser("compare", help="逐题对比两次运行")
    compare.add_argument("--base", nargs="+", required=True, help="基线结果目录（可多个）")
    compare.add_argument("--new", nargs="+", required=True, help="本次结果目录（可多个）")
    compare.set_defaults(func=cmd_compare)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BenchError as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 2
    except ApiFailure as exc:
        print(f"bench: 接口错误 {exc}", file=sys.stderr)
        return 1
