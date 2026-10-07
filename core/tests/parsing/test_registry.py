"""TASK-002：语言识别与解析器注册表测试。"""

from __future__ import annotations

import pytest
from nova_core.parsing import registry
from nova_core.parsing.python import PythonParser
from nova_core.parsing.registry import (
    EXTENSION_LANGUAGE,
    PARSER_ENTRIES,
    ParserUnavailableError,
    detect_language,
    get_parser,
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("a/b/module.py", "python"),
        ("stubs/api.pyi", "python"),
        ("src/main.c", "c"),
        ("include/legacy.h", "c"),
        ("src/engine.cpp", "cpp"),
        ("src/engine.cc", "cpp"),
        ("include/engine.hpp", "cpp"),
        ("include/engine.hh", "cpp"),
        ("docs/design.md", "markdown"),
        ("docs/design.markdown", "markdown"),
        ("README.MD", "markdown"),
        ("nova_core/parsing/base.PY", "python"),
        ("no_extension", None),
        ("archive.tar.gz", None),
        ("dir.with.dot/file", None),
        ("", None),
    ],
)
def test_detect_language(path: str, expected: str | None) -> None:
    assert detect_language(path) == expected


def test_registry_covers_four_languages() -> None:
    assert set(PARSER_ENTRIES) == {"python", "c", "cpp", "markdown"}
    # registry.py 一次写全四语言懒加载条目（TASK-003/004/005 只实现模块文件）
    for module_name, class_name in PARSER_ENTRIES.values():
        assert module_name.startswith("nova_core.parsing.")
        assert class_name.endswith("Parser")
    for language in ("python", "c", "cpp", "markdown"):
        assert language in EXTENSION_LANGUAGE.values()


def test_get_parser_python_is_cached() -> None:
    first = get_parser("python")
    second = get_parser(" Python ")
    assert isinstance(first, PythonParser)
    assert first is second  # 注册表缓存实例


def test_get_parser_unknown_language() -> None:
    with pytest.raises(ParserUnavailableError, match="未注册的语言"):
        get_parser("klingon")


def test_get_parser_missing_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        registry.PARSER_ENTRIES, "brainfuck", ("nova_core.parsing.brainfuck", "BrainfuckParser")
    )
    with pytest.raises(ParserUnavailableError, match="尚不可用"):
        get_parser("brainfuck")


def test_get_parser_missing_class(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        registry.PARSER_ENTRIES, "whitespace", ("nova_core.parsing.base", "WhitespaceParser")
    )
    with pytest.raises(ParserUnavailableError, match="没有 WhitespaceParser"):
        get_parser("whitespace")


def test_parsing_package_exports() -> None:
    import nova_core.parsing as parsing

    for name in ("get_parser", "detect_language", "split_fallback", "TreeSitterParser"):
        assert name in parsing.__all__
        assert getattr(parsing, name) is not None
