# nova Architecture Design Task
> 文档属性：这是 nova 的原始需求来源和早期讨论草案，仅供参考，不是最终设计合同。与 research/Background 或 research/Module 冲突时，以后者为准；具体设计决策以 research/INDEX.md §3 为准。
> 当前 V1 重点支持 Python、C、C++、Markdown；Kotlin 暂不适配，未来语言扩展另行预研。


## 1. 背景

当前工作区：

```text
ACE/
├── source/
│   ├── GitNexus/
│   ├── codegraph/
│   ├── notace-tool-rs/
│   └── ragcode/
│
└── nova/
    └── README.md
```

`source/` 下为参考项目，只用于源码研究和架构借鉴。

`nova/` 是我们准备从零设计和开发的开源项目。

**禁止修改 `source/` 下的任何代码。**

在未确认可以直接复用前，不直接复制参考项目实现，仅学习其架构、数据模型、算法和工程设计。

---

# 2. 项目定位

nova 是一个面向 Coding Agent 的 **Workspace Context Engine**。

它不是单纯的代码搜索工具，也不是单纯的 Vector RAG 或 GraphRAG。

目标是：

> 替代 Coding Agent 在正式分析、Debug、开发前，大量执行 grep、search、read、调用链分析和文档查阅的 Context Acquisition 过程。

通过：

* AST / Tree-sitter
* Symbol / Code Graph
* BM25 / FTS
* Semantic Vector Retrieval
* Hybrid Retrieval
* RRF / Rerank
* Spec / Document Retrieval
* Context Packing
* MCP
* Grounded LLM

为 Codex、Claude Code、Cursor 等 Harness 提供高质量项目上下文。

---

# 3. 核心产品形态

最终用户通过 MCP 将 nova 接入 Coding Harness。

整体期望：

```text
                  Coding Harness
              Codex / Claude / Cursor
                        │
                        │ MCP
                        ▼
                nova MCP Client
                        │
                Auth / Project
                        │
                 HTTPS / API
                        ▼
                nova Service
                        │
            Retrieval Orchestrator
                        │
      ┌─────────┬───────┼───────┬──────────┐
      ▼         ▼       ▼       ▼          ▼
   Exact      BM25    Vector   Graph      Spec
                         │
                        LSP
                         │
                         ▼
                 Evidence Rerank
                         │
                         ▼
                   ContextPack
                  ┌──────┴──────┐
                  ▼             ▼
             Fast Result     Grounded LLM
                  │             │
                  ▼             ▼
          search_context    ask_project
```

服务最终计划部署在自己的 VPS / Server。

---

# 4. MCP Tool 设计

V1 对 Coding Agent 尽量只暴露少量高层 Tool，避免把 Retrieval Planner 的工作重新交给主 Agent。

## 4.1 search_context

Fast 模式。

目标：

> 10 秒内返回当前问题最值得 Agent 阅读的上下文。

不调用 Answer LLM。

内部可以使用：

* Exact Symbol Search
* BM25 / FTS
* Cached Code Graph
* Semantic Retrieval
* Spec / Docs Retrieval
* Lightweight Graph Expansion
* Evidence Rerank

返回的是 ContextPack，而不是简单 Top-K Chunk。

建议包含：

* relevant files
* symbols
* snippets
* call relationships
* flow
* related specs/docs
* citations
* relevance reason
* missing evidence

---

## 4.2 ask_project

Deep 模式。

目标：

> 60 秒内完成一次较完整的项目调查，并直接给出 grounded answer。

内部可以执行：

```text
Query Understanding
→ Retrieval Planning
→ Hybrid Retrieval
→ Graph Expansion
→ Spec / Docs Retrieval
→ 必要时 LSP Verification
→ Evidence Rerank
→ ContextPack
→ Grounded LLM
→ Final Answer
```

期望回答：

* Answer
* Code Flow
* Key Evidence
* Spec / Design Constraints
* Implementation vs Spec
* Likely Root Cause / Edit Point
* Recommended Reads
* Missing Evidence
* Citations

---

## 4.3 可选 Tool

评估是否值得在后续版本增加：

```text
trace_flow
impact_analysis
```

但 V1 默认优先保持：

```text
search_context
ask_project
```

两个核心 Tool。

---

# 5. Workspace 同步模型

由于 nova Service 最终部署在 VPS，需要设计：

```text
Local Workspace
      ↓
nova MCP / Local Client
      ↓
File Scan
      ↓
.gitignore / nova ignore
      ↓
Hash / Change Detection
      ↓
Incremental Sync
      ↓
nova Server
```

重点参考 `notace-tool-rs` 的：

