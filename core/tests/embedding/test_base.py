"""base.py 单元测试：归一化、批切分、前缀与错误分类（TASK-008 §A）。"""

from __future__ import annotations

import numpy as np
import pytest
from nova_core.embedding.base import (
    ApiAuthError,
    EmbeddingConfigError,
    EmbeddingDimMismatchError,
    EmbeddingError,
    LocalModelUnavailableError,
    iter_batches,
    l2_normalize,
    with_prefix,
)


def test_error_kinds_are_stable_and_distinct() -> None:
    kinds = {
        EmbeddingError.kind,
        EmbeddingConfigError.kind,
        LocalModelUnavailableError.kind,
        ApiAuthError.kind,
        EmbeddingDimMismatchError.kind,
    }
    assert len(kinds) == 5  # 上层按 kind 做降级/告警分类，不能重复
    assert issubclass(ApiAuthError, EmbeddingError)
    assert issubclass(EmbeddingDimMismatchError, EmbeddingError)


def test_l2_normalize_returns_unit_rows() -> None:
    matrix = np.array([[3.0, 4.0, 0.0], [0.0, 0.0, 5.0]], dtype=np.float32)
    normalized = l2_normalize(matrix)
    assert np.allclose(np.linalg.norm(normalized, axis=1), 1.0, atol=1e-6)
    assert np.allclose(normalized[0], [0.6, 0.8, 0.0], atol=1e-6)
    assert normalized.dtype == np.float32


def test_l2_normalize_handles_1d_and_zero_vectors() -> None:
    vector = l2_normalize(np.array([0.0, 2.0], dtype=np.float32))
    assert np.allclose(vector, [0.0, 1.0])
    zero = l2_normalize(np.zeros((2, 3), dtype=np.float32))
    assert np.all(np.isfinite(zero))  # 零向量保持零，不产生 NaN
    assert np.all(zero == 0.0)


def test_iter_batches_splits_evenly_and_tail() -> None:
    items = ["a", "b", "c", "d", "e"]
    assert [list(batch) for batch in iter_batches(items, 2)] == [
        ["a", "b"],
        ["c", "d"],
        ["e"],
    ]
    assert [list(batch) for batch in iter_batches([], 3)] == []


def test_iter_batches_rejects_invalid_size() -> None:
    with pytest.raises(EmbeddingConfigError, match="batch_size"):
        list(iter_batches(["a"], 0))


def test_with_prefix_keeps_original_text_without_convention() -> None:
    assert with_prefix("hello", "passage: ") == "passage: hello"
    assert with_prefix("hello", "") == "hello"
