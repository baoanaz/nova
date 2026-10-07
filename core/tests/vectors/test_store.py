"""TASK-009 验收测试：向量存储（LanceDB）+ hash 复用对账。

覆盖：upsert→search 顺序、chunk_id 幂等、delete、get_hashes 命中/未命中、rebuild 换维度、
维度不匹配报错（含 D-07 指引）、空库检索、重开持久化、关闭后拒绝操作、规模冒烟（slow）。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from nova_core.types import VectorRow
from nova_core.vectors import DimensionMismatchError, VectorStore, VectorStoreError

DIM = 8


def unit(axis: int, dim: int = DIM) -> list[float]:
    vector = [0.0] * dim
    vector[axis] = 1.0
    return vector


def diagonal(dim: int = DIM) -> list[float]:
    """与 axis=0/1 单位向量各成 45°（余弦相似度 = √2/2）。"""
    vector = [0.0] * dim
    vector[0] = vector[1] = 1.0
    return vector


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[VectorStore]:
    with VectorStore.open(tmp_path, dim=DIM) as opened:
        yield opened


def test_open_creates_vectors_directory(tmp_path: Path) -> None:
    with VectorStore.open(tmp_path, dim=DIM):
        pass
    assert (tmp_path / "vectors").is_dir()


def test_upsert_then_search_orders_by_similarity(store: VectorStore) -> None:
    store.upsert(
        [
            VectorRow(chunk_id="a", content_hash="h-a", vector=unit(0)),
            VectorRow(chunk_id="b", content_hash="h-b", vector=unit(1)),
            VectorRow(chunk_id="c", content_hash="h-c", vector=diagonal()),
        ]
    )

    hits = store.search(unit(0), top_k=3)
    assert [hit.chunk_id for hit in hits] == ["a", "c", "b"]
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert hits[1].score == pytest.approx(2**-0.5, abs=1e-5)
    assert hits[2].score == pytest.approx(0.0, abs=1e-5)
    assert hits[0].score > hits[1].score > hits[2].score

    assert len(store.search(unit(1), top_k=1)) == 1


def test_upsert_is_idempotent_per_chunk_id(store: VectorStore) -> None:
    store.upsert([VectorRow(chunk_id="a", content_hash="h1", vector=unit(0))])
    store.upsert([VectorRow(chunk_id="a", content_hash="h2", vector=unit(1))])

    assert store.count() == 1
    assert store.get_hashes(["a"]) == {"a": "h2"}
    assert [hit.chunk_id for hit in store.search(unit(1), top_k=5)] == ["a"]

    assert store.upsert([]) == 0
    assert store.count() == 1


def test_delete_removes_rows_from_search(store: VectorStore) -> None:
    store.upsert(
        [
            VectorRow(chunk_id="a", content_hash="h1", vector=unit(0)),
            VectorRow(chunk_id="b", content_hash="h2", vector=unit(1)),
        ]
    )

    assert store.delete(["a"]) == 1
    assert [hit.chunk_id for hit in store.search(unit(0), top_k=5)] == ["b"]
    assert store.get_hashes(["a", "b"]) == {"b": "h2"}
    assert store.count() == 1

    assert store.delete([]) == 0
    assert store.delete(["missing"]) == 0


def test_get_hashes_hit_and_miss(store: VectorStore) -> None:
    store.upsert(
        [
            VectorRow(chunk_id="a", content_hash="h1", vector=unit(0)),
            VectorRow(chunk_id="b", content_hash="h2", vector=unit(1)),
        ]
    )

    assert store.get_hashes(["b", "missing", "a"]) == {"b": "h2", "a": "h1"}
    assert store.get_hashes(["missing"]) == {}
    assert store.get_hashes([]) == {}


def test_empty_store_search_returns_empty(store: VectorStore) -> None:
    assert store.search(unit(0), top_k=10) == []
    assert store.search(unit(0), top_k=0) == []
    assert store.search(unit(0), top_k=-1) == []


def test_rebuild_replaces_table_with_new_dimension(store: VectorStore) -> None:
    store.upsert([VectorRow(chunk_id="a", content_hash="h1", vector=unit(0))])

    store.rebuild(dim=16)
    assert store.dim == 16
    assert store.count() == 0
    assert store.get_hashes(["a"]) == {}

    # 旧维度读写被拒绝，且文案给出 D-07 二级失效指引
    with pytest.raises(DimensionMismatchError, match="D-07"):
        store.search(unit(0), top_k=5)
    with pytest.raises(DimensionMismatchError, match="D-07"):
        store.upsert([VectorRow(chunk_id="a", content_hash="h1", vector=unit(0))])

    store.upsert([VectorRow(chunk_id="b", content_hash="h9", vector=unit(3, dim=16))])
    hits = store.search(unit(3, dim=16), top_k=5)
    assert [hit.chunk_id for hit in hits] == ["b"]
    assert store.count() == 1


def test_open_existing_table_with_other_dim_raises_with_d07_hint(tmp_path: Path) -> None:
    with VectorStore.open(tmp_path, dim=DIM) as store:
        store.upsert([VectorRow(chunk_id="a", content_hash="h1", vector=unit(0))])

    with pytest.raises(DimensionMismatchError) as excinfo:
        VectorStore.open(tmp_path, dim=16)
    message = str(excinfo.value)
    assert "D-07" in message
    assert "rebuild" in message
    assert "期望 dim=16" in message and "实际 dim=8" in message


def test_invalid_dim_and_malformed_vector(store: VectorStore, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        VectorStore.open(tmp_path / "bad", dim=0)

    # 查询向量长度与表不一致
    with pytest.raises(DimensionMismatchError, match="D-07"):
        store.search(unit(0)[:4], top_k=3)

    # 写入行向量长度与表不一致
    with pytest.raises(DimensionMismatchError, match="D-07"):
        store.upsert([VectorRow(chunk_id="a", content_hash="h1", vector=unit(0)[:4])])


def test_data_persists_across_reopen(tmp_path: Path) -> None:
    with VectorStore.open(tmp_path, dim=DIM) as store:
        store.upsert([VectorRow(chunk_id="a", content_hash="h1", vector=unit(0))])

    with VectorStore.open(tmp_path, dim=DIM) as store:
        assert store.get_hashes(["a"]) == {"a": "h1"}
        assert [hit.chunk_id for hit in store.search(unit(0), top_k=3)] == ["a"]


def test_closed_store_rejects_operations(tmp_path: Path) -> None:
    store = VectorStore.open(tmp_path, dim=DIM)
    store.close()

    with pytest.raises(VectorStoreError):
        store.upsert([VectorRow(chunk_id="a", content_hash="h1", vector=unit(0))])
    with pytest.raises(VectorStoreError):
        store.search(unit(0), top_k=1)
    with pytest.raises(VectorStoreError):
        store.get_hashes(["a"])
    with pytest.raises(VectorStoreError):
        store.rebuild(dim=16)


@pytest.mark.slow
def test_scale_smoke_10k_rows_dim_384(tmp_path: Path) -> None:
    """规模冒烟：10k 行 dim=384 upsert + 10 次查询（默认跳过：NOVA_RUN_SLOW=1 启用）。"""
    dim = 384
    rng = np.random.default_rng(20260910)
    rows = [
        VectorRow(
            chunk_id=f"src/mod_{index // 100}.py:fn_{index}:{index}",
            content_hash=f"hash-{index}",
            vector=rng.standard_normal(dim).astype("float32").tolist(),
        )
        for index in range(10_000)
    ]

    with VectorStore.open(tmp_path, dim=dim) as store:
        start = time.perf_counter()
        assert store.upsert(rows) == 10_000
        upsert_seconds = time.perf_counter() - start
        assert store.count() == 10_000

        queries = [rng.standard_normal(dim).astype("float32").tolist() for _ in range(10)]
        start = time.perf_counter()
        for query in queries:
            hits = store.search(query, top_k=10)
            assert len(hits) == 10
            assert all(1.0 >= hit.score > -1.0 for hit in hits)
        search_seconds = time.perf_counter() - start

    print(
        f"[scale-smoke] upsert 10k×{dim}: {upsert_seconds:.2f}s; "
        f"10 queries: {search_seconds:.2f}s（{search_seconds / 10 * 1000:.0f} ms/query）"
    )
    assert upsert_seconds < 60
    assert search_seconds < 60