* 本地 Workspace 扫描
* Blob Hash
* Changed File Detection
* Incremental Upload
* MCP stdio Client
* Remote Service Boundary

但不要假设它远端不可见的 Finnian / ACE 能力属于本地实现。

需要重点设计：

* 首次全量同步
* 后续增量同步
* 删除文件同步
* rename
* branch / commit 信息
* workspace identity
* repo identity
* project identity
* `.gitignore`
* 自定义 ignore
* binary / build / dependency 排除
* 文件大小限制
* hash cache
* stale index
* sync status

---

# 6. Indexing Pipeline

不要把代码仅作为普通文本 Chunk。

目标索引流程建议考虑：

```text
File Scanner
    ↓
Language Detection
    ↓
┌──────────────────┬────────────────────┐
│ Code             │ Docs               │
│                  │                    │
│ Tree-sitter      │ Markdown Parser    │
│ AST              │ Heading / Block    │
│ Symbol           │ SpecBlock          │
│ Call / Import    │ References         │
└─────────┬────────┴─────────┬──────────┘
          │                  │
          ▼                  ▼
             Unified Evidence Model
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
      FTS/BM25     Vector Index   Code Graph
```

重点考虑：

* File
* Chunk
* Symbol
* Edge
* SpecBlock
* SpecLink
* Repository
* Workspace
* IndexVersion

等统一数据模型。

---

# 7. Code Intelligence

V1 正式重点支持：

* C
* C++
* Python
* Markdown

V1 暂不适配 Kotlin，仅保留未来多语言 Parser Provider 的扩展路径。

重点研究 `CodeGraph` 的：

* Tree-sitter parser
* Symbol extraction
* Edge extraction
* caller / callee
* reference
* import
* inheritance
* cross-file resolution
* graph persistence
* graph traversal

同时设计 Code Intelligence Provider 抽象，为未来增加 LSP 做准备：

```text
Code Intelligence
       │
 ┌─────┴─────┐
 ▼           ▼
Tree-sitter  LSP
Structural   Semantic
```

Tree-sitter 负责：

* AST
* Symbol
* Chunk
* Structural Edge

LSP 后续可以负责：

* Definition
* Reference
* Implementation
* Type Resolution

特别用于未来增强 C++ 等语言的跨文件解析准确率。

V1 不要求完整实现 LSP，但架构不要阻止未来接入 clangd 等 Language Server。

---

# 8. Retrieval Engine

不要设计成单一 Vector RAG。

期望：

```text
                 Query
                   │
             Query Planner
                   │
      ┌────────────┼────────────┐
      ▼            ▼            ▼
Exact / FTS     Semantic       Graph
      │            │            │
      └────────────┼────────────┘
                   ▼
              Candidate Set
                   ↓
                  RRF
                   ↓
            Graph Expansion
                   ↓
        Evidence-aware Rerank
                   ↓
              ContextPack
```

重点研究：

### RagCode

参考：

* Query Planner
* Retrieval Orchestration
* Evidence Layer
* ContextPack
* Missing Evidence
* Freshness
* Context Budget

### GitNexus

参考：

* BM25
* Semantic
* RRF
* Process / Flow
* Graph + Retrieval Fusion

### CodeGraph

参考：

* Structural Graph
* Symbol Graph
* Cross-file relationship
* Graph Traversal
* Context exploration

---

# 9. Spec / Docs

nova 不应该只理解“代码现在怎么写”。

还应该理解：

> 项目原本要求怎么实现。

需要把以下内容纳入索引：

* README
* AGENTS.md
* Markdown
* Architecture
* Requirement
* Spec
* ADR
* API Document
* Protocol Document
* Config

V1 优先做确定性 Markdown Parser。

例如：

```text
SpecBlock
- path
- heading
- content
- line range
- references
- mentioned symbols
- mentioned paths
```

不要让 LLM 自动创建强事实关系：

```text
implements_spec
```

LLM 产生的关系只能标记为 inferred / unverified。

需要明确区分：

```text
Code Evidence
Spec Evidence
Runtime Evidence
Inference
```

---

# 10. ContextPack

ContextPack 是 nova 的核心产品之一。

不要简单返回：

```text
Top 20 chunks
```

而应该组织为：

```text
Brief
Freshness
Relevant Files
Symbols
Code Flow
Evidence
Spec Constraints
Missing Evidence
Recommended Reads
Citations
```

需要严格控制：

* token budget
* duplicate snippets
* large files
* repeated context
* low-confidence evidence

最终目标：

> 使用最小 Context 让 Coding Agent 理解问题。

---

# 11. Service 层

nova 最终部署为长期运行的 Server。

请设计独立 Service Layer。

至少考虑：

