# nova 核心引擎技术方案：切片存储、混合检索与可选 AI 总结

> 定位：基于 00-05 调研文档的组合设计提案，不是最终架构决定。
> 范围：只覆盖核心引擎（索引管道 + 检索引擎 + AI 总结层）。MCP 工具、Web UI、Service 后端形态不在本文范围。
> 已确认输入：业务场景以 C/C++/Python 为主；AI 总结模型准备用 deepseek-v4-flash（OpenAI-compatible，URL/KEY 可配置，总结可选）。
> 每个设计点标注出处（参考项目 + 源码路径可复核），标注"无参考"的是 nova 自研点。

## 0. 全景数据流

```
【索引期：一次性/增量】
代码文件 → AST 解析(tree-sitter) → Chunk(符号级切片) + Symbol + Edge
    ├─→ SQLite：结构化表 + FTS5(BM25)          ← codegraph 模式
    ├─→ 向量库：chunk embedding                 ← ragcode 模式
    └─→ 图数据：edges 表（calls/imports/...）    ← codegraph 模式

【检索期：每次查询】
Query → 并行四路召回 → RRF 融合 → 图扩展 → 预算内组装 ContextPack
    ├─ search_context：直接返回 ContextPack（快，不调 LLM）
    └─ ask_project：ContextPack → LLM(deepseek-v4-flash) → 带引用的总结
```

两种消费形态共享同一条检索管线，唯一分叉点在 ContextPack 组装完成之后——这保证快慢模式不会长成两套系统。

---

## 1. 切片存储

### 1.1 核心原则：不要按行切，按符号切

notace 的 400 行机械切法是**薄客户端的权宜之计**（它服务端看不到 AST，见 01 文档 §7.5）。nova 服务端有全量代码，应该做 **AST-aware 符号级切片**。

**切法**（ragcode 的 ChunkKind 模式，src/core/types.ts）：

```text
一个 Python 文件 example.py：

def validate_token(token: str) -> bool:      ← Chunk 1（函数，含 docstring+签名+体）
    """校验 token 有效性"""
    ...

class TokenService:                          ← Chunk 2（类的骨架：签名+方法签名列表）
    def refresh(self, t):                    ← Chunk 3（方法）
        ...
# 模块级散落代码                              ← Chunk 4（块级兜底）
```

- 函数/方法 → 一个 chunk（函数体完整，不跨符号切）
- 类 → 生成"骨架 chunk"（类签名 + 方法签名列表，不含全部方法体——大类不撑爆上下文）+ 每个方法单独 chunk
- 文件头 import 区 + 模块级代码 → 兜底 chunk
- 超长函数体 → chunk 保留完整，渲染时支持 skeleton 展开。**存储切完整，返回可裁剪**：检索和阅读是两个阶段，不要在存储层提前裁剪

为什么函数级是甜点：
1. 检索命中的粒度恰好是"agent 想看的东西"
2. 函数级 chunk 的 embedding 语义最纯（一个函数讲一件事）
3. 图扩展的单位（caller/callee）天然对应符号 → chunk 可反查

### 1.2 统一数据模型（SQLite 表结构）

借鉴 codegraph 的 schema（src/db/schema.sql）+ ragcode 的分层：

```sql
-- 文件表：增量检测的锚点
CREATE TABLE files (
    path            TEXT PRIMARY KEY,     -- repo 内相对路径，正斜杠
    content_hash    TEXT NOT NULL,        -- sha256(内容)，变了就重索引该文件
    language        TEXT NOT NULL,        -- c/cpp/python/markdown
    indexed_at      INTEGER NOT NULL,
    generated       INTEGER DEFAULT 0,    -- 生成代码标记，索引期判定（codegraph 下沉设计）
    branch          TEXT, commit          TEXT   -- git 元数据
);

-- 切片表：检索的主战场
CREATE TABLE chunks (
    id           TEXT PRIMARY KEY,        -- {repo}:{path}:{symbol_fqn} 稳定ID
    file_path    TEXT NOT NULL,
    symbol_fqn   TEXT,                    -- 完全限定名，如 auth.TokenService.refresh
    symbol_kind  TEXT,                    -- function/class/method/module_block
    start_line   INTEGER, end_line INTEGER,
    signature    TEXT,                    -- 函数签名行
    docstring    TEXT,
    content      TEXT NOT NULL,           -- 完整切片内容
    content_hash TEXT NOT NULL            -- 增量：内容变了才重嵌入
);

-- 符号表：exact 检索 + 图节点
CREATE TABLE symbols (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,             -- 短名 refresh
    fqn        TEXT NOT NULL,             -- 全名 auth.TokenService.refresh
    kind       TEXT NOT NULL,
    chunk_id   TEXT,                      -- 指向所属 chunk
    file_path  TEXT, start_line, end_line,
    is_exported INTEGER DEFAULT 0
);

-- 边表：代码关系图（13 种，来自 codegraph EDGE_KINDS）
CREATE TABLE edges (
    source     TEXT NOT NULL,   -- symbol.id
    target     TEXT NOT NULL,
    kind       TEXT NOT NULL,   -- calls/imports/exports/extends/implements/
                                -- references/contains/instantiates/overrides...
    line       INTEGER,         -- 发生位置
    provenance TEXT DEFAULT 'parsed'   -- parsed=真实解析 / synthesized=推断合成
);
-- 边唯一性：(source, target, kind, IFNULL(line,-1)) 建 UNIQUE INDEX
-- codegraph #1034 教训：无唯一约束时重复边导致 callers/impact 计数翻倍

-- BM25 全文索引（SQLite 原生 FTS5，零额外组件）
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    content, signature, docstring, path,
    content='chunks', content_rowid='rowid', tokenize='unicode61'
);
-- 外部内容表 + 触发器同步：更新 chunk 自动维护 FTS

-- 未解析引用：两阶段解析的暂存区（codegraph 最有价值的模式）
CREATE TABLE unresolved_refs (
    from_node_id TEXT, reference_name TEXT,
    status TEXT DEFAULT 'pending'   -- pending→resolved(删除)/failed(保留重试)
);
```

