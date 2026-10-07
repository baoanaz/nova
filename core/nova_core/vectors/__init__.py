"""向量存储（TASK-009）：per-project LanceDB 表 ``chunk_vectors`` 封装。

对外主入口：``VectorStore``（open / upsert / delete / search / get_hashes / rebuild / close）。
"""

from nova_core.vectors.store import (
    CHUNK_ID_COLUMN,
    CONTENT_HASH_COLUMN,
    TABLE_NAME,
    VECTOR_COLUMN,
    VECTORS_DIRNAME,
    DimensionMismatchError,
    VectorStore,
    VectorStoreError,
)

__all__ = [
    "CHUNK_ID_COLUMN",
    "CONTENT_HASH_COLUMN",
    "TABLE_NAME",
    "VECTOR_COLUMN",
    "VECTORS_DIRNAME",
    "DimensionMismatchError",
    "VectorStore",
    "VectorStoreError",
]
