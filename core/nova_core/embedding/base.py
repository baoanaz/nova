"""Embedding 通用能力：错误分类、批处理、前缀与归一化（TASK-008）。

冻结契约：``nova_core.interfaces.EmbeddingProvider`` / ``EmbeddingProfile``（CF-09）。
本模块不依赖任何具体后端（ONNX / HTTP），由 ``local.py`` 与 ``api.py`` 共用。

不变量（卡内 §A，冻结行为）：
1. 所有 provider 返回**单位向量**（L2 归一化后返回，向量库按余弦检索）；
2. 空输入返回空列表；
3. 超长输入在 provider 内部按 ``profile.max_input_tokens`` 截断，不报错。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Literal

import numpy as np

#: embedding 输入侧：``passage`` = 索引侧正文，``query`` = 检索侧查询。
#: e5 系列两侧前缀不同（Module/01 §2.4、TASK-008 §B），因此 provider 需区分调用侧。
Side = Literal["passage", "query"]

#: 零向量保护阈值（避免归一化产生 NaN/Inf）。
_EPS = 1e-12


class EmbeddingError(RuntimeError):
    """embedding 相关错误基类；子类用 ``kind`` 提供稳定分类（日志/降级判断用）。"""

    kind = "embedding"


class EmbeddingConfigError(EmbeddingError):
    """配置非法、模型 slug 未登记等（调用方需修正配置）。"""

    kind = "config"


class LocalModelUnavailableError(EmbeddingError):
    """本地模型文件缺失且无法下载（离线/网络不可用）。"""

    kind = "local_model_unavailable"


class ApiAuthError(EmbeddingError):
    """API 鉴权失败（401/403）。"""

    kind = "api_auth"


class ApiRateLimitError(EmbeddingError):
    """API 限流（429，重试耗尽）。"""

    kind = "api_rate_limit"


class ApiNetworkError(EmbeddingError):
    """网络层失败（连接/超时/传输错误，重试耗尽）。"""

    kind = "api_network"


class ApiResponseError(EmbeddingError):
    """API 返回了不可用响应（状态码、响应体结构、行数/维度不符）。"""

    kind = "api_response"


class EmbeddingDimMismatchError(EmbeddingError):
    """实际输出维度与 ``EmbeddingProfile.dim`` 不一致（注册表与模型文件不匹配）。"""

    kind = "dim_mismatch"


def iter_batches(items: Sequence[str], batch_size: int) -> Iterator[Sequence[str]]:
    """按 ``batch_size`` 切分（本地默认 16 / API 默认 64，见卡内 §A）。"""
    if batch_size < 1:
        raise EmbeddingConfigError(f"batch_size 必须 ≥ 1，收到 {batch_size}")
    for start in range(0, len(items), batch_size):
        yield items[start : start + batch_size]


def with_prefix(text: str, prefix: str) -> str:
    """按模型前缀约定拼接输入。

    调用方传原始文本（不传已带前缀的文本）；不做去重判断，重复调用会重复拼接——这是
    刻意的：前缀属于模型契约，交由 provider 单点负责，避免各调用方各写一套判断。
    """
    return f"{prefix}{text}" if prefix else text


def l2_normalize(vectors: np.ndarray, *, eps: float = _EPS) -> np.ndarray:
    """L2 归一化（支持 1-D / 2-D 输入），返回 ``float32``。

    零向量（模长 < ``eps``）保持为零向量而非 NaN；空行同样保持为零，调用方可据此判定
    输入退化，而不是让 NaN 污染下游 LanceDB 写入。
    """
    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim == 1:
        norm = float(np.linalg.norm(arr))
        return arr if norm < eps else (arr / norm).astype(np.float32)
    if arr.ndim != 2:  # pragma: no cover - 防御性分支
        raise EmbeddingError(f"l2_normalize 只支持 1-D/2-D 输入，收到 ndim={arr.ndim}")
    norms = np.linalg.norm(arr, axis=-1, keepdims=True)
    safe = np.where(norms < eps, 1.0, norms)
    return (arr / safe).astype(np.float32)
