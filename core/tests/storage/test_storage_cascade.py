"""级联删除与 spec_references.stale 传播（Module/01 §4.1）。"""

from __future__ import annotations

from collections.abc import Callable

from nova_core.storage import Store
from nova_core.text import segment
from nova_core.types import (
    ChunkDef,
    CodeFence,
    EdgeDef,
    ParsedFile,
    SpecBlockDef,
    SymbolDef,
)

SPEC_ID = "docs/design.md:刷新流程:1"


def _write_code_file(
    store: Store,
    make_parsed: Callable[..., ParsedFile],
    make_chunk: Callable[..., ChunkDef],
    make_symbol: Callable[..., SymbolDef],
    *,
    path: str = "src/a.py",
    names: tuple[str, str] = ("f", "g"),
) -> tuple[SymbolDef, ...]:
    fqn_a, fqn_b = names
    symbols = (
        make_symbol(name=fqn_a, fqn=fqn_a, start=1, end=3, is_exported=True),
        make_symbol(name=fqn_b, fqn=fqn_b, start=5, end=7),
    )
    chunks = [
        make_chunk(path=path, fqn=fqn_a, start=1, content=f"def {fqn_a}():\n    return 1\n"),
        make_chunk(path=path, fqn=fqn_b, start=5, content=f"def {fqn_b}():\n    return 2\n"),
    ]
    parsed = make_parsed(
        path=path,
        symbols=symbols,
        edges=(EdgeDef(source_fqn=fqn_a, target_name=fqn_b, kind="calls", line=2),),
    )
    store.apply_file_change(parsed, chunks, f"file-hash-{path}")
    return symbols


