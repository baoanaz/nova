"""Embedding provider 工厂（TASK-008 §D）。

映射关系：
- ``mode="local"``（默认，D-44）→ ``LocalOnnxEmbeddingProvider``，模型取自
  ``registry.LOCAL_MODELS``；
- ``mode="api"`` → ``OpenAiCompatibleEmbeddingProvider``，配置 ``EMBED_BASE_URL`` /
  ``EMBED_API_KEY`` / ``EMBED_MODEL``。

本地模型文件缺失且不可下载时，错误在 ``preload=True`` 时立即抛出，否则在首次 ``embed`` 时抛出，
统一为 ``LocalModelUnavailableError``（可读、带可选出路），不会以裸 OSError/HF 异常泄漏到上层。
"""

from __future__ import annotations

import os
import time
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any

import httpx

from nova_core.embedding.api import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_CONNECT,
    DEFAULT_TIMEOUT_TOTAL,
    OpenAiCompatibleEmbeddingProvider,
)
from nova_core.embedding.base import EmbeddingConfigError
from nova_core.embedding.local import (
    DEFAULT_BATCH_SIZE as LOCAL_DEFAULT_BATCH_SIZE,
)
from nova_core.embedding.local import LocalOnnxEmbeddingProvider
from nova_core.embedding.registry import (
    API_MODEL_ALIASES,
    API_MODELS,
    DEFAULT_LOCAL_SLUG,
    ApiModelSpec,
    find_api_spec,
    get_local_spec,
    resolve_transport,
)
from nova_core.interfaces import EmbeddingProvider

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期需要
    from tokenizers import Tokenizer

    from nova_core.embedding.local import OnnxSessionLike

#: 未登记 API 模型的默认截断值（Module/01 §2.4 的 nova 侧 2048 假设）。
UNKNOWN_API_MAX_INPUT_TOKENS = 2048


