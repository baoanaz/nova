"""parsing：语言识别 + tree-sitter 抽取器（TASK-002..005）。

子模块分工：

- ``registry``：``detect_language`` / ``get_parser``（四语言懒加载注册表）；
- ``base``：``TreeSitterParser`` 基类与公共工具；
- ``fallback``：解析失败/未知语言的递归字符兜底切分（TASK-006 消费）；
- ``python`` / ``c`` / ``cpp`` / ``markdown``：各语言抽取器（按任务卡分包交付）。

本文件与 ``registry.py`` / ``base.py`` 同属 TASK-002 所有权，TASK-003/004/005 不得修改。
"""

from nova_core.parsing.base import (
    Extraction,
    FileContext,
    TreeSitterParser,
    collect_parse_errors,
    end_line_of,
    line_of,
    normalize_name,
    walk,
)
from nova_core.parsing.fallback import FallbackBlock, split_fallback
from nova_core.parsing.registry import (
    EXTENSION_LANGUAGE,
    PARSER_ENTRIES,
    ParserUnavailableError,
    detect_language,
    get_parser,
)

__all__ = [
    "EXTENSION_LANGUAGE",
    "PARSER_ENTRIES",
    "Extraction",
    "FallbackBlock",
    "FileContext",
    "ParserUnavailableError",
    "TreeSitterParser",
    "collect_parse_errors",
    "detect_language",
    "end_line_of",
    "get_parser",
    "line_of",
    "normalize_name",
    "split_fallback",
    "walk",
]
