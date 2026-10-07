"""TASK-004：C++ 抽取器验收测试（尽力而为 + 诚实标注，D-08）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from nova_core.parsing.cpp import CppParser
from nova_core.types import EdgeDef, ParsedFile, SymbolDef

SAMPLE_DIR = Path(__file__).parent / "samples" / "cpp"
SAMPLE_PREFIX = "core/tests/parsing/samples/cpp/"
SYNTAX_ERROR_SOURCE = '#include "broken.hpp"\n\nnamespace app {\nclass Broken : public {\n'
PARTIAL_ERROR_SOURCE = """int before() { return 1; }
struct Broken {
  int value GUARDED_BY(mu);
};
int after() { return 2; }
"""


@pytest.fixture(scope="module")
def parser() -> CppParser:
    return CppParser()


def parse_sample(parser: CppParser, name: str) -> ParsedFile:
    path = SAMPLE_DIR / name
    return parser.parse(SAMPLE_PREFIX + name, path.read_text(encoding="utf-8"))


def symbol(pf: ParsedFile, fqn: str) -> SymbolDef:
    match = next((item for item in pf.symbols if item.fqn == fqn), None)
    assert match is not None, f"缺少符号 {fqn}（实际：{[s.fqn for s in pf.symbols]}）"
    return match


def edges(pf: ParsedFile, kind: str, source: str | None = None) -> list[EdgeDef]:
    return [
        edge
        for edge in pf.edges
        if edge.kind == kind and (source is None or edge.source_fqn == source)
    ]


def targets(pf: ParsedFile, kind: str, source: str | None = None) -> set[str]:
    return {edge.target_name for edge in edges(pf, kind, source)}


def unresolved_names(pf: ParsedFile, kind: str | None = None) -> set[str]:
    return {item.name for item in pf.unresolved if kind is None or item.kind == kind}


def test_sample_corpus_has_enough_files() -> None:
    files = list(SAMPLE_DIR.rglob("*.hpp")) + list(SAMPLE_DIR.rglob("*.cpp"))
    assert len(files) >= 10


def test_namespace_nesting_and_out_of_class_definitions(parser: CppParser) -> None:
    header = parse_sample(parser, "namespaces/nested.hpp")
    assert symbol(header, "outer").kind == "namespace"
    assert symbol(header, "outer::inner").kind == "namespace"
    assert symbol(header, "outer::inner::Widget").kind == "class"
    assert symbol(header, "outer::inner::Widget::compute").kind == "method"
    assert symbol(header, "outer::inner::Widget::Widget").kind == "method"  # 构造函数
    assert symbol(header, "outer::inner::Widget::~Widget").kind == "method"  # 析构函数

    source = parse_sample(parser, "namespaces/nested.cpp")
    # 类外定义（A::B::foo）与类内声明同 fqn（chunk_id 靠 start_line 消歧，D-04）
    assert symbol(source, "outer::inner::Widget::run").kind == "method"
    assert symbol(source, "outer::inner::Widget::reset").kind == "method"
    assert symbol(source, "outer::inner::Widget::Widget").kind == "method"
    assert targets(source, "calls", source="outer::inner::Widget::run") == {"compute"}


def test_overloads_are_separate_symbols(parser: CppParser) -> None:
    pf = parse_sample(parser, "overload/overload.cpp")
    overloads = [item for item in pf.symbols if item.name == "compute"]
    assert len(overloads) == 3  # 两个重载 + namespace math 里一个
    assert len({item.start_line for item in overloads}) == 3  # start_line 天然消歧（D-04）
    assert {item.fqn for item in overloads} == {"compute", "math::compute"}


def test_templates_are_single_symbols(parser: CppParser) -> None:
    pf = parse_sample(parser, "templates/templates.hpp")
    assert symbol(pf, "algo::max_value").kind == "function"
    assert symbol(pf, "algo::max_value").start_line == 7  # template_declaration 起始行
    boxes = [item for item in pf.symbols if item.fqn == "algo::Box"]
    assert len(boxes) == 2  # 主模板 + 特化（都保留，start_line 消歧）
    assert all(item.kind == "class" for item in boxes)
    # 特化不静默丢弃：记 unresolved
    assert "Box<bool>" in unresolved_names(pf, "reference")
    # 显式实例化：跳过 + parse_errors 如实标注（不谎称已解析）
    assert any("explicit template instantiation skipped" in e for e in pf.parse_errors)
    assert all("double" not in item.fqn for item in pf.symbols)


def test_template_call_resolves_to_template_definition(parser: CppParser) -> None:
    pf = parse_sample(parser, "templates/use_templates.cpp")
    assert targets(pf, "calls", source="use::use_templates") == {"algo::max_value"}
    assert targets(pf, "calls", source="use::use_box") == {"box.set", "box.get"}


def test_inheritance_and_overrides(parser: CppParser) -> None:
    pf = parse_sample(parser, "inheritance/service.hpp")
    assert symbol(pf, "app::Service").kind == "class"
    # public/protected 一视同仁：都产 extends 边
    assert targets(pf, "extends", source="app::Extended") == {"Base", "Service"}
    assert targets(pf, "extends", source="app::Service") == {"Base"}
    assert targets(pf, "imports") == {SAMPLE_PREFIX + "inheritance/base.hpp"}
    # 同文件能对上的基类方法 → overrides 边（synthesized，仅名字匹配）；基类在别的文件 → unresolved
    extended = edges(pf, "overrides", source="app::Extended::run")
    assert [(edge.target_name, edge.provenance) for edge in extended] == [
        ("app::Service::run", "synthesized")
    ]
    assert "Base::run" in unresolved_names(pf, "reference")
    assert not edges(pf, "overrides", source="app::Service::run")  # Base 在 base.hpp，看不见

    local = parse_sample(parser, "inheritance/local_chain.hpp")
    overrides = edges(local, "overrides", source="app::Square::area")
    assert [(edge.target_name, edge.provenance) for edge in overrides] == [
        ("app::Shape::area", "synthesized")
    ]


def test_cross_file_class_method_call(parser: CppParser) -> None:
    caller = parse_sample(parser, "crossfile/caller.cpp")
    assert targets(caller, "imports") == {SAMPLE_PREFIX + "crossfile/service.hpp"}
    # 解析前状态如实产出：calls 边给 target_name，跨文件解析归 TASK-006
    assert edges(caller, "calls", source="app::call_service")[0].target_name == "service.process"
    definition = parse_sample(parser, "crossfile/service.cpp")
    assert symbol(definition, "app::Service::process").kind == "method"


def test_indirect_calls_are_honestly_unresolved(parser: CppParser) -> None:
    """诚实性回归：无法静态定型的间接调用必须落 unresolved，不得硬连边。"""
    pf = parse_sample(parser, "unresolved/indirect.cpp")
    calls = unresolved_names(pf, "call")
    assert "fn" in calls  # 函数指针参数
    assert "twice" in calls  # lambda 变量调用
    assert "(job.*pmf)" in calls  # 成员指针调用
    assert "handler" in calls  # std::function 变量
    assert "((int (*)(void *))raw)" in calls  # 强制转换后的指针调用
    # 这些名字不得出现在任何 parsed 边里（宁可缺边不可错边）
    parsed_targets = {
        edge.target_name for edge in pf.edges if edge.provenance == "parsed"
    }
    assert parsed_targets.isdisjoint({"fn", "twice", "handler", "(job.*pmf)"})


def test_macro_generated_declarations_are_unresolved(parser: CppParser) -> None:
    pf = parse_sample(parser, "unresolved/macro_gen.cpp")
    # 宏调用无分号：不整体兜底，但错误如实保留
    assert pf.fallback is False
    assert any("missing ;" in e for e in pf.parse_errors)
    assert any("token pasting" in e for e in pf.parse_errors)
    assert symbol(pf, "DECLARE_ACCESSOR").kind == "macro"
    assert symbol(pf, "app::Widget::total").kind == "method"
    # 宏生成的成员声明进 unresolved，不被静默丢弃
    references = unresolved_names(pf, "reference")
    assert "DECLARE_ACCESSOR(width)" in references
    assert "DECLARE_ACCESSOR(size)" in references
    assert all(item.kind == "reference" for item in pf.unresolved if "DECLARE" in item.name)


def test_partial_syntax_error_keeps_unaffected_symbols(parser: CppParser) -> None:
    pf = parser.parse("partial.cpp", PARTIAL_ERROR_SOURCE)

    assert pf.fallback is False
    assert any("syntax error" in error for error in pf.parse_errors)
    assert {item.fqn for item in pf.symbols} == {"before", "after"}


def test_syntax_error_falls_back(parser: CppParser) -> None:
    pf = parser.parse(SAMPLE_PREFIX + "broken/broken.cpp", SYNTAX_ERROR_SOURCE)
    assert pf.fallback is True
    assert pf.parse_errors
    assert pf.symbols == () and pf.edges == () and pf.unresolved == ()
    assert pf.language == "cpp"


def test_parse_is_deterministic(parser: CppParser) -> None:
    for path in sorted(SAMPLE_DIR.rglob("*.hpp")) + sorted(SAMPLE_DIR.rglob("*.cpp")):
        relative = path.relative_to(SAMPLE_DIR).as_posix()
        first = parse_sample(parser, relative)
        assert first == parse_sample(parser, relative)
        assert first == parse_sample(CppParser(), relative)
        assert list(first.symbols) == sorted(
            first.symbols, key=lambda s: (s.start_line, s.end_line, s.fqn)
        )
        assert list(first.edges) == sorted(
            first.edges, key=lambda e: (e.line or 0, e.source_fqn, e.kind, e.target_name)
        )
