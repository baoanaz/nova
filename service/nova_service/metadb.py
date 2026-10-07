"""``nova-meta.db``：服务侧的账户、API Key、项目归属、索引历史与查询审计（TASK-060/061/062/064）。

**为什么单独一个库**：``~/.nova/projects/{id}/index.db`` 是 core 的（纯库纪律 D-34：core 字典里
只有 project，没有用户）；用户体系是**外壳**的概念，因此落在同级目录的 ``nova-meta.db``
（Module/06 §4-A 已声明该文件名）。

口径（本模块冻结）：

- **纯标准库**：``sqlite3`` 直写，不引 ORM、不引 pydantic-settings（与全仓风格一致）；
- **每进程一个连接表**（``threading.local``）：``sqlite3`` 连接不可跨线程共享，而 FastAPI 会把
  同步 handler 丢进线程池；因此按线程开连接，连接级别 ``WAL`` + ``foreign_keys=ON``；
- **时间一律 Unix 秒**（与全仓一致），测试用显式传入的 ``now`` 而非冻结时钟；
- **失败不影响主路径**：索引历史与查询审计都是**旁路**，调用方（``indexer`` / ``routers``）
  必须自己 try/except，不因为"记不上账"而让索引或检索失败。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from nova_service.roles import EARLY_MEMBER_MAX, ROLE_ADMIN, ROLE_BETA, normalize_role, title_for

__all__ = [
    "CALL_TIMELINE_LIMIT",
    "META_DB_FILENAME",
    "IndexRun",
    "IndexStats",
    "LlmConfigRecord",
    "MetaDB",
    "QueryAuditRecord",
    "UsageSummary",
    "User",
]

#: 元数据库文件名（Module/06 §4-A：与 ``projects/`` 同级）。
META_DB_FILENAME = "nova-meta.db"

#: 每项目保留的索引历史条数（超出按 finished_at 裁掉最旧的）。
INDEX_RUN_KEEP = 500
#: 每项目保留的查询审计条数（Module/04 §8 冻结值）。
QUERY_AUDIT_KEEP = 1000
#: 一次 Tool 调用的时间线上限（``GET /api/calls/{callId}``；防止极端情况下一次调用拖出巨量行）。
CALL_TIMELINE_LIMIT = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at    INTEGER NOT NULL,
  is_local      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at   INTEGER NOT NULL,
  expires_at   INTEGER NOT NULL,
  last_seen_at INTEGER
);

CREATE TABLE IF NOT EXISTS api_tokens (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name         TEXT NOT NULL DEFAULT '',
  prefix       TEXT NOT NULL,
  token_hash   TEXT NOT NULL UNIQUE,
  created_at   INTEGER NOT NULL,
  last_used_at INTEGER,
  revoked_at   INTEGER,
  is_custom    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON api_tokens(user_id);

CREATE TABLE IF NOT EXISTS projects (
  project_id   TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  display_name TEXT NOT NULL DEFAULT '',
  created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);

CREATE TABLE IF NOT EXISTS index_runs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id      TEXT NOT NULL,
  state           TEXT NOT NULL,
  started_at      INTEGER NOT NULL,
  finished_at     INTEGER NOT NULL,
  duration_ms     INTEGER NOT NULL,
  files_total     INTEGER NOT NULL DEFAULT 0,
  files_processed INTEGER NOT NULL DEFAULT 0,
  chunks          INTEGER NOT NULL DEFAULT 0,
  errors          INTEGER NOT NULL DEFAULT 0,
  error_text      TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON index_runs(project_id, finished_at DESC);

CREATE TABLE IF NOT EXISTS query_audit (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id        TEXT NOT NULL,
  user_id           TEXT,
  mode              TEXT NOT NULL,
  query             TEXT NOT NULL,
  answerable        INTEGER,
  confidence        TEXT,
  degraded          INTEGER NOT NULL DEFAULT 0,
  latency_ms        INTEGER NOT NULL,
  evidence_count    INTEGER NOT NULL DEFAULT 0,
  docs_count        INTEGER NOT NULL DEFAULT 0,
  used_tokens       INTEGER NOT NULL DEFAULT 0,
  citation_coverage REAL,
  llm_latency_ms    INTEGER,
  answer_tokens     INTEGER,
  request_id        TEXT,
  answer_text       TEXT,
  answer_status     TEXT,
  evidence_json     TEXT NOT NULL DEFAULT '[]',
  created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_project ON query_audit(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS user_llm_config (
  user_id     TEXT PRIMARY KEY,
  model       TEXT NOT NULL,
  base_url    TEXT NOT NULL,
  api_key     TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);

-- TASK-110 §3.1：邀请码（码面 6 位大写字母，首字母即类型 A/B/C）。
--
-- `used_count < max_uses` 的判定**必须在 UPDATE 的 WHERE 里**（见 consume_invite）：
-- 先 SELECT 再 UPDATE 会在并发下超发，那是本表唯一真正要防的错误。
CREATE TABLE IF NOT EXISTS invites (
  code         TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  created_by   TEXT,
  created_at   INTEGER NOT NULL,
  expires_at   INTEGER,
  revoked_at   INTEGER,
  max_uses     INTEGER NOT NULL DEFAULT 1,
  used_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_invites_kind ON invites(kind, created_at DESC);

-- 谁用了哪个码（后台邀请码模块的"使用记录"）。
-- 主键 (code, user_id) 同时是幂等保证：同一个用户重复核销同一个码不会产生第二行。
CREATE TABLE IF NOT EXISTS invite_uses (
  code      TEXT NOT NULL,
  user_id   TEXT NOT NULL,
  used_at   INTEGER NOT NULL,
  PRIMARY KEY (code, user_id)
);
CREATE INDEX IF NOT EXISTS idx_invite_uses_user ON invite_uses(user_id, used_at DESC);
"""

#: schema 模板文件的缓存路径（TASK-112）。模板是本模块 ``_SCHEMA`` + ``_migrate`` 的产物，
#: 因此不会与 schema 定义漂移；进程重启后首次建库时重建一次（~150 ms，只付一次）。
#: 放在临时目录：它是**纯派生数据**，丢了只是下次重算，不进用户数据目录。
_SCHEMA_TEMPLATE: Path | None = None
_SCHEMA_TEMPLATE_LOCK = threading.Lock()


def _build_schema_template(path: Path) -> None:
    """在 ``path`` 上跑一次完整的建表 + 迁移（模板的唯一来源）。"""
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


def _template_path() -> Path:
    """创建进程唯一的模板路径，避免 PID 复用命中旧文件。"""
    fd, raw_path = tempfile.mkstemp(prefix="nova-meta-schema-", suffix=".db")
    os.close(fd)
    return Path(raw_path)


def _copy_schema_template(target: Path) -> bool:
    """把已建好的 schema 模板拷到 ``target``；成功返回 ``True``。

    **失败一律返回 ``False``，由调用方走原来的建表路径**——模板只是加速手段，
    它不可用（磁盘满、临时目录只读、并发拷贝失败）时不得让建库失败。
    """
    global _SCHEMA_TEMPLATE
    try:
        with _SCHEMA_TEMPLATE_LOCK:
            if _SCHEMA_TEMPLATE is None or not _SCHEMA_TEMPLATE.exists():
                candidate = _template_path()
                _build_schema_template(candidate)
                _SCHEMA_TEMPLATE = candidate
            shutil.copyfile(_SCHEMA_TEMPLATE, target)
        return True
    except OSError:
        return False
    except sqlite3.Error:
        # 模板本身损坏（磁盘异常/半写）：丢掉它，下次重建，本次退原路径。
        _SCHEMA_TEMPLATE = None
        return False

#: ``user_llm_config`` 的**增量列**（TASK-113）：与其它表共用 :func:`_add_columns` 路径。
#:
#: 旧行（升级前配过 LLM 的用户）该列为 ``NULL`` → ``llmconfig._user_protocol`` 回落服务端
#: 默认。为什么不回填 ``'openai'``：升级前那行配置本来就跑在 OpenAI 协议上，回填与回落
#: 的结果一样；但**不回填**能让“用户从未选过协议”与“用户明确选了 openai”在库里可区分，
#: 将来若服务端默认改成别的协议，前者会跟着走（符合“没表态就听默认”的语义）。
_USER_LLM_CONFIG_COLUMNS: tuple[tuple[str, str], ...] = (
    #: 上游协议：'openai' | 'responses' | 'anthropic'（取值域见 ``nova_service.llmprotocol``）。
    ("protocol", "TEXT"),
)

