"""两阶段解析的存储原语：upsert / resolve / failed / 裸名边重定向（TASK-006 消费）。"""

from __future__ import annotations

from collections.abc import Callable

from nova_core.storage import EdgeTargetUpdate, RefResolution, Store
from nova_core.types import ChunkDef, EdgeDef, ParsedFile, SymbolDef, UnresolvedRef


def test_apply_file_change_writes_pending_refs_with_name_tail(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    parsed = make_parsed(
        unresolved=(
            UnresolvedRef(from_fqn="f", name="util.greet", kind="call", line=2),
            UnresolvedRef(from_fqn="f", name="util.greet", kind="call", line=2),  # 批内重复
        )
    )
    store.apply_file_change(parsed, [make_chunk()], "h1")
    refs = store.unresolved_refs()
    assert len(refs) == 1
    assert refs[0].status == "pending"
    assert refs[0].name_tail == "greet"
    assert refs[0].file_path == "src/a.py"
    assert store.counts()["refs_pending"] == 1


def test_upsert_unresolved_dedupes(store: Store) -> None:
    ref = UnresolvedRef(from_fqn="f", name="A.refresh", kind="call", line=3)
    assert store.upsert_unresolved([ref], file_path="src/a.py", language="python") == 1
    assert store.upsert_unresolved([ref], file_path="src/a.py", language="python") == 0
    assert store.upsert_unresolved([ref], file_path="src/b.py", language="python") == 0
    assert len(store.unresolved_refs()) == 1


def test_resolve_refs_writes_edge_and_deletes_row(store: Store) -> None:
    store.upsert_unresolved(
        [UnresolvedRef(from_fqn="f", name="greet", kind="call", line=2)],
        file_path="src/a.py",
        language="python",
    )
    ref = store.unresolved_refs()[0]

    assert store.resolve_refs([RefResolution(ref_id=ref.id, target_fqn="util.greet")]) == 1
    assert store.unresolved_refs() == []
    edge = store.edges_for("util.greet")[0]
    assert (edge.source, edge.target, edge.kind) == ("f", "util.greet", "calls")  # call → calls
    assert edge.provenance == "parsed"

    # 已删除的 ref 再解析 → 0（幂等）。
    assert store.resolve_refs([RefResolution(ref_id=ref.id, target_fqn="util.greet")]) == 0


def test_resolve_refs_allows_kind_and_provenance_override(store: Store) -> None:
    store.upsert_unresolved(
        [UnresolvedRef(from_fqn="f", name="refresh", kind="reference", line=4)],
        file_path="src/a.py",
        language="python",
    )
    ref = store.unresolved_refs()[0]
    store.resolve_refs(
        [
            RefResolution(
                ref_id=ref.id, target_fqn="A.refresh", kind="calls", provenance="synthesized"
            )
        ]
    )
    edge = store.edges_for("A.refresh")[0]
    assert edge.kind == "calls" and edge.provenance == "synthesized"


def test_mark_refs_failed_keeps_row_for_retry(store: Store) -> None:
    store.upsert_unresolved(
        [UnresolvedRef(from_fqn="f", name="A::refresh", kind="call", line=1)],
        file_path="src/a.py",
        language="python",
    )
    ref = store.unresolved_refs(status="pending")[0]
    store.upsert_unresolved([UnresolvedRef(from_fqn="g", name="lonely", kind="call", line=9)])

    assert store.mark_refs_failed([ref.id]) == 1
    assert store.mark_refs_failed([999]) == 0
    failed = store.unresolved_refs(status="failed")
    assert len(failed) == 1
    assert failed[0].name_tail == "refresh"
    assert len(store.unresolved_refs(status="pending")) == 1
    assert store.counts()["refs_failed"] == 1


def test_unresolved_edges_and_retarget(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    parsed = make_parsed(
        symbols=(
            SymbolDef(name="greet", fqn="util.greet", kind="function", start_line=1, end_line=3),
        ),
        edges=(
            EdgeDef(source_fqn="f", target_name="greet", kind="calls", line=2),
            EdgeDef(source_fqn="f", target_name="int", kind="calls", line=3),  # 项目外符号
        ),
    )
    store.apply_file_change(parsed, [make_chunk()], "h1")

    raw = store.unresolved_edges(kinds=["calls"])
    assert [(e.target, e.line) for e in raw] == [("greet", 2), ("int", 3)]

    changed = store.retarget_edges(
        [
            EdgeTargetUpdate(
                source="f", target="greet", kind="calls", line=2, new_target="util.greet"
            ),
            EdgeTargetUpdate(
                source="f", target="int", kind="calls", line=3, new_target="builtins.int"
            ),
        ]
    )
    assert changed == 2
    # 落在项目符号 fqn 上的边出列；项目外目标（builtins.int）保持可见，不谎报已解析。
    assert [e.target for e in store.unresolved_edges(kinds=["calls"])] == ["builtins.int"]
    assert {e.target for e in store.edges_for("f")} == {"util.greet", "builtins.int"}


def test_retarget_collision_drops_duplicate_row(
    store: Store, make_parsed: Callable[..., ParsedFile], make_chunk: Callable[..., ChunkDef]
) -> None:
    parsed = make_parsed(
        edges=(
            EdgeDef(source_fqn="f", target_name="greet", kind="calls", line=2),
            EdgeDef(source_fqn="f", target_name="util.greet", kind="calls", line=2),
        )
    )
    store.apply_file_change(parsed, [make_chunk()], "h1")

    changed = store.retarget_edges(
        [
            EdgeTargetUpdate(
                source="f", target="greet", kind="calls", line=2, new_target="util.greet"
            )
        ]
    )
    assert changed == 1
    assert len(store.edges_for("f")) == 1  # 唯一索引（source,target,kind,line）下只剩一条
