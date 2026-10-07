"""TASK-006 chunking 测试夹具（本目录独占）。

原则（任务卡 DoD）：与 TASK-001 的 Store 集成走**真实 SQLite**（``tmp_path`` 项目目录），
不用 mock DB；解析侧尽量走真实抽取器（``get_parser``），必要时才手工构造 ``ParsedFile``。
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Iterator, Sequence

import pytest
from nova_core.chunking import split_file
from nova_core.hashing import file_content_hash
from nova_core.parsing.registry import detect_language, get_parser
from nova_core.storage import Store
from nova_core.types import FileDelta, ParsedFile, SymbolDef

#: 端到端小仓库（真实解析器 → splitter → Store）。
TOKEN_PY = '''"""Token helpers."""

from .util import now


def refresh(token: str) -> str:
    """刷新 token。"""
    return token + now()
'''

CALLER_PY = '''from .token import refresh


def go(token: str) -> str:
    return refresh(token)
'''


@pytest.fixture
def store(tmp_path: pathlib.Path) -> Iterator[Store]:
    with Store.open(tmp_path / "proj") as opened:
        yield opened


@pytest.fixture
def parse_source() -> Callable[[str, str], ParsedFile]:
    """用真实抽取器解析源码（语言由扩展名推断）。"""

    def _parse(path: str, source: str) -> ParsedFile:
        language = detect_language(path)
        assert language is not None, f"无法识别语言：{path}"
        return get_parser(language).parse(path, source)

    return _parse


@pytest.fixture
def ingest_file(
    store: Store,
) -> Callable[[str, str, str | None], tuple[ParsedFile, FileDelta]]:
    """解析 + 切块 + 写库（走真实 Store）；返回 (ParsedFile, FileDelta)。"""

    def _ingest(
        path: str, source: str, language: str | None = None
    ) -> tuple[ParsedFile, FileDelta]:
        resolved = language or detect_language(path)
        assert resolved is not None
        parsed = get_parser(resolved).parse(path, source)
        chunks = split_file(parsed, source)
        delta = store.apply_file_change(
            parsed, chunks, file_content_hash(source.encode("utf-8"))
        )
        return parsed, delta

    return _ingest


@pytest.fixture
def add_symbols(
    store: Store,
) -> Callable[[str, Sequence[tuple[str, str, str, int, int, bool]]], None]:
    """直接写符号行（构造跨文件解析场景用；不产 chunk）。"""

    def _add(path: str, specs: Sequence[tuple[str, str, str, int, int, bool]]) -> None:
        symbols = tuple(
            SymbolDef(
                name=name,
                fqn=fqn,
                kind=kind,
                start_line=start,
                end_line=end,
                is_exported=exported,
            )
            for name, fqn, kind, start, end, exported in specs
        )
        parsed = ParsedFile(path=path, language="python", symbols=symbols)
        store.apply_file_change(parsed, [], file_content_hash(f"{path}:{len(symbols)}".encode()))

    return _add