**三个关键设计**：

1. **边表唯一索引**：一条 UNIQUE 根治重复边事故（codegraph 踩过的坑）。
2. **unresolved_refs 生命周期**：解析阶段遇到跨文件调用解不开，先存 pending；全量索引完成后第二遍统一解析，解不开的标 failed 保留，**下次增量索引有新符号时自动重试**（codegraph #1240）。不需要"完美解析才能出图"。
3. **provenance 列**：区分真实解析边与推断合成边——这是 Task.md "verified vs inferred" 证据分级的落地形态。

### 1.3 向量存储

embedding 的文本**不是裸代码**，ragcode 验证过的组合：

```text
embed_text = f"路径: {path}\n符号: {fqn}\n签名: {signature}\n文档: {docstring}\n代码: {content}"
```

带符号名和路径前缀的 embedding，比裸代码块检索质量高得多（语义查询"哪里校验 token"能命中 auth.TokenService.refresh，因为符号名进入了向量语义空间）。

选型（单 VPS 前提）：

| 方案 | 适合 | 依据 |
|---|---|---|
| **LanceDB（推荐 V1）** | 嵌入式、零运维、列存快 | ragcode 生产验证（src/semantic/lance-semantic-store.ts），可参考其集成方式 |
| pgvector | 已上 PostgreSQL 且想统一 SQL | 备选，多一次 ANN 服务进程的运维 |
| ~~独立向量数据库（Milvus/Qdrant）~~ | ~~V1 不需要~~ | 单 VPS 反模式；GitNexus 教训：不要为未来引入分布式 |

embedding provider 抽象（ragcode 模式）：

```text
EmbeddingProvider 接口
  ├─ DeterministicEmbedding（零配置下限：hash 向量，离线也能跑，只是召回差）
  ├─ OpenAICompatible（配 URL+KEY，接 DeepSeek/硅基流动/自托管）
  └─ 本地小模型（后续可选）
```

首跑零配置可用（deterministic），配置 API 后语义通道才真正发力——ragcode 的"offline-first"策略对开源项目冷启动非常重要。

### 1.4 增量更新（generation 代机制）

ragcode 的分代表（generation table）模式（src/core/contracts.ts 的 prepare/validate/commit），解决"改一个文件要不要全量重嵌入"：

```text
1. file.content_hash 没变 → 完全跳过
2. 变了 → 删旧 chunks/edges → 重新解析写入新 chunks
3. chunk.content_hash 没变的（同文件内没改的函数）→ 向量直接复用
4. 增量完成后：generation+1，原子切换活跃代
```

效果：改 1 个函数，只重嵌入 1 个 chunk。对 embedding API 成本和索引延迟都是数量级的差别。

---

## 2. 检索策略（核心）

### 2.1 四路并行召回

```text
                     Query: "哪里处理 token 过期刷新？"
                              │
        ┌──────────┬──────────┼──────────┬──────────┐
        ▼          ▼          ▼          ▼
   ① Exact     ② BM25     ③ Vector   ④ Graph种子
   符号精确命中  FTS5全文    语义召回    （若有符号名）
   "refresh"   过期/刷新/    token过期   → 反查 callers/callees
   (正则抽符号)  token/...   (embedding)  作为候选补充
```

**① Exact**（codegraph 的正则方案，src/context/index.ts 的 extractSymbolsFromQuery）：从 query 抽驼峰/蛇形/SCREAMING 标识符，直接查 symbols 表精确命中。查询里有 TokenService 这种词时它秒中，且是最高置信证据。

