"""golden runner（TASK-013 §C）：golden set 用例 → recall/MRR 报告。

用途：把"检索质量"变成可回归的数字（Module/02 §7-1）。M1 验收与 TASK-015 的校准都依赖它。

用法（两条等价入口，实现只有这一份）：

```bash
uv run nova-core eval --golden benches/golden --repo <仓库路径> --report benches/results/x.md
uv run python benches/run.py --golden benches/golden --repo <仓库路径> --report benches/results/x.md
```

口径（与 ``benches/README.md`` 一致）：

- 命中 = ``expected`` 中任一条的 ``path`` 出现在 top-k **证据**里，且（若给了 ``symbol``）
  该符号在证据条目中出现（符号字段 / 标题路径 / 正文提及）；
- 排名口径 = ``ContextPack`` 的装填顺序（``E`` 编号顺序，即 agent 实际读到的顺序）；
- 正例统计 recall@5 / recall@10 / MRR；``category='negative'`` 的用例单独统计
  （``answerable=False`` 且 ``missingEvidence`` 非空视为通过），不混进 recall 分母；
- 报告是 Markdown：整体 + 按 ``lang`` + 按 ``category`` + 逐条失败清单。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from nova_core.engine import SearchTrace
from nova_core.types import ContextPack, EvidenceItem

__all__ = [
    "GOLDEN_SUFFIX",
    "NEGATIVE_CATEGORY",
    "CaseResult",
    "Expectation",
    "GoldenCase",
    "GoldenError",
    "GoldenReport",
    "Metrics",
    "load_cases",
    "render_report",
    "run_golden",
    "write_report",
]

#: golden 用例文件后缀（目录模式下按此递归收集）。
GOLDEN_SUFFIX = ".jsonl"
#: 负例类别（仓库不存在 → 期望无强证据）。
NEGATIVE_CATEGORY = "negative"

_UNKNOWN = "unknown"


class GoldenError(ValueError):
    """golden 文件格式错误（CLI 映射为退出码 1）。"""


@dataclass(frozen=True, slots=True)
class Expectation:
    """一条期望命中（``path`` 必填；``symbol`` 可选）。"""

    path: str
    symbol: str | None = None

    def label(self) -> str:
        return f"{self.path}#{self.symbol}" if self.symbol else self.path


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """一条 golden 用例（``benches/README.md`` 的 JSONL 字段）。"""

    id: str
    query: str
    lang: str
    category: str
    expected: tuple[Expectation, ...] = ()
    repo_hint: str = ""
    commit: str = ""
    notes: str = ""

    @property
    def is_negative(self) -> bool:
        return self.category == NEGATIVE_CATEGORY


@dataclass(frozen=True, slots=True)
class CaseResult:
    """单条用例的执行结果。"""

    case: GoldenCase
    rank: int | None = None            # 首个命中的排名（1-based）；未命中为 None
    top: tuple[str, ...] = ()          # top-3 证据标签（失败诊断用）
    answerable: bool = False
    missing_evidence: tuple[str, ...] = ()
    degraded: bool = False
    evidence_count: int = 0
    error: str | None = None           # 执行期异常（不中断整轮）

    @property
    def hit(self) -> bool:
        return self.rank is not None

    @property
    def passed(self) -> bool:
        """负例通过 = 无强证据且如实报了缺口；正例通过 = 命中。"""
        if self.error is not None:
            return False
        if self.case.is_negative:
            return not self.answerable and bool(self.missing_evidence)
        return self.hit


@dataclass(frozen=True, slots=True)
class Metrics:
    """一组用例的指标（``count`` = 统计分母）。"""

    count: int = 0
    recall_at_5: float = 0.0
    recall_at_10: float = 0.0
    mrr: float = 0.0

    def cells(self) -> str:
        return f"{self.count} | {self.recall_at_5:.3f} | {self.recall_at_10:.3f} | {self.mrr:.3f}"


@dataclass(frozen=True, slots=True)
class GoldenReport:
    """一轮 golden 评估的结果（``render_report`` 的输入）。"""

    golden: str
    repo: str
    project_id: str
    results: tuple[CaseResult, ...]
    generated_at: int = 0
    max_tokens: int = 10_000
    top_k: int = 10

    @property
    def positives(self) -> tuple[CaseResult, ...]:
        return tuple(result for result in self.results if not result.case.is_negative)

    @property
    def negatives(self) -> tuple[CaseResult, ...]:
        return tuple(result for result in self.results if result.case.is_negative)

    @property
    def overall(self) -> Metrics:
        return metrics_of(self.positives, top_k=self.top_k)

    def by(self, field: str) -> dict[str, Metrics]:
        groups: dict[str, list[CaseResult]] = {}
        for result in self.positives:
            key = getattr(result.case, field, None) or _UNKNOWN
            groups.setdefault(str(key), []).append(result)
        return {key: metrics_of(value, top_k=self.top_k) for key, value in sorted(groups.items())}

    @property
    def negative_pass_count(self) -> int:
        return sum(1 for result in self.negatives if result.passed)

    @property
    def degraded_count(self) -> int:
        return sum(1 for result in self.results if result.degraded)


# ---------------------------------------------------------------------------
# 用例加载
# ---------------------------------------------------------------------------


def load_cases(golden: str | Path) -> list[GoldenCase]:
    """``--golden`` 指向文件或目录（目录递归收集 ``*.jsonl``，按路径排序保证可复现）。"""
    path = Path(golden)
    if path.is_dir():
        files = sorted(
            candidate for candidate in path.rglob(f"*{GOLDEN_SUFFIX}") if candidate.is_file()
        )
    elif path.is_file():
        files = [path]
    else:
        raise GoldenError(f"golden 路径不存在：{path}")
    cases: list[GoldenCase] = []
    seen: set[str] = set()
    for file in files:
        for lineno, raw in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            case = _parse_case(file, lineno, line)
            if case.id in seen:
                raise GoldenError(f"{file}:{lineno} 用例 id 重复：{case.id}")
            seen.add(case.id)
            cases.append(case)
    return cases


def _parse_case(file: Path, lineno: int, line: str) -> GoldenCase:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise GoldenError(f"{file}:{lineno} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise GoldenError(f"{file}:{lineno} 用例必须是 JSON 对象")
    identifier = str(payload.get("id") or "").strip()
    query = str(payload.get("query") or "").strip()
    if not identifier or not query:
        raise GoldenError(f"{file}:{lineno} 缺少 id 或 query")
    raw_expected = payload.get("expected") or []
    if not isinstance(raw_expected, list):
        raise GoldenError(f"{file}:{lineno} expected 必须是数组")
    expected: list[Expectation] = []
    for item in raw_expected:
        if not isinstance(item, dict) or not item.get("path"):
            raise GoldenError(f"{file}:{lineno} expected 条目必须含 path")
        symbol = item.get("symbol")
        expected.append(Expectation(path=str(item["path"]), symbol=str(symbol) if symbol else None))
    return GoldenCase(
        id=identifier,
        query=query,
        lang=str(payload.get("lang") or _UNKNOWN),
        category=str(payload.get("category") or _UNKNOWN),
        expected=tuple(expected),
        repo_hint=str(payload.get("repo_hint") or ""),
        commit=str(payload.get("commit") or ""),
        notes=str(payload.get("notes") or ""),
    )


# ---------------------------------------------------------------------------
# 执行与判定
# ---------------------------------------------------------------------------


def run_golden(
    cases: Sequence[GoldenCase],
    search: Callable[[str], SearchTrace],
    *,
    golden: str = "",
    repo: str = "",
    project_id: str = "",
    max_tokens: int = 10_000,
    top_k: int = 10,
    now: int | None = None,
) -> GoldenReport:
    """逐条执行 ``search`` 并算指标；单条异常只记入该条结果（不中断整轮）。"""
    results: list[CaseResult] = []
    for case in cases:
        try:
            trace = search(case.query)
        except Exception as exc:  # noqa: BLE001 - 逐条隔离，报告里如实列出
            results.append(
                CaseResult(case=case, error=f"{type(exc).__name__}: {exc}")
            )
            continue
        pack = trace.pack
        items = ordered_evidence(pack)
        results.append(
            CaseResult(
                case=case,
                rank=first_hit_rank(items, case, top_k=top_k),
                top=tuple(_label(item) for item in items[:3]),
                answerable=pack.answerable,
                missing_evidence=tuple(item.code for item in pack.missing_evidence),
                degraded=trace.degraded,
                evidence_count=len(items),
            )
        )
    return GoldenReport(
        golden=golden,
        repo=repo,
        project_id=project_id,
        results=tuple(results),
        generated_at=int(time.time()) if now is None else now,
        max_tokens=max_tokens,
        top_k=top_k,
    )


def ordered_evidence(pack: ContextPack) -> list[EvidenceItem]:
    """证据的排名口径 = 装填顺序（``E`` 编号；evidence 与 docs 共用编号空间，D-21）。"""
    return sorted([*pack.evidence, *pack.docs], key=lambda item: _evidence_index(item.id))


def _evidence_index(identifier: str) -> int:
    digits = identifier[1:]
    return int(digits) if digits.isdigit() else 1 << 30


def first_hit_rank(
    items: Sequence[EvidenceItem], case: GoldenCase, *, top_k: int = 10
) -> int | None:
    """首个命中排名（1-based）；``expected`` 为空或不在 top-k 内 → ``None``。"""
    expectations = case.expected
    if not expectations:
        return None
    for rank, item in enumerate(items[:top_k], start=1):
        if any(_matches(item, expectation) for expectation in expectations):
            return rank
    return None


def _matches(item: EvidenceItem, expectation: Expectation) -> bool:
    if item.path != expectation.path:
        return False
    if expectation.symbol is None:
        return True
    return _mentions_symbol(item, expectation.symbol)


def _mentions_symbol(item: EvidenceItem, symbol: str) -> bool:
    """符号出现在证据条目中（符号字段 / 标题路径 / 正文提及）。"""
    candidates = [value for value in (item.symbol, item.heading_path) if value]
    for value in candidates:
        if value == symbol or value.endswith(f".{symbol}") or value.endswith(f"::{symbol}"):
            return True
    return _MENTION_RE_CACHE.get_or_build(symbol).search(item.content) is not None


class _MentionCache:
    """符号 → 词边界正则（*符号名* 的正文提及判定，避免 ``ChunkDef`` 命中 ``ChunkDefX``）。"""

    def __init__(self) -> None:
        self._patterns: dict[str, re.Pattern[str]] = {}

    def get_or_build(self, symbol: str) -> re.Pattern[str]:
        pattern = self._patterns.get(symbol)
        if pattern is None:
            pattern = re.compile(rf"(?<![\w.]){re.escape(symbol)}(?![\w])")
            self._patterns[symbol] = pattern
        return pattern


_MENTION_RE_CACHE = _MentionCache()


def metrics_of(results: Sequence[CaseResult], *, top_k: int = 10) -> Metrics:
    """recall@5 / recall@10 / MRR（分母 = 传入的正例数）。"""
    hits5 = sum(1 for result in results if result.rank is not None and result.rank <= 5)
    hits10 = sum(1 for result in results if result.rank is not None and result.rank <= 10)
    mrr = 0.0
    for result in results:
        if result.rank is not None:
            mrr += 1.0 / result.rank
    count = len(results)
    if count == 0:
        return Metrics()
    return Metrics(
        count=count,
        recall_at_5=hits5 / count,
        recall_at_10=hits10 / count,
        mrr=mrr / count,
    )


def _label(item: EvidenceItem) -> str:
    target = item.symbol or item.heading_path or item.path
    span = f":{item.lines[0]}-{item.lines[1]}" if item.lines else ""
    return f"{item.path}{span} ({target})"


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def render_report(report: GoldenReport) -> str:
    """GoldenReport → Markdown（确定性：时间戳可用 ``now`` 固定）。"""
    lines: list[str] = ["# nova golden eval 报告", ""]
    generated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report.generated_at))
    lines.extend(
        [
            f"- golden：{report.golden or '(未记录)'}（{len(report.results)} 条用例）",
            f"- repo：{report.repo or '(未记录)'}",
            f"- project：{report.project_id or '(未记录)'}",
            f"- 生成时间：{generated}",
            f"- 预算：maxTokens={report.max_tokens}｜排名口径=ContextPack 装填序"
            f"｜top-k={report.top_k}",
            f"- 向量通道降级用例数：{report.degraded_count}",
            "",
            "## 总体（正例）",
            "",
            "| 指标 | 值 |",
            "|---|---|",
            f"| 正例数 | {report.overall.count} |",
            f"| recall@5 | {report.overall.recall_at_5:.3f} |",
            f"| recall@10 | {report.overall.recall_at_10:.3f} |",
            f"| MRR | {report.overall.mrr:.3f} |",
            f"| 负例通过 | {report.negative_pass_count}/{len(report.negatives)} |",
            "",
        ]
    )
    lines.extend(_group_section("按语言（lang）", report.by("lang")))
    lines.extend(_group_section("按类别（category）", report.by("category")))
    lines.extend(_failure_section(report))
    lines.extend(_negative_section(report))
    return "\n".join(lines).rstrip() + "\n"


def _group_section(title: str, groups: dict[str, Metrics]) -> list[str]:
    lines = [
        f"## {title}",
        "",
        "| 分组 | 用例数 | recall@5 | recall@10 | MRR |",
        "|---|---|---|---|---|",
    ]
    if not groups:
        lines.append("| （无） | 0 | 0.000 | 0.000 | 0.000 |")
    else:
        for key, metrics in groups.items():
            lines.append(f"| {key} | {metrics.cells()} |")
    lines.append("")
    return lines


def _failure_section(report: GoldenReport) -> list[str]:
    failures = [result for result in report.positives if not result.passed]
    lines = ["## 失败清单（正例）", ""]
    if not failures:
        lines.extend(["（无）", ""])
        return lines
    lines.extend(["| id | lang | category | query | 期望 | top-3 |", "|---|---|---|---|---|---|"])
    for result in failures:
        expected = " / ".join(expectation.label() for expectation in result.case.expected) or "-"
        top = " / ".join(result.top) or "-"
        if result.error:
            top = f"错误：{result.error}"
        lines.append(
            f"| {result.case.id} | {result.case.lang} | {result.case.category} | "
            f"{_escape(result.case.query)} | {expected} | {_escape(top)} |"
        )
    lines.append("")
    return lines


def _negative_section(report: GoldenReport) -> list[str]:
    lines = ["## 负例清单", ""]
    if not report.negatives:
        lines.extend(["（无）", ""])
        return lines
    lines.extend(["| id | 通过 | query | answerable | missingEvidence |", "|---|---|---|---|---|"])
    for result in report.negatives:
        mark = "是" if result.passed else "否"
        missing = ", ".join(result.missing_evidence) or "-"
        if result.error:
            missing = f"错误：{result.error}"
        lines.append(
            f"| {result.case.id} | {mark} | {_escape(result.case.query)} | "
            f"{result.answerable} | {_escape(missing)} |"
        )
    lines.append("")
    return lines


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def write_report(report: GoldenReport, path: str | Path) -> Path:
    """报告落盘（父目录按需创建），返回写入路径。"""
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_report(report), encoding="utf-8")
    return target
