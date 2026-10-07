"""向量 sink 与流水线（TASK-114 / P0-2 / P1-4）。

覆盖四条不变量：

1. **复用优先级**：同 id 同内容跳过、同内容换 id 搬移、缓存命中 → 都不重嵌；
2. **窗口内去重**：同窗口相同内容只送 ``embed()`` 一次，且每个 chunk 都有向量行；
3. **流水线失败语义**：消费者异常必须回抛主线程，不静默吞掉半个索引。
4. **并行消费者（TASK-115）**：K 窗在飞时窗口上限与计数不变量不变，失败/背压都不死锁。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import pytest
from nova_core.pipeline.embedding_sink import (
    DEFAULT_EMBED_WORKERS,
    MAX_EMBED_WORKERS,
    WORKERS_ENV,
    EmbeddingPipeline,
    EmbeddingSink,
    embed_workers,
)
from nova_core.types import ChunkDef, VectorRow
from nova_core.vectors import VectorStore
from nova_core.vectors.cache import EmbeddingCache

from .conftest import TEST_DIM, TEST_PROFILE, CountingEmbedding

_MODEL_ID = TEST_PROFILE.model_id


def _chunk(chunk_id: str, content: str) -> ChunkDef:
    """最简 chunk：内容决定 ``content_hash``（与真实切片口径一致：hash 是内容寻址）。"""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return ChunkDef(
        id=chunk_id,
        file_path=chunk_id.split(":", 1)[0],
        symbol_fqn=None,
        symbol_kind="fallback_block",
        start_line=1,
        end_line=2,
        signature="",
        docstring="",
        content=content,
        content_hash=digest,
    )


def _vector(seed: float = 1.0) -> list[float]:
    vector = [0.0] * TEST_DIM
    vector[0] = seed
    return vector


class _TinyWindow(CountingEmbedding):
    """窗口=1 的 provider：让"跨窗口复用"可在单次 feed 内被测到。"""

    batch_size = 1
    concurrency = 1


class _ExplodingEmbedding(CountingEmbedding):
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("embedding boom")


class _TinyExplodingEmbedding(_ExplodingEmbedding):
    """窗口=1 的爆炸替身：投进去立刻失败，用来验证错误能尽早回到主线程。"""

    batch_size = 1
    concurrency = 1


def test_within_window_dedupes_identical_content(vectors: VectorStore) -> None:
    """同窗口相同内容只嵌一次；每个 chunk 仍各有一行向量（计数不变）。"""
    embedding = CountingEmbedding()
    sink = EmbeddingSink(embedding, vectors)

    sink.feed([_chunk("a.py:x:1", "same"), _chunk("b.py:x:1", "same"), _chunk("c.py:x:1", "other")])
    sink.flush()

    assert embedding.calls == 1
    assert len(embedding.batches[0]) == 2, "两个不同内容 → 只送两个文本"
    assert sink.stats.upserted == 3, "每个 chunk 都要有向量行"
    assert sink.stats.embedded + sink.stats.deduped == sink.stats.upserted, "写过的行 = 嵌入 + 去重"
    assert sink.stats.embedded == 3, "同一窗口内没有可复用的行，三行都来自本次嵌入"
    assert vectors.count() == 3


def test_feed_buffers_across_files_until_a_full_window(vectors: VectorStore) -> None:
    """跨文件攒批：不足一个窗口时不发请求，``flush`` 才收尾。

    回归锚点：按文件开窗会把 langchain（平均 ~7 chunk/文件）退化成 2986 次串行小请求。
    """
    embedding = CountingEmbedding()  # 窗口 = DEFAULT_EMBED_WINDOW（512）
    sink = EmbeddingSink(embedding, vectors)

    sink.feed([_chunk("a.py:x:1", "one")])
    sink.feed([_chunk("b.py:x:1", "two")])
    assert embedding.calls == 0, "没攒满窗口就不该调用 embed"

    sink.flush()

    assert embedding.calls == 1
    assert len(embedding.batches[0]) == 2
    assert vectors.count() == 2


def test_pipeline_close_flushes_pending(vectors: VectorStore) -> None:
    """``workers=1``：关停必须把未满窗口的缓冲处理掉（否则向量表缺行）。"""
    embedding = CountingEmbedding()

    with EmbeddingPipeline(embedding, vectors, workers=1) as pipeline:
        pipeline.submit([_chunk("a.py:x:1", "one")])
        pipeline.submit([_chunk("b.py:x:1", "two")])

    assert embedding.calls == 1
    assert vectors.count() == 2


def test_same_id_same_content_is_skipped(vectors: VectorStore) -> None:
    """同 id 同内容：不写、不嵌。"""
    chunk = _chunk("a.py:x:1", "body")
    vectors.upsert([VectorRow(chunk.id, chunk.content_hash, _vector())])
    embedding = CountingEmbedding()
    sink = EmbeddingSink(embedding, vectors)

    sink.feed([chunk])
    sink.flush()

    assert (embedding.calls, sink.stats.upserted, sink.stats.skipped) == (0, 0, 1)


def test_content_move_reuses_existing_vector(vectors: VectorStore) -> None:
    """同内容换 id（行号漂移）：只搬 id，不重嵌。"""
    old = _chunk("old.py:x:1", "body")
    vectors.upsert([VectorRow(old.id, old.content_hash, _vector())])
    new = _chunk("new.py:x:1", "body")
    embedding = CountingEmbedding()
    sink = EmbeddingSink(embedding, vectors)

    sink.feed([new])
    sink.flush()

    assert (embedding.calls, sink.stats.deduped, sink.stats.upserted) == (0, 1, 1)
    assert vectors.get_hashes([new.id]) == {new.id: new.content_hash}


def test_cross_window_reuse_uses_the_vector_table(vectors: VectorStore) -> None:
    """跨窗口复用靠向量表（前一个窗口刚落的行即可见），不需要整仓内存 map。"""
    embedding = _TinyWindow()
    sink = EmbeddingSink(embedding, vectors)

    sink.feed([_chunk("a.py:x:1", "same"), _chunk("b.py:x:1", "same")])

    assert embedding.calls == 1, "窗口=1：第二个 chunk 应靠表命中而非重嵌"
    assert sink.stats.deduped == 1
    assert sink.stats.upserted == 2
    assert sink.stats.upserted == sink.stats.embedded + sink.stats.deduped
    assert vectors.count() == 2


def test_cache_hit_avoids_embedding(vectors: VectorStore, tmp_path: Path) -> None:
    """跨项目缓存命中：不嵌、搬 id、写回新 id。"""
    chunk = _chunk("a.py:x:1", "body")
    with EmbeddingCache.open(tmp_path, _MODEL_ID, TEST_DIM) as cache:
        cache.put(_MODEL_ID, {chunk.content_hash: _vector()})
        embedding = CountingEmbedding()
        sink = EmbeddingSink(embedding, vectors, cache=cache)
        sink.feed([chunk])
        sink.flush()

    assert (embedding.calls, sink.stats.deduped, sink.stats.upserted) == (0, 1, 1)


def test_embedded_vectors_are_written_back_to_cache(
    vectors: VectorStore, tmp_path: Path
) -> None:
    """新嵌的向量要立即写回共享缓存（TASK-111 的跨分支复用来源）。"""
    chunk = _chunk("a.py:x:1", "body")
    with EmbeddingCache.open(tmp_path, _MODEL_ID, TEST_DIM) as cache:
        sink = EmbeddingSink(CountingEmbedding(), vectors, cache=cache)
        sink.feed([chunk])
        sink.flush()
        assert cache.lookup(_MODEL_ID, [chunk.content_hash])


def test_pipeline_reraises_consumer_error(vectors: VectorStore) -> None:
    """消费者（后台线程）异常必须在主线程重抛，不允许静默半成品。"""
    with pytest.raises(RuntimeError, match="embedding boom"):
        with EmbeddingPipeline(_ExplodingEmbedding(), vectors, workers=2) as pipeline:
            pipeline.submit([_chunk("a.py:x:1", "body")])


def test_single_worker_keeps_fifo_reuse(vectors: VectorStore) -> None:
    """``workers=1`` 退回 TASK-114 行为：FIFO 处理，后一窗复用前一窗刚落的向量。"""
    embedding = _TinyWindow()
    with EmbeddingPipeline(embedding, vectors, workers=1) as pipeline:
        pipeline.submit([_chunk("a.py:x:1", "same")])
        pipeline.submit([_chunk("b.py:x:1", "same")])
    stats = pipeline.stats

    assert embedding.calls == 1
    assert stats.deduped == 1
    assert vectors.count() == 2


def test_parallel_workers_keep_window_and_counts(vectors: VectorStore) -> None:
    """K=2：窗口上限不变、计数不变量成立、每个 chunk 都落到向量表。"""
    embedding = _TinyWindow()  # 窗口 = 1，便于观察批次划分
    chunks = [_chunk(f"f{index}.py:x:1", f"body-{index}") for index in range(20)]

    with EmbeddingPipeline(embedding, vectors, workers=2) as pipeline:
        pipeline.submit(chunks)
    stats = pipeline.stats

    assert pipeline.workers == 2
    assert all(len(batch) <= pipeline.window for batch in embedding.batches)
    assert stats.upserted == 20
    assert stats.upserted == stats.embedded + stats.deduped
    assert vectors.count() == 20


def test_embed_workers_reads_env_and_clamps(monkeypatch: pytest.MonkeyPatch) -> None:
    """``NOVA_EMBED_WORKERS``：缺省 2、非法值回落、上限夹住（配置噪声不得让索引失败）。"""
    monkeypatch.delenv(WORKERS_ENV, raising=False)
    assert embed_workers() == DEFAULT_EMBED_WORKERS
    monkeypatch.setenv(WORKERS_ENV, "1")
    assert embed_workers() == 1
    monkeypatch.setenv(WORKERS_ENV, "99")
    assert embed_workers() == MAX_EMBED_WORKERS
    monkeypatch.setenv(WORKERS_ENV, "abc")
    assert embed_workers() == DEFAULT_EMBED_WORKERS


def test_close_dispatches_the_partial_tail(vectors: VectorStore) -> None:
    """关停要把"不满一个窗口的余量"派发出去（否则向量表缺行）。"""
    embedding = CountingEmbedding()  # 窗口 = 512：两段都还留在 pipeline 的待派发缓冲里

    with EmbeddingPipeline(embedding, vectors, workers=2) as pipeline:
        pipeline.submit([_chunk("a.py:x:1", "one")])
        pipeline.submit([_chunk("b.py:x:1", "two")])

    assert vectors.count() == 2
    assert sum(len(batch) for batch in embedding.batches) == 2


def test_backpressure_never_deadlocks(vectors: VectorStore) -> None:
    """生产者远快于消费者（逐个 submit、队列只留 1 窗）时不死锁，最终全部落库。"""
    embedding = _TinyWindow()
    chunks = [_chunk(f"f{index}.py:x:1", f"body-{index}") for index in range(50)]

    with EmbeddingPipeline(embedding, vectors, workers=2, queue_windows=1) as pipeline:
        for chunk in chunks:
            pipeline.submit([chunk])

    assert vectors.count() == 50
    assert pipeline.stats.upserted == 50


def test_consumer_error_does_not_block_producer(vectors: VectorStore) -> None:
    """一个消费者失败后 ``submit`` 尽早重抛——不能把生产者卡在队列上等死。"""
    pipeline = EmbeddingPipeline(
        _TinyExplodingEmbedding(), vectors, workers=2, queue_windows=1
    )
    try:
        with pytest.raises(RuntimeError, match="embedding boom"):
            for index in range(50):
                pipeline.submit([_chunk(f"f{index}.py:x:1", "body")])
    finally:
        pipeline.abort()


def test_pipeline_rejects_submit_after_close(vectors: VectorStore) -> None:
    pipeline = EmbeddingPipeline(CountingEmbedding(), vectors, workers=2)
    pipeline.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        pipeline.submit([_chunk("a.py:x:1", "body")])


def test_pipeline_abort_does_not_flush_pending(vectors: VectorStore) -> None:
    """主流程已失败时 abort 丢弃缓冲，不做无意义的嵌入（也不掩盖原异常）。"""
    embedding = CountingEmbedding()
    pipeline = EmbeddingPipeline(embedding, vectors, workers=2)

    pipeline.submit([_chunk("a.py:x:1", "body")])
    pipeline.abort()

    assert embedding.calls == 0


class _ArrayEmbedding(CountingEmbedding):
    """提供 ``embed_array`` 的 provider：sink 应优先走矩阵接口，不调用 ``embed``。"""

    def __init__(self) -> None:
        super().__init__()
        self.array_calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise AssertionError("有 embed_array 时不应调用 embed")

    def embed_array(self, texts: Sequence[str]):
        import numpy as np

        self.array_calls += 1
        return np.asarray(CountingEmbedding.embed(self, texts), dtype=np.float32)


def test_sink_prefers_embed_array_and_writes_identical_vectors(
    vectors: VectorStore, tmp_path: Path
) -> None:
    import numpy as np

    chunks = [_chunk("a.py:1", "alpha"), _chunk("b.py:1", "beta"), _chunk("c.py:1", "alpha")]
    provider = _ArrayEmbedding()
    sink = EmbeddingSink(provider, vectors)
    sink.feed(chunks)
    sink.flush()
    assert provider.array_calls == 1
    assert sink.stats.embedded == 3 and sink.stats.upserted == 3
    expected = CountingEmbedding().embed(["alpha", "beta"])
    found = vectors.get_vector_arrays_by_hash([chunks[0].content_hash, chunks[1].content_hash])
    assert np.array_equal(found[chunks[0].content_hash][1], np.float32(expected[0]))
    assert np.array_equal(found[chunks[1].content_hash][1], np.float32(expected[1]))
    assert vectors.count() == 3


def test_known_vectors_view_matches_per_window_queries(tmp_path: Path) -> None:
    """``KnownVectors``（开始时一次快照 + 本轮写入登记）与逐窗口查表的结果一致：
    同 id 同内容跳过、换 id 同内容搬移、本轮内重复内容复用刚写入的行。"""
    import numpy as np
    from nova_core.pipeline import embedding_sink

    def run(use_known: bool) -> tuple[dict[str, tuple[str, list[float]]], tuple[int, ...]]:
        with VectorStore.open(tmp_path / f"known-{use_known}", dim=TEST_DIM) as store:
            seed = [_chunk("old.py:1", "kept"), _chunk("old.py:2", "moved")]
            sink = EmbeddingSink(CountingEmbedding(), store)
            sink.feed(seed)
            sink.flush()
            original = embedding_sink.EmbeddingSink.__init__

            def init(self, *args, **kwargs):  # noqa: ANN001, ANN202
                if not use_known:
                    kwargs["known"] = None
                original(self, *args, **kwargs)

            embedding_sink.EmbeddingSink.__init__ = init
            try:
                provider = _TinyWindow()
                with EmbeddingPipeline(provider, store, workers=1) as pipeline:
                    pipeline.submit([
                        _chunk("old.py:1", "kept"),      # 同 id 同内容 → 跳过
                        _chunk("new.py:9", "moved"),     # 换 id 同内容 → 搬移
                        _chunk("new.py:1", "fresh"),
                        _chunk("new.py:2", "fresh"),     # 本轮重复 → 复用刚写入的行
                    ])
                stats = pipeline.stats
            finally:
                embedding_sink.EmbeddingSink.__init__ = original
            rows = {}
            for chunk_id in ("old.py:1", "old.py:2", "new.py:9", "new.py:1", "new.py:2"):
                digest = store.get_hashes([chunk_id]).get(chunk_id)
                vector = store.get_vector_arrays_by_hash([digest])[digest][1] if digest else None
                rows[chunk_id] = (digest, None if vector is None else np.asarray(vector).tolist())
            counts = (stats.upserted, stats.deduped, stats.skipped, stats.embedded, provider.calls)
            return rows, counts

    assert run(True) == run(False)
