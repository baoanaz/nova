# 四项目调研对 nova 的综合启示

> 前置说明：Task.md 的愿景定位为"仅供参考"，本文以调研事实为依据给出启示。
> 已确认的边界条件：业务场景以 C/C++/Python 等常见语言为主；MCP 工具切片方案未定；产品形态（本地/远端切片）待定。
> 本文不是架构决定，是"证据 → 选项"的整理，供后续架构讨论使用。

## 1. 先回答最要紧的问题：四个项目谁最接近 nova？

**没有一个是。** nova 设想的形态 = notace 的 local/remote 边界 + codegraph 的符号抽取 + ragcode 的 ContextPack + GitNexus 的深度图分析，四者交集为空。这正是这个组合作为参考集的价值：nova 的每一层都有专门的参考对象。

各项目对 nova 各层的映射：

```
nova 层（Task.md 愿景）        首选参考            理由
────────────────────────────────────────────────────────────────
本地 MCP Client / 同步    notace-tool-rs    唯一做过 local↔remote 同步协议的
Code Intelligence(解析)   codegraph         C/C++/Python 实测覆盖率最好
                          (GitNexus 补 C++ 语义深度清单，仅思想)
检索编排/ContextPack      ragcode(ContextPack 合同) + GitNexus(RRF/Process)
Missing Evidence          GitNexus          epistemic envelope 完成度最高
ask_project(Grounded LLM) 无直接参考         四个项目都没做服务端 grounded answer
                          （notace 的 advisor 最接近，但服务端不可见）
```

## 2. C/C++/Python 场景的专门结论

这是与参考项目差异最大的地方，单独说：

### 2.1 C/C++ 解析是 nova 最大的技术风险，但有明确路线

- codegraph 证明了 tree-sitter 路线在 C/C++ 上可达 92-95% 跨文件覆盖率，且有函数指针合成器（cfnptr.rs 43K）处理 C 间接调用——**这是 nova 可参考的实现路线**
- GitNexus 给出了 C++ 语义解析的完整功能清单：two-phase lookup、模板约束、用户定义转换、inline namespace、UE 宏预处理——nova V1 可先不做，但 Parser Provider 接口要为它们留位置
- 两者共识：**C 的头文件 wildcard 语义 + 静态链接分析、C++ 的宏/模板**是主要工作量；AST 正则级处理不够，需要 scope 分析
- V1 务实建议：C 先行（语义简单），C++ 降到"尽力而为 + 标注 unresolved"（正好接 GitNexus 的 epistemic 模型——解析不了就如实报告）

### 2.2 Python 解析是舒适区

- 三个项目都用 tree-sitter 处理 Python，codegraph 在 psf/requests 上实测 100% 覆盖
- Python 的坑：动态特性（反射/猴子补丁）→ 参考项目的共同答案都是"如实标注 unresolved，不猜"

### 2.3 Kotlin

- GitNexus vendored tree-sitter-kotlin 并自建 prebuilds（上游只发源码）——若 nova 需要 Kotlin，这是已验证的可行路径，但 V1 建议砍掉（用户场景说 C/C++/Python 为主）

## 3. MCP 工具切片：调研给出的证据

四家切片对比：

| 项目 | 工具数 | 主力工具形态 | 效果证据 |
|---|---|---|---|
| notace | 4 | codebase_retrieval 一个主力 | 每次调用自动同步+检索，agent 零心智负担 |
| codegraph | 8 | explore 一个主力（"call FIRST for almost any question"） | description 明确导航到其他工具；低置信度诚实降级 |
| GitNexus | 16+ | query(process) / context(symbol) 双主力 | 工具描述写教学文案 |
| ragcode | 25+ | get_context 主力但碎片化严重 | 复杂度失控的样本 |

**共识证据**：
1. 每家都有且只有一个"主力上下文工具"（retrieval / explore / query / get_context）——nova 的 search_context 定位正确
2. ragcode 的 25+ 工具是反面教材；notace 的 4 个是正面样本
3. **工具 description 是行为控制层**：四家都把"何时用/何时不用/接下来用什么"写进 description（notace 甚至把"精确查找请用 grep"写进去）
4. codegraph 的洞察（源码注释）：agent 不会主动发现新工具（deferred-MCP 只选已知工具），**高价值信息必须烤进主力工具输出里**，不能指望 agent 自己去调 trace 工具

**对 nova 的含义**：V1 的 search_context + ask_project 两工具方案有充分依据；trace_flow/impact_analysis 等 V2 再说，且即使做了，其产出也应内嵌进 search_context 的 ContextPack（Code Flow 字段）而不是指望 agent 单独调。

## 4. 本地 vs 远端：notace 之外，三个本地项目也给了启示

Task.md 设想"本地 MCP client + VPS 服务"。调研补充了三个视角：

1. **notace 证明了薄客户端可行**：本地只做扫描/hash/上传，检索智能全在服务端——9 个文件搞定客户端。nova 本地 client 可以同样薄。
2. **但本地项目（codegraph/ragcode/GitNexus）的存在说明**：全本地形态有真实需求（隐私/离线/零延迟）。nova 服务端形态必须给出本地给不了的东西：跨设备共享索引、Grounded LLM、Web UI、多项目管理。**这个差异化要在架构文档里明确回答，否则用户为什么不直接用 codegraph？**
3. **GitNexus Server 模式的教训**：单 token 鉴权、Render 部署 $35/月——单用户自托管可活，多租户完全没做。nova 的 User/Project/Auth 是真空白，没有现成参考，需要自己设计。

### 4.1 同步协议要点清单（综合 notace 事实 + 其缺口）

