"""两阶段解析生命周期 / 多义规则 / 裸名边 fqn 化 / spec_references 匹配（TASK-006-B/D）。

DoD 覆盖：pending → resolved（落边 + 删行）；无命中 → failed 保留；新符号后 retry_failed 命中；
同名两符号（same-file / exported）连边策略与 ResolveReport 符合卡内口径；真实 SQLite 集成。
"""

from __future__ import annotations

from collections.abc import Callable

from nova_core.chunking import (
    link_spec_references,
    resolve_edges,
    resolve_pending,
    retry_failed,
)
from nova_core.hashing import file_content_hash
from nova_core.storage import Store
from nova_core.types import EdgeDef, ParsedFile, UnresolvedRef

SymbolSpecs = Callable[[str, list[tuple[str, str, str, int, int, bool]]], None]


def _upsert_ref(
    store: Store,
    name: str,
    *,
    from_fqn: str = "go",
    file_path: str = "pkg/caller.py",
    kind: str = "call",
    line: int = 5,
) -> None:
    store.upsert_unresolved(
        [UnresolvedRef(from_fqn=from_fqn, name=name, kind=kind, line=line)],
        file_path,
        "python",
    )


def _edges(store: Store, fqn: str) -> list[tuple[str, str, str]]:
    return [(edge.target, edge.kind, edge.provenance) for edge in store.edges_for(fqn)]


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


def test_pending_ref_resolves_and_row_is_deleted(store: Store, add_symbols: SymbolSpecs) -> None:
    add_symbols("pkg/util.py", [("helper", "helper", "function", 1, 3, True)])
    _upsert_ref(store, "helper")

    report = resolve_pending(store)

    assert (report.resolved, report.failed) == (1, 0)
    assert report.ambiguous == ()
    assert ("helper", "calls", "parsed") in _edges(store, "go")
    assert store.unresolved_refs() == []


def test_pending_ref_without_match_becomes_failed_then_retry_hits(
    store: Store, add_symbols: SymbolSpecs
) -> None:
    _upsert_ref(store, "helper")

    report = resolve_pending(store)
    assert (report.resolved, report.failed) == (0, 1)
    failed = store.unresolved_refs(status="failed")
    assert [row.name_tail for row in failed] == ["helper"]
    assert _edges(store, "go") == []

    add_symbols("pkg/util.py", [("helper", "helper", "function", 1, 3, True)])
    retry = retry_failed(store, ["helper"])

    assert (retry.resolved, retry.retried) == (1, 1)
    assert ("helper", "calls", "parsed") in _edges(store, "go")
    assert store.unresolved_refs(status="failed") == []


def test_retry_failed_ignores_unrelated_names(store: Store, add_symbols: SymbolSpecs) -> None:
    _upsert_ref(store, "helper")
    resolve_pending(store)

    add_symbols("pkg/util.py", [("other", "other", "function", 1, 3, True)])
    retry = retry_failed(store, ["other"])

    assert (retry.resolved, retry.retried) == (0, 0)
    assert len(store.unresolved_refs(status="failed")) == 1


def test_unresolvable_shapes_are_marked_failed_without_edges(
    store: Store, add_symbols: SymbolSpecs
) -> None:
    """表达式/动态引用只有一个同尾符号时也不得连边（诚实优先）。"""
    add_symbols("pkg/util.py", [("method", "method", "function", 1, 3, True)])
    _upsert_ref(store, "obj.method()")
    _upsert_ref(store, "getattr(obj, 'x')")

    report = resolve_pending(store)

    assert (report.resolved, report.failed) == (0, 2)
    assert _edges(store, "go") == []


# ---------------------------------------------------------------------------
# 多义规则
# ---------------------------------------------------------------------------


def test_same_file_candidate_wins_before_exported(store: Store, add_symbols: SymbolSpecs) -> None:
    add_symbols(
        "pkg/caller.py", [("refresh", "caller.refresh", "function", 1, 3, False)]
    )
    add_symbols("pkg/token.py", [("refresh", "token.refresh", "function", 1, 3, True)])
    _upsert_ref(store, "refresh")

    report = resolve_pending(store)

    assert report.resolved == 1
    assert report.ambiguous == ()
    assert ("caller.refresh", "calls", "parsed") in _edges(store, "go")