#: ``users`` 的**增量列**（TASK-110 §3.1）：与 :data:`_AUDIT_COLUMNS` 同一套 ALTER 路径。
#:
#: 旧库里没有这些列——TASK-110 之前的账户全是"谁都能注册"的平权用户，迁移后它们
#: 一律是 ``role='public'``（**旧用户不受影响**：登录 + MCP 调用照常，只是多了头衔展示）。
#:
#: 为什么 ``role`` 带 ``NOT NULL DEFAULT 'public'`` 而在 :data:`_SCHEMA` 里不写：``ALTER TABLE
#: ADD COLUMN`` 不允许加一个无默认值的 ``NOT NULL`` 列（既有行填什么？），带 ``DEFAULT``
#: 才能一次成功；且 SQLite 会把默认值应用到既有行，因此**不需要额外的 UPDATE 回填**。
_USER_COLUMNS: tuple[tuple[str, str], ...] = (
    #: 'admin' | 'beta' | 'public'（取值域见 ``nova_service.roles``）。
    ("role", "TEXT NOT NULL DEFAULT 'public'"),
    #: 头衔冗余快照（展示/后台列表用）；**权威在** ``roles.TITLE_BY_ROLE``。
    ("title", "TEXT"),
    #: 内测编号（仅前 ``roles.EARLY_MEMBER_MAX`` 名内测玩家有值；其余为 NULL）。
    ("early_member_no", "INTEGER"),
    #: 单人配额覆盖（后台给某个人单独改的）；NULL = 按 ``roles.QUOTA_BY_ROLE``。
    ("quota_bytes", "INTEGER"),
    #: 封禁时刻（非空即封禁；校验凭据时检查它，因此封禁**立即生效**，不等重新登录）。
    ("banned_at", "INTEGER"),
    #: 最后活跃时间（后台用户模块展示；每次凭据校验时刷新）。
    ("last_seen_at", "INTEGER"),
    #: 顺序号（所有人都有，从 1 开始）。控制台展示为 ``ID #001``。
    #:
    #: 与 ``early_member_no`` 的区别：那个是**内测收藏品编号**（只发前 100 名，降级时会被清掉），
    #: 这个是**全站注册顺序**（永不变、永不复用）。两者同时存在是因为它们回答不同的问题：
    #: “我是不是最早那批内测”与“我是第几个用户”。
    ("user_no", "INTEGER"),
)

#: ``api_tokens`` 的**增量列**（TASK-110 §3.4）：自定义 Key 标记（审计用）。
_TOKEN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("is_custom", "INTEGER NOT NULL DEFAULT 0"),
)

#: 迁移后要确保存在的索引（**必须在 ALTER 之后建**：旧库里还没有该列，放在 ``_SCHEMA`` 里会在
#: ``executescript`` 阶段以 ``no such column: request_id`` 直接失败——这正是 TASK-094 §C 实测踩到的
#: 同一类坑）。
_AUDIT_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_audit_request ON query_audit(request_id)",
)

#: 迁移后要确保存在的**唯一索引**（旧行回填之后再建，否则 NULL 会随之多个而冲突）。
_UNIQUE_INDEXES: tuple[str, ...] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_user_no ON users(user_no)",
)


#: ``index_runs`` 的**增量列**（TASK-099 §B）：与 :data:`_AUDIT_COLUMNS` 同一套 ALTER 路径。
#:
#: 为什么不改上面的 ``CREATE TABLE``：旧库（TASK-062 起就已存在）打开新代码时
#: ``CREATE TABLE IF NOT EXISTS`` 对既有表是空操作，缺列会在**第一次 SELECT/INSERT** 时
#: 以 ``no such column: call_id`` 直接失败（TASK-094 §C 实测踩过同一坑）。
_INDEX_RUN_COLUMNS: tuple[tuple[str, str], ...] = (
    #: 同一次 Tool 调用的 id（TASK-099 §B-3）：客户端复用 ``X-Request-Id`` 头承载，
    #: 因此它与同一次调用里 ``query_audit.request_id`` 的值**天然相等**。
    #: ``NULL`` = 旧客户端未带头 / 旧版本写入的记录（不填编造的 id）。
    ("call_id", "TEXT"),
)


#: ``query_audit`` 的**增量列**（TASK-088 §E 引入本模块的第一条 ALTER 路径；TASK-094 §C 追加
#: ``request_id``；TASK-099 §A 追加 ``answer_text`` / ``answer_status``）：``(列名, 列类型)``。
#:
#: 为什么要迁移而不是只改 DDL：TASK-084 已经在用户机上建好了表，而 ``CREATE TABLE IF NOT
#: EXISTS`` 对**既有库**毫无作用（表已存在）。本模块原先没有 ALTER 先例，因此这里建一条最小
#: 安全路径：``PRAGMA table_info`` 检查后再 ``ALTER TABLE ADD COLUMN``——可重复执行、
#: **只加列不改列**；新列全部可空，旧行留 ``NULL``（"没测过"就是 ``NULL``，不编造 0）。
#:
#: ``request_id`` 为 ``NULL`` 表示"这条审计来自落库时还没有 requestId 的旧版本"或"调用方
#: 没绑定"（如离线批量写库），**不填一个编造的 id**——历史页据此显示 ``—``。
_AUDIT_COLUMNS: tuple[tuple[str, str], ...] = (
    #: LLM 调用耗时（毫秒）；未接 LLM 的降级路径为 NULL。
    ("llm_latency_ms", "INTEGER"),
    #: 答案 token 估算；降级路径为 NULL。
    ("answer_tokens", "INTEGER"),
    #: 请求 trace id（TASK-094 §C：与响应头 ``X-Request-Id`` / 日志的 ``requestId`` 同源）。
    ("request_id", "TEXT"),
    #: LLM 答案正文（TASK-099 §A）；**没走 LLM** 时为 NULL（详见 ``record_query`` 的
    #: ``answer_text`` 参数说明）。落库而非事后重算：LLM 输出不可复现（同输入可能不同答案，
    #: 且 provider 可能已换），"事后补算"是幻觉。
    ("answer_text", "TEXT"),
    #: 答案状态（TASK-099 §A）：``answered`` / ``insufficient_evidence`` / ``degraded``。
    #: 与 ``answer_text`` 分开存的原因：**"没调 LLM"（短路）与"调了但答案是空"是两件事**，
    #: 只有状态列能把它们区分开（前者 ``insufficient_evidence`` + NULL）。
    ("answer_status", "TEXT"),
)


@dataclass(frozen=True, slots=True)
class User:
    """账户（``is_local`` = 本地单用户模式的隐式账户，无密码可用）。

    TASK-110 追加 ``role`` / ``title`` / ``early_member_no`` / ``quota_bytes`` / ``banned_at`` /
    ``last_seen_at``：它们在**每一处凭据解析**（session / token）时都要用上（封禁检查、
    角色→能力位），因此必须随 ``User`` 一起返回，而不是每个调用方自己再查一次库。

    默认值给成"公测 + 无编号 + 未封禁"：``auth.local_user()`` 与单测里手搭的 ``User`` 因此
    不需要改一行——本地模式本来就该享有最低档位（它没有账户体系，谈不上特权）。
    """

    id: str
    name: str
    created_at: int
    is_local: bool = False
    #: 'admin' | 'beta' | 'public'（``nova_service.roles``）；旧库行由迁移补成 ``public``。
    role: str = "public"
    #: 头衔冗余快照（权威在 ``roles.TITLE_BY_ROLE``）。
    title: str | None = None
    #: 内测编号（仅前 100 名内测玩家有值）。
    early_member_no: int | None = None
    #: 单人配额覆盖（NULL = 按角色默认）。
    quota_bytes: int | None = None
    #: 封禁时刻（非空即封禁）。
    banned_at: int | None = None
    #: 最后活跃时间。
    last_seen_at: int | None = None
    #: 全站注册顺序号（从 1 开始；控制台展示为 ``ID #001``）。
    user_no: int | None = None

    @property
    def banned(self) -> bool:
        return self.banned_at is not None

    def to_json(self, *, quota_bytes: int | None = None) -> dict[str, Any]:
        """账户的**展示面**（不含密码哈希、不含任何 secret）。

        ``role`` / ``title`` / ``earlyMemberNo`` 与 ``capabilities`` 一起构成前端的身份视图
        （TASK-110 §3.3）；后者由 ``roles.capabilities_for`` 生成，此处不重复实现——
        "头衔与权限同源"。

        ``quota_bytes`` 是**该用户实际生效**的上限，由调用方算好传入（本地模式/无账户时
        走 ``Settings`` 兜底，只有路由层能算出那个值）；缺省时退化为角色默认值
        （旧调用方/单测：它们没有配额这个概念，不应该因此报错）。
        """
        from nova_service.roles import capabilities_for, quota_bytes_for

        limit = (
            quota_bytes
            if quota_bytes is not None
            else quota_bytes_for(self.role, override=self.quota_bytes)
        )
        return {
            "userId": self.id,
            "name": self.name,
            "createdAt": self.created_at,
            "isLocal": self.is_local,
            "role": self.role,
            "title": title_for(self.role),
            "earlyMemberNo": self.early_member_no,
            #: 全站顺序号（所有人都有）。与 ``earlyMemberNo`` 并存：后者是内测收藏品编号。
            "userNo": self.user_no,
            "banned": self.banned,
            "capabilities": capabilities_for(
                self.role,
                early_member_no=self.early_member_no,
                quota_bytes=limit,
            ),
        }