**② BM25**：FTS5 直接查。两个注意点：
- 中文查询要 CJK 分词（GitNexus 有专门的 cjk-segmentation.ts；SQLite 侧可按 bigram 切，或查询前用 jieba 处理）
- 生成代码文件降权（files.generated 索引期已判定，排序期直接用，不读文件头）

**③ Vector**：query embedding → ANN → top-K chunk。这是最慢通道（一次 API 往返 50-200ms），并行化设计时它决定下限。

**④ Graph**：①② 命中符号时，用 calls 边拉 1-2 跳 caller/callee chunk 作为候选（**只进候选池，不直接进结果**——由融合和重排决定去留）。图参与检索的正确姿势：**图不是检索通道，是扩展器**。

### 2.2 RRF 融合（标准公式，GitNexus/Elasticsearch 共识）

```text
score(chunk) = Σ_通道  1 / (K + rank_通道(chunk))     K = 60
```

每路通道只出排名（不出分数），RRF 按排名融合。好处：
1. 免分数归一化（BM25 分数和余弦距离不可比，RRF 直接绕开）
2. 实现约 20 行（GitNexus 的 mergeWithRRF，src/core/search/hybrid-search.ts 是完整参考）
3. 可解释："这个 chunk 被语义和 BM25 同时命中，所以排第一"

被 ≥2 通道同时命中的 chunk 天然排前——这就是混合检索的核心收益。

### 2.3 图扩展 + 证据分层

融合出 top 候选后：

```text
top candidates (RRF)
   → 对每个候选符号，沿 calls 边扩展 1 跳
   → 扩展来的 chunk 标记 evidenceTier = 3（图扩展，最低优先）
   → 原始命中：exact=0, keyword=1, semantic=2（ragcode 的 tier 模式）
   → 最终选择时强制按 tier 排序：精确命中 > 关键词 > 语义 > 图扩展
```

**可选 rerank**（V1.5 再加）：reranker 接口 + circuit breaker（ragcode 的 circuit-breaking-reranker.ts 模式），provider 挂了自动回退 RRF 结果，不阻塞主流程。

### 2.4 重要反面教材：不要做规则引擎

ragcode 的 query-planner 用 300+ 行正则做意图分类（13 种 intent），hybrid-retriever 里塞了 15 路专门化搜索（policy docs / config roles / migration companions...），单文件 141K 行，且明显对特定评测仓库过拟合（正则里硬编码了 iOS 关键词，见 03 文档 §3.3）。

**nova 只保留三件事**：语言检测 + 符号抽取 + 图种子识别。泛化能力靠四路召回 + RRF 本身。

---

## 3. ContextPack 组装（返回给 AI 之前的最后一公里）

检索输出不是 Top-K 裸列表，是带结构的证据包（ragcode ContextPack 合同的裁剪版，去掉 V1 不需要的 memory/budgetTrace 细项）：

```json
{
  "query": "...",
  "answerable": true,
  "confidence": "high|medium|low",
  "freshness": { "indexedAt": "...", "staleFiles": [] },
  "snippets": [
    {
      "path": "auth/token_service.py",
      "symbol": "TokenService.refresh",
      "lines": [45, 82],
      "content": "def refresh(self, t):\n    ...",
      "score": 0.031,
      "evidenceTier": 0,
      "reason": "符号精确命中 + BM25 命中",
      "elidedLines": 0
    }
  ],
  "callPaths": [ ["TokenService.refresh", "verify_expiry", "rotate_token"] ],
  "missingEvidence": [
    { "code": "index_stale", "message": "3 个文件待索引，结果可能过期" }
  ],
  "budget": { "usedTokens": 8200, "hardCap": 12000, "truncated": false }
}
```

**预算控制三招**（防 ContextPack 撑爆模型窗口）：
1. 按 tier 排序装填，预算耗尽即停
2. 大 chunk 降级为 skeleton（签名 + 前后各 N 行），elidedLines 报告省略量
3. 去重：同文件多 chunk 合并相邻行区间；同一符号多版本（重载）只留一个

**MissingEvidence 的设计原则**（GitNexus epistemic envelope 思想，见 04 文档 §4）：缺证据不是一句话，是"code + message + 可选恢复参数"的可执行自愈指令。

---

## 4. AI 总结层（可选，可配置）

### 4.1 Provider 抽象

```typescript
interface AnswerProvider {
  // OpenAI-compatible: POST {BASE_URL}/v1/chat/completions
  complete(req: { system: string; user: string; maxTokens?: number }): Promise<string>;
}
// 配置：ANSWER_BASE_URL / ANSWER_API_KEY / ANSWER_MODEL
// deepseek-v4-flash 走 OpenAI-compatible 协议，零适配成本
```

