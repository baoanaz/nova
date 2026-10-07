"""语言识别与解析器注册表（TASK-002 冻结接口）。

- ``detect_language(path) -> str | None``：按扩展名识别 python/c/cpp/markdown；
  未知 → ``None``（调用方走 ``fallback.py`` 兜底）。
- ``get_parser(language) -> Parser``：四语言懒加载注册表。注册条目一次性写全
  （import 路径 + 类名），模块尚未交付时给出清晰错误；
  TASK-003/004/005 只实现各自模块文件，**不得修改本文件**（并行关键约定）。

扩展名归属口径（.h 是 C/C++ 歧义点，见 TASK-002 执行记录"未决问题"）：
``.h`` 归 C；C++ 头文件用 ``.hpp/.hh/.hxx/.h++``。
"""

from __future__ import annotations

import importlib

from nova_core.interfaces import Parser

#: 扩展名（小写）→ 语言
EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".c++": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".h++": "cpp",
    ".ipp": "cpp",
    ".tpp": "cpp",
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "markdown",
}

#: 语言 → (模块路径, 类名)；懒加载，模块缺失时报 ParserUnavailableError
PARSER_ENTRIES: dict[str, tuple[str, str]] = {
    "python": ("nova_core.parsing.python", "PythonParser"),
    "c": ("nova_core.parsing.c", "CParser"),
    "cpp": ("nova_core.parsing.cpp", "CppParser"),
    "markdown": ("nova_core.parsing.markdown", "MarkdownParser"),
}


_parser_cache: dict[str, Parser] = {}


class ParserUnavailableError(LookupError):
    """语言未注册，或对应抽取器模块尚未交付。"""


def detect_language(path: str) -> str | None:
    """按扩展名识别语言；未知扩展名返回 None（表示走 fallback 兜底切分）。"""
    normalized = path.strip().lower()
    dot = normalized.rfind(".")
    slash = normalized.rfind("/")
    if dot <= slash:  # 无扩展名 / 点号在目录段里
        return None
    return EXTENSION_LANGUAGE.get(normalized[dot:])


def get_parser(language: str) -> Parser:
    """取（并缓存）语言对应的 Parser 实例；模块未交付时抛 ParserUnavailableError。"""
    key = language.strip().lower()
    entry = PARSER_ENTRIES.get(key)
    if entry is None:
        available = ", ".join(sorted(PARSER_ENTRIES))
        raise ParserUnavailableError(f"未注册的语言 {language!r}（已注册：{available}）")

    module_name, class_name = entry
    cached = _parser_cache.get(key)
    if cached is None:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ParserUnavailableError(
                f"语言 {key!r} 的抽取器模块 {module_name} 尚不可用（实现中或未交付）：{exc}"
            ) from exc
        parser_class = getattr(module, class_name, None)
        if parser_class is None:
            raise ParserUnavailableError(
                f"抽取器模块 {module_name} 里没有 {class_name}（实现未完成？）"
            )
        cached = parser_class()
        _parser_cache[key] = cached
    return cached
