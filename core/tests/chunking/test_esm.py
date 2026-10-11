import pytest
from nova_core.chunking import resolve_edges, resolve_pending
from nova_core.chunking.esm import ESMIndex
from nova_core.retrieval.expand import ExpansionLimits, _calls_neighbours, build_flows
from nova_core.types import Candidate


def neighbours(store):
    return _calls_neighbours(store, "src/caller.ts::run", "callees", ExpansionLimits())


@pytest.mark.parametrize("body", [
    "const {job} = obj; job();", "({job} = obj); job();",
    "try {} catch (job) { job(); }", "for (const job of obj) { job(); }",
    "for (var job of obj) {} job();",
    "for (job of obj) {} job();", "for ({job} of obj) {} job();",
])
def test_shadowed_binding_never_becomes_esm_flow(store, ingest_file, body):
    ingest_file("src/lib.ts", "export function work() {}")
    ingest_file("src/caller.ts", 'import {work as job} from "./lib";\n'
                + 'export function run(obj) { ' + body + ' }')
    resolve_pending(store)
    assert neighbours(store) == []
    row = store.exact_symbols("src/caller.ts::run")[0]
    seed = Candidate(row.chunk_id, kind="function", rrf_score=1, score=1,
                     symbol_fqn=row.fqn, path=row.file_path)
    assert build_flows(store, [seed]) == []


@pytest.mark.parametrize("body", [
    "try {} catch (job) {} job();", "for (const job of obj) {} job();",
])
def test_lexical_loop_or_catch_binding_does_not_leak_outside(store, ingest_file, body):
    ingest_file("src/lib.ts", "export function work() {}")
    ingest_file("src/caller.ts", 'import {work as job} from "./lib";\n'
                + 'export function run(obj) { ' + body + ' }')
    assert neighbours(store) == [("src/lib.ts::work", "parsed")]


def test_relative_esm_binding_callers_and_flow_keep_original_binding(store, ingest_file):
    ingest_file("src/lib.ts", "export function work() { return 1; }")
    ingest_file("src/caller.ts",
                'import {work as job} from "./lib";\nexport function run() { job(); }')
    resolve_edges(store)
    assert neighbours(store) == [("src/lib.ts::work", "parsed")]
    callers = _calls_neighbours(store, "src/lib.ts::work", "callers", ExpansionLimits())
    assert callers == [("src/caller.ts::run", "parsed")]
    assert any(e.target == "js:./lib#work" for e in store.edges_for("src/caller.ts::run"))
    row = store.exact_symbols("src/caller.ts::run")[0]
    flows = build_flows(store, [Candidate(
        chunk_id=row.chunk_id, kind="function", rrf_score=1, score=1,
        symbol_fqn=row.fqn, path=row.file_path,
    )])
    assert [n.symbol for n in flows[0].nodes] == ["src/caller.ts::run", "src/lib.ts::work"]


def test_reexport_rename_and_empty_target_invalidate_without_reparsing_caller(store, ingest_file):
    ingest_file("src/lib.ts", "export function work() { return 1; }")
    ingest_file("src/bridge.ts", 'export {work as task} from "./lib";')
    ingest_file("src/caller.ts",
                'import {task} from "./bridge";\nexport function run() { task(); }')
    resolve_edges(store)
    assert neighbours(store) == [("src/lib.ts::work", "parsed")]
    ingest_file("src/lib.ts", "export function renamed() { return 1; }")
    assert neighbours(store) == []
    ingest_file("src/bridge.ts", 'export {renamed as task} from "./lib";')
    assert neighbours(store) == [("src/lib.ts::renamed", "parsed")]
    ingest_file("src/lib.ts", "// implementation removed")
    assert neighbours(store) == []


@pytest.mark.parametrize("mutation", ["work = other;", "work++;", "for (work of values) {}"])
def test_mutated_export_cannot_remain_a_stable_callable(store, ingest_file, mutation):
    ingest_file("src/lib.ts", "export function work() {} " + mutation)
    ingest_file("src/caller.ts", 'import {work} from "./lib"; export function run() { work(); }')
    assert neighbours(store) == []
    assert ESMIndex(store).resolve("js:./lib#work", "src/caller.ts", "imports")


@pytest.mark.parametrize("mutation", [
    "for (lib.work of values) {}", "lib.work++;", "lib[key]++;",
    "({x: lib.work} = values);", "[lib[key]] = values;",
    "for ({x: lib.work} of values) {}",
])
def test_namespace_member_mutation_does_not_keep_old_callable(store, ingest_file, mutation):
    ingest_file("src/lib.ts", "export function work() {}")
    ingest_file("src/caller.ts", 'import * as lib from "./lib";\n'
                + 'export function run(values) { ' + mutation + ' lib.work(); }')
    assert neighbours(store) == []
    assert ESMIndex(store).resolve("js:./lib#work", "src/caller.ts", "imports")


def test_ambiguous_module_does_not_choose_ts_over_existing_js(store, ingest_file):
    ingest_file("src/lib.js", "// existing runtime module, no work export")
    ingest_file("src/lib.ts", "export function work() {}")
    ingest_file("src/caller.ts",
                'import {work} from "./lib.js";\nexport function run() { work(); }')
    assert neighbours(store) == []


def test_dynamic_unknown_does_not_match_foreign_same_name(store, ingest_file):
    ingest_file("fake.py", "def work():\n    return 1")
    ingest_file("src/caller.ts", "export function run() { work(); }")
    report = resolve_pending(store)
    assert report.failed == 1
    assert neighbours(store) == []
    assert store.unresolved_refs(status="failed")


def test_default_and_namespace_imports_and_reexport_cycle(store, ingest_file):
    ingest_file("src/lib.ts", "export default function hidden() {} export function work() {}")
    ingest_file("src/caller.ts", 'import job from "./lib"; import * as lib from "./lib";\n'
                'export function run() { job(); lib.work(); }')
    assert set(neighbours(store)) == {
        ("src/lib.ts::hidden", "parsed"), ("src/lib.ts::work", "parsed"),
    }
    ingest_file("src/a.ts", 'export {task} from "./b";')
    ingest_file("src/b.ts", 'export {task} from "./a";')
    assert ESMIndex(store).resolve("js:./a#task", "src/caller.ts", "calls") == ()


def test_dependency_outside_project_and_package_alias_are_not_followed(store, ingest_file):
    ingest_file("lib.ts", "export function work() {}")
    index = ESMIndex(store)
    assert index.resolve("js:../lib#work", "src/caller.ts", "calls")
    for name in ("js:../../lib#work", "js:package#work", "js:@alias/lib#work", "js:./lib?x#work"):
        assert not index.resolve(name, "src/caller.ts", "calls")


def test_duplicate_js_source_identity_does_not_produce_flow(store, ingest_file):
    ingest_file("src/a.ts", "export function run() { work(); }\n"
                "export function run() { work(); }\nfunction work() {}")
    row = store.exact_symbols("src/a.ts::run")[0]
    seed = Candidate(row.chunk_id, kind="function", rrf_score=1, score=1,
                     symbol_fqn=row.fqn, path=row.file_path)
    assert build_flows(store, [seed]) == []


def test_esm_does_not_build_project_wide_symbol_index(store, ingest_file, monkeypatch):
    ingest_file("src/lib.ts", "export function work() {}")
    ingest_file("src/caller.ts", 'import {work} from "./lib"; export function run() { work(); }')
    def forbidden():
        raise AssertionError("query-time all_symbols must not run")
    monkeypatch.setattr(store, "all_symbols", forbidden)
    assert neighbours(store) == [("src/lib.ts::work", "parsed")]
