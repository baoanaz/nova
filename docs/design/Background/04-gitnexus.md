# GitNexus 调研分析

> 仓库：source/GitNexus（monorepo：gitnexus 主包 + gitnexus-shared + web + claude-plugin + cursor 集成）
> 定位：企业级代码库知识图谱——"把任何代码库索引成知识图谱：每条依赖、调用链、聚类、执行流"，CLI+MCP 本地为主，可选 Web/Server 模式

## 1. 架构总览

```
gitnexus analyze（CLI）
   → 19 阶段 DAG pipeline（typed deps，Kahn 拓扑执行）
   → KnowledgeGraph（内存累积，columnar 关系存储优化）
   → LadybugDB（Kuzu 衍生的嵌入式图数据库，Cypher 查询，CSV 流式加载）
   → FTS 索引 + 可选 embeddings（arctic-embed-xs 384D，本地 ONNX）
gitnexus mcp（stdio 默认 / --http Streamable HTTP + SSE）
gitnexus serve（Server 模式：analyze 任务 + git clone + 上传摄取，面向 Render 部署）
```

### Pipeline Phase DAG（ARCHITECTURE.md，设计最系统的部分）
```
scan → structure → [springConfig, markdown, cobol] → parse → [routes, tools, orm]
  → crossFile → scopeResolution → [springAutoConfiguration, springAop]
  → pruneLocalSymbols → mro → springAopInheritance → di → communities → processes
```
- 每阶段：独立文件 + 显式 deps + typed output；runner 过滤 deps 防隐藏耦合
- **单图累加器**：所有阶段改同一个 KnowledgeGraph
- 可跳过阶段（skipGraphPhases / skipDerivedGraphPhases）支撑快速测试与增量

## 2. 图模型（LadybugDB schema）

- **节点表按类型分表**：File/Folder/Function/Class/Interface/Method/Constructor/CodeElement/Struct/Enum/Macro/Typedef/Union/Namespace/Trait/Impl/TypeAlias/Const/Static/Property/Record/Delegate/Annotation/Template/Module/**Community/Process**/Route/Tool/Section/Embedding
- **单 CodeRelation 表**：28 种 type（CONTAINS/DEFINES/CALLS/IMPORTS/INHERITS/EXTENDS/IMPLEMENTS/.../HANDLES_ROUTE/FETCHES/HANDLES_TOOL/ENTRY_POINT_OF/INJECTS/ADVICED_BY/...），边带 type/confidence/reason/step 属性
- 与 codegraph 对比：codegraph 是"窄边表+类型列"（SQLite 单 edges 表），GitNexus 是"图数据库分表+统一关系表"。nova 用 SQLite/PG 的话更接近 codegraph 模式
- **过载消歧的 Node ID 方案**（Known limitations 一节，坦白得很）：ID 带参数数后缀 `#<paramCount>`，碰撞时加类型 hash `~type1,type2`，C++ const 加 `$const`。**并明说其代价**：新增重载会使 ID 变化（save#1 → save#1~int）

## 3. Process/Flow 提取（nova "Code Flow" 的最佳参考）

src/core/ingestion/process-processor.ts：
```
1. 找入口点（无内部调用者的函数，带 entry-point 评分 + 测试文件识别）
2. 沿 CALLS 边 DFS 前向追踪（maxTraceDepth=10, maxBranching=4, maxProcesses=75, minSteps=3）
3. 相似路径分组去重
4. 启发式命名（"HandleLogin → CreateSession"）
→ Process 节点 + STEP_IN_PROCESS 边（带 step 序号）
```
- 配合 Leiden 社区检测（communities phase，vendored graphology-leiden）：Process 分 intra/cross_community
- **query 工具直接以 Process 为检索单元**——"执行流"是 agent 最容易消费的图形态（比裸 callers/callees 更接近业务问题）

## 4. 认知诚实性：epistemic envelope（全项目最亮眼的设计）

context 工具（360° 符号视图）返回时附带：
```json
{
  "epistemic": "exact" | "lower-bound",   // lower-bound = 存在本视图未列出的调用者
  "boundaries": ["一句话一个人类可读边界"],
  "causes": {
    "scopeExtractionFiles": 0,    // 作用域抽取仍失败的文件数
    "receiverTyping": 0,          // 无法定型接收者而丢弃的调用点数
    "dispatchBoundary": 0,        // DI/接口分发边界外的符号数
    "externalBoundary": 0,        // 离开索引程序的调用（printf/fetch）
    "undecidedSatisfaction": 0    // 无法判定类型是否满足接口的对数
  }
}
```
- 每个字段都是"缺失量计数"而非散文；agent 可以据此决定是否信任结果、是否补索引
- 外部边界（externalBoundary）明确标注"不是缺陷"
- **"REQUIRES RE-INDEX"警告**：这些 causes 依赖新版本分析器写入的元数据，旧索引上的 0 不可信——连"零值本身可能是旧索引"这种元不确定性都建模了

对 nova：Task.md 的 Missing Evidence 设计应该升格为这种"**可计数的无知模型**"——不只是"缺什么"，而是"为什么缺、缺多少、能不能补"。

