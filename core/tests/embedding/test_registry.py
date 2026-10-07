"""registry.py 单元测试：候选模型清单与指纹 id 命名（TASK-008 §A/§B，无网络）。"""

from __future__ import annotations

import pytest
from nova_core.embedding.base import EmbeddingConfigError
from nova_core.embedding.registry import (
    API_MODEL_ALIASES,
    API_MODELS,
    DEFAULT_LOCAL_SLUG,
    LOCAL_MODELS,
    find_api_spec,
    get_local_spec,
)


def test_default_slug_is_registered() -> None:
    assert DEFAULT_LOCAL_SLUG in LOCAL_MODELS


def test_bakeoff_candidates_are_registered() -> None:
    # 卡内 §B 的三个候选（TASK-015 对比清单）必须齐全。
    assert set(LOCAL_MODELS) == {
        "multilingual-e5-small",
        "bge-small-zh-v1.5",
        "arctic-embed-xs",
    }


@pytest.mark.parametrize("slug", sorted(LOCAL_MODELS))
def test_local_specs_are_self_consistent(slug: str) -> None:
    spec = LOCAL_MODELS[slug]
    assert spec.dim > 0
    assert spec.max_input_tokens > 0
    assert spec.pooling in ("mean", "cls")
    assert spec.repo_id and spec.onnx_file and spec.tokenizer_file
    assert spec.repo_file == f"{spec.repo_id}/{spec.onnx_file}"


def test_e5_prefix_convention_matches_card() -> None:
    e5 = LOCAL_MODELS["multilingual-e5-small"]
    assert (e5.query_prefix, e5.passage_prefix) == ("query: ", "passage: ")
    assert e5.pooling == "mean"
    for slug in ("bge-small-zh-v1.5", "arctic-embed-xs"):
        spec = LOCAL_MODELS[slug]
        assert (spec.query_prefix, spec.passage_prefix) == ("", "")
        assert spec.pooling == "cls"


def test_model_id_naming_follows_card() -> None:
    assert LOCAL_MODELS["multilingual-e5-small"].model_id == "local:multilingual-e5-small"
    assert API_MODELS["bge-m3"].model_id == "api:bge-m3"


def test_get_local_spec_unknown_slug_lists_candidates() -> None:
    with pytest.raises(EmbeddingConfigError) as excinfo:
        get_local_spec("not-a-model")
    message = str(excinfo.value)
    assert "not-a-model" in message
    assert "multilingual-e5-small" in message


def test_find_api_spec_returns_none_for_unregistered_model() -> None:
    assert find_api_spec("bge-m3") is API_MODELS["bge-m3"]
    assert find_api_spec("some-self-hosted-model") is None


# --- TASK-046 §A：别名解析 -------------------------------------------------


def test_vendor_prefixed_alias_resolves_to_registered_spec() -> None:
    """官方文档的写法（BAAI/bge-m3）必须命中已登记条目，而不是“未登记模型”。"""
    assert find_api_spec("BAAI/bge-m3") is API_MODELS["bge-m3"]


def test_alias_does_not_change_fingerprint_id() -> None:
    """别名与裸名是同一条目：model_id 相同 → 换写法不触发无谓重嵌（D-07）。"""
    assert find_api_spec("BAAI/bge-m3").model_id == API_MODELS["bge-m3"].model_id == "api:bge-m3"


def test_alias_targets_are_registered_keys() -> None:
    for alias, canonical in API_MODEL_ALIASES.items():
        assert canonical in API_MODELS, f"别名 {alias} 指向未登记条目 {canonical}"
        assert alias not in API_MODELS, f"{alias} 应作为别名而非独立条目"


def test_request_name_is_the_provider_facing_name() -> None:
    """注册表 key 是 nova 侧的，发给 API 的必须是 provider 认的名字。"""
    assert API_MODELS["bge-m3"].api_model == "BAAI/bge-m3"
    # 未设 request_name 的条目回落 name
    assert API_MODELS["text-embedding-3-small"].api_model == "text-embedding-3-small"
