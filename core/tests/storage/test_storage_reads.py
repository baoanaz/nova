"""读路径 API：exact_symbols / chunk 查询 / edges / freshness / config / counts。"""

from __future__ import annotations

from collections.abc import Callable

from nova_core.storage import Store
from nova_core.types import ChunkDef, EdgeDef, ParsedFile, SymbolDef


def _seed(
    store: Store,
    make_parsed: Callable[..., ParsedFile],
    make_chunk: Callable[..., ChunkDef],
) -> list[ChunkDef]:
    symbols = (
        SymbolDef(name="refresh", fqn="Local.refresh", kind="function", start_line=1, end_line=3),
        SymbolDef(
            name="refresh",
            fqn="TokenService.refresh",
            kind="method",
            start_line=5,
            end_line=7,
            is_exported=True,
        ),
    )
    chunks = [
        make_chunk(fqn="Local.refresh", start=1, content="def refresh():\n    pass\n"),
        make_chunk(
            fqn="TokenService.refresh", start=5, content="    def refresh(self):\n        pass\n"
        ),
        make_chunk(fqn="helper", start=9, content="def helper():\n    pass\n"),
    ]
    edges = (
        EdgeDef(source_fqn="TokenService.refresh", target_name="helper", kind="calls", line=6),
        EdgeDef(source_fqn="Local.refresh", target_name="os", kind="imports", line=1),
    )
    store.apply_file_change(make_parsed(symbols=symbols, edges=edges), chunks, "file-hash-1")
    return chunks


def test_exact_symbols_by_name_and_fqn(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    _seed(store, make_parsed, make_chunk)
    by_name = store.exact_symbols("refresh")
    assert {row.fqn for row in by_name} == {"Local.refresh", "TokenService.refresh"}
    assert by_name[0].fqn == "TokenService.refresh"  # 导出符号优先
    assert by_name[0].chunk_id == "src/a.py:TokenService.refresh:5"
    assert by_name[0].is_exported is True

    by_fqn = store.exact_symbols("Local.refresh")
    assert [row.fqn for row in by_fqn] == ["Local.refresh"]

    assert len(store.exact_symbols("refresh", limit=1)) == 1
    assert len(store.exact_symbols("refresh", limit=None)) == 2
    assert store.exact_symbols("missing") == []


def test_chunk_lookup(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    chunks = _seed(store, make_parsed, make_chunk)
    first = store.chunk_by_id(chunks[2].id)
    assert first is not None and first.content == chunks[2].content
    assert first.symbol_kind == "function"
    assert store.chunk_by_id("no:such:1") is None

    # 返回顺序与输入一致，缺失 id 跳过。
    got = store.chunks_by_ids([chunks[1].id, "no:such:1", chunks[0].id])
    assert [c.id for c in got] == [chunks[1].id, chunks[0].id]
    assert store.chunks_by_ids([]) == []


def test_edges_for_both_directions_and_kind_filter(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    _seed(store, make_parsed, make_chunk)
    out_edges = store.edges_for("TokenService.refresh")
    assert [(e.target, e.kind) for e in out_edges] == [("helper", "calls")]

    in_edges = store.edges_for("helper")
    assert [(e.source, e.kind) for e in in_edges] == [("TokenService.refresh", "calls")]

    assert store.edges_for("Local.refresh", kinds=["calls"]) == []
    assert len(store.edges_for("Local.refresh", kinds=["imports"])) == 1
    assert store.edges_for("nobody") == []


def test_freshness_and_counts(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    assert store.freshness().indexed_at is None
    _seed(store, make_parsed, make_chunk)
    freshness = store.freshness()
    assert freshness.indexed_at is not None and freshness.indexed_at > 0
    assert freshness.stale_files == () and freshness.indexing_files == ()
    counts = store.counts()
    assert counts["files"] == 1 and counts["chunks"] == 3 and counts["symbols"] == 2
    assert counts["edges"] == 2


def test_config_get_set_upsert(store: Store) -> None:
    assert store.get_config("embedding_model") is None
    store.set_config("embedding_model", "local:onnx:e5-small")
    assert store.get_config("embedding_model") == "local:onnx:e5-small"
    store.set_config("embedding_model", "api:bge-m3")
    assert store.get_config("embedding_model") == "api:bge-m3"


def test_spec_block_written_to_both_tables(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    from nova_core.types import CodeFence, SpecBlockDef

    block = SpecBlockDef(
        path="README.md",
        heading="架构",
        heading_path="架构 > 认证模块",
        level=2,
        start_line=3,
        end_line=20,
        content="## 认证模块\n\n刷新流程见 refresh。\n",
        doctype="readme",
        code_fences=(CodeFence(lang="python", content="refresh()", line=9),),
    )
    chunk = make_chunk(
        path="README.md",
        fqn="架构 > 认证模块",
        start=3,
        end=20,
        content=block.content,
        kind="spec_block",
    )
    store.apply_file_change(
        make_parsed(path="README.md", language="markdown", spec_blocks=(block,)), [chunk], "h1"
    )
    row = store.chunk_by_id("README.md:架构 > 认证模块:3")
    assert row is not None and row.symbol_kind == "spec_block"
    assert store.counts()["spec_blocks"] == 1
