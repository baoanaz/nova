"""靶场清单与题库的加载、校验。纯数据，不碰网络。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[1]
TOOLS = ("search", "ask")


class BenchError(Exception):
    """清单或题库不合法 / 靶场缺失。"""


@dataclass(frozen=True)
class Target:
    name: str
    language: str
    dir: str
    url: str
    commit: str

    def path(self, root: Path) -> Path:
        return root / self.dir


@dataclass(frozen=True)
class Expected:
    path: str
    symbol: str | None = None


@dataclass(frozen=True)
class Fact:
    desc: str
    any: tuple[str, ...]


@dataclass(frozen=True)
class Question:
    id: str
    repo: str
    tool: str
    lang: str
    query: str
    expected: tuple[Expected, ...] = ()
    expected_paths: tuple[str, ...] = ()
    facts: tuple[Fact, ...] = ()
    reference: str = ""
    verified: str = ""
    extra: dict = field(default_factory=dict, compare=False)


def repos_root() -> Path:
    meta = json.loads((BENCH_DIR / "targets.json").read_text(encoding="utf-8"))
    raw = os.environ.get(meta["repos_root_env"]) or meta["repos_root_default"]
    return Path(raw).expanduser()


def load_targets() -> dict[str, Target]:
    meta = json.loads((BENCH_DIR / "targets.json").read_text(encoding="utf-8"))
    return {name: Target(name=name, **item) for name, item in meta["targets"].items()}


def parse_question(raw: dict) -> Question:
    qid = raw.get("id", "<missing id>")
    tool = raw.get("tool")
    if tool not in TOOLS:
        raise BenchError(f"{qid}: tool 必须是 {TOOLS}，实际 {tool!r}")
    if not str(raw.get("query", "")).strip():
        raise BenchError(f"{qid}: query 为空")
    expected = tuple(Expected(e["path"], e.get("symbol")) for e in raw.get("expected", []))
    facts = tuple(Fact(f["desc"], tuple(f["any"])) for f in raw.get("facts", []))
    if tool == "search" and not expected:
        raise BenchError(f"{qid}: search 题必须有 expected")
    if tool == "ask" and not (facts and raw.get("expected_paths")):
        raise BenchError(f"{qid}: ask 题必须有 facts 与 expected_paths")
    return Question(
        id=qid,
        repo=raw["repo"],
        tool=tool,
        lang=raw.get("lang", "zh"),
        query=raw["query"],
        expected=expected,
        expected_paths=tuple(raw.get("expected_paths", ())),
        facts=facts,
        reference=raw.get("reference", ""),
        verified=raw.get("verified", ""),
    )


def load_questions(name: str) -> list[Question]:
    path = BENCH_DIR / "questions" / f"{name}.jsonl"
    if not path.is_file():
        raise BenchError(f"题库不存在：{path}")
    questions = [
        parse_question(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise BenchError(f"{name}: 题目 id 重复")
    return questions