关键点：
- **AnswerProvider 与检索完全解耦**（Task.md 原则；ragcode 的 RerankerProvider 同款模式）
- search_context 永不调它；ask_project = 检索 + 它
- 配置为空时 ask_project 直接报"未配置总结模型，请用 search_context"——功能降级而非报错

### 4.2 Prompt 结构（grounded 约束）

```text
System:
你是代码调查助手。只依据 <evidence> 标签内的证据回答。
规则：
1. 每个结论必须标注来源 [path:lines]
2. 证据不足时明确说"证据不足"，不要猜
3. 区分"代码事实"与"你的推断"，推断标注 [推断]
4. 禁止虚构调用关系；调用关系只信 callPaths 字段

User:
<question>用户的原始问题</question>
<evidence>
{ContextPack 渲染成 markdown}
</evidence>
```

### 4.3 deepseek-v4-flash 的适配要点

flash 类模型快且便宜但推理容量有限，对输入质量更敏感：

| 适配点 | 做法 | 理由 |
|---|---|---|
| 上下文预算 | ContextPack 硬顶压到 8-12K token | 长输入下 flash 的有效注意力衰减，**宁小勿大** |
| 输入结构化 | evidence 用清晰 markdown 分节（file/symbol/code） | 比裸 JSON 更适合小模型消费 |
| 引用格式 | 要求简单格式 [path:45-82] | 复杂 JSON 引用 schema 小模型学不稳 |
| 输出上限 | max_tokens 2-4K | answer 是"分析+指路"，不是长文 |
| 超时 | 30-60s | flash 秒级响应；对比 notace advisor 给 180s 是因为重推理模型 |
| 温度 | 0-0.3 | 代码调查要事实不要创意 |

成本参考：一次 ask_project ≈ 10K input + 2K output，flash 定价下单次成本可忽略。

---

## 5. 一次 ask_project 的完整时序

```text
0ms    MCP ask_project(query) → 服务端
5ms    符号抽取 + 语言检测（正则，无 LLM）
10ms   ┌ 并行：FTS5 查询(2ms) + embedding API 调用(50-200ms) + 符号精确查(1ms)
250ms  └ 四路候选汇合（embedding 是最慢通道）
260ms  RRF 融合 → top 50 候选
300ms  图扩展 1 跳 → 补充候选 + tier 标注
350ms  ContextPack 组装（预算裁剪、去重、skeleton）
       ├─ search_context：到此返回（<500ms 级）
400ms  ask_project：渲染 evidence → deepseek-v4-flash
2-5s   ← 返回 grounded answer + 引用
```

全链路没有一步是"新的未验证技术"——每个环节都有参考项目的生产验证背书，组合是新的。

---

## 6. 落地顺序（每步独立可验证）

1. **最小闭环**：SQLite + FTS5 + tree-sitter Python 切片 + BM25 单路检索 → 验证切片质量
2. **向量通道**：LanceDB + embedding provider 抽象 → RRF 融合
3. **图**：edges 表 + 两阶段解析（unresolved_refs）→ 图扩展进 ContextPack
4. **C/C++**：tree-sitter C 先行（codegraph 的 cfnptr 函数指针合成思想），C++ 允许"尽力而为 + unresolved 如实标注"
5. **AI 总结**：AnswerProvider + deepseek-v4-flash 配置项

第 1 步两三天能见结果；第 4 步是最大技术风险项，建议提前用 codegraph 在目标代码库上实测覆盖率（05 文档 §9.2）。

---

## 附：本文设计点与参考项目对照表

| 设计点 | 参考来源 | 借鉴方式 |
|---|---|---|
| AST 符号级切片 | ragcode ChunkKind | 思想 |
| 表结构 / 边唯一性 / unresolved_refs | codegraph schema.sql | 思想参考 |
| generation 增量向量 | ragcode 分代表 | 思想 |
| 四路召回中符号正则抽取 | codegraph extractSymbolsFromQuery | 思想 |
| RRF K=60 | GitNexus mergeWithRRF | 标准算法 |
| evidenceTier 分层 | ragcode | 思想 |
| 图作为扩展器而非通道 | GitNexus/GitNexus Process + codegraph 调用路径内嵌 | 思想 |
| ContextPack 字段裁剪 | ragcode ContextPack | 思想 |
| missingEvidence code 化 | GitNexus epistemic + ragcode hint code | 思想 |
| AnswerProvider 抽象 | ragcode RerankerProvider 同构模式 | 自研（接口形态参考） |
| deepseek-v4-flash 适配 | 无参考 | 自研 |
| C/C++ 函数指针合成 | codegraph cfnptr.rs | 可参考其设计思路 |
| 反面教材：规则引擎 planner | ragcode query-planner | 明确不采纳 |
