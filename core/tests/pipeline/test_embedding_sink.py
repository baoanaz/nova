"""向量 sink 与流水线（TASK-114 / P0-2 / P1-4）。

覆盖三条不变量：

1. **复用优先级**：同 id 同内容跳过、同内容换 id 搬移、缓存命中 → 都不重嵌；
2. **窗口内去重**：同窗口相同内容只送 ``embed()`` 一次，且每个 chunk 都有向量行；
3. **流水线失败语义**：消费者异常必须回抛主线程，不静默吞掉半个索引。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import pytest
from zace_core.pipeline.embedding_sink import (
    EmbeddingPipeline,
    EmbeddingSink,
)
from zace_core.types import ChunkDef, VectorRow
from zace_core.vectors import VectorStore
from zace_core.vectors.cache import EmbeddingCache

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
    """流水线关停必须把未满窗口的缓冲处理掉（否则向量表缺行）。"""
    embedding = CountingEmbedding()
    sink = EmbeddingSink(embedding, vectors)

    with EmbeddingPipeline(sink) as pipeline:
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
    sink = EmbeddingSink(_ExplodingEmbedding(), vectors)

    with pytest.raises(RuntimeError, match="embedding boom"):
        with EmbeddingPipeline(sink) as pipeline:
            pipeline.submit([_chunk("a.py:x:1", "body")])


def test_pipeline_keeps_fifo_order_across_submits(vectors: VectorStore) -> None:
    """多次 submit 按 FIFO 处理：后一个窗口能复用前一个窗口刚落的向量。"""
    embedding = _TinyWindow()
    sink = EmbeddingSink(embedding, vectors)

    with EmbeddingPipeline(sink) as pipeline:
        pipeline.submit([_chunk("a.py:x:1", "same")])
        pipeline.submit([_chunk("b.py:x:1", "same")])

    assert embedding.calls == 1
    assert sink.stats.deduped == 1
    assert vectors.count() == 2


def test_pipeline_rejects_submit_after_close(vectors: VectorStore) -> None:
    pipeline = EmbeddingPipeline(EmbeddingSink(CountingEmbedding(), vectors))
    pipeline.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        pipeline.submit([_chunk("a.py:x:1", "body")])


def test_pipeline_abort_does_not_flush_pending(vectors: VectorStore) -> None:
    """主流程已失败时 abort 丢弃缓冲，不做无意义的嵌入（也不掩盖原异常）。"""
    embedding = CountingEmbedding()
    pipeline = EmbeddingPipeline(EmbeddingSink(embedding, vectors))

    pipeline.submit([_chunk("a.py:x:1", "body")])
    pipeline.abort()

    assert embedding.calls == 0
