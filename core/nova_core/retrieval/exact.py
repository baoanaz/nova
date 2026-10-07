"""Exact 通道（TASK-010）：Explicit / Inferred 双档判定与符号匹配。

设计依据：``docs/design/Module/02-检索策略.md`` §4.2-a（D-15）：

- **Explicit**（强种子，tier 0）：反引号包裹标识符 `` `refresh_token` ``、含扩展名的路径
  ``src/auth/token_service.py``、``::`` / ``.`` 标识符链 ``TokenService::refresh``；
- **Inferred**（普通种子，tier 1）：正则抽取驼峰/蛇形/SCREAMING 标识符（codegraph 的
  ``extractSymbolsFromQuery`` 模式）——它是**召回种子不是精确证据**，抽出 ``refresh``
  会同时命中 refreshUI/refreshCache/refreshConfig。

本模块只做"查询 → 显式词元"的解析与"词元 → 候选"的检索，不含评分与融合（rrf.py/fusion.py）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from nova_core.retrieval.fusion import (
    CHANNEL_EXACT,
    CHANNEL_INFERRED,
    REASON_EXPLICIT_PATH,
    TIER_EXPLICIT,
    TIER_SEED,
    make_candidate,
)
from nova_core.storage import Store
from nova_core.types import Candidate

__all__ = [
    "ExplicitQuery",
    "ExplicitToken",
    "REASON_EXPLICIT_PATH",
    "REASON_EXPLICIT_SYMBOL",
    "REASON_INFERRED",
    "SOURCE_EXTENSIONS",
    "explicit_tokens",
    "extract_inferred",
    "parse_explicit",
    "recall_explicit",
    "recall_inferred",
]

#: 判定为"路径词元"的来源文件扩展名白名单（避免把 ``TokenService.refresh`` 当路径）。
SOURCE_EXTENSIONS = frozenset(
    {
        "bash",
        "c",
        "cc",
        "cfg",
        "cmake",
        "cpp",
        "cs",
        "cxx",
        "go",
        "gradle",
        "h",
        "hh",
        "hpp",
        "hxx",
        "ini",
        "ipp",
        "java",
        "js",
        "json",
        "jsx",
        "kt",
        "kts",
        "lua",
        "m",
        "md",
        "mm",
        "php",
        "pl",
        "ps1",
        "py",
        "pyi",
        "r",
        "rb",
        "rs",
        "scala",
        "sh",
        "sql",
        "swift",
        "toml",
        "tpp",
        "ts",
        "tsx",
        "txt",
        "yaml",
        "yml",
    }
)

REASON_EXPLICIT_SYMBOL = "explicit symbol"
REASON_INFERRED = "inferred symbol"

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PATH_TOKEN_RE = re.compile(r"(?<![\w./\-])([\w.\-/]+\.[A-Za-z][A-Za-z0-9]{0,9})(?![\w/\-])")
_CHAIN_RE = re.compile(r"(?<![\w.:])([A-Za-z_]\w*(?:(?:::|\.)[A-Za-z_]\w*)+)(?![\w.])")

_CAMEL_RE = re.compile(r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+\b")
_PASCAL_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_SCREAMING_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*(?:(?:::|\.)[A-Za-z_]\w*)*$")


@dataclass(frozen=True, slots=True)
class ExplicitToken:
    """一个 Explicit 词元；``kind`` ∈ {``symbol``, ``path``}。"""

    text: str
    kind: str


@dataclass(frozen=True, slots=True)
class ExplicitQuery:
    """查询的 Explicit 解析结果（符号词元 + 路径词元，均按出现顺序去重）。"""

    symbols: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    """把已识别的 span 替换为空格，避免同一片段被下游正则重复识别。"""
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for index in range(start, min(end, len(chars))):
            chars[index] = " "
    return "".join(chars)


def _is_path_token(token: str) -> bool:
    dot = token.rfind(".")
    if dot <= 0:
        return False
    return token[dot + 1 :].lower() in SOURCE_EXTENSIONS


def explicit_tokens(query: str) -> list[ExplicitToken]:
    """解析查询中的 Explicit 词元（D-15）。

    识别顺序（先反引号 → 路径 → 标识符链），已识别片段会被遮蔽以免重复归类；
    返回按出现顺序去重的 ``ExplicitToken`` 列表。
    """
    if not query:
        return []
    tokens: list[ExplicitToken] = []
    seen: set[tuple[str, str]] = set()
    masked_spans: list[tuple[int, int]] = []

    def _add(text: str, kind: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        key = (kind, cleaned)
        if key in seen:
            return
        seen.add(key)
        tokens.append(ExplicitToken(text=cleaned, kind=kind))

    for match in _BACKTICK_RE.finditer(query):
        inner = match.group(1).strip()
        if _is_path_token(inner):
            _add(inner, "path")
        elif _IDENTIFIER_RE.match(inner):
            _add(inner, "symbol")
        masked_spans.append(match.span())

    working = _mask(query, masked_spans)

    path_spans: list[tuple[int, int]] = []
    for match in _PATH_TOKEN_RE.finditer(working):
        token = match.group(1)
        if _is_path_token(token):
            _add(token, "path")
            path_spans.append(match.span())
    working = _mask(working, path_spans)

    for match in _CHAIN_RE.finditer(working):
        _add(match.group(1), "symbol")
    return tokens


def parse_explicit(query: str) -> ExplicitQuery:
    """``explicit_tokens`` 的结构化视图（供 recall 主编排直接消费）。"""
    symbols: list[str] = []
    paths: list[str] = []
    for token in explicit_tokens(query):
        target = symbols if token.kind == "symbol" else paths
        if token.text not in target:
            target.append(token.text)
    return ExplicitQuery(symbols=tuple(symbols), paths=tuple(paths))


def extract_inferred(query: str, *, limit: int | None = None) -> list[str]:
    """正则抽取 Inferred 标识符（驼峰/大驼峰/蛇形/SCREAMING），按出现顺序去重。

    Explicit 片段先被遮蔽，因此 ``TokenService::refresh`` 中的 ``TokenService`` 不会
    额外变成 Inferred 种子（它是 Explicit 的整体）。
    """
    if not query:
        return []
    spans = [match.span() for match in _BACKTICK_RE.finditer(query)]
    working = _mask(query, spans)
    working = _mask(working, [m.span() for m in _PATH_TOKEN_RE.finditer(working)
                              if _is_path_token(m.group(1))])
    working = _mask(working, [m.span() for m in _CHAIN_RE.finditer(working)])

    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for pattern in (_CAMEL_RE, _SNAKE_RE, _SCREAMING_RE, _PASCAL_RE):
        for match in pattern.finditer(working):
            token = match.group(0)
            if token in seen:
                continue
            seen.add(token)
            found.append((match.start(), token))
    found.sort(key=lambda item: item[0])
    tokens = [token for _, token in found]
    return tokens if limit is None else tokens[:limit]


def _symbol_spellings(token: str) -> list[str]:
    """``A::b`` 与 ``A.b`` 两种拼写都试（库里 fqn 形态随语言而定，R8）。"""
    variants = [token, token.replace("::", "."), token.replace(".", "::")]
    return list(dict.fromkeys(variants))


def recall_explicit(
    store: Store,
    symbols: tuple[str, ...] | list[str],
    *,
    limit: int = 20,
) -> list[Candidate]:
    """Explicit 符号词元 → 符号表精确/fqn 匹配候选（全量进池，上限 ``limit``）。

    仅使用 ``Store.exact_symbols``（name 或 fqn 精确匹配），不做模糊/子串匹配——
    Explicit 是精确证据（D-15）。路径词元不在此解析（见 ``parse_explicit``：它是
    装填/rerank 信号，池内候选由 fusion 按路径打 tier 0 标记）。
    """
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for token in symbols:
        for spelling in _symbol_spellings(token):
            for row in store.exact_symbols(spelling, limit=None):
                if row.chunk_id is None or row.chunk_id in seen:
                    continue
                seen.add(row.chunk_id)
                candidates.append(
                    make_candidate(
                        row.chunk_id,
                        channel=CHANNEL_EXACT,
                        rank=len(candidates) + 1,
                        tier=TIER_EXPLICIT,
                        reason=f"{REASON_EXPLICIT_SYMBOL} {spelling}",
                    )
                )
                if len(candidates) >= limit:
                    return candidates
    return candidates


def recall_inferred(
    store: Store,
    tokens: list[str] | tuple[str, ...],
    *,
    limit: int = 20,
) -> list[Candidate]:
    """Inferred 词元 → 符号表精确匹配的普通种子（top ``limit``，tier 1）。"""
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for token in tokens:
        for row in store.exact_symbols(token, limit=None):
            if row.chunk_id is None or row.chunk_id in seen:
                continue
            seen.add(row.chunk_id)
            candidates.append(
                make_candidate(
                    row.chunk_id,
                    channel=CHANNEL_INFERRED,
                    rank=len(candidates) + 1,
                    tier=TIER_SEED,
                    reason=f"{REASON_INFERRED} {token}",
                )
            )
            if len(candidates) >= limit:
                return candidates
    return candidates
