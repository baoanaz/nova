"""向量阶段：chunk → 内容寻址复用 → 嵌入 → 向量表 + 缓存（TASK-114 / P0-2 / P1-4）。

为什么单独成模块（计划 §3-A1）：这段逻辑原先埋在 ``Indexer`` 的两个私有方法里，并与
"跨窗口累积整仓向量"纠缠。2026-09-25 实测：langchain 20831 个 chunk 的向量以 Python
``list[float]`` 常驻（~33 KB/条）推高峰值 RSS **+824MB**，且在 2 GiB 机器上把 SQLite 段
拖出 10.5s 的 cgroup 回收写放大；``_rebuild_vectors`` 更是把整份累积结果**拿完就丢**。

本模块只做一件事：把一段 chunk 变成向量表里的行，**不持有任何整仓级状态**。

复用三层（R4：复用键是 hash 不是 id）
-------------------------------------

1. **同 id 同内容**：向量已在表里 → 不写、不嵌（``skipped``）；
2. **同内容换 id**：向量表 / 跨项目缓存里能按 ``content_hash`` 取到 → 只搬 id 写回（``reused``）；
3. **都没有**：``embed()`` 一次（``embedded``），写回向量表，并**立刻**写回共享缓存。

跨窗口的复用由向量表本身承担：每写完一个窗口就落库，下一个窗口的按 hash 查询即可命中——
因此不需要在内存里缓存整仓 ``hash → vector``。窗口内相同内容在送 ``embed()`` 前按 hash 去重
（``_embed_unique``），这是"同一批里相同内容只嵌一次"的真实落地（旧 docstring 声称有
``fresh`` 池去重，代码里 ``produced`` 根本不参与判定）。

**并行消费者（TASK-115）**：向量阶段跑在 ``K`` 个消费者线程里（``NOVA_EMBED_WORKERS``，默认 2）。
TASK-114 的实测账显示单个消费者把"取回 → 解码 → 落库"串成一根链：langchain 冷启动里它一户
占 48.1s，而链路在解码/落库期间空转（``network_busy_s / wall_s = 0.41``）。拆成 K 个后，
窗口 N 在解码/落库时窗口 N+1 的请求仍在飞。代价是峰值向量 ≈ ``K × 窗口 × 33KB``（内存护栏），
以及"两个窗口同时判定同一内容未命中"时会多嵌少量重复内容（向量表"写好即可见"兜住大部分）。

内存上界
--------

任何时刻只持有 **一个窗口** 的 chunk 文本 + 向量：``窗口 = batch_size × concurrency``
（``MAX_EMBED_WINDOW`` 封顶），外加未满一个窗口的待处理缓冲。这是该阶段唯一的内存来源。

**跨文件攒批**：``feed`` 把 chunk 追加进缓冲、攒满一个窗口才处理，``flush`` 收尾。
为什么不能每个文件单独处理：实测 langchain 平均每文件只有 ~7 个 chunk，
按文件开窗会把 20k chunk 变成 2986 次串行 HTTP 调用；攒批后仍是 ~11 次满窗口调用。

失败语义
--------

- 缓存读写失败一律降级为"未命中/未写"（纯优化，不得阻断索引，沿用 TASK-111 口径）；
- ``embed()`` 或向量表写失败向上抛——由 :class:`EmbeddingPipeline` 送回主线程，
  不静默吞掉半个索引。
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from queue import Empty, Queue

from nova_core.chunking import embedding_text
from nova_core.interfaces import EmbeddingProvider
from nova_core.types import ChunkDef, VectorRow
from nova_core.vectors import VectorStore
from nova_core.vectors.cache import EmbeddingCache, EmbeddingCacheError

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_EMBED_WORKERS",
    "DEFAULT_EMBED_WINDOW",
    "EmbeddingPipeline",
    "EmbeddingSink",
    "EmbeddingStats",
    "MAX_EMBED_WORKERS",
    "MAX_EMBED_WINDOW",
    "WORKERS_ENV",
    "embed_workers",
    "embed_window_size",
]

#: 窗口兜底值（chunk 数）：provider 未声明 ``batch_size`` 时使用。
DEFAULT_EMBED_WINDOW = 512
#: 窗口上限（chunk 数）：兜住"批大小 × 并发"被调到极端值的情况（防再次吃到 GB 级内存）。
MAX_EMBED_WINDOW = 4_000
#: 消费者线程数（``NOVA_EMBED_WORKERS`` 的默认值）。
DEFAULT_EMBED_WORKERS = 2
#: 消费者线程数上限：2 核上再多只会加上下文切换（本机实测 K>3 无收益）。
MAX_EMBED_WORKERS = 4
#: 消费者线程数的环境变量名。
WORKERS_ENV = "NOVA_EMBED_WORKERS"


def embed_window_size(embedding: EmbeddingProvider) -> int:
    """一次 ``embed`` + ``upsert`` 处理的 chunk 数（内存安全的上界）。

    取 ``批大小 × 并发``：刚好让 provider 跑满**一轮**并发（不牺牲吞吐），常驻向量量压到
    ``窗口 × 33 KB``（Voyage 默认 500×4 → ~66 MB），并用 ``MAX_EMBED_WINDOW`` 兜住极端配置。
    """
    batch = getattr(embedding, "batch_size", None)
    if not batch:
        return DEFAULT_EMBED_WINDOW
    concurrency = getattr(embedding, "concurrency", None)
    window = max(1, int(batch)) * max(1, int(concurrency or 1))
    return min(window, MAX_EMBED_WINDOW)


def embed_workers() -> int:
    """消费者线程数：``NOVA_EMBED_WORKERS``（默认 2，夹在 1..``MAX_EMBED_WORKERS``）。

    设 ``1`` 即退回 TASK-114 的单消费者行为（对照实验 / 内存极紧的机器）。
    非法值只记日志并回落默认，不抛异常（配置噪声不该让索引失败）。
    """
    raw = os.environ.get(WORKERS_ENV, "").strip()
    if not raw:
        return DEFAULT_EMBED_WORKERS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，按默认 %d", WORKERS_ENV, raw, DEFAULT_EMBED_WORKERS)
        return DEFAULT_EMBED_WORKERS
    return max(1, min(MAX_EMBED_WORKERS, value))


@dataclass
class EmbeddingStats:
    """一次 ingest 的向量阶段计数（``IngestReport`` 的字段来源）。

    口径设计成**可验证的加法**：``upserted == embedded + deduped``
    （``skipped`` 是连行都不用改的数量，单独成桶）。

    - ``upserted``：写入/覆盖的向量行数；
    - ``deduped``：**没有调用 ``embed()`` 就拿到向量**的行数（按内容命中向量表 / 跨项目缓存 /
      本轮更早窗口刚写的行）；
    - ``skipped``：同 id 同内容、连行都不用改的数量；
    - ``embedded``：真正送进 ``embed()`` 的 chunk 数。
    """

    upserted: int = 0
    deduped: int = 0
    skipped: int = 0
    embedded: int = 0


class EmbeddingSink:
    """把 chunk 流写进向量表的同步消费者（单线程使用；线程化见 :class:`EmbeddingPipeline`）。"""

    def __init__(
        self,
        embedding: EmbeddingProvider,
        vectors: VectorStore,
        *,
        cache: EmbeddingCache | None = None,
        window: int | None = None,
    ) -> None:
        self._embedding = embedding
        self._vectors = vectors
        self._cache = cache
        self._model_id = embedding.profile.model_id
        #: 单个 sink 的窗口。并行时由 pipeline 传 ``总窗口 ÷ K``（内存总量守恒，见其 docstring）；
        #: 独立使用时取 provider 的完整窗口。
        self._window = max(1, window) if window is not None else embed_window_size(embedding)
        self.stats = EmbeddingStats()
        self._pending: list[ChunkDef] = []

    @property
    def window(self) -> int:
        """本 sink 的窗口（chunk 数）：独立使用时 = ``批大小 × 并发``；并行时由 pipeline 传入。"""
        return self._window

    def feed(self, chunks: Sequence[ChunkDef]) -> None:
        """追加一段 chunk；攒满一个窗口就处理（跨文件攒批，见模块 docstring）。"""
        self._pending.extend(chunks)
        while len(self._pending) >= self._window:
            window = self._pending[: self._window]
            del self._pending[: self._window]
            self._process_window(window)

    def flush(self) -> None:
        """处理缓冲里不足一个窗口的剩余 chunk（流水线关停前必须调用）。"""
        if not self._pending:
            return
        window = self._pending
        self._pending = []
        self._process_window(window)

    # ------------------------------------------------------------------ 内部

    def _process_window(self, window: Sequence[ChunkDef]) -> None:
        stored = self._vectors.get_hashes([chunk.id for chunk in window])
        pending = [chunk for chunk in window if stored.get(chunk.id) != chunk.content_hash]
        self.stats.skipped += len(window) - len(pending)
        if not pending:
            return

        # 一层：向量表里已有的同内容行（含本 run 更早窗口刚写入的行——落库即可见）。
        reuse = self._vectors.get_vectors_by_hash([chunk.content_hash for chunk in pending])
        # 二层：跨项目缓存（TASK-111）。缓存是优化，任何异常都降级为未命中。
        if self._cache is not None:
            missing = [
                digest
                for digest in dict.fromkeys(chunk.content_hash for chunk in pending)
                if digest not in reuse
            ]
            try:
                for digest, vector in self._cache.lookup(self._model_id, missing).items():
                    reuse.setdefault(digest, ("", vector))
            except EmbeddingCacheError as exc:
                logger.warning("embedding 缓存读取失败，按未命中处理：%s", exc)

        moves: list[VectorRow] = []
        to_embed: list[ChunkDef] = []
        for chunk in pending:
            hit = reuse.get(chunk.content_hash)
            if hit is None:
                to_embed.append(chunk)
                continue
            moves.append(
                VectorRow(chunk_id=chunk.id, content_hash=chunk.content_hash, vector=hit[1])
            )
            self.stats.deduped += 1
        if moves:
            self.stats.upserted += self._vectors.upsert(moves)
        if to_embed:
            self._embed_unique(to_embed)

    def _embed_unique(self, chunks: Sequence[ChunkDef]) -> None:
        """窗口内按 ``content_hash`` 去重后嵌入，再把同一向量挂到各 chunk id 上。"""
        groups: dict[str, list[ChunkDef]] = {}
        for chunk in chunks:
            groups.setdefault(chunk.content_hash, []).append(chunk)
        digests = list(groups)
        vectors = self._embedding.embed([embedding_text(groups[d][0]) for d in digests])
        if len(vectors) != len(digests):
            raise RuntimeError(
                f"embedding 返回行数不匹配：期望 {len(digests)}，实际 {len(vectors)}"
            )
        rows: list[VectorRow] = []
        fresh: dict[str, Sequence[float]] = {}
        for digest, vector in zip(digests, vectors, strict=True):
            normalized = list(vector)
            fresh[digest] = normalized
            for chunk in groups[digest]:
                rows.append(
                    VectorRow(chunk_id=chunk.id, content_hash=digest, vector=normalized)
                )
        self.stats.upserted += self._vectors.upsert(rows)
        self.stats.embedded += len(rows)
        if self._cache is not None:
            try:
                self._cache.put(self._model_id, dict(fresh))
            except EmbeddingCacheError as exc:  # 缓存写失败不影响索引
                logger.warning("embedding 缓存写入失败：%s", exc)


_SENTINEL = object()


class EmbeddingPipeline:
    """把向量阶段放到 K 个后台消费者线程，让"取回"与"解码/落库"跨窗口重叠（P1-4 → TASK-115）。

    TASK-114 是"生产者 1 + 消费者 1"：消费者内部 ``取回 → 解码 → 落库`` 串成一根链，
    langchain 冷启动里它一户占 48.1s、链路在解码/落库期间空转（``network_busy/wall=0.41``）。
    这里把窗口分给 K 个消费者（默认 2），窗口 N 在解码/落库时窗口 N+1/N+2 的请求仍在飞。

    边界：

    - **窗口在 pipeline 层攒满再派发**（不是让每个消费者各自攒）：窗口 = ``批大小 × 并发``，
      它同时是 provider 单次 ``embed()`` 能跑满并发的单位；若按 K 切小窗口，单次调用的批数会
      变少、链路并发反而上不去（实测 K=2 + 切窗：73.98s，与 K=1 的 74.73s 无差）。
      由主线程攒窗还有个好处：中等仓库（chunk 数介于 1~K 窗之间，如 HelloAgents）也能在
      解析期间就派发出第一个满窗并开始取回，不必等关停；
    - **队列单位是窗口**，有界 ``workers + 2``：背压即内存护栏；峰值向量 ≈
      ``K × 窗口 × 33KB``（K 个消费者同时在处理），另有 1 窗待派发缓冲与队列里的 chunk；
    - **SQLite 仍只有主线程写**（per-project 单写者不变量）；LanceDB 由 ``VectorStore`` 的
      RLock 串行化（总量不变，被藏进网络等待）；
    - **顺序无关**：向量按 chunk id 幂等写入，谁先谁后都得到同一张表；
    - **异常不吞**：任一消费者失败 → 记下首个异常并继续排水（不让生产者死锁）→
      ``submit`` 尽早重抛、``close`` 兜底重抛、``abort`` 丢弃缓冲不掩盖原异常。
    """

    def __init__(
        self,
        embedding: EmbeddingProvider,
        vectors: VectorStore,
        *,
        cache: EmbeddingCache | None = None,
        workers: int | None = None,
        queue_windows: int | None = None,
    ) -> None:
        self._workers = max(1, workers) if workers is not None else embed_workers()
        self._window = embed_window_size(embedding)
        self._sinks = [
            EmbeddingSink(embedding, vectors, cache=cache, window=self._window)
            for _ in range(self._workers)
        ]
        #: 主线程攒窗缓冲（< 1 窗）；攒满即派发，关停时把余量作为"尾窗"派发一次。
        self._pending: list[ChunkDef] = []
        self._queue: Queue[object] = Queue(
            maxsize=max(2, (queue_windows or self._workers) + 2)
        )
        self._error: BaseException | None = None
        self._error_lock = threading.Lock()
        self._closed = False
        self._aborted = False
        self._threads = [
            threading.Thread(
                target=self._consume, args=(sink,), name=f"nova-embed-{index}", daemon=True
            )
            for index, sink in enumerate(self._sinks)
        ]
        for thread in self._threads:
            thread.start()

    @property
    def workers(self) -> int:
        """消费者线程数（内存护栏的乘数：峰值 ≈ ``workers × 窗口 × 33KB``）。"""
        return self._workers

    @property
    def window(self) -> int:
        """**每个消费者**的窗口（chunk 数）= ``批大小 × 并发 ÷ K``。"""
        return self._window

    @property
    def stats(self) -> EmbeddingStats:
        """K 个消费者计数的合计（各 sink 自己记，关闭前读到的不是最终值）。"""
        total = EmbeddingStats()
        for sink in self._sinks:
            stats = sink.stats
            total.upserted += stats.upserted
            total.deduped += stats.deduped
            total.skipped += stats.skipped
            total.embedded += stats.embedded
        return total

    def submit(self, chunks: Sequence[ChunkDef]) -> None:
        """投递一段 chunk（内部按窗口切片）；消费者已失败时立即重抛其异常。"""
        if self._closed:
            raise RuntimeError("EmbeddingPipeline 已关闭，不能再 submit")
        if self._error is not None:
            raise self._error
        self._pending.extend(chunks)
        while len(self._pending) >= self._window:
            window = self._pending[: self._window]
            del self._pending[: self._window]
            self._queue.put(window)  # 队列满 → 阻塞，即背压

    def flush_pending(self) -> None:
        """派发未满的尾窗，不等待消费者；用于和只依赖 SQLite 的图解析重叠。"""
        if self._closed:
            raise RuntimeError("EmbeddingPipeline 已关闭")
        if self._pending:
            self._queue.put(self._pending)
            self._pending = []

    def close(self) -> None:
        """等队列排空并返回；任一消费者失败则在此重抛。幂等。"""
        if self._closed:
            return
        self.flush_pending()
        self._closed = True
        for _ in self._threads:
            self._queue.put(_SENTINEL)
        for thread in self._threads:
            thread.join()
        if self._error is not None:
            raise self._error

    def abort(self) -> None:
        """丢弃未处理任务并等待消费者退出（主流程已因异常中断时调用，不掩盖原异常）。"""
        self._closed = True
        self._aborted = True
        self._pending = []
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except Empty:
                break
        for _ in self._threads:
            self._queue.put(_SENTINEL)
        for thread in self._threads:
            thread.join()

    def __enter__(self) -> EmbeddingPipeline:
        return self

    def __exit__(self, *exc_info: object) -> None:
        if exc_info and exc_info[0] is not None:
            self.abort()
        else:
            self.close()

    def _set_error(self, exc: BaseException) -> None:
        with self._error_lock:
            if self._error is None:
                self._error = exc

    def _consume(self, sink: EmbeddingSink) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _SENTINEL:
                    if self._error is None and not self._aborted:
                        try:
                            sink.flush()
                        except BaseException as exc:  # noqa: BLE001 - 同上，回抛主线程
                            self._set_error(exc)
                    return
                if self._error is None:
                    try:
                        sink.feed(item)  # type: ignore[arg-type]
                        sink.flush()  # 队列项就是一个完整窗口，尾窗也立即执行。
                    except BaseException as exc:  # noqa: BLE001 - 记录后回抛主线程
                        self._set_error(exc)
            finally:
                self._queue.task_done()