```text
API Gateway
Auth
User
Workspace
Project
Repository
Sync Service
Index Service
Retrieval Service
Graph Service
LLM Service
Task / Job Service
Usage / Quota
Observability
```

Fast 查询需要尽量同步返回。

Deep 查询可内部执行多阶段 Pipeline，但 MCP 调用最终仍然等待本次结果完成。

> 本节关于同步等待与异步返回的描述属于原始需求，仅供参考；当前同步、索引任务和首次全量处理行为以 research/Module/05-MCP与同步.md 和 research/Module/06-服务化与部署.md 为准。

原始要求：暂时不做异步“稍后返回”的产品设计。

---

# 12. MCP 层

MCP 层应保持 Thin Adapter。

不要把 Retrieval Logic 写进 MCP handler。

期望：

```text
MCP
 ↓
Application Service
 ↓
Context Engine
 ↓
Retrieval / Graph / Index
```

需要支持：

* stdio MCP client / adapter
* remote nova service
* Authentication
* Project / Workspace Selection
* Tool Schema
* Error Handling
* Timeout
* Structured Response

MCP stdout 必须保持协议纯净，日志走 stderr。

---

# 13. Web UI

nova 后续需要 Web UI。

首先实现产品基础能力，而不是复杂可视化。

V1 UI 重点：

```text
Login
Register（可配置关闭）
Project List
Project Detail
Workspace / Repository
Index Status
Last Sync
Files Indexed
Graph Statistics
Search Playground
ask_project Playground
API Token / MCP Configuration
Usage
Settings
```

后续可以考虑：

* Graph Visualization
* Call Flow Visualization
* Retrieval Trace
* ContextPack Inspector
* Query Debugger
* Ranking Debugger

---

# 14. 用户与鉴权

因为代码会同步至远端 VPS，这部分属于核心架构，不要后补。

至少设计：

```text
User
Organization（未来）
Project
Repository
Workspace
API Token
Session
```

要求：

* Password Hash
* Session / JWT 方案评估
* API Token
* Token revoke
* Project Ownership
* 数据隔离
* Repository isolation
* Rate Limit
* Audit Log
* HTTPS
* secret 不进入日志

重点考虑未来多用户场景：

```text
User A
不能访问
User B 的 Code / Vector / Graph / Context
```

所有 Index / Search / Graph 查询必须明确 project scope。

---

# 15. 数据安全

因为用户会上传源码，必须作为核心需求设计。

需要评估：

* VPS 数据存储边界
* TLS
* encryption at rest
* API Token
* password security
* tenant isolation
* log redaction
* source retention
* project delete
* index delete
* embedding 数据删除
* LLM Provider 是否会接触代码
* LLM Provider 配置
* local / self-hosted LLM 的可能性

需要做到：

> 用户明确知道哪些代码会离开本地、发送到哪里。

---

# 16. Grounded LLM

`ask_project` 使用服务器侧 LLM。

LLM 不直接面对整个仓库。

只能基于 Retrieval Engine 提供的 ContextPack 回答。

约束：

```text
只依据 Evidence
必须给引用
区分事实与推断
证据不足时输出 Missing Evidence
禁止虚构调用关系
禁止把 Spec 当作当前实现
禁止把代码实现当作设计要求
```

未来应允许配置：

* OpenAI-compatible API
* Local Model
* Company Internal Model

LLM Provider 必须作为独立接口，不与 Retrieval 强耦合。

---

# 17. 推荐工程分层

请基于源码审计后自行判断最终目录，不需要机械采用以下方案。

可以参考：

```text
nova/
├── apps/
│   ├── server/
│   ├── web/
│   └── cli/
│
├── packages/
│   ├── core/
│   ├── parser/
│   ├── indexing/
│   ├── graph/
│   ├── retrieval/
│   ├── semantic/
│   ├── spec/
│   ├── context/
│   ├── sync/
│   ├── auth/
│   ├── llm/
│   ├── mcp/
│   └── shared/
│
├── docs/
├── benchmark/
└── tests/
```

核心原则：

* 高内聚
* 低耦合
* 单一职责
* 边界清晰
* 接口稳定
* 依赖方向明确
* 实现可替换
* 易测试

特别要求：

```text
MCP 不依赖具体 Retrieval 实现
Retrieval 不依赖 MCP
ContextPack 不依赖 Web
Graph Store 可替换
Vector Store 可替换
LLM Provider 可替换
Parser Provider 可扩展
```

---

# 18. 部署目标

最终服务部署在 VPS。

请设计：

```text
Internet
   │
 HTTPS
   ▼
Reverse Proxy
   │
   ├── Web
   └── API
         │
      nova-server
         │
 ┌───────┼──────────┐
 ▼       ▼          ▼
DB     Vector     Worker
        /Index
```

