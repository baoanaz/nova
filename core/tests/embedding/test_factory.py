"""factory.py 单元测试：配置解析、默认路径选择与可读错误（TASK-008 §D）。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from nova_core.embedding.api import OpenAiCompatibleEmbeddingProvider
from nova_core.embedding.base import EmbeddingConfigError, LocalModelUnavailableError
from nova_core.embedding.factory import EmbeddingConfig, create_provider
from nova_core.embedding.local import LocalOnnxEmbeddingProvider
from nova_core.embedding.registry import DEFAULT_LOCAL_SLUG
from nova_core.interfaces import EmbeddingProvider

from .conftest import StubOnnxSession


def test_default_is_local_onnx_provider() -> None:
    provider = create_provider()
    assert isinstance(provider, LocalOnnxEmbeddingProvider)
    assert isinstance(provider, EmbeddingProvider)  # CF-09 协议兼容
    assert provider.profile.model_id == f"local:{DEFAULT_LOCAL_SLUG}"
    assert provider.profile.dim == 384
    assert provider.batch_size == 16  # 本地默认批大小（卡内 §A）


def test_local_model_selection_by_slug() -> None:
    provider = create_provider({"model": "bge-small-zh-v1.5"})
    assert provider.profile.model_id == "local:bge-small-zh-v1.5"
    assert provider.profile.dim == 512


def test_unknown_local_slug_lists_candidates() -> None:
    with pytest.raises(EmbeddingConfigError) as excinfo:
        create_provider({"model": "nope"})
    assert "arctic-embed-xs" in str(excinfo.value)


def test_local_overrides_are_applied(tokenizer, model_dir: Path) -> None:
    session = StubOnnxSession(dim=32)
    provider = create_provider(
        {
            "model": DEFAULT_LOCAL_SLUG,
            "dim": 32,
            "max_input_tokens": 64,
            "model_dir": str(model_dir),
        },
        tokenizer=tokenizer,
        session=session,
    )
    assert provider.profile.dim == 32
    assert provider.profile.max_input_tokens == 64
    assert len(provider.embed(["hello"])[0]) == 32


def test_api_mode_uses_registry_dim_and_batch_size() -> None:
    provider = create_provider(
        {"mode": "api", "model": "bge-m3", "base_url": "https://api.example.com"}
    )
    assert isinstance(provider, OpenAiCompatibleEmbeddingProvider)
    assert isinstance(provider, EmbeddingProvider)
    assert provider.profile.model_id == "api:bge-m3"
    assert provider.profile.dim == 1024
    assert provider.batch_size == 256  # TASK-049：厂商级安全批大小（siliconflow）


def test_api_mode_requires_model_and_base_url() -> None:
    with pytest.raises(EmbeddingConfigError, match="model"):
        create_provider({"mode": "api", "base_url": "https://api.example.com"})
    with pytest.raises(EmbeddingConfigError, match="base_url"):
        create_provider({"mode": "api", "model": "bge-m3"})


def test_api_unknown_model_requires_explicit_dim() -> None:
    with pytest.raises(EmbeddingConfigError, match="dim"):
        create_provider(
            {"mode": "api", "model": "self-hosted-xyz", "base_url": "https://api.example.com"}
        )
    provider = create_provider(
        {
            "mode": "api",
            "model": "self-hosted-xyz",
            "base_url": "https://api.example.com",
            "dim": 768,
            "max_input_tokens": 1024,
        }
    )
    assert provider.profile.model_id == "api:self-hosted-xyz"
    assert provider.profile.dim == 768
    assert provider.profile.max_input_tokens == 1024


def test_api_accepts_injected_client() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []}))
    )
    provider = create_provider(
        {"mode": "api", "model": "bge-m3", "base_url": "https://api.example.com"},
        client=client,
    )
    assert provider.embed([]) == []


def test_unknown_config_field_is_rejected() -> None:
    with pytest.raises(EmbeddingConfigError) as excinfo:
        create_provider({"model": DEFAULT_LOCAL_SLUG, "nope": 1})
    assert "nope" in str(excinfo.value)


# --- TASK-046 §A：全名开箱可用 ---------------------------------------------


def test_vendor_prefixed_model_is_usable_without_dim() -> None:
    """用户照官方文档填的全名必须开箱可用（本卡主路径，无需 EMBED_DIM）。"""
    provider = create_provider(
        {"mode": "api", "model": "BAAI/bge-m3", "base_url": "https://api.example.com"}
    )
    assert provider.profile.model_id == "api:bge-m3"  # 与裸名同一指纹
    assert provider.profile.dim == 1024


def test_bare_name_keeps_working_route_a() -> None:
    """旧配置 EMBED_MODEL=bge-m3 仍可用（选了路线 a：别名解析）。"""
    provider = create_provider(
        {"mode": "api", "model": "bge-m3", "base_url": "https://api.example.com"}
    )
    assert provider.profile.model_id == "api:bge-m3"


def test_unknown_model_error_points_at_registered_names() -> None:
    """错误信息要指明正确写法（含别名），否则用户会去猜一个同样不可用的裸名。"""
    with pytest.raises(EmbeddingConfigError) as excinfo:
        create_provider({"mode": "api", "model": "nope/xyz", "base_url": "https://api.example.com"})
    message = str(excinfo.value)
    assert "EMBED_DIM" in message
    assert "bge-m3" in message


# --- TASK-046 §B：上限默认值取模型实际能力 ---------------------------------


def test_max_input_tokens_defaults_to_registered_model_capability() -> None:
    provider = create_provider(
        {"mode": "api", "model": "BAAI/bge-m3", "base_url": "https://api.example.com"}
    )
    assert provider.profile.max_input_tokens == 8192


def test_explicit_smaller_cap_is_not_raised() -> None:
    """用户显式给更小值 → 尊重（min 语义，不被抬高）。"""
    provider = create_provider(
        {
            "mode": "api",
            "model": "BAAI/bge-m3",
            "base_url": "https://api.example.com",
            "max_input_tokens": 512,
        }
    )
    assert provider.profile.max_input_tokens == 512


def test_cap_above_model_limit_is_clamped_with_warning() -> None:
    """配置值超模型能力 → 钳回登记值并 warning（TASK-038 的 min 语义）。"""
    with pytest.warns(UserWarning, match="8192"):
        provider = create_provider(
            {
                "mode": "api",
                "model": "BAAI/bge-m3",
                "base_url": "https://api.example.com",
                "max_input_tokens": 99999,
            }
        )
    assert provider.profile.max_input_tokens == 8192


def test_unknown_model_falls_back_to_2048() -> None:
    provider = create_provider(
        {
            "mode": "api",
            "model": "self-hosted-xyz",
            "base_url": "https://api.example.com",
            "dim": 768,
        }
    )
    assert provider.profile.max_input_tokens == 2048


def test_invalid_mode_is_rejected() -> None:
    with pytest.raises(EmbeddingConfigError, match="mode"):
        create_provider({"mode": "onnx"})


def test_invalid_batch_size_is_rejected() -> None:
    with pytest.raises(EmbeddingConfigError, match="batch_size"):
        EmbeddingConfig(batch_size=0)


def test_from_env_reads_embed_variables() -> None:
    config = EmbeddingConfig.from_env(
        {
            "EMBED_MODE": "api",
            "EMBED_MODEL": "bge-m3",
            "EMBED_BASE_URL": "https://api.example.com/v1",
            "EMBED_API_KEY": "sk-x",
            "EMBED_DIM": "1024",
            "EMBED_MAX_INPUT_TOKENS": "4096",
            "EMBED_BATCH_SIZE": "8",
            "EMBED_BATCH_TOKEN_BUDGET": "24576",
            "EMBED_OFFLINE": "true",
        }
    )
    assert config.mode == "api"
    assert config.model == "bge-m3"
    assert config.base_url == "https://api.example.com/v1"
    assert config.api_key == "sk-x"
    assert config.dim == 1024
    assert config.max_input_tokens == 4096
    assert config.batch_size == 8
    assert config.batch_token_budget == 24576
    assert config.offline is True


def test_batch_token_budget_defaults_to_none_and_reaches_provider() -> None:
    """TASK-048：未配置时保持 provider 自带的默认（None 不得写成 0 或覆盖）。"""
    assert EmbeddingConfig.from_env(
        {"EMBED_MODE": "api", "EMBED_MODEL": "bge-m3"}
    ).batch_token_budget is None
    provider = create_provider(
        {
            "mode": "api",
            "model": "bge-m3",
            "base_url": "https://api.example.com",
            "api_key": "sk-x",
            "batch_token_budget": 24576,
            "batch_size": 128,
        }
    )
    assert provider.batch_token_budget == 24576
    assert provider.batch_size == 128


def test_batch_token_budget_rejects_non_positive() -> None:
    with pytest.raises(EmbeddingConfigError, match="batch_token_budget"):
        EmbeddingConfig.from_env({"EMBED_BATCH_TOKEN_BUDGET": "0"})


def test_from_env_defaults_to_local() -> None:
    config = EmbeddingConfig.from_env({})
    assert config.mode == "local"
    assert config.model is None
    assert config.offline is False


def test_from_env_rejects_non_integer_override() -> None:
    with pytest.raises(EmbeddingConfigError, match="EMBED_DIM"):
        EmbeddingConfig.from_env({"EMBED_DIM": "big"})


def test_preload_reports_missing_model_readably(tmp_path: Path) -> None:
    with pytest.raises(LocalModelUnavailableError, match="离线模式"):
        create_provider(
            {"offline": True, "cache_dir": str(tmp_path / "empty-cache"), "preload": True}
        )


def test_lazy_by_default_so_construction_never_downloads(tmp_path: Path) -> None:
    provider = create_provider({"offline": True, "cache_dir": str(tmp_path / "empty-cache")})
    assert isinstance(provider, LocalOnnxEmbeddingProvider)  # 构造不触发加载
    with pytest.raises(LocalModelUnavailableError):
        provider.embed(["hello"])
