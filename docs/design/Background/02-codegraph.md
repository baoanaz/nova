# CodeGraph 调研分析

> 仓库：source/codegraph，TypeScript + Rust kernel，npm 分发 `@colbymchenry/codegraph`
> 定位：100% 本地 Tree-sitter 代码图，SQLite 存储，MCP 暴露 8 个工具，主打"一次 explore 调用拿到带调用路径的真实源码"

## 1. 架构总览

```
codegraph CLI (init/install/upgrade)
   │
   ├── codegraph-kernel/          Rust napi cdylib：tree-sitter parse+extract
   │     每文件只跨一次 JS↔Rust 边界；16+ 语言 extractor 全在 Rust
   │
   ├── src/extraction/            JS 侧：wasm 降级路径、框架抽取器、解析线程池
   ├── src/resolution/            跨文件引用解析：name-matcher + import-resolver + 合成器
   ├── src/db/                    SQLite schema + queries.ts(145K) + WAL 管理
   ├── src/graph/                 遍历：branch-guards(90K)、dead-code、type-hierarchy、named-symbol-flow
   ├── src/context/               NL query → 符号抽取 → FTS → 子图 → 源码分组输出
   ├── src/sync/                  fs.watch / git hooks / worktree 感知
   └── src/mcp/                   daemon + 会话 + 8 工具（tools.ts 335K）
```

**双解析路径**：Rust kernel（快，预编译进分发包）+ tree-sitter wasm（降级，kernel 不可用时），两侧 grammar 版本做 parity 测试锁死（Cargo.toml 注释 + kernel-grammar-parity 测试）。

## 2. 数据模型（src/db/schema.sql，v1 + 9 个 migration）

### nodes 表（23 种 NodeKind）
kind/name/qualified_name/file_path/language/start_line/end_line/docstring/signature/visibility/is_exported/is_async/is_static/is_abstract/decorators/type_parameters/return_type。

关键细节：
- **NODE_KINDS 数组顺序是 Rust kernel 的线协议**（kind 以索引跨边界，只增不重排，src/types.ts 注释）
- qualified_name 支持跨文件消歧

### edges 表（13 种 EdgeKind）
contains / calls / imports / exports / extends / implements / references / type_of / returns / instantiates / overrides / decorates / navigates。

关键细节：
- **边唯一性 = (source, target, kind, line, col)**，UNIQUE INDEX + IFNULL 折叠 NULL——修复过重复边导致 callers/impact 计数膨胀的 bug（#1034，schema 注释）
- `provenance` 列记录边的来源（真实解析 vs 合成）

### unresolved_refs 表（两阶段解析的核心）
```
pending → resolution pass → resolved(删行) 或 failed(保留 + name_tail)
```
- failed 行不删除，带部分索引 `WHERE status='failed'`，**后续 sync 引入新符号时重试**（#1240）
- from_node 级联删除：重新抽取文件自动清掉旧引用
- 这是"批量解析 + 事后消解"的标准模式：抽取阶段不阻塞，解析阶段全局视角

### files 表
content_hash / language / node_count / errors / **generated**（生成代码判定下沉到索引期，排序时不用每请求读文件头）

### name_segment_vocab 表（NL→符号 的桥）
符号名按驼峰/下划线拆段（"OrderStateMachine"→order/state/machine），`(segment, name)` 主键；自然语言查询词可以按段验证命中。FTS 做不了这个（tokenizer 把驼峰当单 token）。删除留孤儿行是**故意的**——行只是"提案"，使用前永远对 nodes 再验证。

## 3. Rust Kernel（codegraph-kernel/）

- napi 3 + tree-sitter 0.25，crate-type = cdylib
- 每语言一个 extractor：tsjs/python/go/rustlang/java/kotlin/swift/dart/csharp/php/ruby/lua/rlang/scala/cfnptr(C 函数指针)/ccpp...
- **动机**：JS 侧逐节点遍历 tree-sitter 的回调跨边界太慢；Rust 侧一次遍历完成全部抽取，每文件只回传一次结构化结果
- C/C++ 专门有 cfnptr.rs（43K）处理函数指针合成——C 的间接调用是这个项目认真处理过的问题

## 4. 跨文件解析（src/resolution/，nova 最该研究的部分）

```
extraction（写 pending refs）
   ↓
ReferenceResolver（src/resolution/index.ts）
   ├── name-matcher.ts (134K)  可见性规则 + 名称匹配 + 点链/作用域链/方法调用匹配
   ├── import-resolver.ts (90K) 各语言 import 语义 + path aliases + go modules + workspace packages
   ├── callback-synthesizer.ts (179K)  回调边合成（JS callback hell → 图边）
   ├── c-fnptr-synthesizer.ts (65K)    C 函数指针 → 调用边
   ├── tier-synthesizer.ts (38K)
   └── 框架路由合成器：expo-router / next / react-router / sveltekit / tanstack / vue-router / goframe
```

设计要点：
- **CHAIN_LANGUAGES / SCOPED_CHAIN_LANGUAGES** 按语言分派链式调用匹配策略（`.` 链 vs Rust `::` 链 vs PHP `this->prop`）
- 解析结果带 provenance；合成边（synthesizer 产出的）与解析边（真实 import/call）区分
- LRU 缓存（默认 5000 条，env 可调）控制大 repo 内存
- ResolverPool：大量 refs 时进 worker 线程池并行
- cooperative-yield：长解析循环让出事件循环

