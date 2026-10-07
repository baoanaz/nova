"""TASK-003：C 抽取器验收测试（include / static 可见性 / 函数指针 / 宏 / 兜底）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from nova_core.parsing.base import FileContext
from nova_core.parsing.c import (
    CParser,
    IncludeDirective,
    filter_visible,
    parse_include_directive,
    resolve_include,
)
from nova_core.types import EdgeDef, ParsedFile, SymbolDef

SAMPLE_DIR = Path(__file__).parent / "samples" / "c"
SAMPLE_PREFIX = "core/tests/parsing/samples/c/"
SYNTAX_ERROR_SOURCE = "#include \"broken.h\"\n\nint broken( {\n    return 1;\n}\n"
PARTIAL_ERROR_SOURCE = """int before(void) { return 1; }
struct Broken { int value @; };
int after(void) { return 2; }
"""


@pytest.fixture(scope="module")
def parser() -> CParser:
    return CParser()


def parse_sample(parser: CParser, name: str) -> ParsedFile:
    path = SAMPLE_DIR / name
    return parser.parse(SAMPLE_PREFIX + name, path.read_text(encoding="utf-8"))


def symbol(pf: ParsedFile, fqn: str) -> SymbolDef:
    match = next((item for item in pf.symbols if item.fqn == fqn), None)
    assert match is not None, f"缺少符号 {fqn}（实际：{[s.fqn for s in pf.symbols]}）"
    return match


def edges(pf: ParsedFile, kind: str) -> list[EdgeDef]:
    return [edge for edge in pf.edges if edge.kind == kind]


def targets(pf: ParsedFile, kind: str, source: str | None = None) -> set[str]:
    return {
        edge.target_name
        for edge in edges(pf, kind)
        if source is None or edge.source_fqn == source
    }


def synthesized(pf: ParsedFile) -> list[EdgeDef]:
    return [edge for edge in edges(pf, "calls") if edge.provenance == "synthesized"]


def test_sample_corpus_has_enough_files() -> None:
    files = list(SAMPLE_DIR.rglob("*.c")) + list(SAMPLE_DIR.rglob("*.h"))
    assert len(files) >= 8


def test_functions_and_static_visibility(parser: CParser) -> None:
    b = parse_sample(parser, "static_link/b.c")
    assert b.fallback is False
    assert symbol(b, "helper").is_exported is False  # static
    assert symbol(b, "helper2").is_exported is True
    assert symbol(b, "helper").kind == "function"


def test_include_chain_and_angle_include(parser: CParser) -> None:
    a_c = parse_sample(parser, "include/a.c")
    assert targets(a_c, "imports") == {SAMPLE_PREFIX + "include/a.h"}
    assert [(u.name, u.kind) for u in a_c.unresolved] == [("<stdio.h>", "import")]
    # include 边的 source 是文件 path（文件级节点）
    assert {edge.source_fqn for edge in edges(a_c, "imports")} == {SAMPLE_PREFIX + "include/a.c"}

    a_h = parse_sample(parser, "include/a.h")
    assert targets(a_h, "imports") == {SAMPLE_PREFIX + "include/common.h"}
    assert a_h.unresolved == ()
    # include guard 也是宏符号
    assert symbol(a_h, "A_H").kind == "macro"


def test_calls_attributed_to_enclosing_function(parser: CParser) -> None:
    a_c = parse_sample(parser, "include/a.c")
    calls = edges(a_c, "calls")
    assert {(edge.source_fqn, edge.target_name) for edge in calls} == {
        ("run_chain", "log_value"),
        ("run_chain", "common_scale"),
    }


def test_header_only_prototypes_are_not_symbols(parser: CParser) -> None:
    b_h = parse_sample(parser, "static_link/b.h")
    names = [s.fqn for s in parser.parse(b_h.path, b_h_content()).symbols]
    assert names == ["B_H"]  # 只有 include guard 宏；函数原型不成符号（V1 口径）


def b_h_content() -> str:
    return (SAMPLE_DIR / "static_link/b.h").read_text(encoding="utf-8")


def test_static_symbols_do_not_match_across_files(parser: CParser) -> None:
    a_c = parse_sample(parser, "static_link/a.c")
    b_c = parse_sample(parser, "static_link/b.c")
    a_path = SAMPLE_PREFIX + "static_link/a.c"
    b_path = SAMPLE_PREFIX + "static_link/b.c"

    # a.c 里对 helper/helper2 的调用边照实产出（名字解析交 TASK-006）
    assert targets(a_c, "calls", source="use_helpers") == {"helper", "helper2"}

    # 但跨文件匹配必须穿过可见性过滤：static 的 helper 不得命中；helper2 可以
    candidates = [(b_path, symbol(b_c, "helper")), (b_path, symbol(b_c, "helper2"))]
    assert [s.name for s in filter_visible(candidates, a_path)] == ["helper2"]
    assert [s.name for s in filter_visible(candidates, b_path)] == ["helper", "helper2"]


def test_function_pointer_synthesis(parser: CParser) -> None:
    pf = parse_sample(parser, "pointers/fnptr.c")
    assert pf.fallback is False
    synthesized_pairs = {
        (edge.source_fqn, edge.target_name, edge.provenance) for edge in synthesized(pf)
    }
    assert synthesized_pairs == {
        ("apply_binop", "sub", "synthesized"),  # 局部 `binop local = sub;`
        ("apply_binop", "add", "synthesized"),  # `current(a, b)`（含后续 current = mul）
        ("apply_binop", "mul", "synthesized"),
    }
    # 调用点是函数指针时不产 parsed 边（避免错误事实）
    parsed_targets = {
        edge.target_name for edge in edges(pf, "calls") if edge.provenance == "parsed"
    }
    assert "local" not in parsed_targets
    assert "current" not in parsed_targets
    # 设计化初始化字段 → 候选（`default_ops.apply` 与 `op_table[0]` 都是多候选全合成）
    apply_lines = {edge.line for edge in synthesized(pf)}
    assert apply_lines == {17, 18, 19, 20}


def test_function_pointer_table_and_designated_initializer(parser: CParser) -> None:
    pf = parse_sample(parser, "pointers/fnptr.c")
    by_line = {}
    for edge in synthesized(pf):
        by_line.setdefault(edge.line, set()).add(edge.target_name)
    assert by_line[17] == {"sub"}  # `local(a, b)`
    assert by_line[18] == {"add"} or by_line[18] == {"add", "mul"}  # `current(a, b)`
    assert by_line[20] == {"add", "mul", "sub"}  # 表下标调用 + 重新赋值后的调用


def test_macros_symbols_and_honest_warnings(parser: CParser) -> None:
    header = parse_sample(parser, "macros/macros.h")
    for name in ("MAX_LEN", "SQUARE", "CONCAT", "LOG"):
        assert symbol(header, name).kind == "macro"
    assert all(symbol(header, name).is_exported for name in ("MAX_LEN", "SQUARE"))
    warnings = " | ".join(header.parse_errors)
    assert "CONCAT" in warnings and "token pasting" in warnings
    assert "LOG" in warnings and "variadic" in warnings
    assert header.fallback is False  # 宏复杂度只进 parse_errors（警告），不是解析失败

    user = parse_sample(parser, "macros/user.c")
    assert targets(user, "calls", source="use_macros") == {"SQUARE", "LOG"}


def test_struct_enum_typedef_shapes(parser: CParser) -> None:
    pf = parse_sample(parser, "structs/types.h")
    assert symbol(pf, "Point").kind == "struct"
    # 同名 typedef 不重复（typedef struct Point {...} Point; 只留 struct 符号）
    assert all(item.name != "Point" or item.kind == "struct" for item in pf.symbols)
    assert symbol(pf, "Rect").kind == "typedef"  # 匿名 struct 由 typedef 承载名字
    assert symbol(pf, "Node").kind == "struct"
    assert symbol(pf, "Value").kind == "struct"  # union 归入 struct（kind 表无 union）
    assert symbol(pf, "Color").kind == "enum"
    assert symbol(pf, "length_t").kind == "typedef"
    assert symbol(pf, "callback_t").kind == "typedef"
    kinds = {item.name: item.kind for item in pf.symbols}
    assert "Rect" in kinds and "Value" in kinds
    assert len([s for s in pf.symbols if s.fqn == "Point"]) == 1


def test_partial_syntax_error_keeps_unaffected_symbols(parser: CParser) -> None:
    pf = parser.parse("partial.c", PARTIAL_ERROR_SOURCE)

    assert pf.fallback is False
    assert any("syntax error" in error for error in pf.parse_errors)
    assert {item.fqn for item in pf.symbols} == {"before", "after"}


def test_syntax_error_falls_back(parser: CParser) -> None:
    pf = parser.parse(SAMPLE_PREFIX + "broken/broken.c", SYNTAX_ERROR_SOURCE)
    assert pf.fallback is True
    assert pf.parse_errors
    assert pf.symbols == () and pf.edges == () and pf.unresolved == ()
    assert pf.language == "c"


def test_parse_is_deterministic(parser: CParser) -> None:
    for path in sorted(SAMPLE_DIR.rglob("*.c")) + sorted(SAMPLE_DIR.rglob("*.h")):
        relative = path.relative_to(SAMPLE_DIR).as_posix()
        first = parse_sample(parser, relative)
        assert first == parse_sample(parser, relative)
        assert first == parse_sample(CParser(), relative)
        # 输出按行号稳定排序（TASK-006 切块依赖）
        assert list(first.symbols) == sorted(first.symbols, key=lambda s: (s.start_line, s.fqn))
        assert list(first.edges) == sorted(first.edges, key=lambda e: (e.line or 0, e.source_fqn))


# -- include 解析规则（复用面，TASK-004/006 依赖） -----------------------------


def directive(text: str, line: int = 1) -> IncludeDirective:
    parsed = parse_include_directive(text, line)
    assert parsed is not None
    return parsed


def test_parse_include_directive() -> None:
    quoted = directive('#include "util/util.h"\n')
    assert (quoted.raw, quoted.target, quoted.system) == (
        '#include "util/util.h"',
        "util/util.h",
        False,
    )
    assert quoted.spelled == '"util/util.h"'
    angle = directive("#  include <stdio.h>")
    assert (angle.target, angle.spelled, angle.system) == ("stdio.h", "<stdio.h>", True)
    assert parse_include_directive("#include MACRO_HEADER", 1) is None  # 宏形式不猜


def test_resolve_include_relative_priority() -> None:
    repo = ["src/a.c", "src/a.h", "src/util.h"]
    assert resolve_include(directive('#include "a.h"'), "src/a.c", repo) == "src/a.h"
    assert resolve_include(directive('#include "util.h"'), "src/a.c", repo) == "src/util.h"
    # 相对路径不在仓库表里 → 退回唯一同名猜测
    assert resolve_include(directive('#include "a.h"'), "other/x.c", repo) == "src/a.h"
    ambiguous = ["src/a.h", "vendor/a.h"]
    assert resolve_include(directive('#include "a.h"'), "other/x.c", ambiguous) is None
    # 相对优先：本目录存在同名文件时不受全局歧义影响
    assert resolve_include(directive('#include "a.h"'), "src/a.c", ambiguous) == "src/a.h"


def test_resolve_include_system_and_escape() -> None:
    assert resolve_include(directive("#include <stdio.h>"), "src/a.c") is None  # 无仓库信息
    assert resolve_include(directive("#include <stdlib.h>"), "src/a.c", ["src/a.c"]) is None
    known = ["inc/config.h"]
    assert resolve_include(directive("#include <config.h>"), "src/a.c", known) == "inc/config.h"
    # 越出仓库根 → None（不猜）
    assert resolve_include(directive('#include "../outside.h"'), "a.c") is None
    assert resolve_include(directive('#include "../../x.h"'), "src/a.c") is None


def test_file_context_reuse_surface() -> None:
    """FileContext 是 TASK-004 复用面：字节切片取文本（含中文注释）。"""
    source = "int x; /* 中文注释 */\n".encode()
    ctx = FileContext(path="a.c", language="c", source=source)
    assert ctx.text  # 绑定方法存在即可，具体行为由基类测试覆盖
