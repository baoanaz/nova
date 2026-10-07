# source/ 参考项目调研总览

> 调研日期：2025-09（基于 source/ 下四个项目的实际源码阅读）
> 调研方式：直接读源码，不依赖 README 宣传口径。每份分析均标注源码路径，可复核。

## 1. 四个项目一句话定位

| 项目 | 一句话定位 | | 语言/运行时 | 规模（源码文件数） |
|---|---|---|---|---|
| notace-tool-rs | 本地 MCP stdio 薄客户端，把代码以 blob 形式增量同步到远端 Finnian/ACE 服务做检索 | | Rust，~2k 行 | 9 个文件（极小） |
| codegraph | 100% 本地的 Tree-sitter 代码图引擎，SQLite 存图，MCP 暴露 explore 等工具 | | TypeScript + Rust kernel（napi） | 619 |
| ragcode | 100% 本地的 Context Engine，SQLite 图 + LanceDB 向量 + 混合检索 + ContextPack 合同 |  | TypeScript (Node ≥ 24) | 534 |
| GitNexus | 本地优先的知识图谱代码智能（LadybugDB/Cypher），BM25+语义+RRF，社区/流程检测，带可选 Server 模式 |  | TypeScript | 2839 |

## 3. 四个项目的架构光谱

四个项目恰好覆盖了 nova 设想的完整光谱：

```
纯薄客户端 ←──────────────────────────────────→ 纯本地引擎
    │                    │                          │
notace-tool-rs        GitNexus/CodeGraph         ragcode
（本地只做同步，      （本地全量索引，            （本地全量索引 +
 检索/LLM 全在远端）   无向量或可选向量）           向量+融合+ContextPack）
```

- notace-tool-rs 展示了"本地 MCP client + 远端 Service"的边界怎么切（nova 的形态）
- codegraph 展示了 Tree-sitter 符号/边抽取 + 跨文件解析的工程化（nova 的 Code Intelligence 层）
- ragcode 展示了混合检索 + ContextPack + 证据分层的完整检索合同（nova 的 Retrieval/Context 层）
- GitNexus 展示了图数据库 + Process/Flow + 认知诚实性的上限（nova 的 Graph/深度分析层）

## 4. 关键能力矩阵

| 能力 | notace | codegraph | ragcode | GitNexus |
|---|---|---|---|---|
| C / C++ 解析 | 服务端（不可见） | Rust kernel，C 92.2% / C++ 94.8% 实测覆盖率 | 无（TS/Python/Go/Rust/Java） | tree-sitter，含 two-phase lookup、模板约束等深度语义 |
| Python 解析 | 服务端 | Rust kernel，psf/requests 100% | tree-sitter | tree-sitter |
| Kotlin | - | Rust kernel | 无 | vendored grammar（自建 prebuilds） |
| Markdown/Spec | 服务端 | 无专门处理 | 有文档系统（多格式+OCR，过度设计） | markdown phase（Section 节点+交叉链接） |
| 向量检索 | 服务端 | **无** | LanceDB + 可插拔 provider | LadybugDB 内置向量（arctic-embed 384D，本地 ONNX） |
| BM25/FTS | 服务端 | SQLite FTS5 | SQLite FTS | LadybugDB FTS + CJK 分词 |
| RRF 融合 | 服务端 | 无 | 自研加权融合（非标准 RRF） | 标准 RRF (K=60) |
| 图遍历/影响面 | 服务端 | callers/callees/impact/explore | impact/trace_flow/verified_subgraph | cypher/context/impact/trace/pdg |
| ContextPack | 服务端 | explore（单工具返回源码分组） | **最完整**（brief/freshness/evidence tiers/edit-readiness/budget trace/missing evidence） | query 返回 Process 分组 |
| 增量同步 | **最成熟**（blob hash + checkpoint + stale 恢复） | fs.watch + git hooks + worktree | lazy freshness gate + watch daemon + dirty file 状态机 | git commit 对比 + embedding cache |
| 多用户/鉴权 | 服务端 bearer token | 无 | 无 | Server 模式单 token（很弱） |
| MCP 工具数量 | 4 | 8 | 25+ | 16+ |

## 5. 各项目深潜文档索引

- 01-notace-tool-rs.md — 本地同步与远端边界的参考
- 02-codegraph.md — Tree-sitter 抽取与跨文件解析的参考
- 03-ragcode.md — 检索编排与 ContextPack 合同的参考
- 04-gitnexus.md — 图模型与认知诚实性的参考
- 05-implications-for-nova.md — 综合对 nova 的启示（含 C/C++/Python 场景、MCP 切片选项）

## 6. 最重要的十个发现（TL;DR）

1. **notace 的 blob 同步协议**：sha256(path||content) 内容寻址、verified cache hit（mtime 匹配仍验 hash）、checkpoint 避免重复上传 2.5MB scope、stale blob 自愈重试——这一套是 nova 本地 client ↔ VPS 同步的直接蓝本（思想层面）。
2. **codegraph 的 Rust napi kernel**：每文件只跨一次 JS/Rust 边界，16+ 语言 extractor 全在 Rust 侧；这是"Tree-sitter 批量解析性能"的工程答案。
3. **codegraph 的 unresolved_refs 生命周期**：pending → resolved(删除) / failed(保留，name_tail 索引，新符号出现时重试)——跨文件解析的两阶段模式值得 nova 采用。
4. **codegraph 的 explore 工具设计**：一个工具返回"按文件分组的真实源码 + 调用路径"，并明确告诉 agent "不要重新 Read 这些文件"；低置信度时诚实降级并指路。这是 search_context 的最佳参考。
5. **ragcode 的 ContextPack 合同**：evidence tier(0-3)、edit-readiness 三态判定、budget trace、missingEvidence 带 hint code 和恢复参数——上下文"合同"设计最完整。
6. **ragcode 的教训**：query planner 是 300+ 行硬编码正则规则，含大量对特定评测场景过拟合的 operator（如 iOS 关键词）；hybrid-retriever 141K 行里塞了 15+ 个专门化 evidence 搜索。**规则引擎检索规划在真实世界不可扩展**，nova 应走"少规则 + LLM/generic 信号"路线。
7. **GitNexus 的 epistemic envelope**：context 工具返回 exact/lower-bound + 五种 causes（receiverTyping、dispatchBoundary、externalBoundary...），明确告诉 agent"图里缺什么、为什么缺"。这是对 nova "Missing Evidence" 设计的最佳补充。
8. **GitNexus 的 Process/Flow 提取**：入口点（无内部调用者）→ DFS 前向追踪 → 去重 → 启发式命名；配合 Leiden 社区检测。"执行流程"是 agent 最能消费的图形态。9. **规模教训**：GitNexus 2839 文件、ragcode 534 文件已呈现复杂度失控迹象（单文件 100-335K）；notace 9 个文件干完了同步层的全部工作。nova V1 应该向 notace 的克制看齐，而非 GitNexus 的铺开。
