"""TASK-012 组装测试夹具（本目录独占）。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence

import pytest
from nova_core.hashing import chunk_content_hash, file_content_hash
from nova_core.storage import Store
from nova_core.types import (
    Candidate,
    ChunkDef,
    ParsedFile,
    SpecBlockDef,
    SymbolDef,
)


@pytest.fixture
def store(tmp_path) -> Iterator[Store]:
    with Store.open(tmp_path / "proj") as opened:
        yield opened


def _make_symbol(
    name: str,
    fqn: str,
    *,
    kind: str = "function",
    start: int = 1,
    end: int = 3,
    is_exported: bool = False,
) -> SymbolDef:
    return SymbolDef(
        name=name, fqn=fqn, kind=kind, start_line=start, end_line=end, is_exported=is_exported
    )


def _write_file(
    store: Store,
    *,
    path: str,
    language: str = "python",
    symbols: Sequence[SymbolDef] = (),
    spec_blocks: Sequence[SpecBlockDef] = (),
    bodies: dict[str, str] | None = None,
    file_hash: str | None = None,
) -> None:
    bodies = bodies or {}
    chunks: list[ChunkDef] = []
    for symbol in symbols:
        content = bodies.get(symbol.fqn, f"{symbol.kind} {symbol.fqn}\n")
        chunks.append(
            ChunkDef(
                id=f"{path}:{symbol.fqn}:{symbol.start_line}",
                file_path=path,
                symbol_fqn=symbol.fqn,
                symbol_kind="class_skeleton" if symbol.kind == "class" else symbol.kind,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                signature=f"{symbol.kind} {symbol.fqn}",
                docstring="",
                content=content,
                content_hash=chunk_content_hash(content),
            )
        )
    for block in spec_blocks:
        chunks.append(
            ChunkDef(
                id=f"{path}:{block.heading_path}:{block.start_line}",
                file_path=path,
                symbol_fqn=block.heading_path,
                symbol_kind="spec_block",
                start_line=block.start_line,
                end_line=block.end_line,
                signature=block.heading,
                docstring="",
                content=block.content,
                content_hash=chunk_content_hash(block.content),
            )
        )
    parsed = ParsedFile(path=path, language=language, symbols=tuple(symbols),
                        spec_blocks=tuple(spec_blocks))
    payload = b"".join(chunk.content.encode("utf-8") for chunk in chunks)
    store.apply_file_change(parsed, chunks, file_hash or file_content_hash(payload))


@pytest.fixture
def seed_file() -> Callable[..., None]:
    return _write_file


@pytest.fixture
def sym() -> Callable[..., SymbolDef]:
    return _make_symbol


@pytest.fixture
def cand() -> Callable[..., Candidate]:
    """按 ``(path, fqn, start)`` 构造候选（chunk_id 规则同 TASK-006）。"""

    def _make(
        path: str,
        fqn: str,
        start: int,
        *,
        score: float,
        tier: int = 1,
        kind: str = "code",
        channels: dict[str, int] | None = None,
        reasons: list[str] | None = None,
        end: int | None = None,
        graph_depth: int = 0,
    ) -> Candidate:
        return Candidate(
            chunk_id=f"{path}:{fqn}:{start}",
            kind=kind,
            rrf_score=score,
            score=score,
            channel_ranks=dict(channels if channels is not None else {"bm25": 1}),
            tier=tier,
            reasons=list(reasons or []),
            symbol_fqn=fqn,
            path=path,
            start_line=start,
            end_line=end if end is not None else start,
            graph_depth=graph_depth,
        )

    return _make