请评估：

* Docker Compose
* Reverse Proxy
* PostgreSQL vs SQLite
* pgvector / LanceDB
* Redis 是否真的需要
* background worker 是否真的需要
* object storage 是否需要
* backup
* migration
* health check
* logs
* metrics

原则：

> V1 优先简单可靠，不因为“未来可能需要”引入不必要的分布式组件。

单 VPS 能跑就不要过早微服务化。

---

# 19. 参考项目审计任务

请深入阅读 `source/` 中实际源码，而不是只看 README。

分别回答：

## RagCode

重点：

* ContextEngine
* Query Planner
* ContextPack
* Retrieval Fusion
* Graph Expansion
* Evidence
* MissingEvidence

哪些设计适合 nova？

---

## GitNexus

重点：

* Parser
* Index
* BM25
* Semantic
* RRF
* Graph
* Process / Flow
* MCP

哪些设计值得借鉴？

---

## CodeGraph

重点：

* Tree-sitter
* Symbol extraction
* Edge model
* Cross-file resolution
* Graph Store
* Graph traversal
* MCP explore

哪些部分适合作为 nova Code Intelligence 基础？

---

## notace-tool-rs

重点：

* Workspace scan
* Incremental blob sync
* MCP stdio
* Local cache
* Remote server boundary
* Error / Timeout

重点学习它的产品边界，不推测不可见的 ACE Server 实现。

---

# 20. 当前阶段不要急着开发

当前任务首先是完成 **nova 的架构设计和工程骨架决策**。

请：

1. 阅读 `source/` 四个项目关键源码。
2. 阅读 `nova/README.md`。
3. 对照上述产品目标。
4. 识别参考项目中真正可以借鉴的设计。
5. 给出 nova 推荐架构。
6. 明确模块边界和依赖方向。
7. 明确本地 MCP Client 与远端 Service 的边界。
8. 明确 Index / Retrieval / Graph / Context / LLM 的数据流。
9. 明确 User / Project / Auth / Web UI 设计。
10. 明确 VPS V1 部署方案。
11. 给出 V1 → V2 演进路径。

如果现有 README 与架构冲突，可以修改 `nova/` 内文档。

禁止修改 `source/`。

---

# 21. 需要输出的设计文档

建议最终在 `nova/docs/` 形成：

```text
docs/
├── architecture.md
├── data-model.md
├── indexing.md
├── retrieval.md
├── code-intelligence.md
├── context-pack.md
├── mcp.md
├── sync-protocol.md
├── service.md
├── auth-security.md
├── web-ui.md
├── deployment.md
├── reference-analysis.md
└── roadmap.md
```

不要求为了凑数量机械拆文档，可以根据内容调整。

README 应最终能够清楚解释：

* nova 是什么
* 为什么需要它
* 核心架构
* Fast / Deep
* MCP 接入方式
* Local Client + Remote Service
* 技术特点
* 开发状态

---

# 22. 最终请重点回答

完成设计后，请给我一份简洁总结：

### 1. nova 最终架构

用 ASCII 图展示。

### 2. 模块划分

每个模块一句话说明职责。

### 3. Local / Server Boundary

哪些发生在开发机，哪些发生在 VPS。

### 4. Query Flow

分别展示：

```text
search_context
```

和：

```text
ask_project
```

完整数据流。

### 5. Reference Mapping

明确：

```text
RagCode     → 借鉴什么
GitNexus    → 借鉴什么
CodeGraph   → 借鉴什么
notace      → 借鉴什么
```

### 6. V1 Scope

明确第一版做什么、不做什么。

### 7. 技术选型

说明为什么选择：

* Runtime
* Database
* Vector Store
* Parser
* Web Framework
* MCP SDK
* LLM Provider abstraction
* Deployment

### 8. 风险

尤其关注：

* C++ Symbol Resolution
* Remote Source Security
* Index Freshness
* Retrieval Accuracy
* Context Token Budget
* VPS Resource Cost

---

# 23. 核心设计原则

整个设计始终围绕一个目标：

> **nova 的核心价值不是“搜索代码”，而是降低 Coding Agent 获取正确项目上下文的成本。**

最终衡量指标不是单纯 Search Latency，而是：

**Context Acquisition Cost**

即一个 Coding Agent 为正确理解当前任务所需要消耗的：

* 时间
* Tool Calls
* Token
* Context Window
* 人工干预

同时提高：

* Recall
* Flow Accuracy
* Citation Validity
* Groundedness
* Answer Accuracy

请围绕这个目标做所有架构取舍，而不是为了使用 RAG、Graph、Vector、LLM 等技术而使用这些技术。