def _write_spec_file(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> str:
    path = "docs/design.md"
    block = SpecBlockDef(
        path=path,
        heading="刷新流程",
        heading_path="刷新流程",
        level=1,
        start_line=1,
        end_line=9,
        content="# 刷新流程\n\n调用 f 完成刷新。\n",
        doctype="design",
        code_fences=(CodeFence(lang="python", content="f()", line=3),),
        mentioned=("f",),
    )
    chunk = make_chunk(
        path=path,
        fqn="刷新流程",
        start=1,
        end=9,
        content=block.content,
        kind="spec_block",
        signature="",
    )
    store.apply_file_change(
        make_parsed(path=path, language="markdown", spec_blocks=(block,)), [chunk], "spec-hash-1"
    )
    return f"{path}:刷新流程:1"


def test_delete_file_clears_rows_and_marks_spec_refs_stale(
    store: Store,
    make_parsed: Callable[..., ParsedFile],
    make_chunk: Callable[..., ChunkDef],
    make_symbol: Callable[..., SymbolDef],
) -> None:
    _write_code_file(store, make_parsed, make_chunk, make_symbol)
    spec_id = _write_spec_file(store, make_parsed, make_chunk)
    assert store.add_spec_refs([(spec_id, "src/a.py:f:1")]) == 1
    assert store.add_spec_refs([(spec_id, "src/a.py:f:1")]) == 0

    store.apply_deletions(["src/a.py"])

    assert store.counts() == {
        "files": 1,  # 只剩 docs/design.md
        "chunks": 1,
        "symbols": 0,
        "edges": 0,
        "spec_blocks": 1,
        "refs_pending": 0,
        "refs_failed": 0,
    }
    assert store.fts_search(segment("return 1"), 10) == []
    refs = store.spec_refs_for_spec(spec_id)
    assert len(refs) == 1 and refs[0].stale is True
    assert refs[0].provenance == "inferred"


def test_delete_keeps_incoming_edges_from_other_files(
    store: Store,
    make_parsed: Callable[..., ParsedFile],
    make_chunk: Callable[..., ChunkDef],
    make_symbol: Callable[..., SymbolDef],
) -> None:
    _write_code_file(store, make_parsed, make_chunk, make_symbol)
    store.apply_file_change(
        make_parsed(
            path="src/b.py",
            symbols=(make_symbol(name="h", fqn="h", start=1, end=3),),
            edges=(EdgeDef(source_fqn="h", target_name="f", kind="calls", line=2),),
        ),
        [make_chunk(path="src/b.py", fqn="h", start=1, content="def h():\n    f()\n")],
        "file-hash-b",
    )

    store.apply_deletions(["src/a.py"])

    # 本文件发出的边（f→g）随文件删除；其它文件指向 f 的入边保留（不误删他人数据）。
    remaining = store.edges_for("f")
    assert [(e.source, e.target, e.kind) for e in remaining] == [("h", "f", "calls")]
    assert store.edges_for("g") == []


def test_delete_spec_file_clears_its_spec_blocks_and_refs(
    store: Store,
    make_parsed: Callable[..., ParsedFile],
    make_chunk: Callable[..., ChunkDef],
    make_symbol: Callable[..., SymbolDef],
) -> None:
    _write_code_file(store, make_parsed, make_chunk, make_symbol)
    spec_id = _write_spec_file(store, make_parsed, make_chunk)
    store.add_spec_refs([(spec_id, "src/a.py:f:1")])

    store.apply_deletions(["docs/design.md"])

    assert store.spec_refs_for_spec(spec_id) == []
    assert store.counts()["spec_blocks"] == 0
    assert store.chunk_by_id(spec_id) is None


def test_delete_is_idempotent(store: Store) -> None:
    store.apply_deletions(["missing/file.py"])
    assert store.counts()["files"] == 0


def test_apply_deletions_returns_removed_chunk_ids(
    store: Store,
    make_parsed: Callable[..., ParsedFile],
    make_chunk: Callable[..., ChunkDef],
    make_symbol: Callable[..., SymbolDef],
) -> None:
    """P1-2：删除要返回被删 chunk id，调用方据此清向量（防跨进程孤儿向量）。

    为什么这个返回值是必需的：``Engine`` 每次 ingest 新建 ``Indexer``，跨调用删除时
    进程内的 ``_known_chunks`` 是空的，旧行为只删 SQLite 不删 LanceDB。
    """
    _write_code_file(store, make_parsed, make_chunk, make_symbol)

    # 期望值：库内真实存在的 id（make_chunk 的 id = ``{path}:{fqn}:{start}``）。
    expected = {"src/a.py:f:1", "src/a.py:g:5"}
    assert {c.id for c in store.chunks_by_ids(sorted(expected))} == expected

    removed = store.apply_deletions(["src/a.py"])

    assert set(removed) == expected
    # 幂等：再删一次（路径已不存在）不应重复上报 id。
    assert store.apply_deletions(["src/a.py"]) == ()
    assert store.apply_deletions(["missing/file.py"]) == ()


def test_overload_same_fqn_all_refs_marked_stale(
    store: Store,
    make_parsed: Callable[..., ParsedFile],
    make_chunk: Callable[..., ChunkDef],
    make_symbol: Callable[..., SymbolDef],
) -> None:
    """同名 fqn 多符号（C++ 重载）：fqn 消失时所有同 fqn 符号的引用都置 stale=1。"""
    path = "src/ov.cpp"
    first_content = "int f() { return 1; }\n"
    second_content = "int f(int x) { return x; }\n"
    symbols = (
        make_symbol(name="f", fqn="f", start=1, end=3),
        make_symbol(name="f", fqn="f", start=10, end=12),
    )
    store.apply_file_change(
        make_parsed(path=path, language="cpp", symbols=symbols),
        [
            make_chunk(path=path, fqn="f", start=1, content=first_content),
            make_chunk(path=path, fqn="f", start=10, content=second_content),
        ],
        "h1",
    )
    spec_id = _write_spec_file(store, make_parsed, make_chunk)
    symbol_ids = [f"{path}:f:1", f"{path}:f:10"]
    assert store.add_spec_refs([(spec_id, sid) for sid in symbol_ids]) == 2

    store.apply_file_change(
        make_parsed(
            path=path,
            language="cpp",
            symbols=(
                make_symbol(name="g", fqn="g", start=1, end=3),
                make_symbol(name="g", fqn="g", start=10, end=12),
            ),
        ),
        [
            make_chunk(path=path, fqn="g", start=1, content=first_content),
            make_chunk(path=path, fqn="g", start=10, content=second_content),
        ],
        "h2",
    )
    refs = store.spec_refs_for_symbols(symbol_ids)
    assert len(refs) == 2 and all(r.stale for r in refs)


def test_rename_marks_refs_stale_and_line_shift_keeps_them(
    store: Store,
    make_parsed: Callable[..., ParsedFile],
    make_chunk: Callable[..., ChunkDef],
    make_symbol: Callable[..., SymbolDef],
) -> None:
    _write_code_file(store, make_parsed, make_chunk, make_symbol, names=("f", "g"))
    spec_id = _write_spec_file(store, make_parsed, make_chunk)
    store.add_spec_refs([(spec_id, "src/a.py:f:1")])

    # 行号漂移（fqn 不变）→ refs 重挂到新 id，不置 stale。
    shifted = make_chunk(fqn="f", start=2, content="def f():\n    return 1\n")
    shifted_symbol = make_symbol(name="f", fqn="f", start=2, end=4, is_exported=True)
    store.apply_file_change(
        make_parsed(symbols=(shifted_symbol, make_symbol(name="g", fqn="g", start=6, end=8))),
        [shifted, make_chunk(fqn="g", start=6, content="def g():\n    return 2\n")],
        "file-hash-2",
    )
    refs = store.spec_refs_for_symbols(["src/a.py:f:2"])
    assert len(refs) == 1 and refs[0].stale is False
    assert store.spec_refs_for_symbols(["src/a.py:f:1"]) == []

    # 改名（fqn 变了）→ 引用被删符号的文档置 stale=1。
    store.apply_file_change(
        make_parsed(
            symbols=(
                make_symbol(name="renamed", fqn="renamed", start=2, end=4, is_exported=True),
                make_symbol(name="g", fqn="g", start=6, end=8),
            )
        ),
        [
            make_chunk(fqn="renamed", start=2, content="def f():\n    return 1\n"),
            make_chunk(fqn="g", start=6, content="def g():\n    return 2\n"),
        ],
        "file-hash-3",
    )
    refs = store.spec_refs_for_symbols(["src/a.py:f:2"])
    assert len(refs) == 1 and refs[0].stale is True
