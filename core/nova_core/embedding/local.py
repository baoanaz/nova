"""本地 ONNX embedding 实现（TASK-008 §B，D-44 默认路径）。

隐私语义：文本只在本机进程内处理，**不出本机**（与 api.py 相反）。

依赖：``onnxruntime``（推理）+ ``tokenizers``（分词）+ ``huggingface-hub``（模型下载/缓存）。
三者在真正加载时才 import（``ensure_loaded``），因此：
- 只跑 API 路径的进程无须付 onnxruntime 加载成本；
- 测试可注入 stub session / 微型 tokenizer，CI 不依赖网络与真实模型文件。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np

from nova_core.embedding.base import (
    EmbeddingConfigError,
    EmbeddingDimMismatchError,
    EmbeddingError,
    LocalModelUnavailableError,
    Side,
    iter_batches,
    l2_normalize,
    with_prefix,
)
from nova_core.embedding.registry import DEFAULT_LOCAL_SLUG, LocalModelSpec, get_local_spec
from nova_core.interfaces import EmbeddingProfile

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期需要
    from tokenizers import Tokenizer

#: 本地批大小（卡内 §A）。
DEFAULT_BATCH_SIZE = 16


class OnnxSessionLike(Protocol):
    """``onnxruntime.InferenceSession`` 的最小依赖面（测试可注入 stub session）。"""

    def get_inputs(self) -> Sequence[object]: ...

    def run(
        self, output_names: Sequence[str] | None, input_feed: dict[str, np.ndarray]
    ) -> Sequence[object]: ...


class LocalOnnxEmbeddingProvider:
    """本地 ONNX provider（不联网也不外发文本；首次使用才加载模型文件）。

    截断策略：按 ``spec.max_input_tokens`` 对 token 序列硬截断（超长不报错，卡内 §B）。
    归一化策略：输出 L2 单位向量（卡内 §A，冻结行为）。

    侧别语义：``embed()`` 走索引侧（passage），``embed_query()`` 走检索侧（query）。
    e5 系列两侧前缀不同，TASK-007 消费 ``embed``、TASK-010 消费 ``embed_query``。
    """

    def __init__(
        self,
        spec: LocalModelSpec | str = DEFAULT_LOCAL_SLUG,
        *,
        tokenizer: Tokenizer | None = None,
        session: OnnxSessionLike | None = None,
        model_dir: str | Path | None = None,
        cache_dir: str | Path | None = None,
        offline: bool = False,
        batch_size: int = DEFAULT_BATCH_SIZE,
        providers: Sequence[str] | None = None,
        intra_op_num_threads: int | None = None,
        input_ids_name: str = "input_ids",
        attention_mask_name: str = "attention_mask",
        token_type_ids_name: str = "token_type_ids",
    ) -> None:
        if batch_size < 1:
            raise EmbeddingConfigError(f"batch_size 必须 ≥ 1，收到 {batch_size}")
        self._spec = get_local_spec(spec) if isinstance(spec, str) else spec
        self._tokenizer = tokenizer
        self._session = session
        self._model_dir = Path(model_dir) if model_dir is not None else None
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._offline = bool(offline)
        self._batch_size = batch_size
        self._providers = list(providers) if providers is not None else None
        self._intra_op_num_threads = intra_op_num_threads
        self._input_ids_name = input_ids_name
        self._attention_mask_name = attention_mask_name
        self._token_type_ids_name = token_type_ids_name
        self._session_input_names: frozenset[str] | None = None

    # -- 只读状态 ---------------------------------------------------------

    @property
    def spec(self) -> LocalModelSpec:
        return self._spec

    @property
    def profile(self) -> EmbeddingProfile:
        """CF-09 指纹：变更即触发 D-07 二级失效（写 ``index_config``）。"""
        return EmbeddingProfile(
            model_id=self._spec.model_id,
            dim=self._spec.dim,
            max_input_tokens=self._spec.max_input_tokens,
        )

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def is_loaded(self) -> bool:
        return self._tokenizer is not None and self._session is not None

    # -- 加载 -------------------------------------------------------------

    def ensure_loaded(self) -> None:
        """加载 tokenizer 与 ONNX 会话（缺文件且不可下载时抛可读错误）。"""
        if self._tokenizer is None:
            self._tokenizer = self._load_tokenizer()
        if self._session is None:
            self._session = self._load_session()

    def _load_tokenizer(self) -> Tokenizer:
        from tokenizers import Tokenizer

        path = self._local_or_download(self._spec.tokenizer_file)
        try:
            return Tokenizer.from_file(str(path))
        except Exception as exc:
            raise LocalModelUnavailableError(
                f"tokenizer 加载失败：{path}（{exc!r}）；"
                "请确认该文件是 tokenizers 可读的 tokenizer.json"
            ) from exc

    def _load_session(self) -> OnnxSessionLike:
        path = self._local_or_download(self._spec.onnx_file)
        try:
            import onnxruntime as ort
        except Exception as exc:  # pragma: no cover - 依赖缺失时才触发
            raise LocalModelUnavailableError(
                f"无法导入 onnxruntime（{exc!r}）；本地路径不可用时可改用 api 模式（D-44）"
            ) from exc
        session_options = None
        if self._intra_op_num_threads is not None:
            session_options = ort.SessionOptions()
            session_options.intra_op_num_threads = self._intra_op_num_threads
        try:
            return ort.InferenceSession(
                str(path),
                sess_options=session_options,
                providers=self._providers,
            )
        except Exception as exc:
            raise LocalModelUnavailableError(
                f"ONNX 会话创建失败：{path}（{exc!r}）；请确认文件与 slug 匹配"
            ) from exc

    def _local_or_download(self, filename: str) -> Path:
        if self._model_dir is not None:
            candidate = self._model_dir / filename
            if candidate.is_file():
                return candidate
            raise LocalModelUnavailableError(
                f"本地模型目录缺少 {filename}：{self._model_dir}；"
                f"该目录应包含 {self._spec.repo_id} 的模型文件（可用 huggingface-hub 预取整目录）"
            )
        return self._download(filename)

    def _download(self, filename: str) -> Path:
        try:
            from huggingface_hub import hf_hub_download
        except Exception as exc:  # pragma: no cover - 依赖缺失时才触发
            raise LocalModelUnavailableError(
                f"无法导入 huggingface-hub（{exc!r}）；可用 model_dir 指认已下载的本地目录"
            ) from exc
        try:
            return Path(
                hf_hub_download(
                    repo_id=self._spec.repo_id,
                    filename=filename,
                    cache_dir=str(self._cache_dir) if self._cache_dir else None,
                    local_files_only=self._offline,
                )
            )
        except Exception as exc:
            raise LocalModelUnavailableError(self._unavailable_message(filename, exc)) from exc

    def _unavailable_message(self, filename: str, exc: Exception) -> str:
        cache = self._cache_dir or "~/.cache/huggingface"
        if self._offline:
            reason = "当前为离线模式（offline=True），缓存中不存在该文件"
        else:
            reason = "无法访问模型仓库（网络不可用或仓库不可达）"
        return (
            f"本地模型文件不可用：{self._spec.repo_id}/{filename}；{reason}。"
            f"可选出路：① 联网环境预取后通过 model_dir 指认目录（离线加载需 offline=True）；"
            f"② 预取到缓存目录 cache_dir={cache}；③ 切换 api 模式（D-44）。"
            f"原始错误：{exc!r}"
        )

    # -- 推理 -------------------------------------------------------------

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """CF-09 契约方法：按索引侧（passage）语义嵌入。"""
        return self.embed_side(texts, "passage")

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        """检索侧（query）语义嵌入；TASK-010 的向量通道调用此方法。

        不在 CF-09 的 ``EmbeddingProvider`` 协议里（协议只有 ``embed``），属于实现层
        扩展，供同一 provider 区分 e5 前缀。
        """
        return self.embed_side(texts, "query")

    def embed_side(self, texts: Sequence[str], side: Side) -> list[list[float]]:
        if side not in ("passage", "query"):  # pragma: no cover - 防御性分支
            raise EmbeddingConfigError(f"side 必须是 'passage' / 'query'，收到 {side!r}")
        items = list(texts)
        if not items:
            return []
        self.ensure_loaded()
        prefix = self._spec.query_prefix if side == "query" else self._spec.passage_prefix
        vectors: list[list[float]] = []
        for batch in iter_batches(items, self._batch_size):
            vectors.extend(self._embed_batch([with_prefix(text, prefix) for text in batch]))
        return vectors

    def _embed_batch(self, batch: Sequence[str]) -> list[list[float]]:
        assert self._tokenizer is not None and self._session is not None  # ensure_loaded 保证
        encodings = self._tokenizer.encode_batch(list(batch), add_special_tokens=True)
        max_tokens = self._spec.max_input_tokens
        rows = [self._encoding_row(enc, max_tokens) for enc in encodings]
        width = max(len(ids) for ids, _ in rows)
        input_ids = np.full((len(rows), width), self._pad_id(), dtype=np.int64)
        attention = np.zeros((len(rows), width), dtype=np.int64)
        for index, (ids, mask) in enumerate(rows):
            input_ids[index, : len(ids)] = ids
            attention[index, : len(mask)] = mask

        feeds = self._build_feeds(input_ids, attention)
        try:
            outputs = self._session.run(None, feeds)
        except Exception as exc:
            raise EmbeddingError(
                f"本地 ONNX 推理失败（model={self._spec.model_id}）：{exc!r}"
            ) from exc
        hidden = np.asarray(outputs[0])
        pooled = self._pool(hidden, attention)
        if pooled.shape[-1] != self._spec.dim:
            raise EmbeddingDimMismatchError(
                f"{self._spec.model_id} 实际输出维度 {pooled.shape[-1]} 与注册表 dim="
                f"{self._spec.dim} 不一致；请确认 onnx_file 与 slug 匹配"
                f"（或显式覆盖 dim，届时 index_config 指纹会随之变化）"
            )
        return l2_normalize(pooled).tolist()

    @staticmethod
    def _encoding_row(encoding: object, max_tokens: int) -> tuple[list[int], list[int]]:
        """取 (ids, attention_mask)，按 ``max_tokens`` 硬截断（超长不报错，卡内 §B）。

        必须沿用 tokenizer 自带的 ``attention_mask``：部分模型仓库的 tokenizer.json
        自带 padding（如 Snowflake/snowflake-arctic-embed-xs），此时 ``ids`` 尾部是 pad；
        若自行重建掩码会把 pad 当真实 token，使向量随批组合漂移（真机冒烟发现的回归）。
        """
        ids = list(getattr(encoding, "ids", []))[:max_tokens]
        raw_mask = list(getattr(encoding, "attention_mask", None) or [])[:max_tokens]
        mask = raw_mask if len(raw_mask) == len(ids) else [1] * len(ids)
        return ids, mask

    def _pad_id(self) -> int:
        assert self._tokenizer is not None
        padding = getattr(self._tokenizer, "padding", None)
        if isinstance(padding, dict) and padding.get("pad_id") is not None:
            return int(padding["pad_id"])
        for token in ("[PAD]", "<pad>", "[pad]"):
            token_id = self._tokenizer.token_to_id(token)
            if token_id is not None:
                return int(token_id)
        return 0

    def _build_feeds(self, input_ids: np.ndarray, attention: np.ndarray) -> dict[str, np.ndarray]:
        declared = self._declared_inputs()
        missing = [
            name
            for name in (self._input_ids_name, self._attention_mask_name)
            if name not in declared
        ]
        if missing:
            raise EmbeddingConfigError(
                f"ONNX 会话缺少必需输入 {missing}；会话声明的输入为 {sorted(declared)}。"
                "请用 input_ids_name / attention_mask_name 指认实际名称"
            )
        feeds = {self._input_ids_name: input_ids, self._attention_mask_name: attention}
        if self._token_type_ids_name in declared:
            feeds[self._token_type_ids_name] = np.zeros_like(input_ids)
        return feeds

    def _declared_inputs(self) -> frozenset[str]:
        assert self._session is not None
        if self._session_input_names is None:
            self._session_input_names = frozenset(
                str(getattr(item, "name", item)) for item in self._session.get_inputs()
            )
        return self._session_input_names

    def _pool(self, hidden: np.ndarray, attention: np.ndarray) -> np.ndarray:
        if hidden.ndim == 2:  # 已池化的导出（少数 ONNX 包直接输出句向量）
            return hidden.astype(np.float32)
        if hidden.ndim != 3:
            raise EmbeddingError(
                f"本地 ONNX 输出维度不可识别（ndim={hidden.ndim}，期望 2 或 3）："
                f"model={self._spec.model_id}"
            )
        if self._spec.pooling == "cls":
            return hidden[:, 0, :].astype(np.float32)
        mask = attention[:, : hidden.shape[1]].astype(np.float32)[:, :, None]
        summed = (hidden.astype(np.float32) * mask).sum(axis=1)
        counts = np.clip(mask.sum(axis=1), 1.0, None)
        return (summed / counts).astype(np.float32)
