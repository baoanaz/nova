"""向量存储（TASK-009）：per-project LanceDB 表 ``chunk_vectors``。

设计依据：``docs/design/Module/01-切片存储.md`` §3.1（LanceDB 选型）、
§3.3（vectors/ 目录 + 表设计）、§4.1（content_hash 增量复用）。
冻结契约：``zace_core.types.{VectorRow,VectorHit}``；维度来源为
``zace_core.interfaces.EmbeddingProfile.dim``（TASK-008）。

实现口径：
- 表名固定 ``chunk_vectors``，列 ``chunk_id / content_hash / vector``，vector 为固定维度
  ``fixed_size_list<float32, dim>``（维度在表创建时定型，与 Module/01 §3.3 一致）；
- 幂等写入用 LanceDB ``merge_insert(chunk_id)``（同 id 覆盖，不产生重复行）；
- 检索用余弦距离，对外返回余弦相似度（``1 - _distance``，越大越相关，按分降序）；
- ``rebuild(dim)`` 用单次 ``create_table(mode="overwrite")`` 原子替换整表（LanceDB OSS 不支持
  ``rename_table``，这是可用的最简原子语义；旧句柄仍可读旧快照，替换后本实例立即切到新表）；
- 维度不匹配（打开已有表 / 写入 / 查询）一律抛 ``DimensionMismatchError``，文案给出
  "触发 D-07 二级失效" 的处置指引。
- **惰性依赖（P1-5）**：``lancedb`` 不在模块顶层导入（其导入链 2.6–3.2s），
  统一走 :mod:`zace_core.vectors._lancedb`；本模块 import 本身零重依赖。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import pyarrow as pa

from zace_core.types import VectorHit, VectorRow
from zace_core.vectors._lancedb import load as _load_lancedb

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期需要（运行期走 _load_lancedb 惰性导入）
    import lancedb

VECTORS_DIRNAME = "vectors"
TABLE_NAME = "chunk_vectors"
CHUNK_ID_COLUMN = "chunk_id"
CONTENT_HASH_COLUMN = "content_hash"
VECTOR_COLUMN = "vector"

_UPSERT_BATCH_SIZE = 1024

_T = TypeVar("_T")

_D07_HINT = (
    "该不一致意味着 embedding 模型/维度已变更，将触发 D-07 二级失效："
    "对项目执行整表重建（VectorStore.rebuild(dim)）并按新模型重新嵌入（TASK-007 增量对账）。"
)


class VectorStoreError(RuntimeError):
    """向量存储操作失败。"""


class DimensionMismatchError(VectorStoreError):
    """向量维度不一致（触发 D-07 二级失效的显式信号）。"""


def _dim_mismatch_message(expected: int, actual: int) -> str:
    return f"向量维度不匹配：期望 dim={expected}，实际 dim={actual}。{_D07_HINT}"


def _vector_schema(dim: int) -> pa.Schema:
    return pa.schema(
        [
            pa.field(CHUNK_ID_COLUMN, pa.string()),
            pa.field(CONTENT_HASH_COLUMN, pa.string()),
            pa.field(VECTOR_COLUMN, pa.list_(pa.float32(), dim)),
        ]
    )


def _schema_vector_dim(schema: pa.Schema) -> int | None:
    """读取表的向量列维度；不是固定维度 float32 向量列时返回 None。"""
    if VECTOR_COLUMN not in schema.names:
        return None
    field_type = schema.field(VECTOR_COLUMN).type
    if pa.types.is_fixed_size_list(field_type) and pa.types.is_float32(field_type.value_type):
        return field_type.list_size
    return None


def _list_table_names(db: lancedb.DBConnection) -> set[str]:
    """列出库内全部表名（``list_tables`` 分页安全）。"""
    names: set[str] = set()
    page_token: str | None = None
    while True:
        response = db.list_tables(page_token=page_token)
        names.update(response.tables)
        if not response.page_token or response.page_token == page_token:
            return names
        page_token = response.page_token


def _sql_literal(value: str) -> str:
    """SQL 字符串字面量（DataFusion 风格：单引号内的单引号翻倍转义）。"""
    return "'" + value.replace("'", "''") + "'"


def _in_clause(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(_sql_literal(value) for value in values)})"


class VectorStore:
    """per-project 向量表封装。

    典型用法::

        with VectorStore.open(project_dir, dim=384) as store:
            store.upsert(rows)
            hits = store.search(query_vector, top_k=20)
    """

    def __init__(
        self, db: lancedb.DBConnection, table: object, dim: int, project_dir: Path
    ) -> None:
        self._db = db
        self._table = table
        self._dim = dim
        self._project_dir = project_dir
        self._lock = threading.RLock()

    # -- 生命周期 ---------------------------------------------------------

    @classmethod
    def open(cls, project_dir: str | Path, dim: int) -> VectorStore:
        """打开/创建 ``{project_dir}/vectors/`` 下的 LanceDB 库与 ``chunk_vectors`` 表。

        表不存在则按 ``dim`` 建表；已存在但维度不同则抛 ``DimensionMismatchError``
        （调用方应走 ``rebuild(dim)``，即 D-07 二级失效）。
        """
        if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
            raise ValueError(f"dim 必须为正整数，收到 {dim!r}")
        directory = Path(project_dir)
        vectors_dir = directory / VECTORS_DIRNAME
        vectors_dir.mkdir(parents=True, exist_ok=True)
        db = _load_lancedb().connect(str(vectors_dir))
        table_names = _list_table_names(db)
        if TABLE_NAME in table_names:
            table = db.open_table(TABLE_NAME)
            actual = _schema_vector_dim(table.schema)
            if actual is None:
                raise VectorStoreError(
                    f"表 {TABLE_NAME} 的 {VECTOR_COLUMN!r} 列不是 fixed_size_list<float32>，"
                    f"实际 schema：{table.schema}"
                )
            if actual != dim:
                raise DimensionMismatchError(_dim_mismatch_message(dim, actual))
        else:
            table = db.create_table(TABLE_NAME, schema=_vector_schema(dim))
        return cls(db=db, table=table, dim=dim, project_dir=directory)

    def close(self) -> None:
        """释放表/库句柄；关闭后再调用任何读写方法都会抛 ``VectorStoreError``。"""
        with self._lock:
            self._table = None
            self._db = None

    def __enter__(self) -> VectorStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def project_dir(self) -> Path:
        return self._project_dir

    def _ensure_open(self) -> None:
        if self._table is None or self._db is None:
            raise VectorStoreError("VectorStore 已关闭；请重新 open() 后再操作。")

    # -- 写 ---------------------------------------------------------------

    def upsert(self, rows: Sequence[VectorRow]) -> int:
        """按 ``chunk_id`` 幂等覆盖写入；返回写入行数。"""
        self._ensure_open()
        if not rows:
            return 0
        payload = []
        for row in rows:
            if len(row.vector) != self._dim:
                raise DimensionMismatchError(_dim_mismatch_message(self._dim, len(row.vector)))
            payload.append(
                {
                    CHUNK_ID_COLUMN: row.chunk_id,
                    CONTENT_HASH_COLUMN: row.content_hash,
                    VECTOR_COLUMN: [float(value) for value in row.vector],
                }
            )
        with self._lock:
            for start in range(0, len(payload), _UPSERT_BATCH_SIZE):
                batch = payload[start : start + _UPSERT_BATCH_SIZE]
                self._translate_errors(partial(self._merge_batch, batch))
        return len(payload)

    def _merge_batch(self, batch: list[dict[str, object]]) -> None:
        (
            self._table.merge_insert(CHUNK_ID_COLUMN)
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(batch)
        )

    def delete(self, chunk_ids: Sequence[str]) -> int:
        """按 ``chunk_id`` 删除；返回删除前的命中行数。"""
        self._ensure_open()
        if not chunk_ids:
            return 0
        where = _in_clause(CHUNK_ID_COLUMN, list(chunk_ids))
        with self._lock:
            before = self.count()
            self._translate_errors(lambda: self._table.delete(where))
            return before - self.count()

    # -- 读 ---------------------------------------------------------------

    def search(self, vector: Sequence[float], top_k: int) -> list[VectorHit]:
        """余弦 ANN 检索；返回按相似度降序的 ``VectorHit``（空表 / top_k<=0 → 空列表）。"""
        self._ensure_open()
        if top_k <= 0:
            return []
        if len(vector) != self._dim:
            raise DimensionMismatchError(_dim_mismatch_message(self._dim, len(vector)))
        with self._lock:
            if self._table.count_rows() == 0:
                return []
            rows = self._translate_errors(
                lambda: self._table.search(
                    [float(value) for value in vector], vector_column_name=VECTOR_COLUMN
                )
                .distance_type("cosine")
                .limit(top_k)
                .to_list()
            )
        hits = [VectorHit(chunk_id=row[CHUNK_ID_COLUMN], score=_row_score(row)) for row in rows]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits

    def get_hashes(self, chunk_ids: Sequence[str]) -> dict[str, str]:
        """返回给定 ``chunk_id`` 的 ``content_hash``；未命中/空输入不返回键。

        供 TASK-007 判断哪些 chunk 可复用向量。
        """
        self._ensure_open()
        unique_ids = list(dict.fromkeys(chunk_ids))
        if not unique_ids:
            return {}
        where = _in_clause(CHUNK_ID_COLUMN, unique_ids)
        with self._lock:
            rows = self._translate_errors(
                lambda: self._table.search(None)
                .where(where)
                .select([CHUNK_ID_COLUMN, CONTENT_HASH_COLUMN])
                .to_list()
            )
        return {row[CHUNK_ID_COLUMN]: row[CONTENT_HASH_COLUMN] for row in rows}

    def get_vectors_by_hash(
        self, content_hashes: Sequence[str]
    ) -> dict[str, tuple[str, list[float]]]:
        """按 ``content_hash`` 读回已有向量（TASK-111）：``hash -> (chunk_id, vector)``。

        为什么需要它：``docs/contracts/PROCESS.md`` §3.2 **R4 早已裁定** "复用键是 hash 不是
        id"，但原实现只按 ``chunk_id`` 比对 ``content_hash``（:meth:`get_hashes`），
        而 ``chunk_id = {path}:{fqn}:{start_line}`` 含行号——**行号一漂移就判为"新 chunk"重嵌**，
        同一份内容被反复付费。本方法让调用方能按内容搬移向量行（只换 id，不重算 embedding）。

        同一 ``content_hash`` 可能有多行（不同 id 同内容）；去重后任取一行即可
        （向量由内容唯一决定，与 id 无关）。
        """
        self._ensure_open()
        unique_hashes = list(dict.fromkeys(content_hashes))
        if not unique_hashes:
            return {}
        where = _in_clause(CONTENT_HASH_COLUMN, unique_hashes)
        with self._lock:
            rows = self._translate_errors(
                lambda: self._table.search(None)
                .where(where)
                .select([CHUNK_ID_COLUMN, CONTENT_HASH_COLUMN, VECTOR_COLUMN])
                .to_list()
            )
        found: dict[str, tuple[str, list[float]]] = {}
        for row in rows:
            digest = row[CONTENT_HASH_COLUMN]
            if digest in found:
                continue
            found[digest] = (row[CHUNK_ID_COLUMN], [float(v) for v in row[VECTOR_COLUMN]])
        return found

    def count(self) -> int:
        """表内行数（测试/对账用辅助方法）。"""
        self._ensure_open()
        with self._lock:
            return int(self._table.count_rows())

    # -- 重建 -------------------------------------------------------------

    def rebuild(self, dim: int) -> None:
        """整表重建：单次 ``create_table(mode="overwrite")`` 原子替换为新的空表。

        用于 embedding 模型/维度变更（D-07 二级失效）。重建后旧数据不可见（新表为空，
        等待 TASK-007 重新嵌入）；本实例立即切换到新表句柄。
        """
        self._ensure_open()
        if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
            raise ValueError(f"dim 必须为正整数，收到 {dim!r}")
        with self._lock:
            table = self._translate_errors(
                lambda: self._db.create_table(
                    TABLE_NAME, schema=_vector_schema(dim), mode="overwrite"
                )
            )
            self._table = table
            self._dim = dim

    # -- 内部 -------------------------------------------------------------

    def _translate_errors(self, operation: Callable[[], _T]) -> _T:
        """执行底层 LanceDB 调用并把维度类报错翻译成带 D-07 指引的异常。"""
        try:
            return operation()
        except (DimensionMismatchError, VectorStoreError):
            raise
        except Exception as exc:  # noqa: BLE001 - 统一封装为 VectorStoreError
            message = str(exc)
            if "dim" in message and ("match" in message or "mismatch" in message):
                raise DimensionMismatchError(
                    f"向量维度不匹配（LanceDB 报错：{message}）。{_D07_HINT}"
                ) from exc
            raise VectorStoreError(f"向量存储操作失败：{type(exc).__name__}: {message}") from exc


def _row_score(row: dict) -> float:
    """LanceDB 结果行 → 余弦相似度（``_distance`` 为距离，``_score`` 兼容旧版本列名）。"""
    if "_distance" in row:
        return 1.0 - float(row["_distance"])
    return float(row["_score"])