notace 已验证的（nova 直接借鉴思想）：
- blob 内容寻址：sha256(path||content)，天然去重与幂等
- verified cache hit：mtime 只做快路径，hash 才是真相
- checkpoint scope：避免每次传全量 blob 列表
- 服务端裁决（skipped_blobs）+ 本地尊重
- 错误三分类 + token 脱敏 + body 截断
- 超时按操作分层（首全量 vs 增量）

notace 缺的（nova 必须补）：
- .gitignore / 自定义 ignore 支持（现在是硬编码目录列表）
- rename 处理（blob 模型免重传但索引语义要处理路径）
- branch/commit 元数据
- 同步状态的用户可见性
- AST 分块（notace 是 400 行机械切；nova 服务端懂结构，可以服务端再切，本地保持文件级——**推荐：本地传文件级 blob，服务端按 AST 语义切块**，这样客户端简单且分块策略可服务端升级）

## 5. 检索架构：从四家对比提炼的推荐形态

```
Query
  → 轻量意图处理（只做语言检测/分词/符号抽取——codegraph 的正则方案，
     不做 ragcode 的规则引擎）
  → 并行召回：Exact(符号) + FTS/BM25 + Vector + Graph(种子扩展)
  → RRF (K=60) 标准融合（GitNexus 方案；不用 ragcode 的自定义加权）
  → 图扩展（Process/Flow 感知——GitNexus 的 Process 单元值得引入）
  → 可选 rerank（ragcode 的 circuit breaker + 回退模式）
  → ContextPack 组装（ragcode 合同 + GitNexus epistemic envelope）
```

关键取舍依据：
- **ragcode 的失败证明**：规则化专门化检索不可维护（141K 单文件 + 场景过拟合）
- **GitNexus 的成功证明**：标准 RRF + Process 分组 + 诚实边界可维护且够用
- **codegraph 的补充**：低置信度必须显式降级输出，不能让 agent 误信

## 6. ContextPack：三份合同的合并建议

| 字段 | 来源 | 说明 |
|---|---|---|
| relevant files + symbols + snippets(reason/score/role/elided) | ragcode | 证据主体 |
| code flow / call path | GitNexus Process + codegraph buildCallPathsSection | 流程内嵌主力工具输出 |
| citations | ragcode（每 snippet 带来源） | 引用强制 |
| freshness | ragcode FreshnessReport | 索引时效 |
| missing evidence（可计数化） | GitNexus epistemic causes + ragcode hint code | "缺什么+为什么+怎么补"三合一 |
| confidence / answerable | ragcode | 置信度显式化 |
| budget trace | ragcode | token 预算审计 |
| spec constraints | 无参考（四家都没有真正的 spec 层） | nova 需自研——唯一没有参考的模块 |

注意：**Spec/Docs → Code 关联是 nova 全部参考项目都没做好的空白**。GitNexus 有 markdown phase（Section 节点+交叉链接）最接近，但没有 implements_spec 语义；ragcode 的文档系统是格式工程（OCR/PDF）不是语义工程。Task.md 的"确定性 Markdown Parser + LLM 推断标记 unverified"路线没有现成参考，是 nova 的自研点，也是潜在差异化价值。

## 7. 技术选型的调研佐证

| 决策点 | 调研佐证 |
|---|---|
| SQLite 作为图存储 | codegraph（nodes/edges/FTS5/unresolved_refs）+ ragcode（better-sqlite3 + FTS）双验证；单 VPS 场景够用 |
| 向量存储 | ragcode 用 LanceDB（嵌入式，免服务）；GitNexus 把向量放进图数据库。nova V1：LanceDB 或 pgvector 二选一，ragcode 的 LanceDB 集成可作为参考 |
| tree-sitter | 四家全用；codegraph 的 Rust kernel 模式是性能答案（若 nova 服务端用 Rust 可全盘借鉴；用 TS 则考虑 wasm 降级路径的 parity 测试模式） |
| RRF K=60 | GitNexus/Elasticsearch 共识值 |
| MCP SDK | notace 手写 JSON-RPC（500 行，支持多协议版本）；其余用官方 SDK。nova 用官方 SDK + 注意 stdout 纯净 + stderr 日志（notace 的纪律） |
| 本地 embedding | GitNexus arctic-embed-xs 384D 本地 ONNX 证明小模型可用；ragcode 的 deterministic embedding 提供"零配置离线可用"的下限保障 |

## 8. 工程纪律的负面清单（三个项目用规模换来的教训）

1. 单文件 >100K 行 = 失控信号：ragcode hybrid-retriever 141K、GitNexus run-analyze 221K、codegraph tools.ts 335K、tree-sitter.ts 312K。nova 评审规则：任何文件超 2K 行要警惕
2. 规则引擎会腐化：ragcode 的正则 operator 是评测驱动的，三个月后没人敢删
3. 工具碎片化不可逆：ragcode 25 个工具一旦有用户就删不掉

## 9. 建议的下一步（供讨论，非决定）

1. **先做架构决策清单**：runtime（Rust vs TS——影响 codegraph kernel 可借鉴度）、存储（SQLite+LanceDB vs PG+pgvector）、MCP 切片（维持两工具方案？）
2. **原型验证两个最高风险项**：
   - C/C++ tree-sitter 抽取在目标代码库（用户实际的 C/C++ 项目）上的符号/调用边覆盖率——codegraph 有现成工具可直接实测
   - Spec Markdown Parser 的 SpecBlock 模型（无参考，需自证）
3. **同步协议设计**：以 notace 思想为蓝本 + 补 .gitignore/rename/branch（本文 4.1 清单）
4. 差异化定位文档：回答"为什么不用 codegraph/ragcode 全本地方案"（本文第 4 节）
