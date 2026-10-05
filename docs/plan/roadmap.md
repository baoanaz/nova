# zace V1 Roadmap（Phase 0/1 详细版）

> 状态：2026-09-10 编排者制定。设计依据 `docs/design/INDEX.md` §3 决策登记表（D-01..D-45）。
> 规划粒度：Phase 0/1 到可执行任务卡（`docs/tasks/`）；Phase 2+ 只到里程碑，靠近时再细化（避免基于未验证假设过度规划）。

## 1. 北极星与验收口径

> 北极星（Demo.md §23）：降低 Coding Agent 的 **Context Acquisition Cost**（时间 / Tool Calls / Token / 人工干预），而不是搜索延迟本身。

| 里程碑 | 验收口径（可演示） | 对应交付 |
|---|---|---|
| **M1** core 闭环 | 在 zace 自身仓库 + 2 个真实仓库上：`zace-core ingest` → `zace-core search "中文/符号混合查询"` 返回 ContextPack Markdown（证据带行号、flows、missing evidence）；改 1 个函数只重嵌该 chunk；golden set 跑分出基线报告 | Phase 1（TASK-001..014） |
| **M2** MCP 闭环 | Codex/Cursor 通过 MCP 调用 `search_context`，VPS docker compose 部署，端到端 <2s（Fast）；首同步大仓库有进度反馈而非无限阻塞 | Phase 2（service + client） |
| **M3** Deep 质量 | `ask_project` 返回 grounded answer + citation 回验；Answerable=false 短路与 LLM 故障降级可用；Spec vs Implementation 差异可判 | Phase 3（flows/spec/deep/LLM） |
| **M4** 产品化 | Web 管理面 + Playground 可用；compose 一键部署（Caddy TLS）；对比基线（同任务 zace vs 纯 grep/read）的 Context Acquisition Cost 报告 | Phase 4（web/deploy/评测） |

M1 是唯一当前需要"战之能胜"的目标；M1 不达标（检索质量明显弱于 agent 自取上下文）时，不允许推进 M2 的体验建设。

## 2. Phase 划分与任务映射

### Phase 0 — 公共底座（编排者执行，冻结后子 AI 才能并行）

| # | 内容 | 产物 |
|---|---|---|
| P0-1 | monorepo 骨架 + 工具链 + CI | `core/ service/ client/ web/ benches/ docs/`、root `pyproject.toml`、`.github/workflows/ci.yml`、`scripts/check_dependency_direction.py` |
| P0-2 | 设计文档迁入 + 决策登记 | `docs/design/`（含 MANIFEST.sha256）、INDEX §3 更新（D-39 定稿、D-44/D-45 新增） |
| P0-3 | 契约冻结 | `docs/contracts/*`、`core/zace_core/{types,interfaces,hashing}.py` |
| P0-4 | 任务体系 | `docs/tasks/README.md`、任务卡模板与 Phase 1 全部卡片 |
| P0-5 | benches 骨架 | `benches/README.md`、`benches/golden/` 样例与格式定义 |

### Phase 1 — zace-core 最小闭环（M1，主战场）

```text
Lane A 索引（001..007）              Lane B 向量（008/009）
┌───────────────────────┐           ┌───────────────────┐
│001 存储+FTSS+CJK       │◄──────────│008 Embedding 双实现│
│002 Parser 基座+Python  │           │009 LanceDB 存储    │
│003 C 抽取器            │           └────────┬──────────┘
│004 C++ 抽取器          │                    │
│005 Markdown SpecBlock  │                    │
│006 Chunk 模型+二阶段解析│                    │
│007 索引流水线+增量失效   │◄───────────────────┘
└──────────┬────────────┘
           ▼
Lane C 检索（010..012）              Lane D 出口（013/014）
┌───────────────────────┐           ┌───────────────────────┐
│010 通道+RRF            │──────────►│013 ContextPack 组装    │
│011 图扩展+确定性 rerank │           │014 CLI+golden runner   │
└───────────────────────┘           │015 bake-off+校准       │
                                    └───────────────────────┘
```

| ID | 任务 | 依赖 | 一句话验收 |
|---|---|---|---|
| TASK-001 | 存储层（SQLite schema / FTS5 / jieba 预分词接入） | 无（契约已冻结） | 建库/写读/删/增量对账单测通过；中文 FTS 查询命中 |
| TASK-002 | Parser 基座 + Python 抽取器 | 契约 | 对 samples 的 Python 文件输出符号/边/unresolved 与期望一致 |
| TASK-003 | C 抽取器 | 002 基座 | 含 include 解析与函数指针 synthesized 边 |
| TASK-004 | C++ 抽取器（尽力而为 + 诚实标注） | 003 | 类/命名空间/重载/模板整体成符号；解不开进 unresolved |
| TASK-005 | Markdown SpecBlock 抽取 | 契约 | 标题树切块 + doctype 判定 + mentioned 抽取 |
| TASK-006 | Chunk 模型 + unresolved 两阶段解析 + 配置指纹 | 001/002..005（soft） | chunk_id/hash/骨架语义正确；pending→resolved/failed 生命周期单测 |
| TASK-007 | 索引流水线（ChangeSet → 增量失效 → 向量对账） | 001/006/008/009（soft） | 改 1 函数只重嵌 1 chunk；删除级联 stale |
| TASK-008 | Embedding Provider 双实现（本地 ONNX 默认 + API） | 契约 | 两个 provider 同接口；profile 指纹写入 index_config |
| TASK-009 | 向量存储（LanceDB）+ hash 复用对账 | 契约 | upsert/delete/search + 复用对账单测 |
| TASK-010 | 检索通道：Exact + BM25 + Vector + RRF（General） | 001/008/009 | 三通道可单独调参；RRF 融合可解释；向量故障降级双通道 |
| TASK-011 | 图扩展 + 确定性 rerank | 010（soft） | calls 1-hop + spec_references 双向；rerank 特征表落地 |
| TASK-012 | ContextPack 组装 + Markdown 渲染 | 010/011（soft） | 预算/单文件上限/tier3 配额/去重三招；渲染契约一致 |
| TASK-013 | core CLI + golden runner | 007/010/012 | `zace-core ingest/search/eval` 端到端可用 |
| TASK-014 | golden set 扩充 + 基线报告 | 013 | 50+ 中英混合查询 + recall/MRR 报告 |
| TASK-015 | embedding bake-off + rerank 初值校准 | 013/014 | 候选模型对比报告 + rerank 分值 v1 |

