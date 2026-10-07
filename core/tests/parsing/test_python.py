"""TASK-002：Python 抽取器验收测试（符号/边/unresolved/确定性/自举）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from nova_core.parsing.python import PythonParser
from nova_core.types import EdgeDef, ParsedFile, SymbolDef, UnresolvedRef

SAMPLE_DIR = Path(__file__).parent / "samples" / "python"
SAMPLE_PREFIX = "core/tests/parsing/samples/python/"
REPO_ROOT = Path(__file__).resolve().parents[3]

SYNTAX_ERROR_SOURCE = "def broken(:\n    return 1\n"


@pytest.fixture(scope="module")
def parser() -> PythonParser:
    return PythonParser()


def parse_sample(parser: PythonParser, name: str) -> ParsedFile:
    return parser.parse(SAMPLE_PREFIX + name, (SAMPLE_DIR / name).read_text(encoding="utf-8"))


def symbol(pf: ParsedFile, fqn: str) -> SymbolDef:
    match = next((item for item in pf.symbols if item.fqn == fqn), None)
    assert match is not None, f"缺少符号 {fqn}（实际：{[s.fqn for s in pf.symbols]}）"
    return match


def edges(pf: ParsedFile, kind: str) -> list[EdgeDef]:
    return [edge for edge in pf.edges if edge.kind == kind]


def targets(pf: ParsedFile, kind: str) -> set[str]:
    return {edge.target_name for edge in edges(pf, kind)}


def unresolved(pf: ParsedFile, kind: str) -> list[UnresolvedRef]:
    return [item for item in pf.unresolved if item.kind == kind]


def test_sample_corpus_has_enough_files() -> None:
    assert len(list(SAMPLE_DIR.rglob("*.py"))) >= 8


def test_module_level_symbols(parser: PythonParser) -> None:
    pf = parse_sample(parser, "module_basics.py")
    assert pf.language == "python"
    assert pf.fallback is False and pf.parse_errors == ()

    assert symbol(pf, "top_level").kind == "function"
    assert symbol(pf, "fetch").kind == "function"  # async 也是 function_definition
    assert symbol(pf, "Service").kind == "class"
    assert symbol(pf, "Service.run").kind == "method"
    assert symbol(pf, "Service.run").is_exported is False

    # 模块级带注解 assignment → symbol；无注解不建 symbol
    constant = symbol(pf, "MODULE_CONSTANT")
    assert (constant.kind, constant.start_line, constant.end_line) == ("variable", 7, 7)
    assert constant.is_exported is True
    assert all(item.name != "PLAIN_NAME" for item in pf.symbols)


def test_module_level_code_owner_is_path(parser: PythonParser) -> None:
    pf = parse_sample(parser, "module_basics.py")
    module_calls = [
        edge
        for edge in edges(pf, "calls")
        if edge.source_fqn == SAMPLE_PREFIX + "module_basics.py"
    ]
    assert {edge.target_name for edge in module_calls} == {"os.getcwd"}


def test_decorator_included_in_symbol_range(parser: PythonParser) -> None:
    pf = parse_sample(parser, "decorated_service.py")
    decorated = symbol(pf, "decorated")
    assert decorated.start_line == 16  # @traced 所在行
    assert decorated.end_line == 18
    assert symbol(pf, "Decorated").start_line == 21  # 类装饰器同样计入
    assert symbol(pf, "Container.build").start_line == 27  # @staticmethod
    assert symbol(pf, "traced.wrapper").start_line == 9  # @wraps(func)


def test_nested_functions_and_classes(parser: PythonParser) -> None:
    pf = parse_sample(parser, "nested_scopes.py")
    assert symbol(pf, "outer.inner").kind == "function"
    assert symbol(pf, "outer.inner").is_exported is False
    assert symbol(pf, "outer.Inner").kind == "class"
    assert symbol(pf, "outer.Inner.method").kind == "method"
    assert symbol(pf, "Outer.method.nested").kind == "function"
    # 嵌套符号的调用归属最内层函数
    inner_calls = [e for e in edges(pf, "calls") if e.source_fqn == "outer.Inner.method"]
    assert [e.target_name for e in inner_calls] == ["inner"]


def test_imports_edges(parser: PythonParser) -> None:
    pf = parse_sample(parser, "imports_module.py")
    owner = SAMPLE_PREFIX + "imports_module.py"
    assert {e.source_fqn for e in edges(pf, "imports")} == {owner}
    assert targets(pf, "imports") == {
        "os",
        "os.path",
        "xml.etree.ElementTree",
        ".sibling",
        "..parent.thing",
        "..parent.pkg.other",
        "star_pkg.*",
    }
    assert pf.symbols == ()  # 纯 import 文件无符号


def test_reexport_and_dunder_all(parser: PythonParser) -> None:
    pf = parse_sample(parser, "pkg_public/__init__.py")
    assert targets(pf, "imports") == {".service.Service", ".service.helper"}
    # __all__ 存在时以 __all__ 为准：不在名单里的模块级符号不算导出
    assert symbol(pf, "VERSION").is_exported is False
    assert symbol(pf, "public_api").is_exported is False
    # 子模块无 __all__：公开名默认导出
    sub = parse_sample(parser, "pkg_public/service.py")
    assert symbol(sub, "helper").is_exported is True
    assert symbol(sub, "Service.run").is_exported is False


def test_extends_edges(parser: PythonParser) -> None:
    basics = parse_sample(parser, "module_basics.py")
    assert [(e.source_fqn, e.target_name) for e in edges(basics, "extends")] == [
        ("Service", "BaseService")
    ]
    hints = parse_sample(parser, "type_hints.py")
    assert targets(hints, "extends") == {"Generic", "Protocol", "Store"}
    assert symbol(hints, "SpecializedStore").kind == "class"


def test_dynamic_features_are_unresolved(parser: PythonParser) -> None:
    pf = parse_sample(parser, "dynamic_calls.py")
    calls = {item.name for item in unresolved(pf, "call")}
    imports = {item.name for item in unresolved(pf, "import")}

    assert "handle_payload" in calls  # getattr(obj, "name")
    assert "getattr(self, target)" in calls  # 名字非字面量：整表达式如实标注
    assert "self.registry[0]" in calls  # 下标调用
    assert "(lambda: handler)" in calls  # 立即调用 lambda
    assert imports == {"json", 'importlib.import_module(name="os.path")'}
    # 动态 import 不猜调用边；普通属性调用仍产边
    assert "importlib.import_module" not in targets(pf, "calls")
    assert "self.send" in targets(pf, "calls") or "handler" in targets(pf, "calls")
    # 所有 unresolved 都带归属符号与行号
    assert all(item.from_fqn == "Client.send" for item in pf.unresolved if item.kind == "import")
    assert all(item.line is not None for item in pf.unresolved)


def test_syntax_error_file_falls_back(parser: PythonParser) -> None:
    pf = parser.parse("core/tests/parsing/samples/python/broken.py", SYNTAX_ERROR_SOURCE)
    assert pf.fallback is True
    assert pf.parse_errors
    assert pf.symbols == () and pf.edges == () and pf.unresolved == ()
    assert pf.language == "python"  # 语言已知，只是不产出半可信符号


def test_parse_is_deterministic(parser: PythonParser) -> None:
    for name in sorted(
        path.relative_to(SAMPLE_DIR).as_posix() for path in SAMPLE_DIR.rglob("*.py")
    ):
        first = parse_sample(parser, name)
        assert first == parse_sample(parser, name)
        assert first == parse_sample(PythonParser(), name)  # 跨实例同样一致
        assert list(first.symbols) == sorted(first.symbols, key=lambda s: (s.start_line, s.fqn))
        assert list(first.edges) == sorted(first.edges, key=lambda e: (e.line or 0, e.source_fqn))


def test_self_hosting_types_and_hashing(parser: PythonParser) -> None:
    """自举：用本抽取器解析 nova 自己的冻结契约文件。"""
    types_pf = parser.parse(
        "core/nova_core/types.py",
        (REPO_ROOT / "core/nova_core/types.py").read_text(encoding="utf-8"),
    )
    assert types_pf.fallback is False
    assert symbol(types_pf, "ChunkDef").kind == "class"
    assert symbol(types_pf, "ParsedFile").kind == "class"
    assert symbol(types_pf, "SymbolDef").end_line > symbol(types_pf, "SymbolDef").start_line

    hashing_pf = parser.parse(
        "core/nova_core/hashing.py",
        (REPO_ROOT / "core/nova_core/hashing.py").read_text(encoding="utf-8"),
    )
    assert hashing_pf.fallback is False
    assert symbol(hashing_pf, "chunk_content_hash").kind == "function"
