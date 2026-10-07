"""TASK-001 存储测试共享夹具（本目录独占，不与其它泳道共享文件）。"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Iterator

import pytest
from nova_core.hashing import chunk_content_hash
from nova_core.storage import Store
from nova_core.types import (
    ChunkDef,
    EdgeDef,
    ParsedFile,
    SpecBlockDef,
    SymbolDef,
    UnresolvedRef,
)


@pytest.fixture
def store(tmp_path: pathlib.Path) -> Iterator[Store]:
    with Store.open(tmp_path / "proj") as opened:
        yield opened


@pytest.fixture
def make_chunk() -> Callable[..., ChunkDef]:
    def _make(
        path: str = "src/a.py",
        fqn: str = "f",
        start: int = 1,
        end: int = 3,
        content: str = "def f():\n    return 1\n",
        kind: str = "function",
        signature: str = "def f()",
        docstring: str = "",
    ) -> ChunkDef:
        return ChunkDef(
            id=f"{path}:{fqn}:{start}",
            file_path=path,
            symbol_fqn=fqn,
            symbol_kind=kind,
            start_line=start,
            end_line=end,
            signature=signature,
            docstring=docstring,
            content=content,
            content_hash=chunk_content_hash(content),
        )

    return _make


@pytest.fixture
def make_symbol() -> Callable[..., SymbolDef]:
    def _make(
        name: str = "f",
        fqn: str = "f",
        kind: str = "function",
        start: int = 1,
        end: int = 3,
        is_exported: bool = False,
    ) -> SymbolDef:
        return SymbolDef(
            name=name,
            fqn=fqn,
            kind=kind,
            start_line=start,
            end_line=end,
            is_exported=is_exported,
        )

    return _make


@pytest.fixture
def make_parsed() -> Callable[..., ParsedFile]:
    def _make(
        path: str = "src/a.py",
        language: str = "python",
        symbols: tuple[SymbolDef, ...] = (),
        edges: tuple[EdgeDef, ...] = (),
        unresolved: tuple[UnresolvedRef, ...] = (),
        spec_blocks: tuple[SpecBlockDef, ...] = (),
        parse_errors: tuple[str, ...] = (),
        fallback: bool = False,
    ) -> ParsedFile:
        return ParsedFile(
            path=path,
            language=language,
            symbols=symbols,
            edges=edges,
            unresolved=unresolved,
            spec_blocks=spec_blocks,
            parse_errors=parse_errors,
            fallback=fallback,
        )

    return _make
