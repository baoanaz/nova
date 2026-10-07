# notace-tool-rs 调研分析

> 仓库：source/notace-tool-rs，Rust，总计 5 个源码文件（blobref.rs / index.rs / finnian.rs / protocol.rs / main.rs + lib.rs）
> 定位：本地 MCP stdio 薄客户端，代理 4 个工具到远端 Finnian/ACE HTTP 服务

## 1. 它是什么

编辑器把它作为子进程拉起，通过 stdio 上的 JSON-RPC 2.0 通信，把工具调用转发给远端服务。本地只做一件事：把工作区代码以内容寻址 blob 的形式增量同步到服务端，让远端基于真实索引回答。

```
Editor (Claude Code / Codex)
   │ stdio JSON-RPC (MCP)
   ▼
not-ace-tool-rs          ┌─ 本地：扫描/分块/hash/增量上传/缓存
   │                     └─ 远端：检索/增强/搜索/建议（不可见，不推测）
   │ HTTPS + Bearer
   ▼
Finnian/ACE Server
```

## 2. 四个工具与远端端点

| 工具 | 端点 | 说明 |
|---|---|---|
| codebase_retrieval | POST /agents/codebase-retrieval | 先增量同步，再检索 |
| enhance_prompt | POST /prompt-enhancer | 可选带项目上下文重写提示词 |
| web_search | POST /web-search | 代理网络搜索 |
| advisor | POST /agents/advisor | 任务中途纠偏建议；旧后端 404 时返回"不支持"而非失败 |

工具描述写法值得注意：codebase_retrieval 的 description 里直接写了 Good/Bad query 示例，并明确把"精确标识符查找"引导去 grep——用 tool description 本身控制 agent 行为（src/finnian.rs tool_definitions）。

## 3. 本地同步引擎（核心价值所在）

### 3.1 Blob 模型（src/blobref.rs）

- **blob 名** = `hex(sha256(path_bytes || content_bytes))`，无分隔符（作者明知 path/content 边界歧义——有测试确认 `blob_name("ab","c") == blob_name("a","bc")`，接受这种歧义因为碰撞概率可忽略）
- **分块**：超过 400 行的文件按行切成 `path#chunk1of3` 形式的多个 blob
- **限制**：单文件 > 128KB 跳过；>10% 不可打印字符判为二进制跳过；控制字符清洗（保留 \n \r \t）
- **编码**：UTF-8 lossy 读取（非法字节替换为 U+FFFD）

### 3.2 索引缓存（src/index.rs）

- 缓存位置：`<project_root>/.not-ace-tool/index.json`（藏在项目内，不是全局目录——注意这意味着它会污染工作区，且受 .gitignore 影响）
- 结构：`{ version, config_hash, entries: { rel_path: {mtime_secs, mtime_nanos, size, blob_hashes[] } } }`
- **config_hash**：分块配置（默认行数）参与 hash，改配置自动失效缓存——chunking 参数变更不会产生脏索引
- 原子写：tmp + rename（Windows 上先删后改名）
- **verified cache hit 语义**：mtime+size 匹配只算"快路径"，仍然重算 blob hash 对比；hash 一致才算缓存命中。防"mtime 不变内容变"和"touch 不触发重传"。有专门测试覆盖这两种情况
- **上传成功才缓存**：文件的所有 blob 都被服务端接受才写入索引；部分接受/被跳过则该文件不进缓存（下次重传）

### 3.3 扫描与忽略

- walkdir 遍历，不跟随符号链接
- 硬编码忽略列表：.git、node_modules、target、dist、build、.venv、__pycache__、.tox、.idea、.vscode、.not-ace-tool
- **没有实现 .gitignore 解析**——这是一个明显的简化，nova 应该做对

### 3.4 批量上传

- POST /batch-upload，Bearer 认证
- 每批 ≤ 1MB（按 path+content 字节数累积分批），30s 超时
- 服务端返回 `{blob_names[], skipped_blobs[]}`；skipped 的文件从索引剔除并计入 skipped_paths

### 3.5 Scope 与 Checkpoint（最有价值的设计）

