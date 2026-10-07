# RagCode 调研分析

> 仓库：source/ragcode，TypeScript (Node ≥ 24, ESM)，npm `ragcode-context-engine`
> 定位：100% 本地的 MCP Context Engine——"让 coding agent 在改代码前先证明自己理解了代码库"
> 核心卖点：ContextPack 合同（引用/新鲜度/证据分层/edit-readiness/missing evidence）

## 1. 架构总览（分层严格，接口在 core，实现在外层）

```
CLI / MCP / Web Dashboard        （三个 surface）
        │
ContextEngine (src/core/engine.ts)   ← 唯一入口合同
        │
┌───────┼─────────┬──────────┬───────────┬─────────┐
indexing   graph     semantic    retrieval    context
(scan/     SQLite    LanceDB     planner      packer
 chunk/    +FTS      +provider   +fusion      +budget
 AST)                            +rerank
        │
      watch（增量新鲜度，lazy by default）
```

- `src/core/contracts.ts`：GraphStore / SemanticStore / EmbeddingProvider / RerankerProvider / ContextEngine 全部接口，生产实现（SQLite/Lance）与测试实现（内存）可互换
- MCP 层无检索逻辑（src/mcp/tools.ts 只做 schema 校验 + dispatch）——"thin adapter"原则执行得很干净，值得 nova 照抄
- 配置工厂（src/config/*-runtime.ts）从环境变量组装实现，每个 store 独立可替换

## 2. 数据模型（src/core/types.ts，最完整的部分）

核心实体链：
```
ProjectIdentity → RepoIndex → CodeFile(FileRole: source/test/config/docs/generated/vendor/build/minified)
                              → CodeChunk(ChunkKind: file/function/class/method/type/variable/block)
                              → SymbolNode(kind + 位置 + 导出性)
                              → GraphEdge(EdgeKind)
FreshnessReport（graph/semantic 双新鲜度、stale/pending/indexing/skipped/dirty 文件集、burst mode）
SearchHit(source: exact/keyword/semantic/graph + EvidenceTier 0-3 + scoreBreakdown)
```

### ContextPack（nova 最该抄的合同）
```typescript
ContextPack {
  query, mode(debug/feature/refactor/review/explain), answerable, confidence(low/med/high),
  confidenceEvidence,          // 术语覆盖率/grounded 数/精确命中数/得分边际/owner 集中度
  brief,                       // 一段话综述
  freshness: FreshnessReport,
  ownerChain: OwnerNode[],     // 涉及文件的 owner 角色与理由
  topology: TopologyEdge[],
  snippets: ContextSnippet[],  // 每条含 reason/score/role/expansionLevel/elidedLineCount
  relationships: RelationshipEvidence[],
  nextQueries, missingEvidence, missingEvidenceHints,  // 带 hint code + 恢复参数
  memorySnippets?,             // 跨 agent 共享记忆注入
  budgetChars, usedChars, budgetTrace  // 硬顶/目标/渲染字节/token 估算/截断降级记录
}
```

- **EvidenceTier 0-3**：精确命中 > 关键词 > 语义 > 图扩展，tier 顺序在最终选择时强制保持
- **expansionLevel 四级**：file_card / skeleton / focused_body / full_body——大文件默认骨架，snippet 报告省略行数
- **MissingEvidenceHint** 带 code（8 种：context_budget_truncated / index_stale / no_context_match...）和 expandContext 恢复参数——**缺证据不是一句话，是可执行的自愈指令**
- **EditReadiness 三态**：safe_to_edit_after_reading / investigate_only / not_enough_context
- CoverageSignal 六项：primary_owner_found / inbound_callers_checked / outbound_flow_checked / tests_checked / unresolved_edges_present / budget_truncated

### VerifiedCodeSubgraph
有界图遍历（默认 maxHops=4, MAX_NODES=32, MAX_EDGES=48, budget 10k chars）：seed 符号 → Dijkstra 风格按边代价扩展 → 节点角色（target/caller/callee/route/test/middleware/...）→ 边来源（ast/lsp/framework_rule/test_import/resource_rule/event_rule/heuristic）+ 置信度。**边的来源枚举就是 Task.md 说的 verified vs inferred 的实现方案。**

## 3. 检索流水线（重点 + 主要反面教材）

### 3.1 HybridRetriever（src/retrieval/hybrid-retriever.ts，141K 行）

```
query → planQuery（规则引擎）
  → 并行：exact(符号) + keyword(FTS) + semantic(Lance)
  → 15+ 个专门化 evidence 搜索（串行 await！）
     policy docs / platform entrypoints / directory listing / config roles /
     cross-language / multi-owner / companion / migration / constant /
     env vars / config-test chain / extension contract...
  → 加权融合（非标准 RRF：semantic 权重按 plan 动态调）
  → graph rerank（子图感知重排，43K 行）
  → 可选外部 reranker（circuit breaker + 失败回退）
  → EvidenceTier 顺序强制 → 模式加权 → 裁剪到 limit
```

### 3.2 QueryPlanner（src/retrieval/query-planner.ts）

- 13 种 intent（exact_symbol/caller_chain/callee_chain/docs_policy/config_schema/test_fixture/...）由**关键词正则**分类
- 输出 operators 列表驱动后续专门化搜索

### 3.3 教训（nova 必须避开的路径）

1. **规则过拟合**：query-planner 里有 `role:terminal-outcome`（匹配中文"终态"）、`role:swift-package`、`role:live-activity`、MLX 语音关键词等——这些是对**特定评测仓库**（iOS/macOS app）过拟合的规则。规则引擎检索规划在跨领域时不可迁移。
2. **15+ 专门化搜索串行执行**，每个都是 if-plan-operator-then-search——复杂度爆炸，hybrid-retriever 单文件 141K。
3. 融合是自定义加权而非标准 RRF，可解释性差。
4. **正面对照**：GitNexus 用标准 RRF + 单一 process 分组，ragcode 用规则 + 15 路专门化——前者可维护。nova 应：**少量通用信号（exact/FTS/semantic/graph）+ 标准 RRF + 图扩展 + 可选 rerank，规则只保留语言级（中英文分词）**。

## 4. 索引与新鲜度

- 解析：TS Compiler API（TS/JS）+ tree-sitter（Python/Go/Rust/Java）+ fallback analyzer；**无 C/C++/Kotlin**（nova 的场景它不覆盖，需要 codegraph/GitNexus 的方案补）
- chunker 按符号边界切块（AST-aware），不是机械行切
- **FreshnessGate（lazy 模式）**：读路径先做轻量 dirty 检查（indexStatusLight），stale/pending 文件 ≤1000 时按需刷新再回答；不装常驻 watcher（supervisor/hot 模式可选）
- 语义索引的**分代表（generation table）机制**：prepare（复用未变向量 + 只嵌入新增）→ validate → commit（原子换表指针）→ abort 可弃；增删改文件不会全量重嵌入
- watch：事件 journal + 文件合并（coalescing）+ 批量重索引调度 + dead-letter 状态

## 5. MCP 工具面（25+ 个，偏多）

index_repo / refresh_index / index_status / watch_status / record_file_events / search_code / **get_context** / get_project_brief / topology_map / find_symbol / explain_file / expand_node / find_owner / find_reuse_candidates / impact_analysis / explain_impact / related_tests / trace_flow / trace_request_flow / review_diff / memory_write/query/list/delete / agent_workflow_validate/status/report / 文档工具...

对比 Task.md 的 nova V1 两工具（search_context / ask_project）设计——ragcode 是"多细粒度工具"路线的极端；nova 的"少而厚"路线更接近 notace（4 个）和 codegraph（8 个）。**MCP 工具切片没有标准答案，但三家共识是：1 个主力上下文工具 + 少量补充工具**。

## 6. Memory 系统（独有特性）

项目级共享 agent 记忆（SQLite+FTS 事件存储）：决策/反馈/偏好/项目事实，MAB（多臂老虎机）注入路由（exploit/explore 槽位），记忆带 verificationState（unverified/verified/unverifiable/stale），**索引后自动重验证记忆与代码的一致性**。实现记忆仍是 advisory——实现事实必须对当前索引验证。这套对 nova V1 不是必需，但"记忆必须可验证"的原则值得记住。

## 7. 值得 nova 借鉴（按优先级）

1. **ContextPack 完整合同**（第 2 节）：tier/reason/budget trace/hint code/edit-readiness——nova ContextPack 的直接蓝本
2. **core/contracts.ts 的接口边界**：所有 surface 依赖合同而非实现，store 可替换——nova 工程分层的样板
3. **freshness lazy gate + 语义分代表**：读前按需刷新 + 增量向量不重嵌入
4. **snippet 的 expansionLevel + elided 计数**：大文件上下文的预算友好表达
5. **MCP thin adapter**：tools.ts 无检索逻辑

## 8. 明确不采纳

1. 规则引擎 query planner 与 15 路专门化 evidence 搜索（不可迁移、不可维护）
2. 25+ MCP 工具的碎片化
3. 文档系统（PDF/OCR/Office 多 gate）远超 nova V1 的 Markdown spec 需求
4. memory/MAB 系统（V2+ 再议）
5. 无 C/C++ 支持（nova 核心场景缺失）