def test_exported_candidate_wins_when_no_same_file_hit(
    store: Store, add_symbols: SymbolSpecs
) -> None:
    add_symbols("pkg/a.py", [("refresh", "a.refresh", "function", 1, 3, False)])
    add_symbols("pkg/b.py", [("refresh", "b.refresh", "function", 1, 3, True)])
    _upsert_ref(store, "refresh", from_fqn="run", file_path="pkg/c.py")

    report = resolve_pending(store)

    assert report.resolved == 1
    assert ("b.refresh", "calls", "parsed") in _edges(store, "run")


def test_ambiguous_refs_connect_all_as_synthesized(store: Store, add_symbols: SymbolSpecs) -> None:
    add_symbols("pkg/a.py", [("refresh", "a.refresh", "function", 1, 3, False)])
    add_symbols("pkg/b.py", [("refresh", "b.refresh", "function", 1, 3, False)])
    _upsert_ref(store, "refresh", from_fqn="run", file_path="pkg/c.py", kind="call", line=9)

    report = resolve_pending(store)

    assert report.resolved == 2  # 一条引用落两条边
    assert len(report.ambiguous) == 1
    entry = report.ambiguous[0]
    assert entry.origin == "pending"
    assert entry.name == "refresh"
    assert entry.candidates == ("pkg/a.py:a.refresh:1", "pkg/b.py:b.refresh:1")
    assert set(_edges(store, "run")) == {
        ("a.refresh", "calls", "synthesized"),
        ("b.refresh", "calls", "synthesized"),
    }
    assert store.unresolved_refs() == []
    assert store.counts()["edges"] == 2


# ---------------------------------------------------------------------------
# 裸名边 fqn 化（R8）
# ---------------------------------------------------------------------------


def test_bare_call_edge_is_retargeted_to_unique_symbol(
    store: Store, add_symbols: SymbolSpecs
) -> None:
    parsed = ParsedFile(
        path="pkg/caller.py",
        language="python",
        symbols=(
            _symbol("caller", "caller", 1, 3),
        ),
        edges=(EdgeDef(source_fqn="caller", target_name="helper", kind="calls", line=2),),
    )
    store.apply_file_change(parsed, [], file_content_hash(b"caller"))
    add_symbols("pkg/util.py", [("helper", "util.helper", "function", 1, 3, False)])

    report = resolve_edges(store)

    assert report.edges_retargeted == 1
    assert report.edges_unresolved == 0
    assert ("util.helper", "calls", "parsed") in _edges(store, "caller")
    assert store.unresolved_edges() == []


def test_import_edge_uses_module_path_hints(store: Store, add_symbols: SymbolSpecs) -> None:
    """``from .token import refresh``：同名符号只在 ``pkg/token.py`` 上，不跨文件猜。"""
    add_symbols("pkg/token.py", [("refresh", "token.refresh", "function", 1, 3, False)])
    add_symbols("pkg/other.py", [("refresh", "other.refresh", "function", 1, 3, False)])
    _upsert_ref(store, ".token.refresh", from_fqn="pkg/caller.py", kind="import", line=1)

    report = resolve_pending(store)

    assert report.resolved == 1
    assert report.ambiguous == ()
    assert ("token.refresh", "imports", "parsed") in _edges(store, "pkg/caller.py")


def test_import_edge_without_matching_module_is_failed(
    store: Store, add_symbols: SymbolSpecs
) -> None:
    add_symbols("pkg/other.py", [("refresh", "other.refresh", "function", 1, 3, False)])
    _upsert_ref(store, ".token.refresh", from_fqn="pkg/caller.py", kind="import", line=1)

    report = resolve_pending(store)

    assert (report.resolved, report.failed) == (0, 1)
    assert list(store.unresolved_refs(status="failed"))[0].name_tail == "refresh"


def test_file_like_and_wildcard_targets_are_skipped(store: Store) -> None:
    parsed = ParsedFile(
        path="src/a.c",
        language="c",
        symbols=(_symbol("main", "main", 1, 5),),
        edges=(
            EdgeDef(source_fqn="main", target_name="src/util.h", kind="imports", line=1),
            EdgeDef(source_fqn="main", target_name="pkg.*", kind="imports", line=2),
        ),
    )
    store.apply_file_change(parsed, [], file_content_hash(b"a"))

    report = resolve_edges(store)

    assert (report.edges_retargeted, report.edges_skipped) == (0, 2)
    assert len(store.unresolved_edges()) == 2  # 保持原样，不猜


# ---------------------------------------------------------------------------
# spec_references（D-06 / D-42）
# ---------------------------------------------------------------------------


