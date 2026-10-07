"""TASK-010 检索测试共享夹具（本目录独占；TASK-011 复用，不修改本文件）。

夹具清单：
- ``store``：临时目录里的空索引库；
- ``seed_file``：把一个文件（符号/边/spec 块）写进索引库；
- ``sym``：构造 ``SymbolDef``；
- ``provider`` / ``provider_cls``：计数型 fake ``EmbeddingProvider``（及其类，供定制）；
- ``vector_stub`` / ``vector_cls``：内存向量桩（及其类，供定制）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence

import pytest
from nova_core.hashing import chunk_content_hash, file_content_hash
from nova_core.interfaces import EmbeddingProfile
from nova_core.storage import Store
from nova_core.types import (
    ChunkDef,
    EdgeDef,
    ParsedFile,
    SpecBlockDef,
    SymbolDef,
    VectorHit,
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
        name=name,
        fqn=fqn,
        kind=kind,
        start_line=start,
        end_line=end,
        is_exported=is_exported,
    )


def _write_file(
    store: Store,
    *,
    path: str,
    language: str = "python",
    symbols: Sequence[SymbolDef] = (),
    edges: Sequence[EdgeDef] = (),
    spec_blocks: Sequence[SpecBlockDef] = (),
    bodies: dict[str, str] | None = None,
    file_hash: str | None = None,
) -> None:
    """写入一个文件的索引行（chunk 与 symbol 一一对应，id 规则同 TASK-006）。"""
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
    parsed = ParsedFile(
        path=path,
        language=language,
        symbols=tuple(symbols),
        edges=tuple(edges),
        spec_blocks=tuple(spec_blocks),
    )
    payload = b"".join(chunk.content.encode("utf-8") for chunk in chunks) or b""
    store.apply_file_change(parsed, chunks, file_hash or file_content_hash(payload))


@pytest.fixture
def seed_file() -> Callable[..., None]:
    return _write_file


@pytest.fixture
def sym() -> Callable[..., SymbolDef]:
    return _make_symbol


class CountingEmbeddingProvider:
    """最小 ``EmbeddingProvider``：计数 ``embed`` / ``embed_query`` 调用次数。

    向量值由文本确定性生成，不触碰模型文件；``raise_on_query`` / ``delay_s`` 供降级与超时测试。
    """

    def __init__(
        self,
        dim: int = 8,
        *,
        raise_on_query: BaseException | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self._profile = EmbeddingProfile(model_id="fake:test", dim=dim, max_input_tokens=512)
        self.embed_calls = 0
        self.embed_query_calls = 0
        self.queries: list[str] = []
        self._raise_on_query = raise_on_query
        self._delay_s = delay_s

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embed_calls += 1
        return [self._vector(text) for text in texts]

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        self.embed_query_calls += 1
        self.queries.extend(texts)
        if self._delay_s:
            import time

            time.sleep(self._delay_s)
        if self._raise_on_query is not None:
            raise self._raise_on_query
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        seed = sum(ord(ch) for ch in text) + 1
        return [float((seed * (i + 1)) % 17) / 17.0 for i in range(self._profile.dim)]


class StubVectorStore:
    """内存向量桩：按 ``(chunk_id, score)`` 表返回 ``VectorHit``（降序，截断 top_k）。"""

    def __init__(
        self,
        hits: Sequence[tuple[str, float]] = (),
        *,
        raise_on_search: BaseException | None = None,
    ) -> None:
        self._hits = list(hits)
        self._raise_on_search = raise_on_search
        self.calls = 0

    def search(self, vector: Sequence[float], top_k: int) -> list[VectorHit]:
        self.calls += 1
        if self._raise_on_search is not None:
            raise self._raise_on_search
        hits = [VectorHit(chunk_id=chunk_id, score=score) for chunk_id, score in self._hits]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]


@pytest.fixture
def provider() -> CountingEmbeddingProvider:
    return CountingEmbeddingProvider()


@pytest.fixture
def provider_cls() -> type[CountingEmbeddingProvider]:
    return CountingEmbeddingProvider


@pytest.fixture
def vector_stub() -> StubVectorStore:
    return StubVectorStore()


@pytest.fixture
def vector_cls() -> type[StubVectorStore]:
    return StubVectorStore