### 实测跨文件覆盖率（README，口径诚实）
TS/JS 95.8%、Python(psf/requests) 100%、Go(gin) 96.6%、Rust(ripgrep) 86.7%、C(redis) 92.2%、C++(leveldb) 94.8%、ObjC 91.6%、Swift 95.3%。残余缺口如实归因：动态分发、反射/DI、框架约定入口、vendored 三方码。

## 5. 检索与 explore（src/context/）

查询流程：
```
NL query
  → extractSymbolsFromQuery（正则：驼峰/蛇形/SCREAMING/缩略词/点号链/小写标识符）
  → name_segment_vocab 验证（自然词 vs 符号段）
  → FTS5 搜索（nodes_fts: name/qualified_name/docstring/signature，触发器同步）
  → GraphTraverser 限深遍历 → Subgraph
  → 逐符号抽取真实源码块，按文件分组
  → markdown 输出 + 调用路径段（calls 边内存推导）
```

三个高价值设计：
1. **低置信度诚实降级**（buildLowConfidenceNote）：查询只命中常见词时，输出显式说"以上入口可能偏，请用精确符号名重查"，并给出最可能的目录。**不装懂，把不确定性交还给 agent 并指路**。
2. **调用路径内嵌输出**（buildCallPathsSection，注释说明了动机）：agent 可靠地读 context 输出但不会主动发现新工具（deferred-MCP 环境只 ToolSearch 已知工具），所以把 flow 信息直接烤进常用工具输出，而不是单独一个 trace 工具。
3. **生成代码判定下沉**：generated 标记索引期写库，排序期一次 probe。

## 6. MCP 工具面（8 个）

| 工具 | 职责 |
|---|---|
| codegraph_search | 快速符号名搜索，只返回位置 |
| codegraph_callers / callees | 调用者/被调用者 |
| codegraph_impact | 影响面 |
| codegraph_node | **双模式**：替代 Read 工具读文件（附依赖者清单=爆炸半径）；或单符号（签名+源码+调用轨迹，重载歧义时一次返回全部匹配定义体） |
| **codegraph_explore** | **主力工具**："几乎任何问题先调它"；返回按文件分组的符号真实源码 + 调用路径；明确声明"treat the shown source as already Read; do NOT re-open those files" |
| codegraph_status / files | 健康/文件树 |

工具描述里大量"WHEN TO USE / AFTER THIS / use X instead"导航文案——**工具间导航写进 description 是共识做法**（notace、GitNexus、codegraph 三家一致）。

## 7. Daemon 与并发（src/mcp/）

- 一个 MCPEngine 多 session（daemon 模式）：共享 SQLite WAL 读连接 + 共享 inotify watch（issue #411）
- QueryPool：daemon 才开的 worker 线程池（多个客户端并发 explore 时不饿死 MCP transport）；单 session 直连模式不开（省一次 worker 往返）
- writer lock：`.codegraph/writer.pid`，多进程写互斥
- liveness watchdog / ppid watchdog：父进程死了守护进程自灭
- **WSL2 感知**（watch-policy.ts）：检测到 WSL2 /mnt 路径自动禁用 fs.watch（inotify 对 9p 文件系统不可靠），降级为 git hooks 同步（commit/merge/checkout 时触发）
- worktree 感知：查询借用其他 worktree 的索引时警告

## 8. 分发工程

- 自带 Node runtime 打包（无 Node 依赖）、install.sh/ps1、`codegraph upgrade` 原地升级
- GitHub Actions 跨平台构建 kernel，npm 分发按 platform-optional-dependencies 拉对的二进制

## 9. 值得 nova 借鉴（按优先级）

1. **unresolved_refs 两阶段解析 + failed 重试**——跨文件解析的正确骨架
2. **Rust kernel 模式**（如果 nova 服务端选 Rust：tree-sitter 批量抽取全在原生侧，一次边界/文件）——对 C/C++ 大仓库是数量级差异
3. **explore 工具契约**：真实源码 + 按文件分组 + 调用路径 + "已读声明" + 低置信度诚实降级——nova search_context 的直接参考
4. **边的唯一性约束与 provenance 列**：防重复边事故；区分解析边/合成边（对应 nova 的 verified vs inferred 证据分级）
5. **NL→符号桥**：identifier segments + FTS 双通道；正则符号抽取（驼峰/蛇形/点链）成本低效果好
6. **工具描述导航文案**模式
7. **WSL2 watch 降级 + git hooks 备胎**：nova 本地 client 在 Windows/WSL 环境必需的工程细节
8. **会话共享 + query pool + writer lock**：如果 nova 本地侧有 daemon，这套并发模型可参考

## 10. 局限与 nova 不采纳的部分

1. **无向量检索**——纯符号+FTS；对"模糊语义问题"（"哪里处理重试逻辑"）弱于混合检索。nova 要向量。
2. 无 Spec/文档理解层
3. 无远端形态，全本地——nova 是 local+remote 混合
4. 框架合成器（react-router/next/expo...）是 JS 生态特化的重投入（callback-synthesizer 179K 行）；nova 的场景是 C/C++/Python，投入应转向 C 宏/函数指针/CMake 源集、Python 动态特性的处理
5. tools.ts 335K 单文件已经失控——nova 的 MCP 层必须保持薄