## 5. 混合检索

- BM25：LadybugDB 内置 FTS（每次查询直读库，无缓存漂移）；CJK 分词专门处理（cjk-segmentation.ts）
- 语义：arctic-embed-xs 384D 本地 ONNX（>50k 节点跳过）；增量靠 SHA1 内容 hash 缓存
- **标准 RRF (K=60)** 融合；group 模式下多 repo 结果也用 RRF 合并
- FTS 不可用时优雅降级为纯语义（修过 #1489 崩溃教训）

## 6. 语言支持（对 nova C/C++/Python 最有参考价值）

语言目录：C、C++、Java、Kotlin、Python、JS、TS、Go、Rust、Swift、PHP、Ruby、C#、Dart、Vue、Zig、COBOL + Spring 深度集成。

### C++（src/core/ingestion/languages/cpp/，工程深度惊人）
- two-phase-lookup（两阶段名字查找）、member-lookup（成员查找）、inline-namespaces、template constraints（模板约束抽取）、user-defined-conversions（用户定义转换序列）、conversion-rank、range-bindings、static linkage/file-local linkage
- 这是为了正确解析 C++ 调用关系做的**真正语义工作**（不是 tree-sitter 正则级），规模：captures.ts 86.7K、query.ts 32.3K 等
- 另有 cpp-ue-preprocessor（Unreal Engine 宏剥离）——说明 C/C++ 的宏预处理是实战需求

### C
- 头文件 wildcard 语义（#include 暴露全部符号）、first-wins MRO、static linkage 分析
- arity 元数据辅助调用消歧

### 通用语言 provider 模式
每个语言 = defineLanguage() 组合：class/field/method/variable/call extractor 配置 + import resolver 配置 + tree-sitter queries + scope captures + arity compatibility。**"语言 = 配置组合"的插件化思路**值得 nova 采纳。

## 7. 增量与存储

- git 感知：lastCommit == HEAD 则跳过（--force 重建）
- 存储：`<repo>/.gitnexus/`（lbug 数据库 + WAL + shadow + 单写锁 + dirty-recovery 停靠的 sidecar）+ `~/.gitnexus/registry.json`（全局仓库注册表，MCP 发现用）
- embedding 缓存：分析前 cache 旧向量 → 建图 → 批量恢复（内容 hash 对账）
- parse-cache.ts（70K）：跨 run 的解析缓存
- v8-sidecar / memory-budget：大仓库的内存工程

## 8. Server 模式（nova 远端形态的参考）

- `gitnexus serve`：私有服务（Render Blueprint：server $25 + web $7 + disk $2.5 ≈ $35/月，与 nova 的 VPS 预算同量级）
- analyze-job / analyze-worker（IPC + worker 进程）/ git-clone（远端拉取）/ upload-ingest（上传摄取）/ sse-progress
- HTTP MCP transport：默认 127.0.0.1，暴露 0.0.0.0 必须带 --auth-token 否则拒启；CORS 限制回环；timingSafeEqual 比较 token
- **鉴权薄弱**：单 token 全权限（README 自认"token 是唯一控制，持 token 可读所有已索引 repo"）——nova 的多用户/项目隔离需求必须自己补，这里没有现成答案

## 9. MCP 工具面（16+）

list_repos / query（Process 检索）/ cypher（裸图查询）/ **context**（360° 符号视图 + epistemic envelope）/ detect_changes / check / rename / impact / explain / pdg_query（可选 PDG）/ route_map / tool_map / shape_check / api_impact / group_list / group_sync / trace

特点：
- resources（gitnexus://repo/{name}/context、schema、process/{name}）配合工具使用
- 工具描述极长且教学化（context 工具的 description 有 40+ 行）
- 输出有 maxTokens 参数控制 MCP 响应体积

## 10. 值得 nova 借鉴（按优先级，仅思想层面）

1. **epistemic envelope / causes 计数**——Missing Evidence 的正确打开方式
2. **Process/Flow 提取**（入口点评分 + DFS 追踪 + 去重 + 命名）——"执行流"作为一等公民
3. **C++ 语义解析的投入清单**：two-phase lookup / 模板约束 / 转换序列 / inline namespace——nova 做 C++ 时的功能清单（V1 可以先不做，但架构不要堵死）
4. **Pipeline Phase DAG**：typed deps + 拓扑执行 + 单图累加——索引流水线的组织方式
5. **语言 = provider 配置组合** 的插件化
6. **Node ID 过载消歧方案的代价分析**（ID 稳定性 vs 消歧能力）
7. RRF (K=60) 标准融合 + CJK 分词
8. git commit 对比的增量判定 + registry.json 全局仓库注册

## 11. 不采纳 / 风险

1. Spring 深度集成（AOP/DI/Actuator/AsyncAPI）是 Java 企业场景特化，nova 场景（C/C++/Python）不需要
2. LadybugDB 依赖本身较新（0.19），生态风险；nova V1 用 SQLite/PG 更稳
3. 单 token 鉴权模型不满足 nova 多用户需求
4. 2839 文件的复杂度本身就是警告：nova 要控制规模
