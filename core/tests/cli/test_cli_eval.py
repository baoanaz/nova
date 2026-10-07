"""TASK-013 §C/DoD：golden runner（cli/eval.py）——指标、报告文件、命中判定。

DoD 原文：`eval` 对 3 条内置样例 gold 输出指标报告文件（2 条正例 + 1 条负例，见 ``GOLDEN_CASES``）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from nova_core.cli.eval import (
    CaseResult,
    Expectation,
    GoldenCase,
    GoldenError,
    Metrics,
    first_hit_rank,
    load_cases,
    metrics_of,
    render_report,
    run_golden,
)

RunCli = Callable[..., tuple[int, str, str]]

GOLDEN_CASES: list[dict] = [
    {
        "id": "fix-0001",
        "repo_hint": "fixture",
        "commit": "self",
        "query": "refresh_token 过期以后应该在哪里重新签发",
        "lang": "zh",
        "category": "symbol",
        "expected": [{"path": "src/auth/token_service.py", "symbol": "refresh_token"}],
    },
    {
        "id": "fix-0002",
        "repo_hint": "fixture",
        "commit": "self",
        "query": "log_event 在哪里实现的",
        "lang": "zh",
        "category": "path",
        "expected": [{"path": "src/util/logging.py", "symbol": "log_event"}],
    },
    {
        "id": "fix-0003",
        "repo_hint": "fixture",
        "commit": "self",
        "query": "PaymentGateway 的重试退避逻辑在哪里实现",
        "lang": "zh",
        "category": "negative",
        "expected": [],
    },
]


def _write_golden(path: Path, cases: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def golden_file(tmp_path: Path) -> Path:
    return _write_golden(tmp_path / "golden" / "sample.jsonl", GOLDEN_CASES)


# --------------------------------------------------------------------------- CLI 端到端


def test_eval_writes_metric_report(
    repo: Path, data_root: Path, golden_file: Path, run_cli: RunCli, tmp_path: Path
) -> None:
    code, _out, err = run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    assert code == 0, err

    report_path = tmp_path / "results" / "eval.md"
    code, out, err = run_cli(
        "eval",
        "--golden",
        str(golden_file),
        "--repo",
        str(repo),
        "--data",
        str(data_root),
        "--report",
        str(report_path),
    )
    assert code == 0, err
    assert report_path.is_file()
    text = report_path.read_text(encoding="utf-8")
    assert text == out[: out.rindex("report:")].rstrip() + "\n", "stdout 与报告文件同源"
    assert f"report: {report_path}" in out

    # 两条正例都在 top-5 命中（fixture 小仓库，目标符号由 Inferred+Vector 命中）。
    assert "- golden：" in text and "（3 条用例）" in text
    assert "| 正例数 | 2 |" in text
    assert "| recall@5 | 1.000 |" in text
    assert "| recall@10 | 1.000 |" in text
    assert "## 失败清单（正例）" in text
    assert "| fix-0001 |" not in text, "两条正例都应命中，失败清单为空"
    # 负例单独统计；fixture 只有 9 个 chunk，向量通道会返回全部切片，
    # 因此"无强相关证据"在该规模上不成立 → 负例判定另由单元测试覆盖
    # （真实仓库的负例口径是 TASK-014/015 的事）。
    assert "## 负例清单" in text and "| fix-0003 |" in text
    assert "| 负例通过 | 0/1 |" in text
    assert "按语言（lang）" in text and "按类别（category）" in text


def test_eval_lists_failures_when_expectation_misses(
    repo: Path, data_root: Path, run_cli: RunCli, tmp_path: Path
) -> None:
    run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    golden = _write_golden(
        tmp_path / "golden" / "miss.jsonl",
        [
            {
                "id": "miss-0001",
                "query": "refresh_token 过期以后应该在哪里重新签发",
                "lang": "zh",
                "category": "symbol",
                "expected": [{"path": "src/nowhere/missing.py", "symbol": "nope"}],
            }
        ],
    )
    report_path = tmp_path / "results" / "miss.md"
    code, out, err = run_cli(
        "eval",
        "--golden",
        str(golden),
        "--repo",
        str(repo),
        "--data",
        str(data_root),
        "--report",
        str(report_path),
    )
    assert code == 0, err
    text = report_path.read_text(encoding="utf-8")
    assert "| recall@5 | 0.000 |" in text
    assert "| miss-0001 |" in text and "src/nowhere/missing.py#nope" in text
    assert "src/auth/token_service.py" in text, "失败清单要带 top-3 便于定位"


def test_eval_requires_indexed_project(
    repo: Path, data_root: Path, golden_file: Path, run_cli: RunCli, tmp_path: Path
) -> None:
    report_path = tmp_path / "results" / "empty.md"
    code, _out, err = run_cli(
        "eval",
        "--golden",
        str(golden_file),
        "--repo",
        str(repo),
        "--data",
        str(data_root),
        "--report",
        str(report_path),
    )
    assert code == 1
    assert "尚未索引" in err
    assert not report_path.exists()


def test_eval_rejects_empty_golden_set(
    repo: Path, data_root: Path, run_cli: RunCli, tmp_path: Path
) -> None:
    run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    empty = tmp_path / "golden" / "empty.jsonl"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("\n# 只有注释\n", encoding="utf-8")
    code, _out, err = run_cli(
        "eval",
        "--golden",
        str(empty),
        "--repo",
        str(repo),
        "--data",
        str(data_root),
        "--report",
        str(tmp_path / "results" / "x.md"),
    )
    assert code == 1 and "golden 集为空" in err


# --------------------------------------------------------------------------- 单元：加载与指标


def test_load_cases_reads_directory_recursively(tmp_path: Path) -> None:
    _write_golden(tmp_path / "golden" / "a.jsonl", GOLDEN_CASES[:2])
    _write_golden(tmp_path / "golden" / "nested" / "b.jsonl", GOLDEN_CASES[2:])
    cases = load_cases(tmp_path / "golden")
    assert [case.id for case in cases] == ["fix-0001", "fix-0002", "fix-0003"]
    assert cases[2].is_negative is True
    assert cases[0].expected == (
        Expectation(path="src/auth/token_service.py", symbol="refresh_token"),
    )


def test_load_cases_rejects_duplicate_id_and_malformed_lines(tmp_path: Path) -> None:
    duplicated = _write_golden(tmp_path / "dup.jsonl", [GOLDEN_CASES[0], GOLDEN_CASES[0]])
    with pytest.raises(GoldenError, match="重复"):
        load_cases(duplicated)

    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"id": "x", "query": }\n', encoding="utf-8")
    with pytest.raises(GoldenError, match="不是合法 JSON"):
        load_cases(broken)

    incomplete = tmp_path / "incomplete.jsonl"
    incomplete.write_text('{"id": "x"}\n', encoding="utf-8")
    with pytest.raises(GoldenError, match="缺少 id 或 query"):
        load_cases(incomplete)

    with pytest.raises(GoldenError, match="不存在"):
        load_cases(tmp_path / "nope.jsonl")


def test_metrics_of_recall_and_mrr() -> None:
    case = GoldenCase(id="c", query="q", lang="zh", category="symbol")
    results = [
        CaseResult(case=case, rank=1),
        CaseResult(case=case, rank=2),
        CaseResult(case=case, rank=15),
        CaseResult(case=case, rank=None),
    ]
    metrics = metrics_of(results)
    assert metrics.count == 4
    assert metrics.recall_at_5 == pytest.approx(0.5)
    assert metrics.recall_at_10 == pytest.approx(0.5)
    assert metrics.mrr == pytest.approx((1 + 0.5 + 1 / 15) / 4)
    assert metrics_of([]) == Metrics()


def test_negative_passed_only_without_answer_and_with_reported_gap() -> None:
    case = GoldenCase(id="n", query="q", lang="zh", category="negative")
    assert CaseResult(case=case, answerable=False, missing_evidence=("no_context_match",)).passed
    assert not CaseResult(case=case, answerable=True, missing_evidence=("no_context_match",)).passed
    assert not CaseResult(case=case, answerable=False).passed
    assert not CaseResult(case=case, answerable=False, error="RuntimeError: boom").passed
    # 正例必须命中；异常不中断整轮（只影响该条）
    positive = GoldenCase(id="p", query="q", lang="zh", category="symbol")
    assert CaseResult(case=positive, rank=3).passed
    assert not CaseResult(case=positive, rank=None).passed


def test_first_hit_rank_requires_path_and_symbol() -> None:
    from nova_core.types import EvidenceItem

    item = EvidenceItem(
        id="E1",
        type="code",
        path="src/auth/token_service.py",
        content="7 | def refresh_token(self, token):\n",
        score=1.0,
        evidence_tier=1,
        reason="inferred",
        symbol="TokenService.refresh_token",
        lines=(7, 9),
    )
    case = GoldenCase(
        id="c",
        query="q",
        lang="zh",
        category="symbol",
        expected=(Expectation(path="src/auth/token_service.py", symbol="refresh_token"),),
    )
    assert first_hit_rank([item], case) == 1

    wrong_path = GoldenCase(
        id="c2", query="q", lang="zh", category="symbol",
        expected=(Expectation(path="src/other.py", symbol="refresh_token"),),
    )
    assert first_hit_rank([item], wrong_path) is None

    wrong_symbol = GoldenCase(
        id="c3", query="q", lang="zh", category="symbol",
        expected=(Expectation(path="src/auth/token_service.py", symbol="PaymentGateway"),),
    )
    assert first_hit_rank([item], wrong_symbol) is None

    no_expectation = GoldenCase(id="c4", query="q", lang="zh", category="negative")
    assert first_hit_rank([item], no_expectation) is None


def test_report_is_deterministic_and_records_per_case_errors() -> None:
    case = GoldenCase(
        id="c1",
        query="q",
        lang="zh",
        category="symbol",
        expected=(Expectation(path="src/a.py", symbol="f"),),
    )

    def boom(_query: str) -> object:
        raise RuntimeError("引擎炸了")

    report = run_golden(
        [case], boom, golden="g.jsonl", repo="/repo", project_id="abc", now=1_700_000_000
    )
    text = render_report(report)
    assert render_report(report) == text, "同一 report 渲染必须字节一致"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(1_700_000_000))
    assert f"生成时间：{stamp}" in text
    assert "RuntimeError: 引擎炸了" in text, "单条异常写入失败清单，不中断整轮"
    assert "| recall@5 | 0.000 |" in text