@dataclass(frozen=True, slots=True)
class IndexRun:
    """一次索引的记录（``running`` 不落库：服务被杀不会留下永远不结束的幽灵行）。"""

    run_id: int
    project_id: str
    state: str
    started_at: int
    finished_at: int
    duration_ms: int
    files_total: int
    files_processed: int
    chunks: int
    errors: int
    error_text: str | None
    #: 同一次 Tool 调用的 id（TASK-099 §B）；``None`` = 旧客户端未带 ``X-Request-Id``。
    call_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "projectId": self.project_id,
            "state": self.state,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "durationMs": self.duration_ms,
            "filesTotal": self.files_total,
            "filesProcessed": self.files_processed,
            "chunks": self.chunks,
            "errors": self.errors,
            "error": self.error_text,
            # TASK-099 §B：与 ``query_audit.requestId`` 同值，前端按它把一次调用的
            # N 次初始化 + 1 次检索归成一组（NULL → 每行独立展示）。
            "callId": self.call_id,
        }


@dataclass(frozen=True, slots=True)
class IndexStats:
    """索引统计（``avgDurationMs`` **只统计成功的 run**——失败 run 的耗时是"失败得多快"）。"""

    total: int
    succeeded: int
    failed: int
    avg_duration_ms: int | None
    min_duration_ms: int | None
    max_duration_ms: int | None
    last_run_at: int | None
    last_state: str | None
    recent: tuple[IndexRun, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "avgDurationMs": self.avg_duration_ms,
            "minDurationMs": self.min_duration_ms,
            "maxDurationMs": self.max_duration_ms,
            "lastRunAt": self.last_run_at,
            "lastState": self.last_state,
            "recent": [run.to_json() for run in self.recent],
        }