- **问题**：每次检索都要把全量 blob 名列表发给服务端；38k blobs ≈ 2.5MB，传输+服务端处理超 30s
- **解法**：首次同步后 POST /checkpoint-blobs 拿 checkpoint_id；后续检索只传 checkpoint 引用
- **降级**：服务端 404 时标记 CheckpointSupport::Unsupported，整个会话回退到传全量 scope（一次探测，会话内记忆）
- **失效恢复**（三条路径，全部只重试一次）：
  1. 服务端报 invalid checkpoint → 作废本地 checkpoint → 带 full scope 重试 → 成功后重建 checkpoint
  2. 服务端报 stale blobs（400/422 + blob 名列表，body 最多读 8MB）→ forget_blobs 从本地索引删除相关文件 → 重新 sync → 重试一次
  3. scope 变化（文件增删改导致 blob 集合不同）→ 直接重建 checkpoint
- 错误信息全部经过 token 脱敏（safe_error_detail），错误 body 截断 512 字节

### 3.6 会话管理

- 进程内 `HashMap<project_root_path, ProjectSession>`，每个项目一个 IndexManager + scope + checkpoint 状态
- 多项目并存（agent 可能在一次会话中查多个 repo）

## 4. MCP 协议实现（src/protocol.rs）

- 手写 JSON-RPC 解析（不用 SDK），~450 行
- 支持协议版本协商：2024-11-05 / 2025-11-25 / 2026-07-28；新版协议通过 `_meta` 传版本，响应加 `resultType: "complete"`
- 实现 initialize / tools/list / tools/call / ping / server/discover
- 错误码规范：-32700 parse / -32600 invalid / -32601 method not found / -32602 invalid params / -32603 internal；自定义 -32022 unsupported protocol version
- **stdout 协议纯净**：只有 JSON-RPC 帧；所有诊断走 stderr
- 工具级错误分两类：InvalidArguments → JSON-RPC error；ExecuteError::Tool → `isError: true` 的正常响应（让 agent 看到失败原因而不是崩溃会话）

## 5. 超时与错误策略

| 场景 | 超时 | 依据（源码注释） |
|---|---|---|
| 普通请求 | 180s | 首次大 repo 全量 scope 上传 38k blobs 实测 >30s，服务端检索 ~1s |
| advisor | 180s | 全量推理 ~51s，30s 默认值会拦腰截断 |
| batch-upload | 30s | 每批 ≤1MB |

教训：**超时要按操作类型分层**，单一超时值在"首次全量 vs 后续增量"之间必然顾此失彼。

## 6. 值得 nova 借鉴的设计（按优先级）

1. **blob 内容寻址 + verified cache hit**：路径+内容双因子 hash，mtime 只是快路径不是信任依据
2. **checkpoint/scope 协议**：避免每次请求重传索引 scope；带优雅降级和三种自愈路径
3. **服务端主导拒绝权**：skipped_blobs 由服务端决定，本地尊重服务端裁决（权限/大小/格式策略集中在服务端，客户端无需升级）
4. **错误分类学**：参数错误 vs 工具执行错误 vs 协议错误，分别映射到 JSON-RPC error / isError result / -32xxx
5. **token 脱敏 + body 截断**：所有对外错误信息先过 redaction
6. **工具描述即行为控制**：good/bad query 直接写进 description

## 7. nova 要避免/改进的点

1. 缓存放项目内 `.not-ace-tool/` 会污染工作区且和 .gitignore 纠缠；nova 建议放项目内（便于多 checkout 隔离）但提供 `nova ignore` 并默认自动写入 .git/info/exclude，或放全局 `~/.nova/projects/<hash>/`
2. 硬编码忽略列表，没有 .gitignore 支持——nova 必须实现
3. 没有 rename 检测（blob 模型下 rename 自然免重传，但索引语义上需要处理路径变化）
4. 没有同步状态对用户可见（skipped notice 只在结果尾部附加）；nova 应有 sync status 工具/输出
5. 400 行机械分块对代码检索质量是权宜之计（远端看不到 AST）；nova 在服务端做 AST chunk，本地 client 可以只做文件级同步——**nova 的服务端可见性给了更优解的空间：本地薄、服务端懂结构**
6. 单线程逐批上传、无压缩——大 repo 首同步可以更快（nova 可加 gzip + 并发）
