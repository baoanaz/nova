-- nova index.db 冻结 DDL v1（CF-01）
-- 来源：docs/design/Module/01-切片存储.md §3.3（定稿待评审 → 契约冻结）
-- 维护者：编排者；变更必须走 docs/plan/orchestration.md §4 契约变更协议。
--
-- 与 Module/01 原文的两处规划期裁定（2026-09-10，见 docs/plan/contracts.md CF-01 备注）：
--   1. chunks_fts 采用【独立 FTS5 表】（非 external content 模式）：
--      D-20/D-45 要求索引侧写入预分词文本，与 external-content 直读 chunks.content 冲突。
--      写入器与 chunks 同事务维护；检索渲染一律用 chunks.content（原文）。
--   2. SpecBlock 采用【双表同 id】：检索单元同时写入 chunks(symbol_kind='spec_block',
--      symbol_fqn=heading_path) 与 spec_blocks(结构扩展字段)，保证单一 RRF 池与统一向量表。
--
-- 三条 hash 语义（D-43，实现见 nova_core/hashing.py）：
--   blob_hash       = sha256(path_bytes || 0x00 || content_bytes)   —— 同步协议/源码镜像
--   content_hash    = sha256(content_bytes)                          —— files 表文件级变更检测
--   chunk.content_hash = sha256(规范化切片全文)                       —— chunk 级向量复用

PRAGMA journal_mode = WAL;

CREATE TABLE index_config (
    key TEXT PRIMARY KEY,          -- parser_config_hash / embedding_model / embedding_dim / schema_version
    value TEXT NOT NULL
);

CREATE TABLE files (
    path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,        -- 文件原始内容 hash（D-43），不与 blob_hash 混用
    language TEXT NOT NULL,
    generated INTEGER DEFAULT 0,
    branch TEXT, commit_id TEXT,
    indexed_at INTEGER NOT NULL,       -- unix seconds
    parse_errors TEXT                  -- JSON 数组：解析警告（非失败；失败不建行）
);

CREATE TABLE chunks (
    id TEXT PRIMARY KEY,               -- {path}:{symbol_fqn}:{start_line}（D-04，代内唯一）
    file_path TEXT NOT NULL,
    symbol_fqn TEXT, symbol_kind TEXT, -- function/method/class_skeleton/spec_block/fallback_block
    start_line INTEGER, end_line INTEGER,
    signature TEXT, docstring TEXT,
    content TEXT NOT NULL,             -- 原文全文（渲染用）
    content_hash TEXT NOT NULL         -- D-43：chunk 级向量复用单位
);
CREATE INDEX idx_chunks_file ON chunks(file_path);
CREATE INDEX idx_chunks_kind ON chunks(symbol_kind);

CREATE TABLE symbols (
    id TEXT PRIMARY KEY,               -- {path}:{fqn}:{start_line}
    name TEXT NOT NULL, fqn TEXT NOT NULL, kind TEXT NOT NULL,
    chunk_id TEXT, file_path TEXT,
    start_line INTEGER, end_line INTEGER,
    is_exported INTEGER DEFAULT 0
);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_fqn ON symbols(fqn);

CREATE TABLE edges (
    source TEXT NOT NULL, target TEXT NOT NULL,
    kind TEXT NOT NULL,                -- calls/imports/exports/extends/implements/
                                       -- references/contains/instantiates/overrides
    line INTEGER,
    provenance TEXT DEFAULT 'parsed'   -- parsed / synthesized（cfnptr 合成等）
);
-- 边唯一性（codegraph #1034 教训：重复边污染 callers/impact）
CREATE UNIQUE INDEX idx_edges_identity
  ON edges(source, target, kind, IFNULL(line, -1));
CREATE INDEX idx_edges_target_kind ON edges(target, kind);
CREATE INDEX idx_edges_source_kind ON edges(source, kind);

CREATE TABLE unresolved_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_symbol TEXT NOT NULL,
    reference_name TEXT NOT NULL,
    reference_kind TEXT NOT NULL,
    line INTEGER, file_path TEXT, language TEXT,
    status TEXT DEFAULT 'pending',     -- pending / failed（resolved 即删行）
    name_tail TEXT DEFAULT ''          -- 'util.greet'→'greet'，failed 重试的匹配键
);
CREATE INDEX idx_unresolved_failed ON unresolved_refs(name_tail) WHERE status='failed';

CREATE TABLE spec_blocks (
    id TEXT PRIMARY KEY,               -- {path}:{heading_path}:{start_line}，与同名 chunks 行同 id
    file_path TEXT NOT NULL,
    doctype TEXT NOT NULL,             -- agent-instructions/readme/design/adr/api/changelog/guide
    heading TEXT, heading_path TEXT,   -- "架构 > 认证模块 > token 刷新流程"（spec fqn）
    heading_level INTEGER,
    start_line INTEGER, end_line INTEGER,
    content TEXT NOT NULL,
    code_fences TEXT                   -- JSON 数组 [{lang, content, line}]，V1 只存不解析
);
CREATE INDEX idx_spec_doctype ON spec_blocks(doctype);

CREATE TABLE spec_references (
    spec_block_id TEXT NOT NULL,
    symbol_id TEXT NOT NULL,
    provenance TEXT DEFAULT 'inferred',-- 永远 inferred（D-06），永不升级为强事实
    stale INTEGER DEFAULT 0            -- symbol 删除/改名时增量期级联置 1
);
CREATE INDEX idx_spec_refs_symbol ON spec_references(symbol_id);
CREATE INDEX idx_spec_refs_stale ON spec_references(spec_block_id) WHERE stale = 1;

-- 独立 FTS5 表（见文件头裁定 1）：writer 写入 jieba 预分词后空格连接的文本（D-20/D-45）。
-- 检索侧查询串走同一分词器；渲染/证据正文一律用 chunks.content。
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    content_seg, signature_seg, docstring_seg, file_path UNINDEXED,
    tokenize='unicode61'
);

-- 向量库（LanceDB，per-project vectors/ 目录，D-03）：
--   表 chunk_vectors(chunk_id TEXT PK, content_hash TEXT, vector fixed_size_list<float32>[dim])，
--   embedding 模型变更 → 整表重建（D-07 二级失效），模型指纹记于 index_config。
