"""单文件纯计算的子进程预取：解析 → 切分 → 内容 hash → generated 判定 → FTS 分词。

为什么需要它：冷启动在 2 vCPU 上实测"服务 CPU 核秒 ≈ 墙钟"，即 Python 部分基本被 GIL
串成一个核。``Indexer`` 主线程里最重的几段（tree-sitter 抽取、切分、jieba/快速路径分词）
都只依赖文件内容本身，因此放进一个**常驻子进程**提前计算，主线程只做 SQLite 写入，
两边真正并行。

不变量（结果与内联路径逐字节相同）：

- 子进程只做纯函数计算，**不碰 SQLite / 向量库**；写入顺序、每文件 SAVEPOINT、失败隔离、
  图解析时机全部留在主线程原样执行；
- 子进程上的**任何异常**（含解析器不可用、切分失败）都返回 ``None``，主线程回退到内联路径，
  由原代码复现同样的错误记录——预取永远不是错误来源；
- 子进程崩溃（``BrokenProcessPool``，例如被 OOM kill）→ 本次剩余文件全部回退内联，
  并丢弃该池，下次使用时重建；
- 语言判定（含 R1 的 ``.h`` 抬升）由主线程给出；消费时语言不一致则丢弃预取结果；
- **递归深度对齐**：抽取器是递归的，``TreeSitterParser.parse`` 把 ``RecursionError`` 吞成
  ``fallback`` 结果，所以解析产物取决于调用时的栈深与递归上限。主线程把内联路径调用
  ``parser.parse`` 时所在栈深与 ``sys.getrecursionlimit()`` 一并传来，子进程先垫栈到同一深度、
  用同一上限再调用；子进程自身栈已比目标深（垫不回去）→ 未命中，回退内联。

何时启用：池已被启动预加载（:func:`warm`）预热，或本次待处理文件数达到
:data:`MIN_COLD_POOL_FILES`（冷启动子进程要 1–2s，小增量不划算）。
``NOVA_PARSE_WORKERS=0`` 关闭；默认 ``min(2, CPU 数)`` 个子进程。实测 2 核上 1 个子进程会
变成新瓶颈（主线程 CPU 6.6s → 1.5s，但墙钟只 6.2s → 5.6s），2 个降到 3.8s。每个子进程
预热后常驻约 120 MB（jieba 词典 + grammar）。
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import sys
import threading
from collections import deque
from collections.abc import Iterable
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass

from nova_core.chunking import split_file
from nova_core.hashing import file_content_hash
from nova_core.parsing.registry import get_parser
from nova_core.pipeline.generated import is_generated
from nova_core.text.segmenter import segment
from nova_core.types import ChunkDef, ParsedFile

__all__ = [
    "LOOKAHEAD_FILES",
    "MIN_COLD_POOL_FILES",
    "PARSE_WORKERS_ENV",
    "PrepareStream",
    "PreparedFile",
    "parse_workers",
    "prepare_file",
    "stack_depth",
    "shutdown",
    "warm",
]

logger = logging.getLogger(__name__)

#: 子进程数的环境变量（``0`` 关闭；默认 ``min(2, CPU 数)``；上限 :data:`MAX_PARSE_WORKERS`）。
PARSE_WORKERS_ENV = "NOVA_PARSE_WORKERS"
DEFAULT_PARSE_WORKERS = min(2, os.cpu_count() or 1)
MAX_PARSE_WORKERS = 4
#: 池未预热时，待处理文件数达到该值才冷启动子进程。
MIN_COLD_POOL_FILES = 500
#: 主线程领先提交的文件数（内存护栏：只有这么多文件的预取结果同时在内存里）。
LOOKAHEAD_FILES = 64


@dataclass(frozen=True, slots=True)
class PreparedFile:
    """一个文件的预取结果（与 ``Indexer._index_file`` 内联计算的产物一一对应）。"""

    path: str
    language: str | None
    parsed: ParsedFile
    chunks: tuple[ChunkDef, ...]
    #: 与 ``chunks`` 对齐的 ``(content_seg, signature_seg, docstring_seg)``。
    segments: tuple[tuple[str, str, str], ...]
    content_hash: str
    generated: bool


def stack_depth() -> int:
    """调用方所在帧的栈深（本线程从底部数起的 Python 帧数，含调用方自身）。"""
    depth = 0
    frame = sys._getframe(1)  # noqa: SLF001 - 只读帧链计数
    while frame is not None:
        depth += 1
        frame = frame.f_back
    return depth


def _parse_at_depth(remaining: int, path: str, text: str, language: str) -> ParsedFile:
    """递归垫栈：``remaining`` 归零的那一帧调用 ``parser.parse``（与内联 ``_parse`` 同深）。"""
    if remaining > 0:
        return _parse_at_depth(remaining - 1, path, text, language)
    return get_parser(language).parse(path, text)


def prepare_file(
    path: str,
    data: bytes,
    language: str | None,
    parse_depth: int | None = None,
    recursion_limit: int | None = None,
) -> PreparedFile | None:
    """子进程入口：复刻内联路径的纯计算部分；任何异常 → ``None``（主线程回退内联）。

    ``parse_depth``：内联路径里调用 ``parser.parse`` 的那一帧的栈深（见模块 docstring 的
    递归深度对齐）；``None`` 时不对齐（仅供直接调用/测试）。
    """
    try:
        if b"\x00" in data:  # 与 Indexer._decode 同口径：二进制由内联路径记录跳过
            return None
        text = data.decode("utf-8", errors="replace")
        if recursion_limit is not None and sys.getrecursionlimit() != recursion_limit:
            sys.setrecursionlimit(recursion_limit)
        if language is None:
            parsed = ParsedFile(path=path, language="fallback", fallback=True)
        elif parse_depth is None:
            parsed = get_parser(language).parse(path, text)
        else:
            # 第一层 _parse_at_depth 的栈深 = 本帧 + 1；需要再垫 remaining 层到达 parse_depth。
            remaining = parse_depth - (stack_depth() + 1)
            if remaining < 0:
                return None
            parsed = _parse_at_depth(remaining, path, text, language)
        chunks = tuple(split_file(parsed, text))
        segments = tuple(
            (segment(chunk.content), segment(chunk.signature), segment(chunk.docstring))
            for chunk in chunks
        )
        return PreparedFile(
            path=path,
            language=language,
            parsed=parsed,
            chunks=chunks,
            segments=segments,
            content_hash=file_content_hash(data),
            generated=is_generated(path, text),
        )
    except Exception:  # noqa: BLE001 - 错误一律交给内联路径复现与记录
        return None


def parse_workers() -> int:
    """子进程数：``NOVA_PARSE_WORKERS``（默认 ``min(2, CPU 数)``；非法值回落默认；0 = 关闭）。"""
    raw = os.environ.get(PARSE_WORKERS_ENV, "").strip()
    if not raw:
        return DEFAULT_PARSE_WORKERS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，按默认 %d", PARSE_WORKERS_ENV, raw, DEFAULT_PARSE_WORKERS)
        return DEFAULT_PARSE_WORKERS
    return max(0, min(MAX_PARSE_WORKERS, value))


# ---------------------------------------------------------------------------
# 进程内共享池
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_executor: ProcessPoolExecutor | None = None
_warmed = False


def _warm_worker() -> None:
    """子进程初始化：预建 tree-sitter 解析器与 jieba 词典（与主进程预加载同口径）。

    预热只是优化：失败必须吞掉——initializer 抛异常会让整个池不可用，之后每次大 ingest
    都白付一次 spawn。真正用到时仍按原路径加载，失败由 ``prepare_file`` 返回未命中。
    """
    try:
        from nova_core.preload import warm_text_and_parsers

        warm_text_and_parsers()
    except Exception:  # noqa: BLE001
        logger.warning("预取子进程预热失败（首次使用时再加载）", exc_info=True)


def _noop() -> None:
    return None


def _get_executor(workers: int) -> ProcessPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            # spawn 而不是 fork：服务进程里已有 uvicorn / embedding / LanceDB 线程，
            # fork 一个多线程进程可能继承被持有的锁而死锁。
            _executor = ProcessPoolExecutor(
                max_workers=workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_warm_worker,
            )
        return _executor


def _discard(executor: ProcessPoolExecutor) -> None:
    """丢弃已损坏的池（下次使用时重建）。"""
    global _executor, _warmed
    with _lock:
        if _executor is executor:
            _executor = None
            _warmed = False
    executor.shutdown(wait=False, cancel_futures=True)


def warm() -> bool:
    """启动并预热共享池（服务启动预加载调用）；关闭或失败返回 ``False``。"""
    global _warmed
    workers = parse_workers()
    if workers <= 0:
        return False
    executor = _get_executor(workers)
    try:
        # 每个子进程都要跑完 initializer：提交 workers 个空任务并等待。
        for future in [executor.submit(_noop) for _ in range(workers)]:
            future.result()
    except BrokenProcessPool:
        _discard(executor)
        return False
    with _lock:
        _warmed = True
    return True


def shutdown() -> None:
    """关闭共享池（测试与进程退出用）。"""
    global _executor, _warmed
    with _lock:
        executor, _executor, _warmed = _executor, None, False
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)


def _executor_for(file_count: int) -> ProcessPoolExecutor | None:
    workers = parse_workers()
    if workers <= 0:
        return None
    with _lock:
        warmed = _warmed and _executor is not None
    if not warmed and file_count < MIN_COLD_POOL_FILES:
        return None
    return _get_executor(workers)


# ---------------------------------------------------------------------------
# 有序预取流
# ---------------------------------------------------------------------------


class PrepareStream:
    """按主线程消费顺序领先提交预取任务；:meth:`take` 取回与当前文件对应的结果。

    ``tasks`` 必须是主线程处理顺序的**子序列**（只含值得预取的文件）。:meth:`take` 对未提交
    的路径返回 ``None``（主线程走内联）。任何失败都只让结果变成 ``None``，不抛给调用方。
    """

    def __init__(
        self, tasks: Iterable[tuple[str, bytes, str | None, int | None, int | None]]
    ) -> None:
        self._tasks = deque(tasks)
        self._executor = _executor_for(len(self._tasks))
        self._inflight: deque[tuple[str, Future[PreparedFile | None]]] = deque()
        self._fill()

    @property
    def active(self) -> bool:
        return self._executor is not None

    def _fill(self) -> None:
        executor = self._executor
        if executor is None:
            return
        try:
            while self._tasks and len(self._inflight) < LOOKAHEAD_FILES:
                task = self._tasks.popleft()
                self._inflight.append((task[0], executor.submit(prepare_file, *task)))
        except Exception:  # noqa: BLE001 - BrokenProcessPool / 已关闭 / spawn 失败：一律回退内联
            self._broken(executor)

    def _broken(self, executor: ProcessPoolExecutor) -> None:
        logger.warning("预取子进程不可用，本次剩余文件回退为主线程内联处理")
        self._executor = None
        self._tasks.clear()
        for _, future in self._inflight:
            future.cancel()
        self._inflight.clear()
        _discard(executor)

    def take(self, path: str) -> PreparedFile | None:
        if not self._inflight or self._inflight[0][0] != path:
            return None
        _, future = self._inflight.popleft()
        executor = self._executor
        try:
            result = future.result()
        except BrokenProcessPool:
            if executor is not None:
                self._broken(executor)
            return None
        except Exception:  # noqa: BLE001 - 预取失败 = 未命中，内联路径负责报错
            result = None
        self._fill()
        return result

    def close(self) -> None:
        """取消尚未开始的预取（主流程异常中断时调用）。"""
        self._tasks.clear()
        for _, future in self._inflight:
            future.cancel()
        self._inflight.clear()
