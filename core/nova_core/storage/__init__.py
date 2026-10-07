"""索引存储层：SQLite schema（CF-01）/ FTS5 / 增量对账 / 级联删除。

对外入口：``Store``（读写）+ 行类型（SymbolRow/EdgeRow/UnresolvedRefRow/SpecRef）。
DDL 副本 ``schema.sql`` 与 ``docs/contracts/index-schema.sql`` 逐字节一致（测试强制）。
"""

from nova_core.storage.db import (
    DB_FILENAME,
    SCHEMA_VERSION,
    SchemaMismatchError,
    read_schema_sql,
)
from nova_core.storage.store import (
    EdgeRow,
    EdgeTargetUpdate,
    RefResolution,
    SpecRef,
    Store,
    SymbolRow,
    UnresolvedRefRow,
)

__all__ = [
    "DB_FILENAME",
    "SCHEMA_VERSION",
    "EdgeRow",
    "EdgeTargetUpdate",
    "RefResolution",
    "SchemaMismatchError",
    "SpecRef",
    "Store",
    "SymbolRow",
    "UnresolvedRefRow",
    "read_schema_sql",
]
