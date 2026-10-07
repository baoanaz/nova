"""三级配置指纹：计算 / 检测 / 写入（TASK-006，决策 D-07）。

设计依据：``docs/design/Module/01-切片存储.md`` §4.2（配置指纹与分层失效）；
契约：``index_config`` 表（CF-01）+ ``EmbeddingProfile``（CF-09）。

口径：

- 一级 ``parser_config_hash``：**解析器与切分规则**的指纹——解析器注册表（扩展名归属 + 模块/类名）、
  切片规则版本、兜底块上限、schema 版本等；变化 → ``full_reparse``（重解析 + 重切 + 重嵌）。
- 二级 ``embedding_model`` + ``embedding_dim``：embedding 指纹，对外可读形态为
  ``{model_id}@{dim}``（D-07 / R3）；变化 → ``reembed``（AST/Symbol/Edge 原样，只重算向量）。
- 两者都没变 → ``none``（走 TASK-001/007 的常规增量）。
- 优先级：``full_reparse`` > ``reembed`` > ``none``（解析层重建必然包含重嵌）。
- ``index_config`` 为空且库里没有任何文件（首建）→ ``none``：没有可失效的既有索引。
  库里有文件但指纹缺失（异常中断）→ 保守判 ``full_reparse``（不假设索引可信）。

执行的副作用（重解析 / 重嵌）归 TASK-007（``Indexer``）；本模块只做计算与判定。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from nova_core.chunking.splitter import (
    CLASS_SKELETON_KIND,
    FALLBACK_KIND,
    MODULE_FQN,
    SPEC_BLOCK_KIND,
)
from nova_core.interfaces import EmbeddingProfile
from nova_core.parsing.fallback import FALLBACK_MAX_LINES
from nova_core.parsing.registry import EXTENSION_LANGUAGE, PARSER_ENTRIES
from nova_core.storage import Store
from nova_core.storage.db import SCHEMA_VERSION

__all__ = [
    "EMBEDDING_DIM_KEY",
    "EMBEDDING_MODEL_KEY",
    "PARSER_CONFIG_KEY",
    "PARSER_CONFIG_VERSION",
    "IndexFingerprint",
    "Invalidation",
    "check_fingerprint",
    "compute_parser_config_hash",
    "default_parser_config",
    "stored_fingerprint",
    "write_fingerprint",
]

PARSER_CONFIG_KEY = "parser_config_hash"
EMBEDDING_MODEL_KEY = "embedding_model"
EMBEDDING_DIM_KEY = "embedding_dim"

#: 解析/切片规则版本：改解析口径或切分逻辑（含伪 fqn、骨架规则）时必须 +1。
PARSER_CONFIG_VERSION = 2


class Invalidation(StrEnum):
    """配置指纹比对结果（D-07 分层失效）。"""

    NONE = "none"
    REEMBED = "reembed"
    FULL_REPARSE = "full_reparse"


@dataclass(frozen=True, slots=True)
class IndexFingerprint:
    """一个索引状态的三级指纹（parser 层 + embedding 层）。"""

    parser_config_hash: str
    embedding_model: str
    embedding_dim: int

    @property
    def embedding_profile(self) -> str:
        """``{model_id}@{dim}``（D-07 / R3 的可读形态，用于日志与报告）。"""
        return f"{self.embedding_model}@{self.embedding_dim}"

    @classmethod
    def build(
        cls,
        embedding: EmbeddingProfile,
        *,
        parser_overrides: Mapping[str, Any] | None = None,
    ) -> IndexFingerprint:
        return cls(
            parser_config_hash=compute_parser_config_hash(parser_overrides),
            embedding_model=embedding.model_id,
            embedding_dim=embedding.dim,
        )


def default_parser_config() -> dict[str, Any]:
    """当前解析器/切片规则的可比较快照（进 sha256 的规范化 JSON）。"""
    return {
        "version": PARSER_CONFIG_VERSION,
        "schema_version": SCHEMA_VERSION,
        "extensions": dict(sorted(EXTENSION_LANGUAGE.items())),
        "parsers": {lang: list(entry) for lang, entry in sorted(PARSER_ENTRIES.items())},
        "chunking": {
            "fallback_max_lines": FALLBACK_MAX_LINES,
            "module_fqn": MODULE_FQN,
            "spec_block_kind": SPEC_BLOCK_KIND,
            "fallback_kind": FALLBACK_KIND,
            "class_skeleton_kind": CLASS_SKELETON_KIND,
        },
    }


def compute_parser_config_hash(overrides: Mapping[str, Any] | None = None) -> str:
    """解析器/切片规则字典 → sha256（键排序的紧凑 JSON；``overrides`` 顶层浅合并）。"""
    config = default_parser_config()
    if overrides:
        config.update(overrides)
    payload = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stored_fingerprint(store: Store) -> IndexFingerprint | None:
    """读出库内已写入的指纹；任一键缺失（或 dim 非整数）→ None。"""
    parser_hash = store.get_config(PARSER_CONFIG_KEY)
    model = store.get_config(EMBEDDING_MODEL_KEY)
    dim = store.get_config(EMBEDDING_DIM_KEY)
    if parser_hash is None or model is None or dim is None:
        return None
    try:
        embedding_dim = int(dim)
    except ValueError:
        return None
    return IndexFingerprint(
        parser_config_hash=parser_hash, embedding_model=model, embedding_dim=embedding_dim
    )


def check_fingerprint(store: Store, current: IndexFingerprint) -> Invalidation:
    """比对库内指纹与当前配置，返回需要执行的失效层级（D-07）。"""
    stored = stored_fingerprint(store)
    if stored is None and store.counts()["files"] == 0:
        return Invalidation.NONE  # 空库首建：没有既有索引可失效
    if stored is None or stored.parser_config_hash != current.parser_config_hash:
        return Invalidation.FULL_REPARSE
    if (
        stored.embedding_model != current.embedding_model
        or stored.embedding_dim != current.embedding_dim
    ):
        return Invalidation.REEMBED
    return Invalidation.NONE


def write_fingerprint(store: Store, current: IndexFingerprint) -> None:
    """把当前指纹写入 ``index_config``（首建或失效重建完成后调用）。"""
    store.set_config(PARSER_CONFIG_KEY, current.parser_config_hash)
    store.set_config(EMBEDDING_MODEL_KEY, current.embedding_model)
    store.set_config(EMBEDDING_DIM_KEY, str(current.embedding_dim))
