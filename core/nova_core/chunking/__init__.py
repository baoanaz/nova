"""chunking：Chunk 切分 + unresolved 两阶段解析 + 三级配置指纹（TASK-006）。

子模块分工：

- ``splitter``：``ParsedFile`` + 原文 → ``ChunkDef``（chunk_id / 三通道输入 / 类骨架 / 兜底切分）；
- ``resolver``：pending → resolved/failed 生命周期、裸名边 fqn 化、spec_references 匹配写库；
- ``fingerprint``：``parser_config_hash`` / ``embedding_profile`` 计算、检测（D-07）与写入。

上游原语（TASK-001 ``Store`` 与 TASK-002 ``split_fallback``）一律复用，不在本包重写。
"""

from nova_core.chunking.fingerprint import (
    EMBEDDING_DIM_KEY,
    EMBEDDING_MODEL_KEY,
    PARSER_CONFIG_KEY,
    IndexFingerprint,
    Invalidation,
    check_fingerprint,
    compute_parser_config_hash,
    stored_fingerprint,
    write_fingerprint,
)
from nova_core.chunking.resolver import (
    AmbiguousRef,
    ResolveReport,
    link_spec_references,
    resolve_edges,
    resolve_graph,
    resolve_pending,
    retry_failed,
)
from nova_core.chunking.splitter import (
    CLASS_SKELETON_KIND,
    EMBEDDING_BODY_MAX_CHARS,
    FALLBACK_KIND,
    ID_DISAMBIGUATION_SEP,
    MODULE_FQN,
    SPEC_BLOCK_KIND,
    chunk_id,
    embedding_text,
    spec_block_id,
    split_file,
)

__all__ = [
    "CLASS_SKELETON_KIND",
    "EMBEDDING_BODY_MAX_CHARS",
    "EMBEDDING_DIM_KEY",
    "EMBEDDING_MODEL_KEY",
    "FALLBACK_KIND",
    "ID_DISAMBIGUATION_SEP",
    "MODULE_FQN",
    "PARSER_CONFIG_KEY",
    "SPEC_BLOCK_KIND",
    "AmbiguousRef",
    "IndexFingerprint",
    "Invalidation",
    "ResolveReport",
    "check_fingerprint",
    "chunk_id",
    "compute_parser_config_hash",
    "embedding_text",
    "link_spec_references",
    "resolve_edges",
    "resolve_graph",
    "resolve_pending",
    "retry_failed",
    "spec_block_id",
    "split_file",
    "stored_fingerprint",
    "write_fingerprint",
]
