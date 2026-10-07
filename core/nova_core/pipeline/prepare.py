"""单文件预处理（纯计算）与上传期后台预取。

一个文件进库前的纯计算——范围判定、解码、解析、切分、内容 hash、generated 判定、FTS 分词——
只依赖文件字节与语言，全部收敛在 :func:`prepare_file` 这一个函数里：内联索引、上传期预取、
向量重建三条路径都调用它，结果因此按构造一致（此前三份实现各自漂移）。

**解析栈预算固定**（:data:`CANONICAL_PARSE_DEPTH`）：抽取器是递归的，``TreeSitterParser.parse``
把 ``RecursionError`` 吞成 ``fallback`` 结果，因此解析产物原本取决于调用方当时的栈深——同一个
文件经 CLI 与经服务索引可能得到不同结果。这里先把栈垫到固定深度再调用解析器，任何入口
（主线程、后台线程、CLI、测试）结果一致。代价：嵌套接近递归上限的病态文件，可用递归预算
比过去略少（约百帧量级）；正常代码不受影响。

**上传期预取**（:class:`UploadPrefetcher`）：首次同步时客户端要先把全部文件上传完才 flush，
这段时间服务端 CPU 大半空闲。预取器把落盘的 blob 交给**一个低优先级（``nice``）子进程**
提前跑 :func:`prepare_file`，flush 时 ingest 只取已算完的结果，其余照常内联。为什么必须是
子进程、为什么只在上传期用，见 :class:`UploadPrefetcher` 的实测说明。

不变量：预取只做纯计算，不碰 SQLite / 向量库；结果按 ``(项目, 路径, blob hash, 语言, 范围阈值,
递归上限)`` 精确匹配才被使用，否则内联计算——预取永远不改变索引结果，只改变计算发生的时间。
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import pickle
import sys
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Iterable
from concurrent.futures import Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass

from nova_core.chunking import split_file
from nova_core.hashing import file_content_hash
from nova_core.interfaces import Parser
from nova_core.parsing.registry import detect_language, get_parser
from nova_core.pipeline.generated import is_generated
from nova_core.pipeline.ignore import SKIP_REASON_BINARY, IndexScope, oversize_reason
from nova_core.text.segmenter import segment
from nova_core.types import ChunkDef, ParsedFile

__all__ = [
    "CANONICAL_PARSE_DEPTH",
    "PREFETCH_ENV",
    "PREFETCH_MAX_MB_ENV",
    "PrefetchView",
    "PrepareResult",
    "PrepareSkip",
    "PreparedFile",
    "UploadPrefetcher",
    "parse_at_canonical_depth",
    "prepare_file",
    "scope_key",
]

logger = logging.getLogger(__name__)

#: 调用 ``parser.parse`` 的那一帧所在的固定栈深（见模块 docstring）。须大于任何真实入口的
#: 调用栈深度（服务 / CLI / pytest 都在百帧以内）；超过时退化为"就地调用"并告警一次。
CANONICAL_PARSE_DEPTH = 200

#: 上传期预取开关（``0`` / ``false`` / ``no`` / ``off`` 关闭；默认开启）。
PREFETCH_ENV = "NOVA_UPLOAD_PREFETCH"
#: 预取暂存上限（按**源码字节**计，MB）。实测预取结果常驻约为源码的 8 倍（解析产物 + chunk +
#: FTS 分词）；默认 24 MB 源码 ≈ 200 MB 对象，LangChain 量级可全部覆盖，超出部分 flush 时内联。
PREFETCH_MAX_MB_ENV = "NOVA_PREFETCH_MAX_MB"
DEFAULT_PREFETCH_MAX_MB = 24
#: 预取结果的最长保留时间（秒）：上传后迟迟不 flush 的结果不长期占内存。
PREFETCH_TTL_S = 15 * 60
#: 预取子进程的 nice 值：只用上传处理与客户端扫描都不用的空闲 CPU。
PREFETCH_NICE = 10


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedFile:
    """可入库的预处理结果（``Store.apply_file_change`` 与向量阶段需要的全部输入）。"""

    path: str
    language: str | None
    parsed: ParsedFile
    chunks: tuple[ChunkDef, ...]
    #: 与 ``chunks`` 对齐的 ``(content_seg, signature_seg, docstring_seg)``。
    segments: tuple[tuple[str, str, str], ...]
    content_hash: str
    generated: bool
    #: 需按序追加到 ``IngestReport.errors`` 的记录（解析错误；文件本身仍入库）。
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PrepareSkip:
    """不入库的文件：超限/二进制（``skip_reason``），或切分失败（``errors`` 末项）。"""

    path: str
    skip_reason: str | None
    errors: tuple[str, ...] = ()


PrepareResult = PreparedFile | PrepareSkip

_depth_warned = False


def _stack_depth() -> int:
    """调用方所在帧的栈深（本线程从底部数起的 Python 帧数，含调用方自身）。"""
    depth = 0
    frame = sys._getframe(1)  # noqa: SLF001 - 只读帧链计数
    while frame is not None:
        depth += 1
        frame = frame.f_back
    return depth


def _parse_at(remaining: int, parser: Parser, path: str, text: str) -> ParsedFile:
    if remaining > 0:
        return _parse_at(remaining - 1, parser, path, text)
    return parser.parse(path, text)


def parse_at_canonical_depth(parser: Parser, path: str, text: str) -> ParsedFile:
    """在固定栈深 :data:`CANONICAL_PARSE_DEPTH` 调用 ``parser.parse``（结果与调用方无关）。"""
    global _depth_warned
    # 第一层 _parse_at 的栈深 = 本帧 + 1；remaining 归零的那一帧就是调用 parse 的帧。
    remaining = CANONICAL_PARSE_DEPTH - (_stack_depth() + 1)
    if remaining < 0:
        if not _depth_warned:
            _depth_warned = True
            logger.warning(
                "调用栈深于固定解析栈预算 %d，本次解析结果可能依赖调用方栈深",
                CANONICAL_PARSE_DEPTH,
            )
        remaining = 0
    return _parse_at(remaining, parser, path, text)


def _parse(path: str, text: str, language: str | None) -> tuple[ParsedFile, tuple[str, ...]]:
    """解析单文件：未知语言或抽取器不可用 → 兜底 ``ParsedFile``（不中断 ingest）。"""
    if language is None:
        return ParsedFile(path=path, language="fallback", fallback=True), ()
    try:
        parsed = parse_at_canonical_depth(get_parser(language), path, text)
    except Exception as exc:  # ParserUnavailableError / 抽取器缺陷
        return (
            ParsedFile(
                path=path,
                language=language,
                parse_errors=(f"{type(exc).__name__}: {exc}",),
                fallback=True,
            ),
            (f"{path}: {type(exc).__name__}: {exc}",),
        )
    if parsed.parse_errors:
        return parsed, (f"{path}: " + "; ".join(parsed.parse_errors),)
    return parsed, ()


def prepare_file(
    path: str, data: bytes, language: str | None, scope: IndexScope
) -> PrepareResult:
    """一个文件进库前的全部纯计算（范围 → 解码 → 解析 → 切分 → hash / generated / 分词）。

    与 ``Indexer`` 原内联实现逐项同口径：超限与二进制记 skip；含 NUL 记二进制 skip；
    解析错误记录但文件仍入库；切分（及其后的纯计算）失败 → 记 ``"{path}: {类型}: {信息}"``
    并跳过该文件。除内存耗尽一类无法隔离的错误外不抛异常。
    """
    readable, size_reason = scope.should_read(path, len(data))
    if not readable:
        return PrepareSkip(path, size_reason or oversize_reason(len(data)))
    decodable, binary_reason = scope.check_bytes(data)
    if not decodable:
        return PrepareSkip(path, binary_reason or SKIP_REASON_BINARY)
    if b"\x00" in data:  # 二进制（含 NUL）跳过，不产兜底块
        return PrepareSkip(path, SKIP_REASON_BINARY)
    text = data.decode("utf-8", errors="replace")
    parsed, errors = _parse(path, text, language)
    try:
        chunks = tuple(split_file(parsed, text))
        segments = tuple(
            (segment(chunk.content), segment(chunk.signature), segment(chunk.docstring))
            for chunk in chunks
        )
        content_hash = file_content_hash(data)
        generated = is_generated(path, text)
    except Exception as exc:  # 单文件切分失败 → 如实记录并跳过（TASK-018 §C）
        return PrepareSkip(path, None, (*errors, f"{path}: {type(exc).__name__}: {exc}"))
    return PreparedFile(
        path=path,
        language=language,
        parsed=parsed,
        chunks=chunks,
        segments=segments,
        content_hash=content_hash,
        generated=generated,
        errors=errors,
    )


def scope_key(scope: IndexScope) -> tuple[int, float, int]:
    """范围阈值的可比较键（预取结果只在同一阈值下复用）。"""
    return (scope.max_bytes, scope.binary_ratio, scope.probe_bytes)


# ---------------------------------------------------------------------------
# 上传期预取（低优先级子进程）
# ---------------------------------------------------------------------------

#: ``(项目, 路径, blob hash, 语言, 范围阈值, 递归上限)``。
_Key = tuple[str, str, str, "str | None", tuple[int, float, int], int]


def _prepare_task(
    path: str,
    data: bytes,
    language: str | None,
    scope: tuple[int, float, int],
    recursion_limit: int,
) -> bytes:
    """子进程入口：同一个 :func:`prepare_file`，结果压缩序列化后交回（主进程按需解开）。"""
    if sys.getrecursionlimit() != recursion_limit:
        sys.setrecursionlimit(recursion_limit)
    max_bytes, binary_ratio, probe_bytes = scope
    result = prepare_file(
        path,
        data,
        language,
        IndexScope(max_bytes=max_bytes, binary_ratio=binary_ratio, probe_bytes=probe_bytes),
    )
    return zlib.compress(pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL), 1)


def _worker_init() -> None:
    """子进程降为低优先级：只吃上传 / 索引都不用的空闲 CPU（见模块 docstring）。"""
    try:
        os.nice(PREFETCH_NICE)
    except OSError:  # 平台不支持或无权限：照常运行，只是不降级
        pass


def _noop() -> None:
    return None


class PrefetchView:
    """一次 ingest 对预取结果的视图；用完必须 :meth:`close`（释放本项目剩余的预取）。"""

    def __init__(self, owner: UploadPrefetcher, namespace: str, scope: IndexScope) -> None:
        self._owner = owner
        self._namespace = namespace
        self._scope_key = scope_key(scope)
        self._closed = False
        self.hits = 0

    def take(
        self, path: str, blob_hash: str | None, language: str | None
    ) -> PrepareResult | None:
        """取回与 ``(path, blob_hash, language)`` 完全匹配且**已算完**的预取结果，否则 ``None``。

        不等待尚未算完的项：子进程是低优先级的，等它不如主线程直接算。
        """
        if blob_hash is None:
            return None
        result = self._owner._take(
            (self._namespace, path, blob_hash, language, self._scope_key,
             sys.getrecursionlimit())
        )
        if result is not None:
            self.hits += 1
        return result

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._owner.discard(self._namespace)


class UploadPrefetcher:
    """上传期预取：一个低优先级子进程按上传顺序跑 :func:`prepare_file`，结果按键暂存。

    为什么是进程而不是线程（实测，2 vCPU 同型约束下）：后台**线程**做 CPU 计算时，上传请求
    每次 I/O（读 socket、写 blob）释放 GIL 后要等满切换间隔才拿得回来（GIL 护航效应）——
    5 批 × 100 个 blob 的落盘从 0.02s 变成 6.3s。子进程不共享 GIL；结果反序列化只占现算成本
    的约 5%（29 MB 源码：现算 3.5s，解包 0.2s）。Round 5 进程池不划算的原因是它在 flush
    **之后**才算，与主线程、向量阶段抢同两个核；这里只在上传期用空闲 CPU（``nice``），
    ingest 时只取已算完的结果。

    线程安全：状态受 ``_lock`` 保护；``ProcessPoolExecutor`` 本身线程安全。
    """

    def __init__(self, *, max_source_bytes: int) -> None:
        self._max_source_bytes = max_source_bytes
        self._lock = threading.Lock()
        #: key → (结果 future, 源码字节, 登记时刻)；登记顺序即年龄顺序。
        self._entries: OrderedDict[_Key, tuple[Future[bytes], int, float]] = OrderedDict()
        self._held_bytes = 0
        self._executor: ProcessPoolExecutor | None = None
        self._closed = False

    @classmethod
    def from_env(cls) -> UploadPrefetcher | None:
        """按环境变量构造；关闭时返回 ``None``。"""
        raw = os.environ.get(PREFETCH_ENV, "").strip().lower()
        if raw in ("0", "false", "no", "off"):
            return None
        try:
            mb = int(os.environ.get(PREFETCH_MAX_MB_ENV, "") or DEFAULT_PREFETCH_MAX_MB)
        except ValueError:
            mb = DEFAULT_PREFETCH_MAX_MB
        if mb <= 0:
            return None
        return cls(max_source_bytes=mb * 1024 * 1024)

    @property
    def held_bytes(self) -> int:
        with self._lock:
            return self._held_bytes

    def warm(self) -> None:
        """提前拉起子进程（非阻塞：spawn 与导入在后台进行）。"""
        with self._lock:
            executor = self._ensure_executor()
            if executor is not None:
                try:
                    executor.submit(_noop)
                except (BrokenProcessPool, RuntimeError):
                    self._executor = None

    # -- 生产者（上传请求） -------------------------------------------------

    def submit(
        self, namespace: str, items: Iterable[tuple[str, str, bytes]], scope: IndexScope
    ) -> int:
        """登记上传落盘的 ``(path, blob_hash, data)``；返回入队数（超出内存护栏的不入队）。

        语言按扩展名识别；C++ 仓库里 ``.h`` 的 R1 抬升要到 ingest 才知道，这类文件届时
        语言不匹配、走内联。不阻塞调用方（只把任务交给子进程）。
        """
        accepted = 0
        limit = sys.getrecursionlimit()
        key_scope = scope_key(scope)
        now = time.monotonic()
        with self._lock:
            if self._closed:
                return 0
            self._expire(now)
            executor = self._ensure_executor()
            if executor is None:
                return 0
            for path, blob_hash, data in items:
                language = detect_language(path)
                key: _Key = (namespace, path, blob_hash, language, key_scope, limit)
                if key in self._entries:
                    continue
                if self._held_bytes + len(data) > self._max_source_bytes:
                    break
                try:
                    future = executor.submit(
                        _prepare_task, path, data, language, key_scope, limit
                    )
                except (BrokenProcessPool, RuntimeError):
                    self._executor = None
                    break
                self._entries[key] = (future, len(data), now)
                self._held_bytes += len(data)
                accepted += 1
        return accepted

    def discard(self, namespace: str) -> None:
        """丢弃一个项目的全部预取（ingest 结束 / 项目删除时）。"""
        with self._lock:
            for key in [key for key in self._entries if key[0] == namespace]:
                self._drop(key)

    def wait_idle(self, timeout: float | None = None) -> None:
        """等当前登记的预取全部算完（测试与诊断用）。"""
        with self._lock:
            futures = [entry[0] for entry in self._entries.values()]
        wait(futures, timeout=timeout)

    def close(self) -> None:
        """关停子进程（服务退出时调用；未完成的预取直接取消）。"""
        with self._lock:
            self._closed = True
            for key in list(self._entries):
                self._drop(key)
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    # -- 消费者（ingest） ---------------------------------------------------

    def view(self, namespace: str, scope: IndexScope) -> PrefetchView:
        return PrefetchView(self, namespace, scope)

    def _take(self, key: _Key) -> PrepareResult | None:
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                return None
            self._held_bytes -= entry[1]
        future = entry[0]
        if not future.done():
            future.cancel()  # 还在排队则不再算；正在算的结果丢弃
            return None
        try:
            payload = future.result()
        except Exception:  # noqa: BLE001 - 预取失败 = 未命中，内联路径负责报错
            return None
        return pickle.loads(zlib.decompress(payload))  # noqa: S301 - 本进程子进程产出的数据

    # -- 内部（调用方持锁） ---------------------------------------------------

    def _ensure_executor(self) -> ProcessPoolExecutor | None:
        if self._closed:
            return None
        if self._executor is None:
            # spawn 而不是 fork：服务进程里已有 uvicorn / 向量 / LanceDB 线程，
            # fork 一个多线程进程可能继承被持有的锁而死锁。
            self._executor = ProcessPoolExecutor(
                max_workers=1,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_worker_init,
            )
        return self._executor

    def _drop(self, key: _Key) -> None:
        future, size, _ = self._entries.pop(key)
        future.cancel()
        self._held_bytes -= size

    def _expire(self, now: float) -> None:
        while self._entries:
            key, (_, _, created) = next(iter(self._entries.items()))
            if now - created < PREFETCH_TTL_S:
                break
            self._drop(key)