TOKEN_SOURCE = '''class TokenService:
    def refresh(self) -> str:
        return "new"


class LegacyService:
    def refresh(self) -> str:
        return "old"
'''

DOC_SOURCE = """# 精确引用

见 `TokenService.refresh` 实现。

# 同名引用

还有 `refresh` 与 `pkg/token.py`。
"""


def test_spec_reference_matching_fqn_exact_then_all_namesakes(
    store: Store, ingest_file: Callable[..., object]
) -> None:
    parsed_code, _ = ingest_file("pkg/token.py", TOKEN_SOURCE)
    parsed_doc, _ = ingest_file("docs/design.md", DOC_SOURCE)

    report = link_spec_references(store, [parsed_doc])

    blocks = {block.heading: block for block in parsed_doc.spec_blocks}
    exact_block = _spec_id(parsed_doc.path, blocks["精确引用"])
    loose_block = _spec_id(parsed_doc.path, blocks["同名引用"])
    method = _symbol_id(parsed_code, "TokenService.refresh")
    legacy = _symbol_id(parsed_code, "LegacyService.refresh")

    exact_rows = store.spec_refs_for_spec(exact_block)
    loose_rows = store.spec_refs_for_spec(loose_block)

    assert report.spec_refs == 4
    # fqn 精确匹配优先：``TokenService.refresh`` 不牵连 LegacyService.refresh
    assert method in {row.symbol_id for row in exact_rows}
    assert legacy not in {row.symbol_id for row in exact_rows}
    # 同名歧义全挂（宁多勿漏）
    assert {row.symbol_id for row in loose_rows} == {method, legacy}
    assert all(row.provenance == "inferred" for row in exact_rows + loose_rows)
    assert all(row.stale is False for row in exact_rows + loose_rows)


def test_spec_reference_ids_match_spec_blocks_table(
    store: Store, ingest_file: Callable[..., object]
) -> None:
    _, _ = ingest_file("pkg/token.py", TOKEN_SOURCE)
    parsed_doc, _ = ingest_file("docs/design.md", DOC_SOURCE)
    link_spec_references(store, [parsed_doc])

    block = parsed_doc.spec_blocks[0]
    spec_id = _spec_id(parsed_doc.path, block)
    assert store.spec_refs_for_spec(spec_id)  # 双表同 id：能查到引用行


def test_spec_references_go_stale_when_symbol_file_is_deleted(
    store: Store, ingest_file: Callable[..., object]
) -> None:
    ingest_file("pkg/token.py", TOKEN_SOURCE)
    parsed_doc, _ = ingest_file("docs/design.md", DOC_SOURCE)
    link_spec_references(store, [parsed_doc])
    spec_id = _spec_id(parsed_doc.path, parsed_doc.spec_blocks[0])
    assert any(not row.stale for row in store.spec_refs_for_spec(spec_id))

    store.apply_deletions(["pkg/token.py"])

    assert all(row.stale for row in store.spec_refs_for_spec(spec_id))


def test_link_spec_references_ignores_paths_and_expressions(
    store: Store, ingest_file: Callable[..., object]
) -> None:
    parsed_code, _ = ingest_file("pkg/token.py", TOKEN_SOURCE)
    doc = """# 路径

`docs/design.md` 与 `pkg/token.py`、`TokenService.refresh()` 与 `if x == 1`。
"""
    parsed_doc, _ = ingest_file("docs/note.md", doc)

    report = link_spec_references(store, [parsed_doc])

    block = parsed_doc.spec_blocks[0]
    rows = store.spec_refs_for_spec(_spec_id(parsed_doc.path, block))
    # 路径与表达式形态不建引用；命中来自 ``TokenService.refresh()`` 与其 camelCase 词
    assert report.spec_refs == 2
    assert {row.symbol_id for row in rows} == {
        _symbol_id(parsed_code, "TokenService.refresh"),
        _symbol_id(parsed_code, "TokenService"),
    }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _symbol(name: str, fqn: str, start: int, end: int):  # type: ignore[no-untyped-def]
    from nova_core.types import SymbolDef

    return SymbolDef(name=name, fqn=fqn, kind="function", start_line=start, end_line=end)


def _spec_id(path: str, block) -> str:  # type: ignore[no-untyped-def]
    return f"{path}:{block.heading_path}:{block.start_line}"


def _symbol_id(parsed: ParsedFile, fqn: str) -> str:
    symbol = next(item for item in parsed.symbols if item.fqn == fqn)
    return f"{parsed.path}:{symbol.fqn}:{symbol.start_line}"
