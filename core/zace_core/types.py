"""zace-core 冻结数据类型（CF-08/CF-09）。

来源：docs/design/Module/01 §2/§2.4、Module/02 §4、Module/03 §2、Module/04 §2、Module/06 §1。
维护者：编排者；变更前须在任务卡执行记录里提契约变更申请、等裁决。
检索侧候选（Candidate）字段名与 docs/contracts/contextpack.schema.json 对齐。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 索引侧：Parser（TASK-002..005）产出 → Chunking/流水线（TASK-006/007）消费
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SymbolDef:
    name: str                     # 符号短名，如 "refresh"
    fqn: str                      # 限定名，如 "TokenService.refresh"；spec 块为 heading_path
    kind: str                     # function/method/class/struct/enum/typedef/macro/namespace/...
    start_line: int               # 1-based，含
    end_line: int                 # 1-based，含
    is_exported: bool = False     # 入口点评分依据（Module/02 §4.4-a）


@dataclass(frozen=True, slots=True)
class EdgeDef:
    source_fqn: str
    target_name: str              # 解析前目标名（裸名/相对名），由 TASK-006 二阶段解析成 fqn
    kind: str                     # calls/imports/exports/extends/implements/references/contains/...
    line: int | None = None
    provenance: str = "parsed"    # parsed / synthesized


@dataclass(frozen=True, slots=True)
class UnresolvedRef:
    from_fqn: str
    name: str
    kind: str                     # call/import/reference/...
    line: int | None = None


@dataclass(frozen=True, slots=True)
class CodeFence:
    lang: str
    content: str
    line: int


@dataclass(frozen=True, slots=True)
class SpecBlockDef:
    """Markdown SpecBlock（Module/01 §2.2）。id = {path}:{heading_path}:{start_line}。"""

    path: str
    heading: str
    heading_path: str             # "架构 > 认证模块 > token 刷新流程"
    level: int
    start_line: int
    end_line: int
    content: str
    doctype: str                  # agent-instructions/readme/design/adr/api/changelog/guide
    code_fences: tuple[CodeFence, ...] = ()
    mentioned: tuple[str, ...] = ()   # 行内 code / 符号名候选（SPEC_REFS 匹配输入，TASK-005/007）


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """单文件解析结果（Parser 契约）。解析失败 → fallback=True，由 TASK-006 兜底切分。"""

    path: str                     # 仓库相对路径，正斜杠
    language: str                 # python/c/cpp/markdown/fallback
    symbols: tuple[SymbolDef, ...] = ()
    edges: tuple[EdgeDef, ...] = ()
    unresolved: tuple[UnresolvedRef, ...] = ()
    spec_blocks: tuple[SpecBlockDef, ...] = ()   # 仅 markdown
    parse_errors: tuple[str, ...] = ()
    fallback: bool = False


# ---------------------------------------------------------------------------
# 存储 ↔ 流水线 ↔ 向量库 的增量交接
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChunkDef:
    """chunks 表的写入单元（TASK-006 产出）。"""

    id: str                       # {path}:{symbol_fqn}:{start_line}
    file_path: str
    symbol_fqn: str | None
    symbol_kind: str              # function/method/class_skeleton/spec_block/fallback_block
    start_line: int
    end_line: int
    signature: str
    docstring: str
    content: str                  # 原文全文（存储与 FTS/向量输入的三通道规则见 Module/01 §2.4）
    content_hash: str             # D-43：sha256(规范化后完整切片内容)


@dataclass(frozen=True, slots=True)
class FileDelta:
    """单个文件索引写入后的对账结果（TASK-001 返回，TASK-007 用于向量增量）。"""

    path: str
    new_chunk_ids: tuple[str, ...]        # 新增/变更，需要 embedding
    reused_chunk_ids: tuple[str, ...]     # content_hash 未变，向量直接复用
    removed_chunk_ids: tuple[str, ...]    # 需要删除向量


@dataclass(frozen=True, slots=True)
class VectorRow:
    chunk_id: str
    content_hash: str
    vector: list[float]


@dataclass(frozen=True, slots=True)
class VectorHit:
    chunk_id: str
    score: float                  # 余弦相似度（越大越相关）


# ---------------------------------------------------------------------------
# 检索侧：TASK-011/012 产出 → TASK-013 组装（字段名与 ContextPack 对齐）
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    chunk_id: str
    kind: str                     # code/test/spec/fallback
    rrf_score: float              # 融合分，写入后不再变（Module/02 §4.3）
    score: float                  # 当前排序分（rerank 后覆盖为最终分）
    channel_ranks: dict[str, int] = field(default_factory=dict)  # {"exact":1,"bm25":7,"vector":3}
    tier: int = 2                 # 0..3（D-17：可信度元数据 + 装填资格线 + rerank 特征）
    reasons: list[str] = field(default_factory=list)
    symbol_fqn: str | None = None
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    graph_depth: int = 0          # 图扩展深度（0 = 直接命中）


@dataclass(frozen=True, slots=True)
class FlowNode:
    symbol: str
    path: str
    line: int


@dataclass(frozen=True, slots=True)
class Flow:
    id: str                       # F1/F2...
    nodes: tuple[FlowNode, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class MissingEvidence:
    code: str                     # Module/03 §5 七类
    message: str                  # 必含"缺什么 + 为什么缺"
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class Freshness:
    indexed_at: int | None = None
    stale_files: tuple[str, ...] = ()
    indexing_files: tuple[str, ...] = ()


@dataclass
class EvidenceItem:
    id: str                       # E1 / E7...（evidence 与 docs 共用编号空间，D-21）
    type: str                     # code/test/spec
    path: str
    content: str                  # 带行号原文（渲染格式见 Module/03 §6）
    score: float
    evidence_tier: int
    reason: str
    symbol: str | None = None
    heading_path: str | None = None
    doctype: str | None = None
    lines: tuple[int, int] | None = None
    elided_lines: int = 0
    stale_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Budget:
    used_tokens: int
    hard_cap: int
    truncated: bool = False
    omitted_count: int = 0


@dataclass
class ContextPack:
    query: str
    mode: str                     # fast / deep
    answerable: bool
    confidence: str               # high/medium/low（Module/03 §4.4 确定性判定）
    freshness: Freshness
    evidence: list[EvidenceItem] = field(default_factory=list)   # 代码/测试证据
    docs: list[EvidenceItem] = field(default_factory=list)       # spec 证据（共用 E 编号）
    flows: list[Flow] = field(default_factory=list)
    missing_evidence: list[MissingEvidence] = field(default_factory=list)
    next_queries: list[str] = field(default_factory=list)
    budget: Budget | None = None


# ---------------------------------------------------------------------------
# 服务层 ↔ core（Module/06 §1）的传输对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlobInput:
    path: str                     # 仓库相对路径
    content: bytes
    blob_hash: str


@dataclass(frozen=True, slots=True)
class ChangeSet:
    added: tuple[BlobInput, ...] = ()
    modified: tuple[BlobInput, ...] = ()
    deleted: tuple[str, ...] = ()
    branch: str | None = None
    commit_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectHandle:
    project_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class SyncStatus:
    project_id: str
    files_indexed: int = 0
    chunks: int = 0
    symbols: int = 0
    edges: int = 0
    pending_jobs: int = 0
    indexing_files: tuple[str, ...] = ()
    last_indexed_at: int | None = None


@dataclass(frozen=True, slots=True)
class AskResult:
    status: str                   # answered / insufficient_evidence / degraded（D-24/D-26）
    answer: str                   # Markdown（含 citation；degraded 时为降级说明+渲染包）
    pack: ContextPack
    meta: dict[str, Any] = field(default_factory=dict)   # llmLatencyMs / citationCoverage 等
