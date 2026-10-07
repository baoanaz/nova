"""nova_core.embedding：``EmbeddingProvider`` 双实现（TASK-008 / D-44）与模型注册表。

对外入口：
- ``create_provider(config)``：工厂（默认本地 ONNX；``mode="api"`` 时走 OpenAI-compatible API）；
- ``LocalOnnxEmbeddingProvider`` / ``OpenAiCompatibleEmbeddingProvider``：两个实现；
- ``LOCAL_MODELS`` / ``API_MODELS``：候选模型注册表（TASK-015 bake-off 的输入）；
- 错误类型见 ``base.py``（便于上层做降级判断）。

隐私边界：本地实现不出本机；API 实现会把文本发往第三方（见 ``api.py`` docstring）。
"""

from __future__ import annotations

from nova_core.embedding.api import OpenAiCompatibleEmbeddingProvider
from nova_core.embedding.base import (
    ApiAuthError,
    ApiNetworkError,
    ApiRateLimitError,
    ApiResponseError,
    EmbeddingConfigError,
    EmbeddingDimMismatchError,
    EmbeddingError,
    LocalModelUnavailableError,
    Side,
    l2_normalize,
)
from nova_core.embedding.factory import EmbeddingConfig, create_provider
from nova_core.embedding.local import LocalOnnxEmbeddingProvider
from nova_core.embedding.registry import (
    API_MODELS,
    DEFAULT_LOCAL_SLUG,
    LOCAL_MODELS,
    ApiModelSpec,
    LocalModelSpec,
    find_api_spec,
    get_local_spec,
)

__all__ = [
    "API_MODELS",
    "DEFAULT_LOCAL_SLUG",
    "LOCAL_MODELS",
    "ApiAuthError",
    "ApiModelSpec",
    "ApiNetworkError",
    "ApiRateLimitError",
    "ApiResponseError",
    "EmbeddingConfig",
    "EmbeddingConfigError",
    "EmbeddingDimMismatchError",
    "EmbeddingError",
    "LocalModelSpec",
    "LocalModelUnavailableError",
    "LocalOnnxEmbeddingProvider",
    "OpenAiCompatibleEmbeddingProvider",
    "Side",
    "create_provider",
    "find_api_spec",
    "get_local_spec",
    "l2_normalize",
]
