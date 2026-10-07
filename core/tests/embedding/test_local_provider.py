"""local.py 单元测试：stub ONNX 会话 + 微型 tokenizer（TASK-008 DoD，全程无网络）。

覆盖：归一化模长、批切分、profile、e5 前缀注入、mean/CLS 池化、pad 不影响结果、
超长截断、维度不匹配、离线可读错误、2-D 已池化导出、token_type_ids 兼容。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from nova_core.embedding.base import (
    EmbeddingConfigError,
    EmbeddingDimMismatchError,
    LocalModelUnavailableError,
)
from nova_core.embedding.local import LocalOnnxEmbeddingProvider
from nova_core.embedding.registry import LOCAL_MODELS, LocalModelSpec

from .conftest import CLS_ID, RecordingTokenizer, StubOnnxSession, one_hot_rows

E5 = LOCAL_MODELS["multilingual-e5-small"]
ARCTIC = LOCAL_MODELS["arctic-embed-xs"]


def make_provider(
    tokenizer,
    *,
    spec: LocalModelSpec | None = None,
    session: StubOnnxSession | None = None,
    **kwargs,
) -> tuple[LocalOnnxEmbeddingProvider, StubOnnxSession]:
    resolved_spec = spec or E5
    resolved_session = session or StubOnnxSession(dim=resolved_spec.dim)
    provider = LocalOnnxEmbeddingProvider(
        resolved_spec, tokenizer=tokenizer, session=resolved_session, **kwargs
    )
    return provider, resolved_session


def manual_unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def test_profile_comes_from_registry(tokenizer) -> None:
    provider, _ = make_provider(tokenizer)
    profile = provider.profile
    assert profile.model_id == "local:multilingual-e5-small"
    assert profile.dim == E5.dim
    assert profile.max_input_tokens == E5.max_input_tokens


def test_vectors_are_unit_length(tokenizer) -> None:
    provider, _ = make_provider(tokenizer)
    vectors = provider.embed(["hello", "hello world", "token 过期 刷新"])
    assert len(vectors) == 3
    for vector in vectors:
        assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-6


def test_empty_input_returns_empty_without_inference(tokenizer) -> None:
    provider, session = make_provider(tokenizer)
    assert provider.embed([]) == []
    assert provider.embed_query([]) == []
    assert session.feeds == []


def test_empty_string_is_embeddable(tokenizer) -> None:
    provider, _ = make_provider(tokenizer)
    [vector] = provider.embed([""])
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-6


def test_batching_respects_batch_size(tokenizer) -> None:
    provider, session = make_provider(tokenizer, batch_size=2)
    provider.embed(["hello", "world", "token", "过期", "刷新"])
    assert [feed["input_ids"].shape[0] for feed in session.feeds] == [2, 2, 1]


def test_mean_pooling_matches_manual_computation(tokenizer) -> None:
    provider, _ = make_provider(tokenizer)
    # 期望值用 provider 实际会喂给模型的文本（含 e5 passage 前缀）独立重算。
    encoded = tokenizer.encode(f"{E5.passage_prefix}hello world", add_special_tokens=True)
    expected = manual_unit(one_hot_rows(list(encoded.ids), E5.dim).mean(axis=0))
    [vector] = provider.embed(["hello world"])
    assert np.allclose(vector, expected, atol=1e-6)


def test_cls_pooling_uses_first_token(tokenizer) -> None:
    provider, _ = make_provider(tokenizer, spec=ARCTIC)
    encoded = tokenizer.encode("hello world", add_special_tokens=True)
    assert encoded.ids[0] == CLS_ID
    [vector] = provider.embed(["hello world"])
    expected = manual_unit(one_hot_rows([CLS_ID], ARCTIC.dim)[0])
    assert np.allclose(vector, expected, atol=1e-6)


def test_padding_does_not_change_vectors(tokenizer) -> None:
    provider, session = make_provider(tokenizer)
    [alone] = provider.embed(["hello"])
    batched = provider.embed(["hello", "hello world token 过期 刷新"])
    assert session.feeds[-1]["input_ids"].shape[1] > session.feeds[0]["input_ids"].shape[1]
    assert np.allclose(alone, batched[0], atol=1e-6)


def test_tokenizer_with_builtin_padding_keeps_vectors_stable(padded_tokenizer) -> None:
    """回归：tokenizer.json 自带 padding 时，不能把 pad 当真实 token（真机冒烟发现）。"""
    provider, session = make_provider(padded_tokenizer)
    [alone] = provider.embed(["hello"])
    batched = provider.embed(["hello", "hello world token 过期 刷新"])
    expected = padded_tokenizer.encode_batch(
        ["passage: hello", "passage: hello world token 过期 刷新"], add_special_tokens=True
    )
    feed = session.feeds[-1]
    assert feed["input_ids"].tolist() == [list(enc.ids) for enc in expected]
    assert feed["attention_mask"].tolist() == [list(enc.attention_mask) for enc in expected]
    assert 0 in feed["attention_mask"][0].tolist()  # 确实出现了 pad 位
    assert np.allclose(alone, batched[0], atol=1e-6)


def test_truncation_respects_max_input_tokens(tokenizer) -> None:
    spec = replace(E5, max_input_tokens=4)
    provider, session = make_provider(tokenizer, spec=spec)
    text = "hello world token 过期 刷新"
    [vector] = provider.embed([text])
    feed = session.feeds[0]
    full_ids = tokenizer.encode(
        f"{E5.passage_prefix}{text}", add_special_tokens=True
    ).ids
    assert len(full_ids) > 4
    assert feed["input_ids"].shape == (1, 4)
    assert feed["input_ids"].tolist() == [full_ids[:4]]  # 硬截断，不报错
    assert feed["attention_mask"].tolist() == [[1, 1, 1, 1]]
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-6


def test_truncation_value_is_exposed_in_profile(tokenizer) -> None:
    provider, _ = make_provider(tokenizer, spec=replace(E5, max_input_tokens=128))
    assert provider.profile.max_input_tokens == 128


def test_query_and_passage_prefixes_are_injected(tokenizer) -> None:
    recorder = RecordingTokenizer(tokenizer)
    provider, _ = make_provider(recorder)
    provider.embed(["hello"])
    provider.embed_query(["hello"])
    assert recorder.batches == [["passage: hello"], ["query: hello"]]


def test_models_without_prefix_keep_text_as_is(tokenizer) -> None:
    recorder = RecordingTokenizer(tokenizer)
    provider, _ = make_provider(recorder, spec=ARCTIC)
    provider.embed(["hello"])
    provider.embed_query(["hello"])
    assert recorder.batches == [["hello"], ["hello"]]


def test_profile_is_identical_for_both_sides(tokenizer) -> None:
    provider, _ = make_provider(tokenizer)
    before = provider.profile
    provider.embed_query(["hello"])
    assert provider.profile == before


def test_dim_mismatch_raises(tokenizer) -> None:
    provider, _ = make_provider(tokenizer, session=StubOnnxSession(dim=7))
    with pytest.raises(EmbeddingDimMismatchError, match="dim="):
        provider.embed(["hello"])


def test_missing_required_onnx_inputs_raises(tokenizer) -> None:
    session = StubOnnxSession(dim=E5.dim, input_names=("foo",))
    provider, _ = make_provider(tokenizer, session=session)
    with pytest.raises(EmbeddingConfigError, match="ONNX 会话缺少必需输入"):
        provider.embed(["hello"])


def test_token_type_ids_fed_only_when_declared(tokenizer) -> None:
    with_tt, with_tt_session = make_provider(tokenizer)
    with_tt.embed(["hello"])
    assert "token_type_ids" not in with_tt_session.feeds[0]

    session = StubOnnxSession(
        dim=E5.dim, input_names=("input_ids", "attention_mask", "token_type_ids")
    )
    provider, _ = make_provider(tokenizer, session=session)
    provider.embed(["hello"])
    feed = session.feeds[0]
    assert feed["token_type_ids"].shape == feed["input_ids"].shape
    assert not feed["token_type_ids"].any()  # 单句输入 → 全 0


def test_pooled_2d_output_is_supported(tokenizer) -> None:
    session = StubOnnxSession(dim=E5.dim, pooled=True)
    provider, _ = make_provider(tokenizer, session=session)
    [vector] = provider.embed(["hello"])
    expected = manual_unit(one_hot_rows([CLS_ID], E5.dim)[0])
    assert np.allclose(vector, expected, atol=1e-6)


def test_tokenizer_is_loaded_from_model_dir(model_dir: Path) -> None:
    provider = LocalOnnxEmbeddingProvider(
        E5, model_dir=model_dir, session=StubOnnxSession(dim=E5.dim)
    )
    assert not provider.is_loaded  # tokenizer 尚未加载
    vectors = provider.embed(["hello"])
    assert provider.is_loaded
    assert abs(float(np.linalg.norm(vectors[0])) - 1.0) < 1e-6


def test_offline_missing_model_raises_readable_error(tmp_path: Path) -> None:
    provider = LocalOnnxEmbeddingProvider(
        E5, offline=True, cache_dir=tmp_path / "empty-cache"
    )
    with pytest.raises(LocalModelUnavailableError) as excinfo:
        provider.embed(["hello"])
    message = str(excinfo.value)
    assert "离线模式" in message
    assert "model_dir" in message  # 给出可选出路，而不是裸 HF/OSError


def test_model_dir_missing_onnx_file_raises_readable_error(model_dir: Path) -> None:
    provider = LocalOnnxEmbeddingProvider(E5, model_dir=model_dir)
    with pytest.raises(LocalModelUnavailableError, match="onnx/model.onnx"):
        provider.ensure_loaded()


def test_batch_size_must_be_positive(tokenizer) -> None:
    with pytest.raises(EmbeddingConfigError, match="batch_size"):
        make_provider(tokenizer, batch_size=0)
