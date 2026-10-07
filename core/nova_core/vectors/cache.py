"""跨项目 embedding 缓存（TASK-111）：``{data_root}/cache/embeddings/<model>-<dim>/``。

**为什么需要它**：TASK-111 让分支参与项目身份后，同一仓库的每个分支/每个 worktree 都成了
独立项目——污染与串分支消失了，但**同一份文件的向量会被每个分支各嵌一次**。实测：

```text
main worktree : new=5913  vectors upserted=5913
lane-c        : new=5842  vectors upserted=5842   ← 两者 55%–98% 内容相同，却零复用
```

这与"最小消耗 embedding 额度"直接冲突。本模块是 **data_root 级的按内容寻址缓存**：
内容（``chunk.content_hash``，与路径/行号无关）与模型指纹决定向量，因此**跨分支、跨项目、
跨 worktree 可命中同一行**。

**它为什么不是对 D-03 的违反**：D-03 约束的是**权威存储**（每个项目自己的
``vectors/`` 目录，检索照旧只读它）。本模块是**可丢弃的缓存**——清掉它只损失一次
重新嵌入，不影响任何索引的正确性，与 `qcache` 的查询向量侧车缓存同类。

缓存目录按 ``<model_id>-<dim>`` 分片，因此天然不会把不同模型/维度的向量混用
（换模型 = 换目录，旧目录可安全删除）。

``lancedb`` 与 ``vectors.store`` 一样走惰性导入（P1-5），模块顶层零重依赖。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa

from nova_core.vectors._lancedb import load as _load_lancedb

CACHE_DIRNAME = "cache"
EMBEDDINGS_DIRNAME = "embeddings"
TABLE_NAME = "embedding_cache"
CACHE_KEY_COLUMN = "cache_key"
VECTOR_COLUMN = "vector"

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class EmbeddingCacheError(RuntimeError):
    """缓存读写失败（调用方应降级为"当作未命中"，不得让索引失败）。"""


def cache_key(model_id: str, content_hash: str) -> str:
    """``(模型, 内容)`` → 主键：同一内容在同一模型下只有一行。"""
    return f"{model_id}\x00{content_hash}"


def _schema(dim: int) -> pa.Schema:
    return pa.schema(
        [
            pa.field(CACHE_KEY_COLUMN, pa.string()),
            pa.field(VECTOR_COLUMN, pa.list_(pa.float32(), dim)),
        ]
    )


def _shard_name(model_id: str, dim: int) -> str:
    safe = _SAFE_NAME_RE.sub("_", model_id).strip("_") or "model"
    return f"{safe}-{dim}"


class EmbeddingCache:
    """按 ``(model_id, content_hash)`` 存向量的可丢弃缓存。

    用法::

        with EmbeddingCache.open(data_root, model_id, dim) as cache:
            hit = cache.lookup([content_hash])          # 未命中不返回键
            cache.put({content_hash: vector})
    """

    def __init__(self, db: object, table: object, dim: int, directory: Path) -> None:
        self._db = db
        self._table = table
        self._dim = dim
        self._directory = directory

    @classmethod
    def open(cls, data_root: str | Path, model_id: str, dim: int) -> EmbeddingCache:
        """打开/创建缓存；目录按 ``<model_id>-<dim>`` 分片（换模型即换目录）。"""
        if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
            raise ValueError(f"dim 必须为正整数，收到 {dim!r}")
        directory = (
            Path(data_root).expanduser()
            / CACHE_DIRNAME
            / EMBEDDINGS_DIRNAME
            / _shard_name(model_id, dim)
        )
        directory.mkdir(parents=True, exist_ok=True)
        db = _load_lancedb().connect(str(directory))
        names = {
            name for name in _list_tables(db)
        }
        if TABLE_NAME in names:
            table = db.open_table(TABLE_NAME)
        else:
            table = db.create_table(TABLE_NAME, schema=_schema(dim))
        return cls(db=db, table=table, dim=dim, directory=directory)

    def close(self) -> None:
        self._table = None
        self._db = None

    def __enter__(self) -> EmbeddingCache:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def directory(self) -> Path:
        return self._directory

    def lookup(
        self, model_id: str, content_hashes: Sequence[str]
    ) -> dict[str, list[float]]:
        """按内容反查向量：``content_hash -> vector``（未命中不返回键）。

        任何底层异常都转成 :class:`EmbeddingCacheError`——调用方**必须**当作未命中降级，
        缓存坏掉不能让索引失败（它是优化，不是正确性来源）。
        """
        if self._table is None:
            raise EmbeddingCacheError("EmbeddingCache 已关闭")
        unique = list(dict.fromkeys(content_hashes))
        if not unique:
            return {}
        keys = [cache_key(model_id, digest) for digest in unique]
        found: dict[str, list[float]] = {}
        try:
            for start in range(0, len(keys), 512):
                chunk = keys[start : start + 512]
                rows = (
                    self._table.search(None)
                    .where(f"{CACHE_KEY_COLUMN} IN ({_literals(chunk)})")
                    .select([CACHE_KEY_COLUMN, VECTOR_COLUMN])
                    .to_list()
                )
                for row in rows:
                    key = row[CACHE_KEY_COLUMN]
                    digest = key.split("\x00", 1)[-1]
                    found[digest] = [float(value) for value in row[VECTOR_COLUMN]]
        except Exception as exc:  # noqa: BLE001 - 缓存失败一律降级
            raise EmbeddingCacheError(f"读取 embedding 缓存失败：{exc}") from exc
        return found

    def put(self, model_id: str, vectors: dict[str, Sequence[float]]) -> int:
        """写入 ``content_hash -> vector``；返回写入行数。

        异常降级为 :class:`EmbeddingCacheError`（调用方按"写失败不影响索引"处理）。
        """
        if self._table is None:
            raise EmbeddingCacheError("EmbeddingCache 已关闭")
        if not vectors:
            return 0
        payload = []
        for digest, vector in vectors.items():
            if len(vector) != self._dim:
                raise EmbeddingCacheError(
                    f"向量维度不匹配：缓存 dim={self._dim}，收到 {len(vector)}"
                )
            payload.append(
                {
                    CACHE_KEY_COLUMN: cache_key(model_id, digest),
                    VECTOR_COLUMN: [float(value) for value in vector],
                }
            )
        try:
            for start in range(0, len(payload), 512):
                batch = payload[start : start + 512]
                (
                    self._table.merge_insert(CACHE_KEY_COLUMN)
                    .when_matched_update_all()
                    .when_not_matched_insert_all()
                    .execute(batch)
                )
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingCacheError(f"写入 embedding 缓存失败：{exc}") from exc
        return len(payload)

    def count(self) -> int:
        if self._table is None:
            raise EmbeddingCacheError("EmbeddingCache 已关闭")
        return int(self._table.count_rows())


def _literals(values: Sequence[str]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def _list_tables(db: object) -> list[str]:
    names: list[str] = []
    page_token: str | None = None
    while True:
        response = db.list_tables(page_token=page_token)
        names.extend(response.tables)
        if not response.page_token or response.page_token == page_token:
            return names
        page_token = response.page_token
