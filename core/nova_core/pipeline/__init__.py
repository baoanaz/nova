"""pipeline：索引流水线（TASK-007）。

子模块分工：

- ``indexer``：``Indexer``（ChangeSet → 增量失效 → 向量对账）+ ``IngestReport``；
- ``source``：``SourceProvider`` 协议与目录实现（全量重解析 / 存量向量重建的输入）；
- ``ignore``：四层忽略规则（第 0 层白名单强制包含 + ``.novaignore`` > ``.gitignore`` > 内置）
  与大小/二进制阈值（TASK-037 / TASK-097 / D-28 / R42-R43）。
"""

from nova_core.pipeline.ignore import (
    ALLOWLIST_SCOPES,
    DEFAULT_ALLOWLIST,
    DEFAULT_ALLOWLIST_DIRS,
    DEFAULT_ALLOWLIST_FILENAMES,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_SKIP_DIR_PATTERNS,
    DEFAULT_SKIP_DIRS,
    Allowlist,
    IgnoreRules,
    IndexScope,
)
from nova_core.pipeline.indexer import (
    CPP_EXTENSIONS,
    H_EXTENSION,
    LANGUAGES_KEY,
    Indexer,
    IngestReport,
)
from nova_core.pipeline.source import (
    DirectorySource,
    SourcePathError,
    SourceProvider,
)

__all__ = [
    "ALLOWLIST_SCOPES",
    "CPP_EXTENSIONS",
    "DEFAULT_ALLOWLIST",
    "DEFAULT_ALLOWLIST_DIRS",
    "DEFAULT_ALLOWLIST_FILENAMES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_SKIP_DIRS",
    "DEFAULT_SKIP_DIR_PATTERNS",
    "Allowlist",
    "DirectorySource",
    "H_EXTENSION",
    "IgnoreRules",
    "IndexScope",
    "Indexer",
    "IngestReport",
    "LANGUAGES_KEY",
    "SourcePathError",
    "SourceProvider",
]
