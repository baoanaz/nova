"""DDL 契约落地：schema.sql 逐字节一致 + 建库/版本校验（CF-01）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import nova_core.storage
import pytest
from nova_core.storage import SCHEMA_VERSION, SchemaMismatchError, Store

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_DDL = REPO_ROOT / "docs" / "contracts" / "index-schema.sql"
PACKAGE_DDL = Path(nova_core.storage.__file__).parent / "schema.sql"

EXPECTED_TABLES = {
    "index_config",
    "files",
    "chunks",
    "symbols",
    "edges",
    "unresolved_refs",
    "spec_blocks",
    "spec_references",
}


def test_schema_sql_byte_identical_to_contract() -> None:
    assert PACKAGE_DDL.read_bytes() == CONTRACT_DDL.read_bytes()


def test_open_creates_database_and_applies_ddl(tmp_path: Path) -> None:
    db_path = tmp_path / "proj" / "index.db"
    with Store.open(tmp_path / "proj"):
        pass
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert EXPECTED_TABLES <= tables
        assert "chunks_fts" in tables  # FTS5 虚拟表建表成功
        version = conn.execute(
            "SELECT value FROM index_config WHERE key = 'schema_version'"
        ).fetchone()
        assert version is not None and version[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_reopen_existing_database_succeeds(tmp_path: Path) -> None:
    with Store.open(tmp_path / "proj") as first:
        assert first.get_config("schema_version") == SCHEMA_VERSION
    with Store.open(tmp_path / "proj") as second:
        assert second.get_config("schema_version") == SCHEMA_VERSION


def test_missing_schema_version_raises(tmp_path: Path) -> None:
    with Store.open(tmp_path / "proj") as store:
        assert store.get_config("schema_version") == SCHEMA_VERSION
    conn = sqlite3.connect(tmp_path / "proj" / "index.db")
    conn.execute("UPDATE index_config SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaMismatchError):
        Store.open(tmp_path / "proj")


def test_empty_database_file_gets_initialized(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "index.db").write_bytes(b"")
    with Store.open(proj) as store:
        assert store.get_config("schema_version") == SCHEMA_VERSION
        assert store.counts()["files"] == 0
