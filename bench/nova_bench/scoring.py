"""评分：纯函数。search 看期望依据在证据列表里的名次；ask 看事实覆盖与引用。

自动评分只是粗筛，口径写在 README；答案是否真正正确仍需人工 / LLM 复核（results.jsonl 保留全文）。
"""

from __future__ import annotations

import posixpath
from dataclasses import asdict, dataclass

from .targets import Expected, Question

#: ask 判定通过：答案已生成、事实覆盖 ≥ 2/3、期望文件被答案引用 ≥ 1/2。
ASK_FACT_THRESHOLD = 2 / 3
ASK_CITE_THRESHOLD = 0.5
RECALL_K = 10


@dataclass
class SearchScore:
    ranks: list[int | None]
    first_rank: int | None
    hit5: bool
    hit10: bool
    recall10: float
    mrr: float

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class AskScore:
    status: str
    fact_hits: list[bool]
    fact_cov: float
    cited: list[bool]
    cite_cov: float
    retrieved: list[bool]
    passed: bool

    def to_json(self) -> dict:
        return asdict(self)


def ordered_evidence(pack: dict | None) -> list[dict]:
    """代码 / 测试证据与文档证据共用 E 编号；按编号即装填顺序（最相关在前）。"""
    if not pack:
        return []
    items = [*pack.get("evidence", []), *pack.get("docs", [])]

    def number(item: dict) -> int:
        tail = str(item.get("id", ""))[1:]
        return int(tail) if tail.isdigit() else 10**9

    return sorted(items, key=number)


def _matches(item: dict, expected: Expected) -> bool:
    if item.get("path") != expected.path:
        return False
    if expected.symbol is None:
        return True
    return expected.symbol in (item.get("symbol") or "") or expected.symbol in (
        item.get("content") or ""
    )


def score_search(question: Question, pack: dict | None) -> SearchScore:
    items = ordered_evidence(pack)
    ranks: list[int | None] = []
    for expected in question.expected:
        rank = next((i for i, item in enumerate(items, 1) if _matches(item, expected)), None)
        ranks.append(rank)
    found = [r for r in ranks if r is not None]
    first = min(found) if found else None
    return SearchScore(
        ranks=ranks,
        first_rank=first,
        hit5=first is not None and first <= 5,
        hit10=first is not None and first <= RECALL_K,
        recall10=sum(1 for r in found if r <= RECALL_K) / len(ranks),
        mrr=1 / first if first else 0.0,
    )


def _cited(answer: str, path: str) -> bool:
    return path in answer or posixpath.basename(path) in answer


def score_ask(question: Question, response: dict) -> AskScore:
    status = str(response.get("status", ""))
    answer = str(response.get("answer", ""))
    lowered = answer.lower()
    fact_hits = [any(k.lower() in lowered for k in fact.any) for fact in question.facts]
    cited = [_cited(answer, p) for p in question.expected_paths]
    retrieved_paths = {e.get("path") for e in response.get("evidenceSummary", [])}
    retrieved = [p in retrieved_paths for p in question.expected_paths]
    fact_cov = sum(fact_hits) / len(fact_hits)
    cite_cov = sum(cited) / len(cited)
    passed = (
        status == "answered" and fact_cov >= ASK_FACT_THRESHOLD and cite_cov >= ASK_CITE_THRESHOLD
    )
    return AskScore(status, fact_hits, fact_cov, cited, cite_cov, retrieved, passed)
