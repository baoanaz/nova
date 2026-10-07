"""spec_references 写库原语与读写一致性（TASK-006 D 节消费）。"""

from __future__ import annotations

from collections.abc import Callable

from nova_core.storage import Store
from nova_core.types import ChunkDef, ParsedFile, SpecBlockDef, SymbolDef


def _seed_spec_and_code(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> tuple[str, str]:
    code = make_parsed(
        symbols=(SymbolDef(name="f", fqn="f", kind="function", start_line=1, end_line=3),)
    )
    store.apply_file_change(
        code, [make_chunk(fqn="f", start=1, content="def f():\n    pass\n")], "h1"
    )

    block = SpecBlockDef(
        path="docs/design.md",
        heading="刷新",
        heading_path="刷新",
        level=1,
        start_line=1,
        end_line=5,
        content="# 刷新\n\n调用 f。\n",
        doctype="design",
        mentioned=("f",),
    )
    spec_chunk = make_chunk(
        path="docs/design.md", fqn="刷新", start=1, end=5, content=block.content, kind="spec_block"
    )
    store.apply_file_change(
        make_parsed(path="docs/design.md", language="markdown", spec_blocks=(block,)),
        [spec_chunk],
        "h2",
    )
    return "docs/design.md:刷新:1", "src/a.py:f:1"


def test_add_spec_refs_is_idempotent(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    spec_id, symbol_id = _seed_spec_and_code(store, make_parsed, make_chunk)
    assert store.add_spec_refs([(spec_id, symbol_id), (spec_id, symbol_id)]) == 1
    assert store.add_spec_refs([(spec_id, symbol_id)]) == 0

    refs = store.spec_refs_for_spec(spec_id)
    assert len(refs) == 1
    assert refs[0].symbol_id == symbol_id
    assert refs[0].provenance == "inferred" and refs[0].stale is False

    by_symbol = store.spec_refs_for_symbols([symbol_id, "no:such:1"])
    assert [(r.spec_block_id, r.symbol_id) for r in by_symbol] == [(spec_id, symbol_id)]
    assert store.spec_refs_for_symbols([]) == []


def test_rewriting_spec_file_clears_its_old_refs(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    spec_id, symbol_id = _seed_spec_and_code(store, make_parsed, make_chunk)
    store.add_spec_refs([(spec_id, symbol_id)])

    block = SpecBlockDef(
        path="docs/design.md",
        heading="刷新",
        heading_path="刷新",
        level=1,
        start_line=1,
        end_line=6,
        content="# 刷新\n\n改版：调用 f。\n",
        doctype="design",
        mentioned=("f",),
    )
    spec_chunk = make_chunk(
        path="docs/design.md", fqn="刷新", start=1, end=6, content=block.content, kind="spec_block"
    )
    store.apply_file_change(
        make_parsed(path="docs/design.md", language="markdown", spec_blocks=(block,)),
        [spec_chunk],
        "h3",
    )

    # 旧引用行随 spec 文件重写清除（等 TASK-006 重匹配重建，保证二次 ingest 不累积）。
    assert store.spec_refs_for_spec(spec_id) == []
