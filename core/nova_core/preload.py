"""长驻进程的启动预加载：把首次索引才付的一次性导入/初始化成本挪到服务启动。

为什么需要：这些成本与仓库内容无关、每个进程只付一次，但默认是惰性的（P1-5：CLI 的多数
路径用不到它们），于是全部落在服务启动后的**第一次** ingest 里：

- ``import lancedb`` 的导入链（实测 2.6–3.2s，见 ``vectors/_lancedb.py``）；
- jieba 前缀词典（首个非 ASCII 快速路径之外的 chunk 才触发）；
- tree-sitter 各语言 grammar 与 Parser 实例；
- 解析预取子进程（``pipeline.prepare``）：spawn + 导入 + 子进程内预建解析器与 jieba 词典。

长驻服务（``nova-service serve`` / ``local``）启动时调用 :func:`preload_runtime`。CLI 与测试
不调用，惰性语义不变。预加载只是优化：任何一项失败只记日志，不阻止服务启动——真正用到时
仍按原路径加载并照常报错。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

__all__ = ["preload_runtime", "warm_text_and_parsers"]

logger = logging.getLogger(__name__)


def _lancedb() -> None:
    from nova_core.vectors._lancedb import load

    load()


def _jieba() -> None:
    from nova_core.text.segmenter import _load_jieba

    _load_jieba().initialize()


def _parsers() -> None:
    from nova_core.parsing.base import TreeSitterParser
    from nova_core.parsing.registry import PARSER_ENTRIES, get_parser

    for language in PARSER_ENTRIES:
        parser = get_parser(language)
        if isinstance(parser, TreeSitterParser):
            parser.parser()


def _parse_worker() -> None:
    from nova_core.pipeline.prepare import warm

    warm()


def warm_text_and_parsers() -> None:
    """jieba 词典 + tree-sitter 解析器（预取子进程的 initializer 也调用它）。"""
    _jieba()
    _parsers()


_STEPS: tuple[tuple[str, Callable[[], None]], ...] = (
    ("lancedb", _lancedb),
    ("jieba", _jieba),
    ("parsers", _parsers),
    ("parse_worker", _parse_worker),
)


def preload_runtime() -> dict[str, float]:
    """依次预加载各项，返回 ``名称 -> 耗时秒``（失败项不计入，只记 warning）。"""
    timings: dict[str, float] = {}
    for name, step in _STEPS:
        started = time.perf_counter()
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - 预加载失败不得阻止服务启动
            logger.warning("启动预加载 %s 失败（首次使用时再加载）：%s", name, exc)
            continue
        timings[name] = round(time.perf_counter() - started, 3)
    logger.info("启动预加载完成：%s", timings)
    return timings