def _clamp_to_model_limit(spec: ApiModelSpec, configured: int | None) -> int:
    """生效的 ``max_input_tokens``：用户值只降不升（``min`` 语义，TASK-046 §B）。

    - 未给 → 用**已登记模型的登记值**（bge-m3 = 8192，即模型实际能力）；
    - 给了更小值 → 尊重用户（更省 token / 更快）；
    - 给了**大于模型能力**的值 → 钳回登记值并发 ``UserWarning``：超过该值的请求会被
      provider 拒绝（实测 8193 即 `400 code=20015`），静默抬高只会让索引在深处失败。

    这是 TASK-038（本地 clamping）的同一语义在 API 侧的对应实现；本卡不改 ``local.py``。
    """
    if configured is None:
        return spec.max_input_tokens
    if configured > spec.max_input_tokens:
        warnings.warn(
            f"max_input_tokens={configured} 超过 {spec.model_id} 的实际上限 "
            f"{spec.max_input_tokens}，已钳制为 {spec.max_input_tokens}"
            f"（超出部分会被 provider 拒绝）",
            UserWarning,
            stacklevel=3,
        )
        return spec.max_input_tokens
    return configured


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    """embedding 工厂配置（默认值即 D-44 的默认路径：本地 ONNX，源码不出本机）。

    环境变量（``EmbeddingConfig.from_env``）：``EMBED_MODE`` / ``EMBED_MODEL`` /
    ``EMBED_BASE_URL`` / ``EMBED_API_KEY`` / ``EMBED_CACHE_DIR`` / ``EMBED_MODEL_DIR`` /
    ``EMBED_DIM`` / ``EMBED_MAX_INPUT_TOKENS`` / ``EMBED_BATCH_SIZE`` / ``EMBED_OFFLINE``。
    """

    mode: str = "local"
    model: str | None = None
    dim: int | None = None
    max_input_tokens: int | None = None
    batch_size: int | None = None
    #: API 侧的**累计 token 预算**（TASK-046 §D 引入该参数；本字段是 TASK-048 新增的配置入口）。
    #: 只有 API 模式消费；本地 ONNX 按 tokenizer 自行截断，与预算无关。
    batch_token_budget: int | None = None
    #: （TASK-049）并发批数。``None`` → 按模型/厂商推荐值，最终回落 1（串行）。
    #: **默认 1 是有意的**：免费档限流脆弱，并发应由用户显式开启。
    concurrency: int | None = None
    #: （TASK-049）显式指定厂商（``siliconflow`` / ``voyage`` / ``openai`` / ``generic``）。
    #: ``None`` 时按模型条目或 ``base_url`` 推断。
    provider: str | None = None
    cache_dir: str | None = None
    model_dir: str | None = None
    offline: bool = False
    base_url: str | None = None
    api_key: str | None = None
    timeout_connect: float = DEFAULT_TIMEOUT_CONNECT
    timeout_total: float = DEFAULT_TIMEOUT_TOTAL
    max_retries: int = DEFAULT_MAX_RETRIES
    preload: bool = False

    def __post_init__(self) -> None:
        if self.mode not in ("local", "api"):
            raise EmbeddingConfigError(f"mode 必须是 'local' 或 'api'，收到 {self.mode!r}")
        for name in ("dim", "max_input_tokens", "batch_size", "batch_token_budget", "concurrency"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise EmbeddingConfigError(f"{name} 必须 ≥ 1，收到 {value}")
        if self.max_retries < 0:
            raise EmbeddingConfigError(f"max_retries 不能为负，收到 {self.max_retries}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: Any) -> EmbeddingConfig:
        """从环境变量构造配置；``overrides`` 覆盖 env（显式参数优先）。"""
        source: Mapping[str, str] = os.environ if env is None else env
        data: dict[str, Any] = {
            "mode": source.get("EMBED_MODE", "local"),
            "model": source.get("EMBED_MODEL") or None,
            "base_url": source.get("EMBED_BASE_URL") or None,
            "api_key": source.get("EMBED_API_KEY") or None,
            "cache_dir": source.get("EMBED_CACHE_DIR") or None,
            "model_dir": source.get("EMBED_MODEL_DIR") or None,
        }
        for name, raw_key in (
            ("dim", "EMBED_DIM"),
            ("max_input_tokens", "EMBED_MAX_INPUT_TOKENS"),
            ("batch_size", "EMBED_BATCH_SIZE"),
            ("batch_token_budget", "EMBED_BATCH_TOKEN_BUDGET"),
            ("concurrency", "EMBED_CONCURRENCY"),
        ):
            raw = source.get(raw_key)
            if raw:
                data[name] = _as_int(raw, raw_key)
        provider = source.get("EMBED_PROVIDER")
        if provider:
            data["provider"] = provider.strip() or None
        offline = source.get("EMBED_OFFLINE")
        if offline:
            data["offline"] = offline.strip().lower() in ("1", "true", "yes", "on")
        data.update(overrides)
        return cls(**data)


def _as_int(raw: str, name: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise EmbeddingConfigError(f"环境变量 {name} 必须是整数，收到 {raw!r}") from None


def _coerce_config(config: EmbeddingConfig | Mapping[str, Any] | None) -> EmbeddingConfig:
    if config is None:
        return EmbeddingConfig.from_env()
    if isinstance(config, EmbeddingConfig):
        return config
    if isinstance(config, Mapping):
        known = {field.name for field in fields(EmbeddingConfig)}
        unknown = sorted(set(config) - known)
        if unknown:
            raise EmbeddingConfigError(
                f"EmbeddingConfig 收到未知字段 {unknown}；可用字段：{sorted(known)}"
            )
        return EmbeddingConfig(**dict(config))
    raise EmbeddingConfigError(
        f"config 必须是 EmbeddingConfig / Mapping / None，收到 {type(config)}"
    )


def create_provider(
    config: EmbeddingConfig | Mapping[str, Any] | None = None,
    *,
    tokenizer: Tokenizer | None = None,
    session: OnnxSessionLike | None = None,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
) -> EmbeddingProvider:
    """构造 embedding provider（默认本地 ONNX）。

    ``tokenizer`` / ``session`` / ``client`` / ``sleep`` 为测试与离线预加载入口
    （注入后不触碰网络与模型文件）。
    """
    resolved = _coerce_config(config)
    if resolved.mode == "api":
        return _create_api(resolved, client=client, sleep=sleep)
    return _create_local(resolved, tokenizer=tokenizer, session=session)


def _create_local(
    config: EmbeddingConfig,
    *,
    tokenizer: Tokenizer | None,
    session: OnnxSessionLike | None,
) -> LocalOnnxEmbeddingProvider:
    slug = config.model or DEFAULT_LOCAL_SLUG
    spec = get_local_spec(slug)
    spec = replace(
        spec,
        dim=config.dim or spec.dim,
        max_input_tokens=config.max_input_tokens or spec.max_input_tokens,
    )
    provider = LocalOnnxEmbeddingProvider(
        spec,
        tokenizer=tokenizer,
        session=session,
        model_dir=config.model_dir,
        cache_dir=config.cache_dir,
        offline=config.offline,
        batch_size=config.batch_size or LOCAL_DEFAULT_BATCH_SIZE,
    )
    if config.preload:
        # 缺文件且无网时在此抛 LocalModelUnavailableError（启动期可见，而不是索引中途失败）。
        provider.ensure_loaded()
    return provider


def _create_api(
    config: EmbeddingConfig,
    *,
    client: httpx.Client | None,
    sleep: Callable[[float], None] | None,
) -> OpenAiCompatibleEmbeddingProvider:
    if not config.model:
        raise EmbeddingConfigError("api 模式必须指定 model（EMBED_MODEL，例如 bge-m3）")
    if not config.base_url:
        raise EmbeddingConfigError(
            "api 模式必须指定 base_url"
            "（EMBED_BASE_URL，如 https://api.openai.com 或自建 vLLM 地址）"
        )
    spec = find_api_spec(config.model)
    if spec is None:
        if config.dim is None:
            raise EmbeddingConfigError(
                f"未登记的 API 模型 {config.model!r}：必须显式提供 dim（EMBED_DIM），"
                f"否则 profile.dim 会失真并破坏 D-07 指纹；已登记模型：{sorted(API_MODELS)}；"
                f"已登记别名：{sorted(API_MODEL_ALIASES)}"
            )
        spec = ApiModelSpec(
            name=config.model,
            dim=config.dim,
            max_input_tokens=config.max_input_tokens or UNKNOWN_API_MAX_INPUT_TOKENS,
            transport=config.provider,
            notes="运行时显式配置（未登记模型）",
        )
    else:
        spec = replace(
            spec,
            dim=config.dim or spec.dim,
            max_input_tokens=_clamp_to_model_limit(spec, config.max_input_tokens),
        )
    # TASK-049 §3.2：批参数三级回落 env > 模型 > 厂商 > 全局默认。
    transport = resolve_transport(spec, base_url=config.base_url, provider=config.provider)
    batch_size = config.batch_size or spec.batch_size or transport.safe_batch_items
    budget = config.batch_token_budget or spec.batch_token_budget
    return OpenAiCompatibleEmbeddingProvider(
        spec,
        base_url=config.base_url,
        api_key=config.api_key,
        client=client,
        batch_size=batch_size,
        **(
            {"batch_token_budget": budget}
            if budget is not None
            else {"batch_token_budget": transport.max_batch_tokens}
        ),
        transport=transport,
        concurrency=config.concurrency or spec.concurrency or 1,
        max_retries=config.max_retries,
        sleep=sleep or time.sleep,
        timeout_total=config.timeout_total,
        timeout_connect=config.timeout_connect,
    )


__all__ = [
    "UNKNOWN_API_MAX_INPUT_TOKENS",
    "EmbeddingConfig",
    "EmbeddingProvider",
    "create_provider",
]
