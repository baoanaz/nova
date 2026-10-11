import pytest
from nova_core.parsing.registry import detect_language, get_parser


@pytest.mark.parametrize("path,language", [
    ("a.js", "javascript"), ("a.jsx", "javascript"), ("a.mjs", "javascript"),
    ("a.cjs", "javascript"), ("a.ts", "typescript"), ("a.tsx", "tsx"),
    ("a.mts", "typescript"), ("a.cts", "typescript"),
])
def test_new_extensions(path, language):
    assert detect_language(path) == language


def test_functions_classes_calls_and_unicode_are_stable():
    source = '''// 中文字节偏移
export function 工作() { return done(); }
function done() { return 1; }
export class Box {
  run() { this.helper(); }
  helper() { return 1; }
}
export const arrow = () => done();
'''
    parser = get_parser("javascript")
    parsed = parser.parse("src/a.js", source)
    assert not parsed.fallback and not parsed.parse_errors
    assert parsed == parser.parse("src/a.js", source)
    symbols = {s.name: s for s in parsed.symbols}
    assert symbols["工作"].start_line == 2
    assert symbols["run"].fqn == "src/a.js::Box.run"
    assert symbols["arrow"].kind == "function"
    assert all(s.fqn.startswith("src/a.js::") for s in parsed.symbols)
    assert ("src/a.js::工作", "src/a.js::done", 2) in {
        (e.source_fqn, e.target_name, e.line) for e in parsed.edges if e.kind == "calls"
    }


def test_typescript_and_tsx_grammar_cache_are_independent():
    ts = get_parser("typescript")
    tsx = get_parser("tsx")
    source = "export const View = () => <Panel/>;"
    assert ts.parse("a.ts", "export interface Port { run(): void; }").symbols
    assert not tsx.parse("a.tsx", source).fallback
    assert ts.parse("a.ts", source).fallback
    assert not tsx.parse("a.tsx", source).fallback
    assert ts.grammar() is not tsx.grammar()


@pytest.mark.parametrize("body", [
    "function run(job) { job(); }",
    "function run() { job = other; job(); }",
    "function run() { obj.job(); obj[key](); import('./lib'); }",
    "function run(obj) { const {job} = obj; job(); }",
    "function run(obj) { const {other: job} = obj; job(); }",
    "function run(obj) { const [job] = obj; job(); }",
    "function run({job}) { job(); }",
    "function run(obj) { ({job} = obj); job(); }",
    "function run(obj) { [job] = obj; job(); }",
    "function run(obj) { try {} catch (job) { job(); } }",
    "function run(obj) { try {} catch ({job}) { job(); } }",
    "function run(obj) { for (const job of obj) { job(); } }",
    "function run(obj) { for (var job of obj) {} job(); }",
])
def test_dynamic_and_shadowed_calls_are_not_guessed(body):
    parsed = get_parser("typescript").parse("a.ts", 'import { work as job } from "./lib";\n' + body)
    assert not parsed.fallback
    assert not [e for e in parsed.edges if e.kind == "calls"]
    assert parsed.unresolved


def test_default_export_named_function_is_not_named_export():
    parsed = get_parser("javascript").parse("a.js", "export default function hidden() {}")
    symbols = {s.name: s for s in parsed.symbols}
    assert symbols["default"].is_exported
    assert not symbols["hidden"].is_exported
    assert any(e.kind == "exports" and e.target_name == "a.js::hidden" for e in parsed.edges)
    anonymous = get_parser("javascript").parse("b.js", "export default function() {}")
    assert not anonymous.fallback
    assert anonymous.symbols[0].name == "default"


def test_local_export_has_one_symbol_and_block_definition_does_not_leak():
    parsed = get_parser("javascript").parse("a.js", "function foo() {} export {foo};")
    assert len(parsed.symbols) == 1 and parsed.symbols[0].is_exported
    scoped = get_parser("javascript").parse("b.js", "if (ok) { function work() {} } work();")
    assert not [e for e in scoped.edges if e.kind == "calls"]
    assert any(r.name == "work" for r in scoped.unresolved)


def test_long_binding_is_not_truncated_into_another_valid_name():
    parsed = get_parser("typescript").parse(
        "a.ts", 'import {foo} from "./' + "x" * 200 + '"; foo();',
    )
    assert not parsed.edges
    assert parsed.unresolved


def test_bad_syntax_falls_back_without_guessing():
    parsed = get_parser("typescript").parse("a.ts", "export function broken( {")
    assert parsed.fallback and parsed.parse_errors
    assert not parsed.symbols and not parsed.edges