（依赖细节与"soft 依赖"含义见各卡；卡内"依赖"节的 blockedBy 是硬约束。）

### Phase 2 — service + client（M2）
service 外壳（REST/鉴权/租户/索引 job）+ Rust client（MCP + 懒同步 + checkpoint 自愈）→ MCP 端到端闭环。契约已冻结（CF-05/CF-06），可提前预研，但代码合并以 core 接口稳定为前提。

### Phase 3 — 检索质量与 Deep（M3）
路由四分支 + 结构路径 + Gap 二轮 + SpecBlock 检索权重 + AI 总结（citation 回验/降级/审计）。

### Phase 4 — 产品化（M4）
Web 管理面 + Playground；docker compose + TLS + 备份脚本；与"agent 纯 grep/read"的对照评测。

## 3. 关键路径与并行安全

- **关键路径**：TP-001 → TASK-006 → TASK-007 → TASK-010 → TASK-012 → TASK-013（Phase 1 内部）；对外关键路径是 Phase 2 的 service/client。
- **可并行度**：TASK-002/003/004/005/008/009 六张卡互不依赖（纯函数或独立模块），是拿到骨架后第一批可同时开工的卡。
- **文件所有权**：见各卡"交付物"节；同一目录同一时间只允许一张卡 in_progress（任务板状态表是唯一仲裁）。
- **集成点**：每张卡合并前必须保持全仓基线绿（ruff + 依赖方向 + pytest）；M1 集成由 TASK-013 卡承载验收，编排者执行一次端到端复核。

## 3.5 M1 达成状态（2026-09-10）

| 里程碑 | 口径 | 状态 |
|---|---|---|
| **M1** core 闭环 | 在 zace 自身 + 2 个真实仓库上可用；改 1 函数只重嵌 1 chunk；golden set 出基线 | **已达成**（2026-09-10）：zace-core 11.5k 行 / 501 tests；aibox 434 文件 / 5760 chunks 索引成功；smoke 集 e2e recall@5 0.593 |

**M1 遗留（有意延后，见 `docs/contracts/PROCESS.md` §3.6）**：

- R21 装填配比、R24 排序判别力、R31 answerable 脆弱点 —— 均**冻结待真实数据**（TASK-023 → TASK-050）；
- 当前 60 条 golden set 降级为 smoke + 回归护栏（R29），**不作为优化目标**。

下一阶段：`docs/plan/phase2-roadmap.md`（M2 = MCP 端到端闭环）。

## 4. 风险登记（Phase 0/1 视角）

| 风险 | 影响 | 缓解 |
|---|---|---|
| C++ 解析正确率低（模板/重载/两阶段查找） | 图扩展质量差，agent 不信任 | D-08 已定"尽力而为+诚实标注"；TASK-004 卡内要求 unresolved 如实进入 Missing Evidence；用真实 C++ 仓库样本回归 |
| 检索质量不达标（V1 胜负手） | 产品价值不成立 | M1 必须先出 golden set 基线（TASK-013/014/015）；rerank 分值走校准而非拍脑袋 |
| CJK 分词/中文查询不稳 | 一等场景失效（D-20） | TASK-001 卡内中文 FTS 验收用例必测；两侧同一分词器 |
| 首索引时长（大 C++ 仓库） | 体验崩 | Phase 1 用 benchmarks 实测单文件解析耗时；Phase 2 断点续传（D-31）兜底 |
| 多 AI 并行契约漂移 | 集成返工 | 契约冻结 + 文件所有权 + 卡片依赖 DAG + 编排者评审 |
| VPS 资源（embedding 本地推理 CPU） | 部署成本/延迟 | TASK-015 bake-off 出实测数据；必要时 API provider 兜底（D-44 双实现） |

## 5. V1 明确不做（继承 Module 各自的"V1 不做"清单）

LSP / compile_commands、C++ 模板实例化与两阶段查找、跨项目检索、注册表图谱可视化、Redis/队列/对象存储、k8s、常驻 watcher、流式输出、多轮 LLM 循环、速率限制（hosted 化前必须补）。