@dataclass(frozen=True, slots=True)
class QueryAuditRecord:
    """一条查询审计（**不含源码内容**：04 §8 冻结"只存 evidence 元数据"）。"""

    query_id: int
    project_id: str
    mode: str
    query: str
    answerable: bool | None
    confidence: str | None
    degraded: bool
    latency_ms: int
    evidence_count: int
    docs_count: int
    used_tokens: int
    citation_coverage: float | None
    llm_latency_ms: int | None
    answer_tokens: int | None
    #: 请求 trace id（TASK-094 §C）；``None`` = 这条记录落库时没有 requestId（旧版本/未绑定）。
    request_id: str | None
    created_at: int
    #: LLM 答案正文（TASK-099 §A）；``None`` = **没走 LLM**（证据不足短路 / 未配置 / 调用失败）。
    answer_text: str | None = None
    #: 答案状态（TASK-099 §A）：``answered`` / ``insufficient_evidence`` / ``degraded``。
    answer_status: str | None = None
    #: 证据清单（TASK-107）：**不含源码正文**的 ``[{id, path, lines, tier, score}]``。
    #:
    #: 这是历史页"Tool 输出"对 search_context 的可展示内容——它如实反映工具**返回了什么
    #: 证据**（编号/路径/行号/分层/分数），而不破 Module/04 §8 的"审计不含源码内容"。
    #: 落库侧一直有写（``evidence_json``），本字段把它读出来交给前端。
    evidence: tuple[dict[str, Any], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "queryId": self.query_id,
            "projectId": self.project_id,
            "mode": self.mode,
            "query": self.query,
            "answerable": self.answerable,
            "confidence": self.confidence,
            "degraded": self.degraded,
            "latencyMs": self.latency_ms,
            "evidenceCount": self.evidence_count,
            "docsCount": self.docs_count,
            "usedTokens": self.used_tokens,
            "citationCoverage": self.citation_coverage,
            # TASK-088 §E：LLM 耗时与答案 token（未走 LLM 的请求为 None，"没测过"不是 0）。
            "llmLatencyMs": self.llm_latency_ms,
            "answerTokens": self.answer_tokens,
            # TASK-094 §C：历史页展示它，用户报错时拿它去查服务端日志（TASK-090 的端点）。
            "requestId": self.request_id,
            # TASK-099 §A：历史页弹窗的「输出」列。answerStatus 与 answerText 分开给，
            # 前端才能区分"没调 LLM"（insufficient_evidence + null）与"调了得到空答案"。
            "answerText": self.answer_text,
            "answerStatus": self.answer_status,
            # TASK-107：search_context 的"Tool 输出"= 真实返回的证据清单（不含源码正文）。
            "evidence": [dict(item) for item in self.evidence],
            "createdAt": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class LlmConfigRecord:
    """用户级 LLM 配置（TASK-099 §C-2）。

    **``api_key`` 是明文**（卡内 §C-2 的裁定：单用户自部署下 DB 与 ``.env`` 同一信任域，
    加密不增加实际安全性）。因此本对象**只在服务端内部流转**：
    任何 HTTP 响应都不得包含它（连长度、前缀都不行），日志与错误走 ``redact_text``。
    """

    user_id: str
    model: str
    base_url: str
    api_key: str
    created_at: int
    updated_at: int

    #: 上游协议（TASK-113 / D-47）；``None`` = 旧行（迁移前写入）→ 回落服务端默认。
    protocol: str | None = None

    def to_json(self) -> dict[str, Any]:
        """对外形态：**只有 "key 已配置" 这个布尔**，不含 key 本身或其任何可测量属性。"""
        return {
            "model": self.model,
            "baseUrl": self.base_url,
            "apiKeyConfigured": bool(self.api_key),
            "protocol": self.protocol,
            "updatedAt": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class UsageSummary:
    """用量聚合（``citationCoverageAvg`` 在 LLM 接入前恒为 ``None``——"尚未测量"不是 0）。"""

    total: int
    succeeded: int
    insufficient: int
    failed: int
    avg_latency_ms: int | None
    p95_latency_ms: int | None
    confidence_distribution: Mapping[str, int]
    citation_coverage_avg: float | None
    top_queries: tuple[tuple[str, int], ...] = ()
    recent: tuple[QueryAuditRecord, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "insufficient": self.insufficient,
            "failed": self.failed,
            "avgLatencyMs": self.avg_latency_ms,
            "p95LatencyMs": self.p95_latency_ms,
            "confidenceDistribution": dict(self.confidence_distribution),
            "citationCoverageAvg": self.citation_coverage_avg,
            "topQueries": [{"query": q, "count": n} for q, n in self.top_queries],
            "recent": [record.to_json() for record in self.recent],
        }


class MetaDB:
    """``nova-meta.db`` 的访问层（每线程一个连接）。

    用法：``db = MetaDB.open(data_root / META_DB_FILENAME)``；连接按线程懒建，
    因此可以在 FastAPI 线程池与后台索引线程里直接用。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False

    # ------------------------------------------------------------------ 打开与连接

    @classmethod
    def open(cls, path: str | Path) -> MetaDB:
        """打开并确保 schema 就绪（幂等；并发首个请求由 ``_init_lock`` 串行）。"""
        db = cls(path)
        db._ensure_schema()
        return db

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_schema(self) -> None:
        """建库（或在旧库上跑迁移），幂等。

        TASK-112：**新库**走模板拷贝。实测每建一个空库要 157 ms，其中 ~155 ms 是
        “把 27 个 schema 对象写到磁盘并 fsync”而不是计算（同样语句在内存库上只要 2.9 ms）；
        而全仓 1100+ 用例里有 ~900 个各建一次库，光这一项就是 ~2 分钟。
        从**同一个 schema 代码生成**的模板文件拷贝过来（1.3 ms）语义完全等价：
        模板就是本函数的产物，不手写、不会与 ``_SCHEMA``/``_migrate`` 漂移。

        为什么只对“文件不存在”的快路径生效：**已存在**的库必须照原样打开
        （它可能是旧版本建的，必须走 ``_migrate`` 补列），拷贝会覆盖用户数据——
        这里只在“从零建库”时替换实现，不改变任何已有库的行为。
        """
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            copied = False
            if not self._path.exists():
                copied = _copy_schema_template(self._path)
            with self._connect() as conn:
                if not copied:
                    # 已有库可能缺少后来新增的表；模板不可用时也走原始建表路径。
                    conn.executescript(_SCHEMA)
                _migrate(conn)
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), timeout=10.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            # WAL：读写并发（索引线程写 run、HTTP 线程读统计）不互相阻塞。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------ 账户（TASK-060）

    def create_user(
        self,
        name: str,
        password_hash: str,
        *,
        is_local: bool = False,
        role: str | None = None,
        early_member_no: int | None = None,
        now: int | None = None,
    ) -> User:
        """创建账户（``name`` 冲突抛 :class:`sqlite3.IntegrityError`，HTTP 层转 409）。

        ``role``（TASK-110）：不传则不写该列（走 DDL 的 ``DEFAULT 'public'``）；
        传了则同时写 ``title`` 冗余快照（两个字段一次落库，不会出现"角色是 beta 而头衔是旅人"）。
        ``early_member_no`` 只对 ``beta`` 有意义，调用方负责分配
        （见 :meth:`next_early_member_no`）。

        ``user_no``（全站顺序号）在**同一事务内**自增取号（``MAX(user_no)+1``），
        因此并发注册不会发重号；由唯一索引兼底。
        """
        created = int(now if now is not None else time.time())
        user_id = secrets.token_hex(16)
        resolved = normalize_role(role) if role is not None else None
        columns = "id, name, password_hash, created_at, is_local"
        values: list[Any] = [user_id, name, password_hash, created, 1 if is_local else 0]
        if resolved is not None:
            columns += ", role, title"
            values += [resolved, title_for(resolved)]
        if early_member_no is not None:
            columns += ", early_member_no"
            values.append(int(early_member_no))
        with self._write() as conn:
            user_no = _next_user_no(conn)
            columns += ", user_no"
            values.append(user_no)
            placeholders = ", ".join("?" for _ in values)
            conn.execute(f"INSERT INTO users ({columns}) VALUES ({placeholders})", values)
        return User(
            id=user_id,
            name=name,
            created_at=created,
            is_local=is_local,
            role=resolved or "public",
            title=title_for(resolved or "public"),
            early_member_no=early_member_no,
            user_no=user_no,
        )

    def get_user_by_name(self, name: str) -> tuple[User, str] | None:
        """``(user, password_hash)``；不存在返回 ``None``。"""
        row = self._connect().execute(
            "SELECT * FROM users WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return _user(row), str(row["password_hash"])

    def get_user(self, user_id: str) -> User | None:
        row = self._connect().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return _user(row) if row is not None else None

    def user_count(self) -> int:
        row = self._connect().execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"])

    def ensure_local_user(self, name: str = "local", *, now: int | None = None) -> User:
        """取（或建）本地单用户模式的隐式账户（``is_local=1``）。

        为什么需要它：TASK-061 §B 要求 ``attach`` 把归属登记给本地用户，而
        :func:`nova_service.auth.local_user` 是**不落库的幻影**（R34）——``projects.user_id``
        有指向 ``users(id)`` 的外键，没有真实行就写不进归属。

        只在调用方已经决定要写归属时使用（即本地模式且 app 级 ``MetaDB`` 已存在，见
        ``routers/projects._claim_project``）；**不在此处建库**（R34：本地模式默认无
        ``nova-meta.db``）。
        ``password_hash`` 恒为空串：本地隐式账户**没有密码**，也不得能用密码登录
        （``verify_password`` 对非法编码串返回 ``False``）。
        """
        existing = self.get_user_by_name(name)
        if existing is not None:
            return existing[0]
        try:
            return self.create_user(name, "", is_local=True, now=now)
        except sqlite3.IntegrityError:  # 并发首次 attach：另一个线程刚建好，取回它
            raced = self.get_user_by_name(name)
            if raced is None:  # pragma: no cover - 仅当行被并发删除才会走到
                raise
            return raced[0]

    # ------------------------------------------------------------------ 会话（TASK-060）

    def create_session(self, user_id: str, *, ttl_s: int, now: int | None = None) -> str:
        created = int(now if now is not None else time.time())
        session_id = secrets.token_hex(32)
        with self._write() as conn:
            conn.execute(
                "INSERT INTO sessions (id, user_id, created_at, expires_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, user_id, created, created + ttl_s, created),
            )
        return session_id

    def resolve_session(self, session_id: str, *, now: int | None = None) -> User | None:
        """有效则返回用户并刷新 ``last_seen_at``；过期则删除并返回 ``None``。

        TASK-110：返回的是**完整**的用户行（角色/封禁状态），因为调用方（``auth.authenticate``）
        要据此判两件事——是否管理员、是否已封禁。封禁判定在调用方做（返 ``None`` 会让
        "封禁"与"会话过期"在日志里无法区分）。
        """
        current = int(now if now is not None else time.time())
        conn = self._connect()
        row = conn.execute(
            "SELECT s.expires_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        if int(row["expires_at"]) <= current:
            with self._write() as writer:
                writer.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return None
        user = _user(row)
        with self._write() as writer:
            writer.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE id = ?", (current, session_id)
            )
            writer.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (current, user.id))
        return replace(user, last_seen_at=current)

    def delete_session(self, session_id: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    # ------------------------------------------------------------------ API Key（TASK-060）

    def create_token(
        self,
        user_id: str,
        *,
        token_hash: str,
        prefix: str,
        name: str = "",
        is_custom: bool = False,
        now: int | None = None,
    ) -> str:
        """登记一个 API Key 的哈希（明文只在创建响应里出现一次，**不落库**）。

        TASK-110 §3.4：``is_custom`` 标记这是用户自定义的 Key（拓荒者特权）。它与随机 Key 走
        **同一条签发路径**（同一张表、同一个唯一索引、同一套校验）——差异只是"明文谁选的"，
        因此没有任何理由把它做成分支式的第二套逻辑。记录该标记纯粹为了审计：
        自定义 Key 熵更低，出问题时先看它们。

        ``token_hash`` 冲突抛 :class:`sqlite3.IntegrityError`（唯一索引），HTTP 层转 409。
        """
        created = int(now if now is not None else time.time())
        token_id = secrets.token_hex(16)
        with self._write() as conn:
            conn.execute(
                "INSERT INTO api_tokens (id, user_id, name, prefix, token_hash, created_at,"
                " is_custom) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_id, user_id, name, prefix, token_hash, created, 1 if is_custom else 0),
            )
        return token_id

    def list_tokens(self, user_id: str) -> list[dict[str, Any]]:
        """该用户的**有效** Key（已撤销的不列；**绝不含明文或哈希**）。"""
        rows = self._connect().execute(
            "SELECT id, name, prefix, created_at, last_used_at, is_custom FROM api_tokens"
            " WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "name": str(row["name"]),
                "prefix": str(row["prefix"]),
                "createdAt": int(row["created_at"]),
                "lastUsedAt": row["last_used_at"],
                # TASK-110 §3.4：前端据此在列表里区分"随机生成"与"自定义"（自定义 Key 属特权，
                # 用户会想知道哪一把是自己选的）。
                "isCustom": bool(row["is_custom"]),
            }
            for row in rows
        ]

    def find_user_by_token_hash(
        self, token_hash: str, *, now: int | None = None
    ) -> User | None:
        """按哈希反查用户（仅有效 Key；命中即刷新 ``last_used_at``）。

        有意**不过滤 ``banned_at``**：封禁判定统一在 ``auth.authenticate`` 里做
        （否则 `无此 Key` 与 `已封禁` 在调用方无法区分，而审计与排查需要这个区别）。
        返回值是完整用户行（TASK-110：角色与封禁状态都要用）。
        """
        current = int(now if now is not None else time.time())
        row = self._connect().execute(
            "SELECT t.id AS token_id, u.* FROM api_tokens t"
            " JOIN users u ON u.id = t.user_id"
            " WHERE t.token_hash = ? AND t.revoked_at IS NULL",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        with self._write() as conn:
            conn.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (current, row["token_id"])
            )
            conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (current, row["id"]))
        return replace(_user(row), last_seen_at=current)

    def revoke_token(self, user_id: str, token_id: str, *, now: int | None = None) -> bool:
        """软删（只撤销**自己的** Key；不存在/非本人 → ``False``）。"""
        revoked = int(now if now is not None else time.time())
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE api_tokens SET revoked_at = ?"
                " WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                (revoked, token_id, user_id),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------ 邀请码（TASK-110 §3.1）

    def create_user_with_invite(
        self,
        name: str,
        password_hash: str,
        *,
        code: str,
        now: int | None = None,
    ) -> tuple[User, str]:
        """**原子地**核销邀请码 + 创建账户（TASK-110 注册的唯一入口）。

        为什么不拆成"先 redeem 再 create_user"：那样一定有一个时刻"码已消耗、账户还没建"，
        只要中间一步失败（重名、磁盘满、进程被杀），用户就拿着一张废码而无处申诉。
        两件事必须同生共死，因此它们在这个方法里共用一个 ``BEGIN IMMEDIATE``。

        返回 ``(user, kind)``；码不可用抛 :class:`nova_service.invites.InviteRejected`，
        重名抛 :class:`sqlite3.IntegrityError`（事务回滚，码不会被浪费）。

        内测编号在同一事务内取号（``MAX(early_member_no) + 1``，上限
        :data:`nova_service.roles.EARLY_MEMBER_MAX`），因此并发注册不会发重号；
        超过上限则如实为 ``NULL``（用户要求：第 101 名起不再发编号）。
        """
        from nova_service.invites import (
            REJECT_EXHAUSTED,
            REJECT_EXPIRED,
            REJECT_REVOKED,
            REJECT_UNKNOWN,
            InviteRejected,
        )
        from nova_service.roles import KIND_TO_ROLE

        current = int(now if now is not None else time.time())
        user_id = secrets.token_hex(16)
        with self._write() as conn:
            row = conn.execute(
                "SELECT kind, revoked_at, expires_at, used_count, max_uses FROM invites"
                " WHERE code = ?",
                (code,),
            ).fetchone()
            if row is None:
                raise InviteRejected(code, REJECT_UNKNOWN)
            if row["revoked_at"] is not None:
                raise InviteRejected(code, REJECT_REVOKED)
            if row["expires_at"] is not None and int(row["expires_at"]) <= current:
                raise InviteRejected(code, REJECT_EXPIRED)
            # 核销与账户创建在同一事务里，因此这里不需要"先查再用"的并发顾虑。
            cursor = conn.execute(
                "UPDATE invites SET used_count = used_count + 1"
                " WHERE code = ? AND revoked_at IS NULL"
                "   AND (expires_at IS NULL OR expires_at > ?)"
                "   AND used_count < max_uses",
                (code, current),
            )
            if cursor.rowcount != 1:
                raise InviteRejected(code, REJECT_EXHAUSTED)
            kind = str(row["kind"])
            role = normalize_role(KIND_TO_ROLE.get(kind))
            member_no: int | None = None
            if role == ROLE_BETA:
                top = conn.execute(
                    "SELECT MAX(early_member_no) AS n FROM users"
                    " WHERE early_member_no IS NOT NULL"
                ).fetchone()
                candidate = 1 if top is None or top["n"] is None else int(top["n"]) + 1
                member_no = candidate if candidate <= EARLY_MEMBER_MAX else None
            conn.execute(
                "INSERT INTO users (id, name, password_hash, created_at, is_local, role, title,"
                " early_member_no, last_seen_at, user_no) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    name,
                    password_hash,
                    current,
                    role,
                    title_for(role),
                    member_no,
                    current,
                    _next_user_no(conn),
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO invite_uses (code, user_id, used_at) VALUES (?, ?, ?)",
                (code, user_id, current),
            )
        user = self.get_user(user_id)
        assert user is not None  # 同一事务刚写入；取不到即库损坏，宁可炸也不要返回假账户
        return user, kind

    def create_invite(
        self,
        code: str,
        kind: str,
        *,
        created_by: str | None = None,
        max_uses: int = 1,
        expires_at: int | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        """登记一个邀请码（``code`` 冲突抛 :class:`sqlite3.IntegrityError`）。

        ``max_uses`` / ``expires_at`` 的校验在路由层做（它们表达的是运维意图，错误文案属于
        HTTP 面）；本层只保证写入是原子的。
        """
        created = int(now if now is not None else time.time())
        with self._write() as conn:
            conn.execute(
                "INSERT INTO invites (code, kind, created_by, created_at, expires_at, max_uses)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (code, kind, created_by, created, expires_at, int(max_uses)),
            )
        return {
            "code": code,
            "kind": kind,
            "createdBy": created_by,
            "createdAt": created,
            "expiresAt": expires_at,
            "maxUses": int(max_uses),
            "usedCount": 0,
            "revokedAt": None,
        }

    def consume_invite(
        self, code: str, *, user_id: str, now: int | None = None
    ) -> tuple[bool, str | None, str | None]:
        """原子核销：返回 ``(ok, kind, reason)``。

        **并发安全的唯一实现方式**（卡内 §3.1 冻结）：一切都压在**一条 UPDATE** 上，
        ``WHERE`` 里同时检查 ``revoked_at IS NULL``、``expires_at`` 与 ``used_count < max_uses``，
        以 ``rowcount`` 判成败。绝不能改写成"先 SELECT 查明可用、再 UPDATE"——那正是并发超发的
        经典写法（两个请求都读到 ``used_count=0``，然后都自增，``max_uses=1`` 的码被用两次）。

        ``reason`` 只在失败时非空，且**区分原因只为日志与后台排查**：注册是未登录端点，
        对外一律同一文案（不提供"这个码存不存在"的探测面）。

        为什么把 ``invite_uses`` 的写入放在同一个事务里："码已消耗"与"记录谁用了"必须同生共死，
        否则会出现用尽了却查不到使用人的孤儿码（后台排查正好靠这份记录）。
        """
        from nova_service.invites import (
            REJECT_EXHAUSTED,
            REJECT_EXPIRED,
            REJECT_REVOKED,
            REJECT_UNKNOWN,
        )

        current = int(now if now is not None else time.time())
        with self._write() as conn:
            row = conn.execute(
                "SELECT kind, revoked_at, expires_at, used_count, max_uses FROM invites"
                " WHERE code = ?",
                (code,),
            ).fetchone()
            if row is None:
                return False, None, REJECT_UNKNOWN
            if row["revoked_at"] is not None:
                return False, None, REJECT_REVOKED
            if row["expires_at"] is not None and int(row["expires_at"]) <= current:
                return False, None, REJECT_EXPIRED
            cursor = conn.execute(
                "UPDATE invites SET used_count = used_count + 1"
                " WHERE code = ? AND revoked_at IS NULL"
                "   AND (expires_at IS NULL OR expires_at > ?)"
                "   AND used_count < max_uses",
                (code, current),
            )
            if cursor.rowcount != 1:
                # 并发下唯一会走到这里的分支：另一个请求刚把最后一个名额用掉。
                return False, None, REJECT_EXHAUSTED
            conn.execute(
                "INSERT OR IGNORE INTO invite_uses (code, user_id, used_at) VALUES (?, ?, ?)",
                (code, user_id, current),
            )
        return True, str(row["kind"]), None

    def invite(self, code: str) -> dict[str, Any] | None:
        """单个邀请码（含使用记录）；不存在 → ``None``。"""
        row = self._connect().execute(
            "SELECT * FROM invites WHERE code = ?", (code,)
        ).fetchone()
        return None if row is None else self._invite_payload(row)

    def list_invites(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """全部邀请码（新建在前；后台邀请码模块）。"""
        rows = self._connect().execute(
            "SELECT * FROM invites ORDER BY created_at DESC, code ASC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [self._invite_payload(row) for row in rows]

    def _invite_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        """邀请码行 → 后台展示结构（含使用记录；``to_json`` 风格由路由层决定）。"""
        uses = self._connect().execute(
            "SELECT u.code, u.user_id, u.used_at, us.name AS user_name"
            " FROM invite_uses u LEFT JOIN users us ON us.id = u.user_id"
            " WHERE u.code = ? ORDER BY u.used_at ASC",
            (str(row["code"]),),
        ).fetchall()
        return {
            "code": str(row["code"]),
            "kind": str(row["kind"]),
            "createdBy": row["created_by"],
            "createdAt": int(row["created_at"]),
            "expiresAt": row["expires_at"],
            "maxUses": int(row["max_uses"]),
            "usedCount": int(row["used_count"]),
            "revokedAt": row["revoked_at"],
            "uses": [
                {
                    "userId": str(item["user_id"]),
                    "userName": item["user_name"],
                    "usedAt": int(item["used_at"]),
                }
                for item in uses
            ],
        }

    def revoke_invite(self, code: str, *, now: int | None = None) -> bool:
        """失效一个码（幂等：已失效/不存在均返 ``False``，由调用方决定是不是 404）。"""
        revoked = int(now if now is not None else time.time())
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE invites SET revoked_at = ? WHERE code = ? AND revoked_at IS NULL",
                (revoked, code),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------ 后台（TASK-110 §3.5）

    def list_users(self, *, limit: int = 500) -> list[User]:
        """全部账户（新建在前；后台用户模块）。"""
        rows = self._connect().execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        return [_user(row) for row in rows]

    def set_user_role(
        self,
        user_id: str,
        role: str,
        *,
        early_member_no: int | None = None,
        clear_early_member: bool = False,
        now: int | None = None,
    ) -> User | None:
        """后台改身份（同时写 ``title`` 冗余快照）；用户不存在 → ``None``。

        ``clear_early_member`` 用于"从内测降为公测"时把编号清掉：编号是内测专属收藏品，
        留在公测用户身上会让账户页显示一个不属于该身份的号（卡内 §1.4 只对内测定义编号）。
        """
        resolved = normalize_role(role)
        sets = ["role = ?", "title = ?"]
        params: list[Any] = [resolved, title_for(resolved)]
        if early_member_no is not None:
            sets.append("early_member_no = ?")
            params.append(int(early_member_no))
        elif clear_early_member or resolved != ROLE_BETA:
            sets.append("early_member_no = NULL")
        params.append(user_id)
        with self._write() as conn:
            cursor = conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
            if cursor.rowcount == 0:
                return None
        return self.get_user(user_id)

    def set_user_quota(self, user_id: str, quota_bytes: int | None) -> User | None:
        """后台改单人配额覆盖（``None`` = 恢复按角色默认）；用户不存在 → ``None``。"""
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE users SET quota_bytes = ? WHERE id = ?",
                (None if quota_bytes is None else int(quota_bytes), user_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_user(user_id)

    def set_user_banned(self, user_id: str, *, banned: bool, now: int | None = None) -> User | None:
        """封禁 / 恢复（封禁同时**删掉该用户全部会话**：token 靠校验看 ``banned_at``，
        会话本来就活不过下次校验，但删掉更彻底——浏览器里那份 cookie 不会在"解封后又自动可用"）。
        """
        stamp = int(now if now is not None else time.time()) if banned else None
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE users SET banned_at = ? WHERE id = ?", (stamp, user_id)
            )
            if cursor.rowcount == 0:
                return None
            if banned:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return self.get_user(user_id)

    def find_user_by_name_exact(self, name: str) -> User | None:
        """按名字取用户（管理员提升用；与 ``get_user_by_name`` 的区别：不带密码哈希）。"""
        found = self.get_user_by_name(name)
        return None if found is None else found[0]

    def promote_first_admin(self, name: str, *, now: int | None = None) -> User | None:
        """把指定名字的账户提为管理员（**幂等**）；不存在 → ``None``。

        为什么不用"最早创建的账户"当管理员：那个语义在真实部署里会挑错人（先来试手的同事、
        或迁移前的临时账号会比真正的负责人更早）。指定名字是**可预测**的（卡内 §7.1 已拍板）。
        """
        user = self.find_user_by_name_exact(name)
        if user is None:
            return None
        if user.role == ROLE_ADMIN and user.banned_at is None:
            return user
        return self.set_user_role(user.id, ROLE_ADMIN, clear_early_member=True, now=now)

    # ------------------------------------------------------------------ 项目归属（TASK-061）

    def claim_project(
        self, user_id: str, project_id: str, display_name: str = "", *, now: int | None = None
    ) -> tuple[bool, str | None]:
        """认领项目。返回 ``(ok, owner_user_id)``：``ok=False`` 时 ``owner`` 是**已有**归属者。

        幂等：同一用户重复认领同一 projectId → ``(True, user_id)``，不产生第二行。
        """
        created = int(now if now is not None else time.time())
        with self._write() as conn:
            row = conn.execute(
                "SELECT user_id FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if row is not None:
                owner = str(row["user_id"])
                return owner == user_id, owner
            conn.execute(
                "INSERT INTO projects (project_id, user_id, display_name, created_at)"
                " VALUES (?, ?, ?, ?)",
                (project_id, user_id, display_name, created),
            )
        return True, user_id

    def owns_project(self, user_id: str, project_id: str) -> bool:
        row = self._connect().execute(
            "SELECT 1 FROM projects WHERE project_id = ? AND user_id = ?", (project_id, user_id)
        ).fetchone()
        return row is not None

    def delete_project_owner(self, project_id: str) -> int:
        """删掉该项目的归属行（返回删除行数；供**管理员删项目**用）。

        为什么必须删：项目目录由 ``EngineManager.delete_project`` 删掉后，
        ``projects`` 表里那行就成了幽灵——``GET /api/projects`` 会列出一个目录已不存在的项目，
        点进去就报 404。归属行与目录必须同生共死。

        历史数据（``index_runs`` / ``query_audit``）**不删**：它们是运营证据（TASK-093 的真实
        使用数据闭环就靠它），而且删了会让后台统计凭空缩水。
        """
        with self._write() as conn:
            cursor = conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            return cursor.rowcount

    def list_projects(self, user_id: str) -> list[str]:
        rows = self._connect().execute(
            "SELECT project_id FROM projects WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [str(row["project_id"]) for row in rows]

    # ------------------------------------------------------------------ 索引历史（TASK-062）

    def record_index_run(
        self,
        project_id: str,
        *,
        state: str,
        started_at: int,
        finished_at: int,
        files_total: int = 0,
        files_processed: int = 0,
        chunks: int = 0,
        errors: int = 0,
        error_text: str | None = None,
        call_id: str | None = None,
    ) -> int:
        """写一条索引记录并裁剪到 :data:`INDEX_RUN_KEEP` 条（返回 ``run_id``）。

        ``call_id``（TASK-099 §B）：**同一次 Tool 调用**的 id，由客户端经 ``X-Request-Id``
        携带（与服务端 ``current_request_id()`` 同源）。缺省 ``None`` = 旧客户端未带头或
        离线写入——不填编造的 id，前端据此降级为"每行独立展示"。
        """
        duration_ms = max(0, int(finished_at - started_at)) * 1000
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO index_runs (project_id, state, started_at, finished_at,"
                " duration_ms, files_total, files_processed, chunks, errors, error_text, call_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    state,
                    int(started_at),
                    int(finished_at),
                    duration_ms,
                    int(files_total),
                    int(files_processed),
                    int(chunks),
                    int(errors),
                    error_text,
                    call_id,
                ),
            )
            run_id = int(cursor.lastrowid or 0)
            conn.execute(
                "DELETE FROM index_runs WHERE project_id = ? AND id NOT IN"
                " (SELECT id FROM index_runs WHERE project_id = ?"
                "  ORDER BY finished_at DESC, id DESC LIMIT ?)",
                (project_id, project_id, INDEX_RUN_KEEP),
            )
        return run_id

    def index_stats(self, project_id: str, *, limit: int = 20) -> IndexStats:
        """聚合 + 最近 ``limit`` 条（``avgDurationMs`` **只统计 succeeded**）。"""
        conn = self._connect()
        rows = conn.execute(
            "SELECT state, duration_ms FROM index_runs WHERE project_id = ?", (project_id,)
        ).fetchall()
        succeeded = [int(row["duration_ms"]) for row in rows if row["state"] == "done"]
        failed = sum(1 for row in rows if row["state"] != "done")
        recent_rows = conn.execute(
            "SELECT * FROM index_runs WHERE project_id = ?"
            " ORDER BY finished_at DESC, id DESC LIMIT ?",
            (project_id, max(1, int(limit))),
        ).fetchall()
        last = recent_rows[0] if recent_rows else None
        return IndexStats(
            total=len(rows),
            succeeded=len(succeeded),
            failed=failed,
            avg_duration_ms=int(sum(succeeded) / len(succeeded)) if succeeded else None,
            min_duration_ms=min(succeeded) if succeeded else None,
            max_duration_ms=max(succeeded) if succeeded else None,
            last_run_at=int(last["finished_at"]) if last is not None else None,
            last_state=str(last["state"]) if last is not None else None,
            recent=tuple(_index_run(row) for row in recent_rows),
        )

    def all_index_stats(self, project_ids: Sequence[str], *, limit: int = 20) -> IndexStats:
        """跨项目汇总（当前用户的项目；``recent`` 按时间混排）。"""
        if not project_ids:
            return IndexStats(0, 0, 0, None, None, None, None, None)
        placeholders = ",".join("?" for _ in project_ids)
        params = list(project_ids)
        conn = self._connect()
        rows = conn.execute(
            f"SELECT project_id, state, duration_ms FROM index_runs"
            f" WHERE project_id IN ({placeholders})",
            params,
        ).fetchall()
        succeeded = [int(row["duration_ms"]) for row in rows if row["state"] == "done"]
        failed = sum(1 for row in rows if row["state"] != "done")
        recent_rows = conn.execute(
            f"SELECT * FROM index_runs WHERE project_id IN ({placeholders})"
            f" ORDER BY finished_at DESC, id DESC LIMIT ?",
            [*params, max(1, int(limit))],
        ).fetchall()
        last = recent_rows[0] if recent_rows else None
        return IndexStats(
            total=len(rows),
            succeeded=len(succeeded),
            failed=failed,
            avg_duration_ms=int(sum(succeeded) / len(succeeded)) if succeeded else None,
            min_duration_ms=min(succeeded) if succeeded else None,
            max_duration_ms=max(succeeded) if succeeded else None,
            last_run_at=int(last["finished_at"]) if last is not None else None,
            last_state=str(last["state"]) if last is not None else None,
            recent=tuple(_index_run(row) for row in recent_rows),
        )

    def index_runs(self, project_id: str, *, limit: int = 20) -> list[IndexRun]:
        rows = self._connect().execute(
            "SELECT * FROM index_runs WHERE project_id = ?"
            " ORDER BY finished_at DESC, id DESC LIMIT ?",
            (project_id, max(1, int(limit))),
        ).fetchall()
        return [_index_run(row) for row in rows]

    # ------------------------------------------------------------------ 用户级 LLM 配置（§C）

    def get_llm_config(self, user_id: str) -> LlmConfigRecord | None:
        """该用户的 LLM 配置（未配置 → ``None``，调用方回落服务端默认）。

        **内部专用**：返回值含明文 key，严禁直接序列化进 HTTP 响应。
        """
        row = self._connect().execute(
            "SELECT user_id, model, base_url, api_key, protocol, created_at, updated_at"
            " FROM user_llm_config WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return None if row is None else _llm_config_record(row)

    def save_llm_config(
        self,
        user_id: str,
        *,
        model: str,
        base_url: str,
        api_key: str | None = None,
        protocol: str | None = None,
        now: int | None = None,
    ) -> LlmConfigRecord:
        """写入（或覆盖）该用户的 LLM 配置。

        ``api_key=None`` 表示**保持不变**（"只改模型名不想重输 key"——卡内 §C-3 的
        "``apiKey`` 传空串表示保持不变"）。首次写入时 key 不能为空（没有旧值可继承）→
        :class:`ValueError`，由路由层转 400（这里是唯一能判断"是不是首次"的地方）。

        ``protocol``（TASK-113）：``None`` 表示**保持不变**（与 ``api_key`` 同一语义——
        用户只改模型名时不该把他选的协议抹掉）。首次写入为 ``None`` 则存 ``NULL``，
        读取时回落服务端默认。
        """
        current = int(now if now is not None else time.time())
        existing = self.get_llm_config(user_id)
        resolved_key = api_key if api_key is not None else (existing.api_key if existing else None)
        if not resolved_key:
            raise ValueError("首次保存必须提供 apiKey（之后可留空表示保持不变）")
        resolved_protocol = (
            existing.protocol if protocol is None and existing is not None else protocol
        )
        created = existing.created_at if existing is not None else current
        with self._write() as conn:
            conn.execute(
                "INSERT INTO user_llm_config"
                " (user_id, model, base_url, api_key, protocol, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET"
                " model = excluded.model, base_url = excluded.base_url,"
                " api_key = excluded.api_key, protocol = excluded.protocol,"
                " updated_at = excluded.updated_at",
                (user_id, model, base_url, resolved_key, resolved_protocol, created, current),
            )
        return LlmConfigRecord(
            user_id=user_id,
            model=model,
            base_url=base_url,
            api_key=resolved_key,
            created_at=created,
            updated_at=current,
            protocol=resolved_protocol,
        )

    def delete_llm_config(self, user_id: str) -> bool:
        """删除该用户的 LLM 配置（→ 回落服务端默认）；不存在返回 ``False``。

        幂等语义由调用方决定（§C-3：``DELETE`` 不报 404，删除本来就是"让它不在"）。
        """
        with self._write() as conn:
            cursor = conn.execute("DELETE FROM user_llm_config WHERE user_id = ?", (user_id,))
            return cursor.rowcount > 0

    # ------------------------------------------------------------------ 调用时间线（TASK-099 §B）

    def index_runs_by_call(self, call_id: str) -> list[IndexRun]:
        """该 callId 下的全部索引记录（**跨项目**：一次调用可能同时初始化多个仓库）。"""
        rows = self._connect().execute(
            "SELECT * FROM index_runs WHERE call_id = ? ORDER BY started_at ASC, id ASC",
            (call_id,),
        ).fetchall()
        return [_index_run(row) for row in rows]

    def used_tokens_sum(
        self, project_ids: Sequence[str], *, days: int = 30, now: int | None = None
    ) -> int:
        """窗口内全部调用的 ``used_tokens`` 之和（后台统计模块）。

        为什么不在 ``usage_summary`` 里一起算：那个函数的形状已经冻结给用户侧页面了，
        它的 ``to_json`` 不要多出一个只给后台用的字段；而 "用了多少 token" 是个聚合整数，
        单独一条 SQL 比让每个调用方 `sum(...)` 一遍更便宜也更不容易算错。
        """
        if not project_ids:
            return 0
        current = int(now if now is not None else time.time())
        since = current - max(1, int(days)) * 86400
        placeholders = ",".join("?" for _ in project_ids)
        row = self._connect().execute(
            f"SELECT COALESCE(SUM(used_tokens), 0) AS n FROM query_audit"
            f" WHERE project_id IN ({placeholders}) AND created_at >= ?",
            [*project_ids, since],
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def queries_by_request_id(self, request_id: str) -> list[QueryAuditRecord]:
        """该 trace id（= callId）下的全部查询审计。"""
        rows = self._connect().execute(
            "SELECT * FROM query_audit WHERE request_id = ? ORDER BY created_at ASC, id ASC",
            (request_id,),
        ).fetchall()
        return [_audit_record(row) for row in rows]

    def visible_call_ids(self, project_ids: Sequence[str], *, limit: int = 50) -> list[str]:
        """当前可见项目下最近的 callId（去重、按最近活动倒序）。

        只为**归属校验**服务（见 ``routers/ops.py`` 的 ``/api/calls/{callId}``）：云端形态下
        不能让人拿别人的 callId 读到别人的时间线，而时间线本身是跨项目的，无法靠单个
        projectId 判定。空列表 = 该用户没有任何带 callId 的记录。

        **两个来源都要看**（``index_runs.call_id`` 与 ``query_audit.request_id``）：
        一次调用可能只有检索而没有任何上传（索引早已就绪），只看 run 表会让这种调用
        连自己都无法访问（实测踩到：Alice 查自己的 callId 得到 404）。
        """
        if not project_ids:
            return []
        placeholders = ",".join("?" for _ in project_ids)
        rows = self._connect().execute(
            f"SELECT call_id, MAX(at) AS at FROM ("
            f"  SELECT call_id, finished_at AS at FROM index_runs"
            f"   WHERE project_id IN ({placeholders}) AND call_id IS NOT NULL"
            f"  UNION ALL"
            f"  SELECT request_id AS call_id, created_at AS at FROM query_audit"
            f"   WHERE project_id IN ({placeholders}) AND request_id IS NOT NULL"
            f") GROUP BY call_id ORDER BY at DESC LIMIT ?",
            [*project_ids, *project_ids, max(1, int(limit))],
        ).fetchall()
        return [str(row["call_id"]) for row in rows]

    # ------------------------------------------------------------------ 查询审计（TASK-064）

    def record_query(
        self,
        *,
        project_id: str,
        mode: str,
        query: str,
        latency_ms: int,
        answerable: bool | None = None,
        confidence: str | None = None,
        degraded: bool = False,
        evidence_count: int = 0,
        docs_count: int = 0,
        used_tokens: int = 0,
        citation_coverage: float | None = None,
        llm_latency_ms: int | None = None,
        answer_tokens: int | None = None,
        request_id: str | None = None,
        answer_text: str | None = None,
        answer_status: str | None = None,
        evidence: Sequence[Mapping[str, Any]] = (),
        user_id: str | None = None,
        now: int | None = None,
    ) -> int:
        """写一条查询审计（``evidence`` 只存 ``{id,path,lines,tier,score}`` 元数据）。

        ``request_id``（TASK-094 §C）是**当前请求的 trace id**（与响应头 ``X-Request-Id`` 同源）；
        缺省 ``None`` 表示调用方未绑定（旧调用方/离线写库）——旧行因此可以看出"这条没有 trace"，
        而不是被填上一个编造的 id。TASK-099 §B-3：客户端在同一次 Tool 调用里发**同一个**
        ``X-Request-Id``，因此这个字段同时是那次调用的 callId（与 ``index_runs.call_id`` 同值）。

        ``answer_text`` / ``answer_status``（TASK-099 §A）是**LLM 答案正文与状态**。三条分支：

        - ``answerable=false`` 短路（D-24）→ ``answer_text=None`` + ``"insufficient_evidence"``；
        - 未配置/调用失败（D-26 降级）→ ``answer_text=None`` + ``"degraded"``；
        - 成功 → 正文 + ``"answered"``。

        因此 **``answer_text`` 为 NULL 不等于"调了 LLM 但答案是空"**——区分靠 ``answer_status``
        （把"没调"记成"调了空答案"是 TASK-099 明令禁止的失信）。长度上限由写入方
        （``nova_service.audit.ANSWER_STORE_MAX_CHARS``）负责卡，本层不重复截断。

        **调用方必须自己 try/except**：审计是旁路，记不上账不影响检索（TASK-064 §C）。
        """
        import json

        created = int(now if now is not None else time.time())
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO query_audit (project_id, user_id, mode, query, answerable,"
                " confidence, degraded, latency_ms, evidence_count, docs_count, used_tokens,"
                " citation_coverage, llm_latency_ms, answer_tokens, request_id, answer_text,"
                " answer_status, evidence_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    user_id,
                    mode,
                    query,
                    None if answerable is None else (1 if answerable else 0),
                    confidence,
                    1 if degraded else 0,
                    int(latency_ms),
                    int(evidence_count),
                    int(docs_count),
                    int(used_tokens),
                    citation_coverage,
                    llm_latency_ms,
                    answer_tokens,
                    request_id,
                    answer_text,
                    answer_status,
                    json.dumps([dict(item) for item in evidence], ensure_ascii=False),
                    created,
                ),
            )
            query_id = int(cursor.lastrowid or 0)
            conn.execute(
                "DELETE FROM query_audit WHERE project_id = ? AND id NOT IN"
                " (SELECT id FROM query_audit WHERE project_id = ?"
                "  ORDER BY created_at DESC, id DESC LIMIT ?)",
                (project_id, project_id, QUERY_AUDIT_KEEP),
            )
        return query_id

    def usage_summary(
        self, project_ids: Sequence[str], *, days: int = 30, recent_limit: int = 20,
        now: int | None = None,
    ) -> UsageSummary:
        """用量聚合（``days`` 窗口；``succeeded`` = ``answerable`` 为真的次数）。"""
        if not project_ids:
            return UsageSummary(0, 0, 0, 0, None, None, {}, None)
        current = int(now if now is not None else time.time())
        since = current - max(1, int(days)) * 86400
        placeholders = ",".join("?" for _ in project_ids)
        params: list[Any] = [*project_ids, since]
        conn = self._connect()
        rows = conn.execute(
            f"SELECT answerable, confidence, latency_ms, degraded, citation_coverage, query"
            f" FROM query_audit WHERE project_id IN ({placeholders}) AND created_at >= ?",
            params,
        ).fetchall()

        total = len(rows)
        succeeded = sum(1 for row in rows if row["answerable"] == 1)
        insufficient = sum(1 for row in rows if row["answerable"] == 0)
        # "失败" = 降级且没有答案（检索链本身出错/D-26 降级），与"证据不足"是两件事。
        failed = sum(1 for row in rows if row["degraded"] == 1 and row["answerable"] is None)
        latencies = sorted(int(row["latency_ms"]) for row in rows)
        coverages = [
            float(row["citation_coverage"])
            for row in rows
            if row["citation_coverage"] is not None
        ]
        distribution: dict[str, int] = {}
        for row in rows:
            key = str(row["confidence"]) if row["confidence"] else "unknown"
            distribution[key] = distribution.get(key, 0) + 1
        counts: dict[str, int] = {}
        for row in rows:
            text = str(row["query"])
            counts[text] = counts.get(text, 0) + 1
        top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]
        recent_rows = conn.execute(
            f"SELECT * FROM query_audit WHERE project_id IN ({placeholders}) AND created_at >= ?"
            f" ORDER BY created_at DESC, id DESC LIMIT ?",
            [*params, max(1, int(recent_limit))],
        ).fetchall()
        return UsageSummary(
            total=total,
            succeeded=succeeded,
            insufficient=insufficient,
            failed=failed,
            avg_latency_ms=int(sum(latencies) / len(latencies)) if latencies else None,
            p95_latency_ms=_percentile(latencies, 0.95),
            confidence_distribution=distribution,
            citation_coverage_avg=(
                round(sum(coverages) / len(coverages), 4) if coverages else None
            ),
            top_queries=tuple(top),
            recent=tuple(_audit_record(row) for row in recent_rows),
        )


def _percentile(sorted_values: Sequence[int], ratio: float) -> int | None:
    """最近秩法百分位（``sorted_values`` 必须已升序）；空序列 → ``None``。"""
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, int(round(ratio * len(sorted_values))) - 1))
    return int(sorted_values[index])


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等增量迁移（TASK-088 §E 引入本模块的第一条 ALTER 路径）。

    纪律：

    - **只加列**（``ADD COLUMN``），不改名/不改类型/不删列——旧版本代码仍能读写同一张表；
    - 每条语句前用 ``PRAGMA table_info`` 判存在性，因此重复打开同一库不会报错；
    - 失败向上抛（schema 不完整时要及早暴露，而不是让审计静默写不进去）。

    TASK-110 §3.1 追加 ``users`` 的六个身份列。它们**只加列**，因此：

    - 旧用户（迁移前就存在）拿到 ``role='public'``，登录与 MCP 调用逐字不变；
    - 角色提升（谁是管理员）**不在迁移里做**：那是环境相关的运营决策，
      由 ``nova_service.routers.admin`` 暴露的显式提升入口或一次性 SQL 完成——
      把"哪个用户名是管理员"写进 schema 迁移等于把部署信息烧进代码。
    """
    _add_columns(conn, "query_audit", _AUDIT_COLUMNS)
    _add_columns(conn, "index_runs", _INDEX_RUN_COLUMNS)
    _add_columns(conn, "users", _USER_COLUMNS)
    _add_columns(conn, "api_tokens", _TOKEN_COLUMNS)
    _add_columns(conn, "user_llm_config", _USER_LLM_CONFIG_COLUMNS)
    _backfill_user_no(conn)
    # 索引在列存在之后建（见 _AUDIT_INDEXES 的注释：放 _SCHEMA 里会让旧库打开直接失败）。
    for statement in _AUDIT_INDEXES:
        conn.execute(statement)
    # 唯一索引必须在**回填之后**建：回填前全是 NULL，而唯一索引允许多个 NULL（不会冲突），
    # 但一旦回填错了就很难发现。先回填再建，让约束在建立时就校验一遍。
    for statement in _UNIQUE_INDEXES:
        conn.execute(statement)


def _backfill_user_no(conn: sqlite3.Connection) -> None:
    """给没有顺序号的账户技号（旧库升级；按 ``created_at`` 升序，同刻按 ``rowid``）。

    幂等：只碰 ``user_no IS NULL`` 的行，已发号的不动。为什么要回填而不是发新号时补：
    用户看到的 ``ID #001`` 必须是**注册顺序**，晚补的话首位用户会拿到一个大数字，
    而他明明是最早注册的。
    """
    row = conn.execute("SELECT MAX(user_no) AS n FROM users").fetchone()
    next_no = 1 if row is None or row["n"] is None else int(row["n"]) + 1
    pending = conn.execute(
        "SELECT rowid FROM users WHERE user_no IS NULL ORDER BY created_at ASC, rowid ASC"
    ).fetchall()
    for item in pending:
        conn.execute("UPDATE users SET user_no = ? WHERE rowid = ?", (next_no, item["rowid"]))
        next_no += 1


def _llm_config_record(row: sqlite3.Row) -> LlmConfigRecord:
    # ``protocol`` 是 TASK-113 的增量列：旧库迁移后的行该列为 ``NULL``（→ 回落服务端默认），
    # 用 ``row.keys()`` 判存在性可容"测试手搭最小表"与"迁移前后列集合不同"两种情况
    # （与 ``_user`` 的既有口径一致：缺列取默认值，不编造）。
    keys = set(row.keys())
    return LlmConfigRecord(
        user_id=str(row["user_id"]),
        model=str(row["model"]),
        base_url=str(row["base_url"]),
        api_key=str(row["api_key"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        protocol=str(row["protocol"]) if "protocol" in keys and row["protocol"] else None,
    )


def _add_columns(
    conn: sqlite3.Connection, table: str, columns: Sequence[tuple[str, str]]
) -> None:
    """给 ``table`` 补齐 ``columns`` 里缺失的列（幂等：先查 ``PRAGMA table_info``）。

    所有表共用这一条路径，纪律见 :func:`_migrate`：只加列、不改名/改类型/删列，
    因此旧版本代码仍能读写同一张表。
    """
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def _user(row: sqlite3.Row) -> User:
    """``users`` 行 → :class:`User`。

    为什么用 ``row.keys()`` 判列存在而不假设它恒在：**测试与工具会手搭最小表**
    （如 ``test_local_mode`` 直接 ``db.create_user``），而历史库在迁移前后列集合也可能不同。
    缺列就取默认值（不编造），比 ``sqlite3.Row`` 的 ``IndexError`` 好：
    后者会让"一个老库少一列"变成全面 500。
    """
    keys = set(row.keys())
    role = normalize_role(str(row["role"])) if "role" in keys else "public"
    return User(
        id=str(row["id"]),
        name=str(row["name"]),
        created_at=int(row["created_at"]),
        is_local=bool(row["is_local"]),
        role=role,
        title=title_for(role),
        early_member_no=_optional_int(row, keys, "early_member_no"),
        quota_bytes=_optional_int(row, keys, "quota_bytes"),
        banned_at=_optional_int(row, keys, "banned_at"),
        last_seen_at=_optional_int(row, keys, "last_seen_at"),
        user_no=_optional_int(row, keys, "user_no"),
    )


def _optional_int(row: sqlite3.Row, keys: set[str], column: str) -> int | None:
    """可缺列的可空整数（缺列与 NULL 都返回 ``None``——“没这个信息”就是 ``None``）。"""
    if column not in keys:
        return None
    value = row[column]
    return None if value is None else int(value)


def _next_user_no(conn: sqlite3.Connection) -> int:
    """下一个全站顺序号（``MAX(user_no) + 1``，空表从 1 开始）。

    用 ``MAX`` 而不是 ``COUNT(*)``：顺序号必须**永不复用**。删过一个用户（或将来的清理）
    后 ``COUNT`` 会把他的号发给新人，而他递出去的名片上的 ``ID #N`` 就撞车了。
    """
    row = conn.execute("SELECT MAX(user_no) AS n FROM users").fetchone()
    return 1 if row is None or row["n"] is None else int(row["n"]) + 1


def _index_run(row: sqlite3.Row) -> IndexRun:
    return IndexRun(
        run_id=int(row["id"]),
        project_id=str(row["project_id"]),
        state=str(row["state"]),
        started_at=int(row["started_at"]),
        finished_at=int(row["finished_at"]),
        duration_ms=int(row["duration_ms"]),
        files_total=int(row["files_total"]),
        files_processed=int(row["files_processed"]),
        chunks=int(row["chunks"]),
        errors=int(row["errors"]),
        error_text=row["error_text"],
        call_id=row["call_id"],
    )


def _audit_record(row: sqlite3.Row) -> QueryAuditRecord:
    return QueryAuditRecord(
        query_id=int(row["id"]),
        project_id=str(row["project_id"]),
        mode=str(row["mode"]),
        query=str(row["query"]),
        answerable=None if row["answerable"] is None else bool(row["answerable"]),
        confidence=row["confidence"],
        degraded=bool(row["degraded"]),
        latency_ms=int(row["latency_ms"]),
        evidence_count=int(row["evidence_count"]),
        docs_count=int(row["docs_count"]),
        used_tokens=int(row["used_tokens"]),
        citation_coverage=row["citation_coverage"],
        llm_latency_ms=row["llm_latency_ms"],
        answer_tokens=row["answer_tokens"],
        request_id=row["request_id"],
        created_at=int(row["created_at"]),
        answer_text=row["answer_text"],
        answer_status=row["answer_status"],
        evidence=_parse_evidence_json(row["evidence_json"]),
    )


def _parse_evidence_json(raw: Any) -> tuple[dict[str, Any], ...]:
    """``evidence_json`` → 证据元组（TASK-107）。

    历史库里可能是不合法 JSON（旧版本/手改）——那种情况如实当“没有证据”处理，
    不让一条脏记录把整个历史页接口打挂（与 ``load()`` 对损坏缓存的宽容口径一致）。
    """
    if not raw:
        return ()
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(item for item in parsed if isinstance(item, dict))
