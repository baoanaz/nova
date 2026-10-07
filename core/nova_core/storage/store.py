"""Store：per-project 索引存储（Module/01 §3.2/§3.3；CF-01/CF-02/CF-08）。

职责划分（TASK-001 交付物）：
- 写路径：``apply_file_change`` 单事务重建该文件的 files/chunks/spec_blocks/symbols/
  edges/FTS/unresolved_refs 行并按 content_hash 对账；``apply_deletions`` 级联清库并
  置 ``spec_references.stale=1``；
- 读路径：exact_symbols / chunk_by_id / chunks_by_ids / fts_search / edges_for /
  spec_refs_* / freshness / counts；
- 配置：get_config / set_config（index_config 指纹键值）；
- TASK-006 两阶段解析原语：upsert_unresolved / unresolved_refs / resolve_refs /
  mark_refs_failed / unresolved_edges / retarget_edges；
- TASK-006 spec↔code 匹配写库原语：add_spec_refs（provenance 恒为 inferred，D-06）。

FileDelta 对账语义（CF-08，本卡冻结，TASK-007 消费）：
- ``new_chunk_ids``    本次写入的 chunk，其 content_hash 不在本文件旧 hash 集合中（新增/变化）
                       —— 需要 embedding；
- ``reused_chunk_ids`` 本次写入的 chunk，其 content_hash 在旧集合中（hash 未变）
                       —— 向量可按 hash 复用；注意 id 可能因行号漂移变化，复用键是 hash；
- ``removed_chunk_ids`` 本文件旧 chunk id 在本次写入后不再存在 —— 需要删除对应向量。
- 三个集合两两不重叠：变化的 id 只出现在 new（不在 removed），因此下游无论先 upsert(new)
  再 delete(removed) 还是反序都不会丢数据。

边（edges）归属：按 **source 侧**归属文件——文件变更/删除时只清理 ``source ∈ 本文件
符号 fqn ∪ 本次 parsed.edges 的 source_fqn`` 的行；指向本文件符号的入边属于其它文件，
不在此处删除（否则其它文件不重解析就永久丢边）。跨文件 fqn 解析算法归 TASK-006。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nova_core.storage.db import DB_FILENAME, connect, ensure_database, transaction
from nova_core.text import segment
from nova_core.types import ChunkDef, FileDelta, Freshness, ParsedFile, UnresolvedRef

__all__ = [
    "FTS_COLUMN_WEIGHTS",
    "EdgeRow",
    "EdgeTargetUpdate",
    "FtsOperator",
    "RefResolution",
    "SpecRef",
    "Store",
    "SymbolRow",
    "UnresolvedRefRow",
]

_SQLITE_PARAM_BATCH = 500

#: BM25 列权重（顺序 = ``chunks_fts`` 索引列 ``content_seg, signature_seg, docstring_seg``；
#: ``file_path`` 为 UNINDEXED 不参与）。符号名列加权，保证符号名命中排在长 docstring 的
#: 偶然提及之前（codegraph ``queries.ts:1505-1513`` 的同类做法）。
#: **TASK-015 校准项**：与 TASK-015 bake-off 一并调；属通道内权重不是通道间权重（D-16 不违反）。
FTS_COLUMN_WEIGHTS = (1.0, 5.0, 1.0)

#: ``Store.fts_search`` 的多词组合语义：``or``（任一 token 命中，默认）/ ``and``（全 token 命中）。
FtsOperator = Literal["or", "and"]

# unresolved_refs.reference_kind → edges.kind 的默认映射（TASK-006 可用 kind 覆盖）。
_EDGE_KIND_BY_REF_KIND = {
    "call": "calls",
    "import": "imports",
    "extends": "extends",
    "implements": "implements",
    "reference": "references",
}


@dataclass(frozen=True, slots=True)
class SymbolRow:
    """symbols 表行（exact_symbols 返回；带 chunk_id 供检索侧定位切片）。"""

    id: str
    name: str
    fqn: str
    kind: str
    chunk_id: str | None
    file_path: str | None
    start_line: int | None
    end_line: int | None
    is_exported: bool


@dataclass(frozen=True, slots=True)
class EdgeRow:
    """edges 表行（edges_for / unresolved_edges 返回）。"""

    source: str
    target: str
    kind: str
    line: int | None
    provenance: str


@dataclass(frozen=True, slots=True)
class UnresolvedRefRow:
    """unresolved_refs 表行（两阶段解析生命周期输入）。"""

    id: int
    from_symbol: str
    reference_name: str
    reference_kind: str
    line: int | None
    file_path: str | None
    language: str | None
    status: str
    name_tail: str


@dataclass(frozen=True, slots=True)
class RefResolution:
    """一条 pending ref 的解析结果：写边 + 删 ref 行（TASK-006 产出）。"""

    ref_id: int
    target_fqn: str
    kind: str | None = None  # None → 按 reference_kind 默认映射（见模块常量）
    provenance: str = "parsed"  # parsed / synthesized（多义全连时 synthesized）


@dataclass(frozen=True, slots=True)
class EdgeTargetUpdate:
    """裸名边 → fqn 边的重定向（TASK-006 二阶段解析 parsed.edges 的 target_name）。"""

    source: str
    target: str  # 旧 target（裸名/相对名）
    kind: str
    line: int | None
    new_target: str  # 解析后的 fqn


@dataclass(frozen=True, slots=True)
class SpecRef:
    """spec_references 表行（spec↔code 桥，provenance 恒 inferred）。"""

    spec_block_id: str
    symbol_id: str
    provenance: str
    stale: bool


def _name_tail(name: str) -> str:
    """'util.greet' → 'greet'；'A::refresh' → 'refresh'（failed 重试匹配键）。"""
    return name.replace("::", ".").split(".")[-1]


def _batches(items: Sequence[str], size: int = _SQLITE_PARAM_BATCH) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _symbol_from_row(row: sqlite3.Row) -> SymbolRow:
    return SymbolRow(
        id=str(row["id"]),
        name=str(row["name"]),
        fqn=str(row["fqn"]),
        kind=str(row["kind"]),
        chunk_id=None if row["chunk_id"] is None else str(row["chunk_id"]),
        file_path=None if row["file_path"] is None else str(row["file_path"]),
        start_line=None if row["start_line"] is None else int(row["start_line"]),
        end_line=None if row["end_line"] is None else int(row["end_line"]),
        is_exported=bool(row["is_exported"]),
    )


def _edge_from_row(row: sqlite3.Row) -> EdgeRow:
    return EdgeRow(
        source=str(row["source"]),
        target=str(row["target"]),
        kind=str(row["kind"]),
        line=None if row["line"] is None else int(row["line"]),
        provenance=str(row["provenance"] or "parsed"),
    )


def _chunk_from_row(row: sqlite3.Row) -> ChunkDef:
    return ChunkDef(
        id=str(row["id"]),
        file_path=str(row["file_path"]),
        symbol_fqn=None if row["symbol_fqn"] is None else str(row["symbol_fqn"]),
        symbol_kind=str(row["symbol_kind"] or ""),
        start_line=int(row["start_line"] or 0),
        end_line=int(row["end_line"] or 0),
        signature=str(row["signature"] or ""),
        docstring=str(row["docstring"] or ""),
        content=str(row["content"]),
        content_hash=str(row["content_hash"]),
    )


def _spec_ref_from_row(row: sqlite3.Row) -> SpecRef:
    return SpecRef(
        spec_block_id=str(row["spec_block_id"]),
        symbol_id=str(row["symbol_id"]),
        provenance=str(row["provenance"]),
        stale=bool(row["stale"]),
    )


def _delete_fts_for_file(conn: sqlite3.Connection, file_path: str) -> None:
    """独立 FTS5 表无外键，按 rowid 显式清行（CF-01 规划期裁定 1）。"""
    rows = conn.execute("SELECT rowid FROM chunks WHERE file_path = ?", (file_path,)).fetchall()
    if rows:
        conn.executemany("DELETE FROM chunks_fts WHERE rowid = ?", [(r["rowid"],) for r in rows])


def _insert_fts_row(conn: sqlite3.Connection, rowid: int, chunk: ChunkDef) -> None:
    """写入 jieba 预分词文本（D-20/D-45）；渲染一律读 chunks.content 原文。"""
    conn.execute(
        "INSERT INTO chunks_fts(rowid, content_seg, signature_seg, docstring_seg, file_path) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            rowid,
            segment(chunk.content),
            segment(chunk.signature),
            segment(chunk.docstring),
            chunk.file_path,
        ),
    )


#: 字面量检索的匹配文本（TASK-101 §A）：符号名 + 签名 + 正文，小写由调用方在 SQL 内处理。
#: 为什么不是只扫 ``content``：``unicode61`` 与 jieba 都不切 camelCase，查询里的
#: ``capability`` 在 ``CapabilityDefinition`` 里只能靠子串命中——而该串多出现在符号名/签名处。
_LITERAL_BLOB_SQL = (
    "lower(coalesce(symbol_fqn, '') || char(10) || coalesce(signature, '')"
    " || char(10) || content)"
)

class Store:
    """per-project 索引库门面；打开即校验 schema，所有写路径单事务。"""

    def __init__(self, conn: sqlite3.Connection, project_dir: Path) -> None:
        self._conn = conn
        self._project_dir = project_dir

    # ------------------------------------------------------------------ 生命周期

    @classmethod
    def open(cls, project_dir: str | Path) -> Store:
        """打开/初始化 ``{project_dir}/index.db``；校验 ``index_config.schema_version``。

        新建库执行 CF-01 DDL；已存在的库若 schema_version 不匹配则抛
        :class:`nova_core.storage.db.SchemaMismatchError`（不自动迁移）。
        """
        path = Path(project_dir)
        path.mkdir(parents=True, exist_ok=True)
        conn = connect(path / DB_FILENAME)
        try:
            ensure_database(conn)
        except BaseException:
            conn.close()
            raise
        return cls(conn, path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def project_dir(self) -> Path:
        """项目数据目录（vectors/ 等兄弟目录由后续卡在此目录下建立）。"""
        return self._project_dir

    # ------------------------------------------------------------------ 配置

    def get_config(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM index_config WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_config(self, key: str, value: str) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                "INSERT INTO index_config(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    @contextmanager
    def write_batch(self) -> Iterator[None]:
        """把一批单文件写合并进**一个**写事务（TASK-114 / P1-3）。

        为什么需要它：冷启动逐文件 ``BEGIN IMMEDIATE`` + ``COMMIT`` 在 langchain 规模下
        仅提交开销就有 3.3s（2986 个事务）。这里开一个外层事务，内部每次
        :meth:`apply_file_change` 的 ``transaction()`` 自动退化为 ``SAVEPOINT``——
        **单文件失败隔离不变**（失败只回滚该文件的保存点），整批写只在最后提交一次。

        代价：写锁持有到整批结束、失败时整批回滚（比"留半成品"更符合 ingest 的原子语义）、
        WAL 在提交前持续增长。调用方负责把批的大小控制在合理范围。
        """
        with transaction(self._conn):
            yield

    # ------------------------------------------------------------------ 写路径

    def apply_file_change(
        self,
        parsed: ParsedFile,
        chunks: Sequence[ChunkDef],
        file_content_hash: str,
        commit: str | None = None,
        *,
        generated: bool = False,
    ) -> FileDelta:
        """单事务写入/替换一个文件的全部索引行，返回 content_hash 对账结果。

        - ``chunks`` 为 TASK-006 的切分产物（同文件全量，含 spec_block chunk 双表成员）；
        - ``parsed.edges`` 原样写入（``target_name`` 允许是裸名，fqn 化归 TASK-006）；
        - ``parsed.unresolved`` 重写本文件 unresolved_refs（status 归 pending）；
        - 本文件旧符号中消失的 fqn → 其 spec_references 置 stale=1；
        - 同名 fqn 的行号漂移 → refs 从旧 id 重挂到新 id（保持 fqn 级引用有效）；
        - ``commit`` 仅作为同步元数据写入 ``files.commit_id``；
        - ``generated``（TASK-REVIEW-RUNTIME P2-5）由**扫描/索引期**传入（见
          :mod:`nova_core.pipeline.generated`）。此列以前硬编码为 0，导致 rerank 只能靠
          文件名约定代理，无法识别内容 banner（如 ``DO NOT EDIT`` 头）。

        **冷启动快路径（TASK-114 / P1-3）**：先按主键探测 ``files`` 是否有本路径。
        没有 ⇒ 本文件从未入库 ⇒ 旧 chunks/symbols/spec_blocks/unresolved_refs 必然为空
        （写入与删除都成对发生），于是跳过旧行定位 SELECT、FTS rowid SELECT 与 4 条 DELETE。
        实测这些空转在 langchain 冷启动占 11.4s 中的绝大部分，而真正的行插入只要 1.1s。
        """
        file_path = parsed.path
        new_chunks = list(chunks)
        for chunk in new_chunks:
            if chunk.file_path != file_path:
                raise ValueError(
                    f"chunk {chunk.id!r} 的 file_path={chunk.file_path!r} 与"
                    f" parsed.path={file_path!r} 不一致"
                )
        for block in parsed.spec_blocks:
            if block.path != file_path:
                raise ValueError(
                    f"spec_block {block.heading_path!r} 的 path={block.path!r} 与"
                    f" parsed.path={file_path!r} 不一致"
                )

        with transaction(self._conn) as conn:
            known_file = (
                conn.execute("SELECT 1 FROM files WHERE path = ?", (file_path,)).fetchone()
                is not None
            )
            if known_file:
                old_rows = conn.execute(
                    "SELECT id, content_hash FROM chunks WHERE file_path = ? ORDER BY rowid",
                    (file_path,),
                ).fetchall()
                old_symbols = conn.execute(
                    "SELECT id, fqn FROM symbols WHERE file_path = ? ORDER BY rowid",
                    (file_path,),
                ).fetchall()
            else:
                old_rows, old_symbols = [], []
            old_by_id = {str(r["id"]): str(r["content_hash"]) for r in old_rows}
            old_hashes = set(old_by_id.values())

            self._apply_spec_ref_cascade(conn, file_path, old_symbols, parsed)

            if known_file:
                _delete_fts_for_file(conn, file_path)
                conn.execute("DELETE FROM chunks WHERE file_path = ?", (file_path,))
                conn.execute("DELETE FROM symbols WHERE file_path = ?", (file_path,))
                conn.execute("DELETE FROM spec_blocks WHERE file_path = ?", (file_path,))
                conn.execute("DELETE FROM unresolved_refs WHERE file_path = ?", (file_path,))
            old_fqns = {str(r["fqn"]) for r in old_symbols}
            edge_sources = old_fqns | {s.fqn for s in parsed.symbols} | {
                e.source_fqn for e in parsed.edges
            }
            if edge_sources:
                conn.executemany(
                    "DELETE FROM edges WHERE source = ?", [(s,) for s in sorted(edge_sources)]
                )

            conn.execute(
                "INSERT INTO files(path, content_hash, language, generated, branch, commit_id,"
                " indexed_at, parse_errors) VALUES(?, ?, ?, ?, NULL, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET"
                " content_hash = excluded.content_hash,"
                " language = excluded.language,"
                " generated = excluded.generated,"
                " commit_id = excluded.commit_id,"
                " indexed_at = excluded.indexed_at,"
                " parse_errors = excluded.parse_errors",
                (
                    file_path,
                    file_content_hash,
                    parsed.language,
                    int(generated),
                    commit,
                    int(time.time()),
                    json.dumps(list(parsed.parse_errors), ensure_ascii=False),
                ),
            )

            chunk_ids = set()
            for chunk in new_chunks:
                cursor = conn.execute(
                    "INSERT INTO chunks(id, file_path, symbol_fqn, symbol_kind, start_line,"
                    " end_line, signature, docstring, content, content_hash)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk.id,
                        chunk.file_path,
                        chunk.symbol_fqn,
                        chunk.symbol_kind,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.signature,
                        chunk.docstring,
                        chunk.content,
                        chunk.content_hash,
                    ),
                )
                chunk_ids.add(chunk.id)
                _insert_fts_row(conn, int(cursor.lastrowid or 0), chunk)

            for symbol in parsed.symbols:
                symbol_id = f"{file_path}:{symbol.fqn}:{symbol.start_line}"
                conn.execute(
                    "INSERT INTO symbols(id, name, fqn, kind, chunk_id, file_path, start_line,"
                    " end_line, is_exported) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        symbol_id,
                        symbol.name,
                        symbol.fqn,
                        symbol.kind,
                        symbol_id if symbol_id in chunk_ids else None,
                        file_path,
                        symbol.start_line,
                        symbol.end_line,
                        int(symbol.is_exported),
                    ),
                )

            for block in parsed.spec_blocks:
                conn.execute(
                    "INSERT INTO spec_blocks(id, file_path, doctype, heading, heading_path,"
                    " heading_level, start_line, end_line, content, code_fences)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"{block.path}:{block.heading_path}:{block.start_line}",
                        block.path,
                        block.doctype,
                        block.heading,
                        block.heading_path,
                        block.level,
                        block.start_line,
                        block.end_line,
                        block.content,
                        json.dumps(
                            [
                                {"lang": fence.lang, "content": fence.content, "line": fence.line}
                                for fence in block.code_fences
                            ],
                            ensure_ascii=False,
                        ),
                    ),
                )

            if parsed.edges:
                conn.executemany(
                    "INSERT OR IGNORE INTO edges(source, target, kind, line, provenance)"
                    " VALUES(?, ?, ?, ?, ?)",
                    [
                        (e.source_fqn, e.target_name, e.kind, e.line, e.provenance)
                        for e in parsed.edges
                    ],
                )

            self._insert_unresolved_rows(conn, parsed.unresolved, file_path, parsed.language)

            return FileDelta(
                path=file_path,
                new_chunk_ids=tuple(c.id for c in new_chunks if c.content_hash not in old_hashes),
                reused_chunk_ids=tuple(c.id for c in new_chunks if c.content_hash in old_hashes),
                removed_chunk_ids=tuple(i for i in old_by_id if i not in chunk_ids),
            )

    def apply_deletions(self, paths: Sequence[str]) -> tuple[str, ...]:
        """级联删除文件：files/chunks/symbols/edges/FTS/unresolved/spec 全清。

        引用被删符号的 ``spec_references`` 置 ``stale=1``（行保留，供 MissingEvidence 警告）；
        幂等：路径不存在时不报错。

        **返回被删掉的 chunk id**（TASK-REVIEW-RUNTIME P1-2）：调用方据此清理向量库。
        为什么必须由本方法返回而不是让调用方自己查（见 ``indexer`` 模块 docstring 的边界说明）：
        删除是单事务的，先查后删会发生 TOCTOU；且跨进程/跨 ``Indexer`` 实例时调用方内存里
        没有这批 id，旧行为只删 SQLite 不删 LanceDB，留下**孤儿向量**。
        """
        removed: list[str] = []
        with transaction(self._conn) as conn:
            for path in paths:
                rows = conn.execute(
                    "SELECT id FROM chunks WHERE file_path = ? ORDER BY rowid", (path,)
                ).fetchall()
                removed.extend(str(r["id"]) for r in rows)
                symbols = conn.execute(
                    "SELECT id, fqn FROM symbols WHERE file_path = ? ORDER BY rowid", (path,)
                ).fetchall()
                if symbols:
                    conn.executemany(
                        "UPDATE spec_references SET stale = 1 WHERE symbol_id = ?",
                        [(str(r["id"]),) for r in symbols],
                    )
                conn.execute(
                    "DELETE FROM spec_references WHERE spec_block_id IN"
                    " (SELECT id FROM spec_blocks WHERE file_path = ?)",
                    (path,),
                )
                _delete_fts_for_file(conn, path)
                conn.execute("DELETE FROM chunks WHERE file_path = ?", (path,))
                conn.execute("DELETE FROM symbols WHERE file_path = ?", (path,))
                conn.execute("DELETE FROM spec_blocks WHERE file_path = ?", (path,))
                conn.execute("DELETE FROM unresolved_refs WHERE file_path = ?", (path,))
                if symbols:
                    conn.executemany(
                        "DELETE FROM edges WHERE source = ?",
                        [(str(r["fqn"]),) for r in symbols],
                    )
                conn.execute("DELETE FROM files WHERE path = ?", (path,))
        return tuple(removed)

    # ------------------------------------------------------- TASK-006 解析原语

    def upsert_unresolved(
        self,
        refs: Sequence[UnresolvedRef],
        file_path: str | None = None,
        language: str | None = None,
    ) -> int:
        """插入 pending 引用（重复键跳过）；返回新增行数。

        去重键：(from_fqn, name, kind, IFNULL(line, -1))——与 apply_file_change 内一致。
        """
        inserted = 0
        with transaction(self._conn) as conn:
            for ref in refs:
                inserted += self._insert_one_unresolved(conn, ref, file_path, language)
        return inserted

    def unresolved_refs(
        self, status: str | None = None, file_path: str | None = None
    ) -> list[UnresolvedRefRow]:
        """读取 unresolved 行（可按 status/file_path 过滤；按 id 升序）。"""
        sql = (
            "SELECT id, from_symbol, reference_name, reference_kind, line, file_path, language,"
            " status, name_tail FROM unresolved_refs"
        )
        params: list[str] = []
        where: list[str] = []
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if file_path is not None:
            where.append("file_path = ?")
            params.append(file_path)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        rows = self._conn.execute(sql, params).fetchall()
        return [
            UnresolvedRefRow(
                id=int(r["id"]),
                from_symbol=str(r["from_symbol"]),
                reference_name=str(r["reference_name"]),
                reference_kind=str(r["reference_kind"]),
                line=None if r["line"] is None else int(r["line"]),
                file_path=None if r["file_path"] is None else str(r["file_path"]),
                language=None if r["language"] is None else str(r["language"]),
                status=str(r["status"]),
                name_tail=str(r["name_tail"] or ""),
            )
            for r in rows
        ]

    def resolve_refs(self, resolutions: Sequence[RefResolution]) -> int:
        """写解析边并删除对应 ref 行（同一事务）；返回实际处理条数。

        边 kind 缺省按 ``reference_kind`` 映射（call→calls 等，见模块常量）；
        多义全连时由 TASK-006 传 ``provenance='synthesized'``。
        """
        resolved = 0
        with transaction(self._conn) as conn:
            for item in resolutions:
                row = conn.execute(
                    "SELECT from_symbol, reference_kind, line FROM unresolved_refs WHERE id = ?",
                    (item.ref_id,),
                ).fetchone()
                if row is None:
                    continue
                kind = item.kind or _EDGE_KIND_BY_REF_KIND.get(
                    str(row["reference_kind"]), "references"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO edges(source, target, kind, line, provenance)"
                    " VALUES(?, ?, ?, ?, ?)",
                    (
                        str(row["from_symbol"]),
                        item.target_fqn,
                        kind,
                        row["line"],
                        item.provenance,
                    ),
                )
                conn.execute("DELETE FROM unresolved_refs WHERE id = ?", (item.ref_id,))
                resolved += 1
        return resolved

    def mark_refs_failed(self, ref_ids: Sequence[int]) -> int:
        """把 ref 行置 ``status='failed'`` 并补写 name_tail（保留供重试）；返回处理条数。"""
        marked = 0
        with transaction(self._conn) as conn:
            for ref_id in ref_ids:
                row = conn.execute(
                    "SELECT reference_name, name_tail FROM unresolved_refs WHERE id = ?",
                    (ref_id,),
                ).fetchone()
                if row is None:
                    continue
                tail = str(row["name_tail"] or "") or _name_tail(str(row["reference_name"]))
                conn.execute(
                    "UPDATE unresolved_refs SET status = 'failed', name_tail = ? WHERE id = ?",
                    (tail, ref_id),
                )
                marked += 1
        return marked

    def unresolved_edges(self, kinds: Sequence[str] | None = None) -> list[EdgeRow]:
        """读取 target 尚未落成 fqn 的边（TASK-006 二阶段解析输入，按 source/target 稳定排序）。"""
        sql = (
            "SELECT source, target, kind, line, provenance FROM edges"
            " WHERE target NOT IN (SELECT fqn FROM symbols)"
        )
        params: list[str] = []
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        sql += " ORDER BY source, target, kind, IFNULL(line, -1)"
        return [_edge_from_row(r) for r in self._conn.execute(sql, params).fetchall()]

    def retarget_edges(self, updates: Sequence[EdgeTargetUpdate]) -> int:
        """裸名边重定向到 fqn；目标边已存在时删除旧行（等价完成解析）；返回影响行数。"""
        changed = 0
        with transaction(self._conn) as conn:
            for update in updates:
                where = (
                    "source = ? AND target = ? AND kind = ? AND IFNULL(line, -1) = IFNULL(?, -1)"
                )
                params = (update.source, update.target, update.kind, update.line)
                try:
                    cursor = conn.execute(
                        f"UPDATE edges SET target = ? WHERE {where}",
                        (update.new_target, *params),
                    )
                except sqlite3.IntegrityError:
                    cursor = conn.execute(f"DELETE FROM edges WHERE {where}", params)
                changed += cursor.rowcount
        return changed

    def add_spec_refs(self, refs: Sequence[tuple[str, str]]) -> int:
        """写入 spec_references（``provenance`` 恒 ``'inferred'``，D-06）；返回新增行数。

        ``refs`` 为 (spec_block_id, symbol_id) 序列；已存在的组合跳过（幂等）。
        """
        inserted = 0
        seen: set[tuple[str, str]] = set()
        with transaction(self._conn) as conn:
            for spec_block_id, symbol_id in refs:
                key = (spec_block_id, symbol_id)
                if key in seen:
                    continue
                seen.add(key)
                exists = conn.execute(
                    "SELECT 1 FROM spec_references"
                    " WHERE spec_block_id = ? AND symbol_id = ? LIMIT 1",
                    (spec_block_id, symbol_id),
                ).fetchone()
                if exists is not None:
                    continue
                conn.execute(
                    "INSERT INTO spec_references(spec_block_id, symbol_id, provenance, stale)"
                    " VALUES(?, ?, 'inferred', 0)",
                    (spec_block_id, symbol_id),
                )
                inserted += 1
        return inserted

    # ------------------------------------------------------------------ 读路径

    def exact_symbols(self, name: str, limit: int | None = 20) -> list[SymbolRow]:
        """按 name 或 fqn 精确匹配符号（Exact 通道，D-15）；``limit=None`` 表示不限量。

        排序：导出符号优先、fqn 精确命中优先、文件路径/行号稳定。
        """
        sql = (
            "SELECT id, name, fqn, kind, chunk_id, file_path, start_line, end_line, is_exported"
            " FROM symbols WHERE name = ? OR fqn = ?"
            " ORDER BY is_exported DESC, CASE WHEN fqn = ? THEN 0 ELSE 1 END,"
            " file_path, start_line, id"
        )
        params: list[object] = [name, name, name]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_symbol_from_row(r) for r in self._conn.execute(sql, params).fetchall()]

    def symbols_in_container(
        self, container: str, *, kinds: Sequence[str] | None = None, limit: int | None = 200
    ) -> list[SymbolRow]:
        """按**容器前缀**枚举成员符号（TASK-109 §G6：容器-成员断层）。

        为什么需要它（实测缺陷，2026-09-15）：查询点名了容器（``CapabilityGateway``）时，
        类骨架能进包，但它下面的方法（``._reserve_idempotency`` / ``._should_retry`` /
        ``._normalize``）一条都不进——而这些方法恰恰是用户问的行为所在。现有读 API 做不到
        这一步：:meth:`exact_symbols` 只按 ``name``/``fqn`` 精确匹配（类名匹配不到成员），
        :meth:`symbol_literal_search` 只扫 ``symbols.name``（成员的名字是 ``_invoke``，
        不含容器名，因此从 ``CapabilityGateway`` 查不到它）。

        ``container`` 给**去掉成员部分的容器 fqn**（``CapabilityGateway`` /
        ``A::B``）；同时按 ``.`` 与 ``::`` 两种分隔符匹配，因为 Python 与 C++ 抽取器
        对同一个成员产出不同拼写（``A.b`` vs ``A::b``）。

        排序：按 ``file_path, start_line``（**声明顺序**，与源码阅读顺序一致，确定性），
        行号缺失的排在最后。``limit=None`` 表示不限量（G6 由调用方按自身配额截断）。
        """
        cleaned = container.strip()
        if not cleaned:
            return []
        sql = (
            "SELECT id, name, fqn, kind, chunk_id, file_path, start_line, end_line, is_exported"
            " FROM symbols WHERE (fqn LIKE ? OR fqn LIKE ?)"
        )
        params: list[object] = [f"{cleaned}.%", f"{cleaned}::%"]
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        sql += " ORDER BY file_path, start_line IS NULL, start_line, id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_symbol_from_row(r) for r in self._conn.execute(sql, params).fetchall()]

    def chunk_covering(self, file_path: str, line: int) -> ChunkDef | None:
        """按 ``文件 + 行号`` 取**覆盖该行**的切片（TASK-101 §B：图扩展落到定义块）。

        用途：符号表里有些符号（如 ``(module)``、类骨架之外的声明）没有 ``chunk_id``，
        但确实有 ``file_path/start_line``。图扩展此前遇到这类符号就放弃（不编造证据），
        于是一条真实的调用边在包内完全不可见（实测：``capability_definitions`` →
        ``tools/hmi/definitions.py`` 被整条丢弃）。本方法给出该符号所在的切片。

        选块口径：覆盖该行、行区间**最短**的切片（最贴近该符号），符号名相等者优先。
        """
        row = self._conn.execute(
            "SELECT id, file_path, symbol_fqn, symbol_kind, start_line, end_line, signature,"
            " docstring, content, content_hash FROM chunks"
            " WHERE file_path = ? AND start_line <= ? AND end_line >= ?"
            " ORDER BY (end_line - start_line) ASC, CASE WHEN symbol_fqn = ? THEN 0 ELSE 1 END,"
            " start_line, id LIMIT 1",
            (file_path, line, line, f"({line})"),
        ).fetchone()
        return None if row is None else _chunk_from_row(row)

    def literal_search(
        self, phrase: str, *, limit: int = 30, min_length: int = 4
    ) -> list[tuple[str, int]]:
        """短语**字面量**检索（TASK-101 §A）：子串命中，返回 ``(chunk_id, 命中次数)``。

        为什么需要它（实测缺陷）：查询里的长字面量（``cvi-agent-aibox``、``confirmation=REQUIRED``、
        ``AGENT_GRAPH_BACKEND``）经 CJK/分词器切碎后进入 BM25 的 OR 匹配，会被高频词
        （``agent`` / ``配置``）淹没；而它们恰恰是"配置键、CLI 名、常量名"这类问题的唯一指纹。
        本方法不做分词、不分词序，直接在切片正文里找连续子串。

        - 大小写不敏感（``lower()`` 双侧）；
        - 匹配范围 = ``symbol_fqn`` + ``signature`` + ``content``（见 :data:`_LITERAL_BLOB_SQL`）：
          标识符词根（``capability`` ⊂ ``CapabilityDefinition`` / ``capability_definitions``）
          只有在符号名与签名里才看得到——``unicode61`` 不切 camelCase，FTS 也找不到它；
        - ``min_length`` 以下的短语不查（短词必然泛滥，且那是 BM25 的职责）；
        - 排序：**符号名含该短语者优先**（定义处，实测 ``capability`` ⊂ ``capability_definitions``）
          → 命中次数降序 → 切片更短者优先 → id 稳定；
        - 性能：单条 SQL 全表扫描（实测 287 文件 / 3416 切片约 20ms），每查询至多数个短语。
        """
        cleaned = phrase.strip()
        if len(cleaned) < min_length:
            return []
        rows = self._conn.execute(
            "SELECT id, symbol_match,"
            " MAX(1, (length(blob) - length(replace(blob, lower(?), ''))) / length(?)) AS hits"
            " FROM (SELECT id, end_line, start_line, symbol_fqn,"
            f"  {_LITERAL_BLOB_SQL} AS blob,"
            "  CASE WHEN instr(lower(coalesce(symbol_fqn, '')), lower(?)) > 0 THEN 0 ELSE 1 END"
            "   AS symbol_match FROM chunks)"
            " WHERE instr(blob, lower(?)) > 0"
            " GROUP BY id"
            " ORDER BY symbol_match ASC, hits DESC, (end_line - start_line) ASC, id LIMIT ?",
            (cleaned, cleaned, cleaned, cleaned, limit),
        ).fetchall()
        return [(str(row["id"]), int(row["hits"])) for row in rows]

    def symbol_literal_search(self, phrase: str, *, limit: int = 20) -> list[str]:
        """按**符号名**做子串匹配（TASK-101 §A 弱短语）：返回 ``chunk_id`` 列表。

        与 :meth:`literal_search` 的分工：那个扫正文（用户点名的字面量），这个只扫
        ``symbols.name/fqn``——用于"查询里写了标识符词根（``capability`` ⊂
        ``capability_definitions``/``CapabilityDefinition``）"的情形，``unicode61`` 不切
        camelCase/下划线，FTS 找不到它。**只匹配符号名**是为了压制噪声：正文里几乎每个
        调用点都含该词根，符号名里则集中在定义/Provider 处。

        排序：短名优先（``capability_definitions`` 比 ``NewFriendCapabilityProvider._invoke``
        更可能是答案）→ 名字典序稳定。
        """
        cleaned = phrase.strip()
        if not cleaned:
            return []
        rows = self._conn.execute(
            "SELECT chunk_id FROM symbols"
            " WHERE chunk_id IS NOT NULL AND instr(lower(name), lower(?)) > 0"
            " ORDER BY length(name) ASC, name, file_path, start_line LIMIT ?",
            (cleaned, limit),
        ).fetchall()
        seen: list[str] = []
        for row in rows:
            chunk_id = str(row["chunk_id"])
            if chunk_id not in seen:
                seen.append(chunk_id)
        return seen

    def chunk_by_id(self, chunk_id: str) -> ChunkDef | None:
        row = self._conn.execute(
            "SELECT id, file_path, symbol_fqn, symbol_kind, start_line, end_line, signature,"
            " docstring, content, content_hash FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        return None if row is None else _chunk_from_row(row)

    def chunks_by_ids(self, chunk_ids: Sequence[str]) -> list[ChunkDef]:
        """批量取切片；返回顺序与输入一致，缺失 id 跳过。"""
        found: dict[str, ChunkDef] = {}
        for batch in _batches(list(chunk_ids)):
            placeholders = ", ".join("?" for _ in batch)
            rows = self._conn.execute(
                "SELECT id, file_path, symbol_fqn, symbol_kind, start_line, end_line, signature,"
                f" docstring, content, content_hash FROM chunks WHERE id IN ({placeholders})",
                list(batch),
            ).fetchall()
            for row in rows:
                found[str(row["id"])] = _chunk_from_row(row)
        return [found[i] for i in chunk_ids if i in found]

    def fts_search(
        self,
        segmented_query: str,
        limit: int = 50,
        *,
        operator: FtsOperator = "or",
    ) -> list[tuple[str, float]]:
        """BM25 检索：``segmented_query`` 必须已经过 :func:`nova_core.text.segment`（D-45）。

        多 token 组合语义由 ``operator`` 决定（R11，2026-09-10 集成期裁定）：

        - ``"or"``（默认）：token 用 ``OR`` 连接 → 任一 token 命中即入候选，由 bm25 打分排序。
          中文自然语言查询分词后 token 多（7+），隐式 AND 会恒零命中（真实缺陷 R11）；
        - ``"and"``：保留高精度语义（全 token 必须命中），供需要精确性的调用方显式选择。

        每个 token 均引号包裹（避免把 FTS 语法字符当运算符）；打分用
        :data:`FTS_COLUMN_WEIGHTS` 加列权重，符号名列优先。
        返回 ``(chunk_id, bm25 分)``，分值为原始 bm25（负数，越小越相关）并按它升序。
        """
        if operator not in ("or", "and"):
            raise ValueError(f"operator 必须是 'or' 或 'and'，收到 {operator!r}")
        tokens = [t for t in segmented_query.split() if t.strip()]
        if not tokens:
            return []
        quoted = ['"' + t.replace('"', '""') + '"' for t in tokens]
        match = (" OR " if operator == "or" else " ").join(quoted)
        weights = ", ".join(repr(float(weight)) for weight in FTS_COLUMN_WEIGHTS)
        rows = self._conn.execute(
            "SELECT c.id AS chunk_id,"
            f" bm25(chunks_fts, {weights}) AS score"
            " FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid"
            f" WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts, {weights}) LIMIT ?",
            (match, limit),
        ).fetchall()
        return [(str(r["chunk_id"]), float(r["score"])) for r in rows]

    def edges_for(self, fqn: str, kinds: Sequence[str] | None = None) -> list[EdgeRow]:
        """返回与 fqn 相关的边（**双向**：source 侧出边 + target 侧入边）。

        TASK-011 图扩展按方向自行过滤（callers = target 侧，callees = source 侧）。
        """
        sql = (
            "SELECT source, target, kind, line, provenance FROM edges"
            " WHERE (source = ? OR target = ?)"
        )
        params: list[object] = [fqn, fqn]
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            params.extend(kinds)
        sql += " ORDER BY kind, source, target, IFNULL(line, -1)"
        return [_edge_from_row(r) for r in self._conn.execute(sql, params).fetchall()]

    def reexport_sources(self, name: str, *, suffix: str = "__init__.py") -> list[str]:
        """符号名的**公开导出点**：导入了该符号的包入口文件（TASK-MCP-CHAIN）。

        为什么不能复用 :meth:`edges_for`：它按 **fqn 精确匹配**，而 ``imports`` 边的
        两端拼法不同（实测）：符号表里是裸名 ``create_agent``，边里存的是模块路径形式
        ``langchain.agents.factory.create_agent``。只按裸名查会一条也拿不到。

        口径：边类型 ``imports`` + 边的 target **尾部名**与 ``name`` 相等 + 边的
        **源路径**以 ``suffix`` 结尾（默认 ``__init__.py``——包入口才是“公开导出点”，
        否则一堆 ``examples/`` 与 ``tests/`` 也会变成候选）。

        ``.`` 与 ``::`` 两种分隔符都匹配（Python 与 C++ 抽取器的差异）。
        """
        cleaned = name.strip()
        if not cleaned:
            return []
        rows = self._conn.execute(
            "SELECT DISTINCT source, target FROM edges"
            " WHERE kind = 'imports' AND source LIKE ?",
            (f"%{suffix}",),
        ).fetchall()
        found: list[str] = []
        for row in rows:
            target = str(row["target"])
            if target.replace("::", ".").rsplit(".", 1)[-1] != cleaned:
                continue
            source = str(row["source"])
            if source not in found:
                found.append(source)
        return found

    def chunks_with_marker(
        self, chunk_ids: Sequence[str], *, marker: str
    ) -> frozenset[str]:
        """给定 chunk 中，正文含 ``marker`` 的那些 chunk_id（批量、单次查询）。

        用途（TASK-MCP-BUDGET）：rerank 的“已弃用符号”信号。判据是**符号级**的正文字面量
        （如 ``@deprecated(``），而不是文件名或路径——装饰器只标注它直接修饰的那个符号，
        所以同一符号在跃包与兼容层各有一份时只有后者被命中。

        为什么不用“文件导入了 deprecation 工具”：实测会误伤跃包本身
        （``langchain_core`` 是 deprecation 工具的提供者，其导入使真实实现被误降）。

        ``marker`` 大小写不敏感子串匹配。空输入或空 marker → 空集（不查库）。
        """
        wanted = [cid for cid in chunk_ids if cid]
        if not wanted or not marker:
            return frozenset()
        found: set[str] = set()
        for batch in _batches(sorted(set(wanted))):
            placeholders = ", ".join("?" for _ in batch)
            rows = self._conn.execute(
                "SELECT id FROM chunks"
                f" WHERE id IN ({placeholders}) AND content LIKE ?",
                [*batch, f"%{marker.lower()}%"],
            ).fetchall()
            found.update(str(r["id"]) for r in rows)
        return frozenset(found)

    def spec_refs_for_symbols(self, symbol_ids: Sequence[str]) -> list[SpecRef]:
        """按符号批量取 spec 引用（代码命中 → 设计意图，TASK-011）。"""
        refs: list[SpecRef] = []
        for batch in _batches(list(symbol_ids)):
            placeholders = ", ".join("?" for _ in batch)
            rows = self._conn.execute(
                "SELECT spec_block_id, symbol_id, provenance, stale FROM spec_references"
                f" WHERE symbol_id IN ({placeholders}) ORDER BY spec_block_id, symbol_id",
                list(batch),
            ).fetchall()
            refs.extend(_spec_ref_from_row(r) for r in rows)
        return refs

    def spec_refs_for_spec(self, spec_block_id: str) -> list[SpecRef]:
        """取某个 spec 块的引用行（spec 命中 → 实现位置，TASK-011）。"""
        rows = self._conn.execute(
            "SELECT spec_block_id, symbol_id, provenance, stale FROM spec_references"
            " WHERE spec_block_id = ? ORDER BY symbol_id",
            (spec_block_id,),
        ).fetchall()
        return [_spec_ref_from_row(r) for r in rows]

    def freshness(self) -> Freshness:
        """如实报告新鲜度：``indexed_at`` = files 表最大 indexed_at。

        ``stale_files`` / ``indexing_files`` 属同步层（Module/05）知识，本层无数据源，
        恒为空元组——不猜、不造（D-30）。
        """
        row = self._conn.execute("SELECT MAX(indexed_at) AS latest FROM files").fetchone()
        latest = None if row is None or row["latest"] is None else int(row["latest"])
        return Freshness(indexed_at=latest)

    def counts(self) -> dict[str, int]:
        """表行数快照（Phase 2 sync_status / 集成检查用，避免调用方直连 SQLite）。"""
        row = self._conn.execute(
            "SELECT (SELECT COUNT(*) FROM files) AS files,"
            " (SELECT COUNT(*) FROM chunks) AS chunks,"
            " (SELECT COUNT(*) FROM symbols) AS symbols,"
            " (SELECT COUNT(*) FROM edges) AS edges,"
            " (SELECT COUNT(*) FROM spec_blocks) AS spec_blocks,"
            " (SELECT COUNT(*) FROM unresolved_refs WHERE status = 'pending') AS refs_pending,"
            " (SELECT COUNT(*) FROM unresolved_refs WHERE status = 'failed') AS refs_failed"
        ).fetchone()
        return {key: int(row[key]) for key in row.keys()}

    def generated_files(self) -> frozenset[str]:
        """标记为 generated 的文件路径集合（TASK-REVIEW-RUNTIME P2-5）。

        供 rerank 直接读``files.generated``列，替代原先的“文件名约定代理”——旧实现该列
        硬编码为 0，无法识别靠内容 banner（如 ``DO NOT EDIT`` 头）标记的生成文件。
        一次查询取全量（典型规模下是千行级，不是热路径）。
        """
        rows = self._conn.execute(
            "SELECT path FROM files WHERE generated != 0 ORDER BY path"
        ).fetchall()
        return frozenset(str(row["path"]) for row in rows)

    # ------------------------------------------------------------------ 内部实现

    def _apply_spec_ref_cascade(
        self,
        conn: sqlite3.Connection,
        file_path: str,
        old_symbols: Sequence[sqlite3.Row],
        parsed: ParsedFile,
    ) -> None:
        """符号删除/改名 → stale=1；同名 fqn 行号漂移 → refs 重挂到新符号 id。"""
        old_by_fqn: dict[str, list[str]] = {}
        for row in old_symbols:
            old_by_fqn.setdefault(str(row["fqn"]), []).append(str(row["id"]))
        new_by_fqn: dict[str, list[str]] = {}
        for symbol in parsed.symbols:
            new_by_fqn.setdefault(symbol.fqn, []).append(
                f"{file_path}:{symbol.fqn}:{symbol.start_line}"
            )

        gone_ids = [
            symbol_id
            for fqn, ids in old_by_fqn.items()
            if fqn not in new_by_fqn
            for symbol_id in ids
        ]
        if gone_ids:
            conn.executemany(
                "UPDATE spec_references SET stale = 1 WHERE symbol_id = ?",
                [(i,) for i in gone_ids],
            )
        moves: list[tuple[str, str]] = []
        for fqn, old_ids in old_by_fqn.items():
            new_ids = new_by_fqn.get(fqn)
            if (
                new_ids is not None
                and len(new_ids) == 1
                and len(old_ids) == 1
                and new_ids[0] != old_ids[0]
            ):
                moves.append((new_ids[0], old_ids[0]))
        if moves:
            conn.executemany(
                "UPDATE spec_references SET symbol_id = ? WHERE symbol_id = ?",
                moves,
            )
        # 本文件旧 spec 块的引用行整体删除，等 TASK-006 重匹配重建（幂等前提）。
        conn.execute(
            "DELETE FROM spec_references WHERE spec_block_id IN"
            " (SELECT id FROM spec_blocks WHERE file_path = ?)",
            (file_path,),
        )

    def _insert_one_unresolved(
        self,
        conn: sqlite3.Connection,
        ref: UnresolvedRef,
        file_path: str | None,
        language: str | None,
    ) -> int:
        exists = conn.execute(
            "SELECT 1 FROM unresolved_refs WHERE from_symbol = ? AND reference_name = ?"
            " AND reference_kind = ? AND IFNULL(line, -1) = IFNULL(?, -1) LIMIT 1",
            (ref.from_fqn, ref.name, ref.kind, ref.line),
        ).fetchone()
        if exists is not None:
            return 0
        conn.execute(
            "INSERT INTO unresolved_refs(from_symbol, reference_name, reference_kind, line,"
            " file_path, language, status, name_tail) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?)",
            (ref.from_fqn, ref.name, ref.kind, ref.line, file_path, language, _name_tail(ref.name)),
        )
        return 1

    def _insert_unresolved_rows(
        self,
        conn: sqlite3.Connection,
        refs: Sequence[UnresolvedRef],
        file_path: str,
        language: str,
    ) -> None:
        seen: set[tuple[str, str, str, int]] = set()
        for ref in refs:
            key = (ref.from_fqn, ref.name, ref.kind, -1 if ref.line is None else ref.line)
            if key in seen:
                continue
            seen.add(key)
            self._insert_one_unresolved(conn, ref, file_path, language)
