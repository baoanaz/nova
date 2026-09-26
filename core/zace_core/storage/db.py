"""SQLite 连接管理、建库、PRAGMA 与事务上下文（TASK-001 交付物）。

边界：
- ``schema.sql`` 是 ``docs/contracts/index-schema.sql``（CF-01）的逐字节副本（测试强制）；
- 本模块只做连接/PRAGMA/建库/事务，不含业务语义（业务在 ``store.py``）；
- 直接 sqlite3，不引入 ORM（Module/01 §3.3 明确）。

并发模型：per-project 单写者——WAL 允许“单写多读”，写事务用 ``BEGIN IMMEDIATE``
立即拿写锁，避免升级死锁（Module/01 §4.3）。

**嵌套事务（TASK-114）**：同一个连接上的嵌套 ``transaction()`` 自动退化为 ``SAVEPOINT``
（进入时已在外层事务内即视为嵌套）。这让"一整轮文件共用一个写事务、但每个文件仍可独立回滚"
成为可能——单文件失败隔离（TASK-018 §C）不变，同时省掉每文件一次 ``COMMIT`` 的固定开销。
"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

__all__ = [
    "DB_FILENAME",
    "SCHEMA_VERSION",
    "SchemaMismatchError",
    "connect",
    "initialize_database",
    "read_schema_sql",
    "transaction",
    "validate_schema_version",
]

SCHEMA_VERSION = "1"
"""index.db schema 版本；写入 ``index_config.schema_version``，启动时校验。"""

DB_FILENAME = "index.db"
"""项目目录内的库文件名（Module/01 §3.3：``{project_id}/index.db``）。"""

_savepoint_seq = itertools.count()
"""嵌套事务的 SAVEPOINT 名序号（``SAVEPOINT`` 要求名字在同一时刻唯一）。"""


class SchemaMismatchError(RuntimeError):
    """现有 index.db 的 schema 与代码期望不一致（需迁移/重建，不得静默继续）。"""


def read_schema_sql() -> str:
    """读取打包内的 DDL 副本（必须与 ``docs/contracts/index-schema.sql`` 逐字节一致）。"""
    return (
        resources.files("zace_core.storage")
        .joinpath("schema.sql")
        .read_text(encoding="utf-8")
    )


def connect(db_path: str | Path) -> sqlite3.Connection:
    """建立连接并设置运行时 PRAGMA（WAL / synchronous=NORMAL / busy_timeout / Row 工厂）。

    使用 ``isolation_level=None``（autocommit），事务一律走 :func:`transaction` 显式控制。
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _is_initialized(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'index_config'"
    ).fetchone()
    return row is not None


def initialize_database(conn: sqlite3.Connection) -> None:
    """首次建库：执行 CF-01 DDL 并登记 schema_version（幂等）。"""
    conn.executescript(read_schema_sql())
    conn.execute(
        "INSERT INTO index_config(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (SCHEMA_VERSION,),
    )


def ensure_database(conn: sqlite3.Connection) -> None:
    """空库/未初始化库 → 建库；随后校验 schema_version。"""
    if not _is_initialized(conn):
        initialize_database(conn)
    validate_schema_version(conn)


def validate_schema_version(conn: sqlite3.Connection) -> None:
    """校验 ``index_config.schema_version``；不一致即报错（不尝试自动迁移）。"""
    row = conn.execute("SELECT value FROM index_config WHERE key = 'schema_version'").fetchone()
    found = None if row is None else str(row["value"])
    if found != SCHEMA_VERSION:
        raise SchemaMismatchError(
            f"index.db schema_version={found!r}，代码期望 {SCHEMA_VERSION!r}——"
            "禁止在旧库上继续写入；请重建项目索引（原库可保留备份后删除）。"
        )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式写事务：``BEGIN IMMEDIATE`` … ``COMMIT`` / 异常 ``ROLLBACK``。

    嵌套调用（同一连接、已在外层事务内）自动退化为 ``SAVEPOINT``：内层异常只回滚到该保存点，
    不影响外层已做的写；外层异常才 ``ROLLBACK`` 整段。语义与 SQLite 的保存点一致。
    """
    if conn.in_transaction:
        name = f"zace_sp_{next(_savepoint_seq)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
