# MCP 异步预热与工作区智能索引

> 最终愿景：MCP 秒级可用；索引后台提前构建；Git 分支独立且复用内容向量缓存；仓库外区域仅索引 Markdown；同机多个 Agent 共享任务；首次检索尽可能直接复用预热结果。
>
> 本文记录用户最终确认的方案，覆盖 Module 05 中旧的“串行请求、首次工具调用才同步、多 harness 最终一致”设想。设计目标不代表全部已验收；交付测试与性能记录见文末。

## 1. 最终索引策略

| 工作区情况 | 默认策略 | 身份与兼容性 |
|---|---|---|
| Git 主工作区，有 Remote | 完整或增量代码索引 | 保持现有 Remote + 仓库相对路径 + 分支算法 |
| Git 主工作区，无 Remote（含仅 git init） | 完整或增量代码索引 | 保持规范化绝对路径基础身份；不误判为文档目录 |
| Git Linked Worktree | 当前分支独立索引，复用现有 Embedding Cache | 正常路径保持历史分支项目身份；不推断来源分支，不只读复用基础索引 |
| 非 Git 区域，包括隐藏目录 | 递归索引 `.md` / `.markdown`，忽略其他类型 | 规范化工作区绝对路径的独立文档项目 |
| 非 Git 代码项目，用户显式开启 | 允许完整索引 | `NOVA_NON_GIT_INDEX=full`；不作为默认行为 |
| 混合工作区 | 每个 Git 项目独立同步，仓库外 Markdown 单独同步 | 查询融合当前工作区相关项目，清楚标注证据来源 |

没有 Remote 不等于没有 Git。Git 分类依据 Git 命令与 git-dir/common-dir 元数据，不能仅凭 `.git` 是文件还是目录判断 Worktree。Submodule 不误判为 Linked Worktree。

## 2. 工作区拆分与范围

```text
Agent 工作区
├── repository-a/       → A 当前分支代码索引
├── repository-b/       → B 当前分支代码索引
├── docs/               → 仓库外 Markdown 文档索引
├── .claude/            → 仓库外 Markdown 文档索引
└── .agent/             → 仓库外 Markdown 文档索引
```

- 如果传入根目录本身属于 Git 仓库，保留原有项目相对路径范围与检索链路。
- 如果根目录是多项目容器，发现其中 Git 根并剪枝：Git 内文件不再进入仓库外文档项目。
- Markdown 扩展名大小写不敏感；不把 `.mdx`、无扩展名、配置、源码、凭据等当成 Markdown。
- 沿用现有 `.novaignore` / `.gitignore` / 内置排除与白名单规则；文件类型边界不被白名单绕过。
- 默认不跟随目录符号链接扫描工作区之外；隐藏目录按相同规则处理，敏感/依赖/Git 元数据目录仍排除。
- 非 Git Markdown 模式拒绝文件系统根和用户主目录；默认上限为遍历 10000 项、500 个文档、合计 16 MiB，并沿用单文件 128 KiB 与二进制限制。超限明确报错，不静默截断成“完整索引”。
- 文件新增、修改、删除通过原有增量扫描/对账/删除通知同步；全部文档被删除也需要同步删除，不保留幽灵证据。

## 3. 初始化与任务生命周期

```text
initialize → 立即响应
notifications/initialized → 后台 WorkspaceResolver → 工作区规划与预热
                         └→ tools/list、ping、其他请求照常处理
search_context / ask_project → 加入已有任务 / 复用成功版本 → 正常检索
```

- 仅在初始化握手完成后触发；重复 initialized 通知不重复启动。
- 定位优先级：显式 `NOVA_PROJECT_DIR` → `CLAUDE_PROJECT_DIR` → 验证 MCP 进程 CWD；显式路径必须为有效绝对目录，错误或空值直接跳过自动预热，不回退到别的目录。
- CWD 验证接受：Git 命令确认的工作区、常见项目标记（README / AGENTS / CLAUDE / manifest）或浅层直接 Git 子项目。只做有界本地探测，不先扫描整个候选目录。
- 所有自动预热来源都拒绝根目录、Home、系统与 Agent/插件/依赖缓存目录；无项目信号时不自动上传，保留首次工具调用的 `project_root` 懒同步。
- 路径验证在 initialized 后使用 `spawn_blocking`，不进入 initialize / tools/list / ping 的等待链。
- `NOVA_AUTO_WARMUP=off` 关闭初始化预热，不关闭工具调用的正常懒同步。
- 耗时请求并发派发，stdout 单写入者只输出 JSON-RPC；日志写 stderr。
- 取消一个工具调用只取消该等待者，不杀死共享索引任务；预热失败不影响 MCP 启动。
- 超时涵盖锁等待、扫描、上传与索引等待，默认总上限 600 秒；`NOVA_WARMUP_TIMEOUT_SECS` 可调。
- Agent 退出释放进程持有的 OS 锁；后续调用通过已上传缓存与服务端持久化 pending 项恢复。

## 4. 复用、去重与一致性

- 继续使用 `sync_project()` 的原流水线：resolve → 增量扫描 → 上传/删除 → flush 等待 → checkpoint → 查询。
- 进程内共享任务，跨进程使用 OS 文件锁。锁文件不删除，防止旧锁 inode 与新锁文件产生双重写者。
- 同一缓存根下，服务端端点/项目相同的写操作串行；成功结果只对相同凭据、规范化工作区、选择策略与文件版本可复用。
- 同一工作区任务合并，避免重复内容扫描/上传/Embedding/checkpoint；复用前仍需进行本地版本校验，不能依赖可能粗粒度的 mtime。
- 修改索引前使旧成功记录失效；完成后原子发布。同步中内容变更时再做增量对账；身份/分支变化时明确要求重试，不把混合版本标成就绪。
- 已完成结果默认复用 60 秒，`NOVA_WARMUP_READY_TTL_SECS` 可调；过期继续原增量流程，未变化内容不重复上传/Embedding。
- 不同工作区不能因 remote/分支相同就错误复用就绪结果。正常历史 Git 分支 ID 不迁移；冲突工作区通过额外可选路由隔离，不修改 Rust/Python 基础身份算法。
- 绝对路径文档项目可使用服务端 `clientScope` 路由，真实账户来自认证上下文，设备/工作区标识来自客户端；同账户跨设备同路径不会相互覆盖。
- 新路由必须被服务端确认，旧服务端不能忽略隔离字段后仍被客户端当作支持隔离。
- checkpoint 目前是 scope 登记与传输优化，不是真正不可变索引快照；本实现不能把旧 checkpoint 冒充历史快照。

**协调边界**：本轮目标是同机共享缓存的多 MCP 进程；沿用服务端现有项目锁/后台 flush 任务。不是跨设备分布式租约，也不宣称多 ASGI worker 的 LanceDB 写入已获得全局事务隔离。

## 5. Embedding 与检索质量

- Worktree 继续构建独立的 FTS / Vector / Graph 项目，允许解析/存储耗时；相同内容通过现有按模型/维度和内容哈希的 Embedding Cache 复用，不重复开发向量缓存。
- 内容变化可能产生新的 Embedding，不能承诺 Worktree 零新增调用；缓存关闭、清理或模型切换时要如实记录。
- 单项目请求沿用原 `search` / `ask` 与预算，保证既有检索质量不因预热改变。
- 混合工作区查询关联项目并标注 Git 根/文档根与分支；部分项目失败要说明覆盖缺口，不能静默伪装成完整回答。
- 首版不重写 core 全局跨项目 reranker；优先复用现有检索流水线并有界融合结果。多项目 `ask_project` 透明降级为融合检索证据，不触发 N 次总结模型调用；单项目回答链路不变。
- 不扩展 MCP 工具参数，不让 Agent 主动选择预热工具或底层索引工具。

## 6. 日志与验收

日志关注：工作区类型、文件策略、任务 start/join/reuse/failed、版本变化、扫描缓存命中、待上传/删除文件、同步阶段耗时。Embedding 调用/token/缓存命中继续利用原 service/core 索引 run 观测，不编造未收到的 token 数据。

验收项：
1. 初始化响应、tools/list、ping 在服务端索引被人为阻塞时仍快速可用。
2. 初始化自动上传，无需 Agent 调用工具；重复通知不重复同步。
3. search/ask 等待同一预热，完成后不重复上传/flush/checkpoint。
4. 两个真实 MCP 进程共享任务；任务失败、超时、取消、持锁进程被杀后可恢复。
5. Git 有/无 Remote、Linked Worktree、分支切换沿用正确身份与增量行为。
6. 非 Git 隐藏目录只上传 Markdown，新增/删除正确；混合工作区 Git 区域不重复进文档索引。
7. 单项目检索合同不变；混合查询包含代码与文档，部分失败有提示。
8. 本地 mock 基准测 MCP 就绪时间和重复任务次数，不调用付费 API；真实 Embedding 成本测量需单独授权。

## 7. 实施顺序与当前记录

实施顺序：历史身份兼容与工作区拆分 → 混合同步/检索融合 → 多进程/取消/恢复专项测试 → 本地性能基准 → 只读 review。

### 交付验证（2026-10-11）

- Rust 客户端：100 项通过（新增 8 项 Resolver 单测和 4 项真实 MCP CWD 回归）；额外的 MCP 就绪 benchmark 单独运行通过。
- Python 相关回归：65 项通过（用户/设备路由、Embedding Cache 复用、projects、deferred sync、query、OpenAPI 路径合同）。
- 两个真实 MCP 子进程共享同一缓存：只产生一次上传、一次 checkpoint，各自正常查询。
- 初始化自动预热、重复 initialized 通知、search/ask 等待时 ping/tools/list 并发响应均有二进制测试。
- 混合工作区代码/隐藏 Markdown 来源融合、Git 区域剪枝、最后一个文档删除、显式非 Git 完整索引与切回 Markdown 的删除对账均通过。
- Linked Worktree 分支、Detached HEAD、切回已索引分支，以及冲突 checkout 保留主工作区历史 ID、隔离路由变化后的 ready 失效均通过。
- 假 provider 证明第二个分支的相同代码未新增文档 `embed()` 输入；未运行真实付费 Embedding API，也不把 mock 计数当作真实 Token 账单。
- 已调用只读子代理 review；修复其指出的本地扫描取消、准备阶段总超时和最终路由 scope 绑定问题。合作式取消及正常 EOF 退出有回归覆盖。
- 指定 Python 文件 `ruff check`、交付文件 `git diff --check` 均通过。

本地 mock 就绪基准：debug 构建，20 个真实进程样本；无显式工作区变量，通过 Git CWD 后台定位，服务端已有索引任务持续处于 running：

| 指标 | P50 | P95 |
|---|---:|---:|
| 进程启动到 initialize 响应 | 4.783 ms | 6.029 ms |
| initialized 后 ping + tools/list 两个响应 | 2.618 ms | 3.809 ms |

该基准只证明 MCP 就绪与消息处理不等待索引，不代表真实网络首次索引或检索耗时。

复现：
```bash
cargo test --locked --manifest-path client/Cargo.toml -j 1
cargo test --locked --manifest-path client/Cargo.toml -j 1 --test auto_warmup -- --ignored --nocapture --test-threads=1
pytest service/tests/test_client_scope.py service/tests/test_projects_api.py service/tests/test_sync_deferred.py service/tests/test_query_api.py service/tests/test_skeleton.py -o addopts= -q
```

保留边界：同机跨进程协调需要共享缓存根，不提供跨设备分布式租约；多项目 `ask_project` 返回融合证据而非跨项目统一总结。文档/冲突工作区隔离需要新版服务端确认 `clientScope`，普通 Git 分支项目与工具 schema 保持兼容。

## 8. Codex CLI 工作区定位实测

官方 npm 包 `@openai/codex@0.162.1`，源码 tag `rust-v0.162.1`，固定 commit `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`。临时下载并隔离 HOME / CODEX_HOME，不修改用户配置、不读取用户 Key。

官方实现证据：
- [rmcp_client.rs](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/codex-mcp/src/rmcp_client.rs#L1222-L1258)：本地 stdio 使用 `LocalStdioServerLauncher::new(runtime_context.local_process_cwd())`。
- [stdio_server_launcher.rs](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/rmcp-client/src/stdio_server_launcher.rs#L265-L283)：MCP 未配置 `cwd` 时使用 fallback CWD，并对启动命令调用 `current_dir(&cwd)`。
- 实测 MCP 探针收到 `codex-mcp-client` 0.162.1 的 initialize、initialized 和 tools/list；两种工作区变量都未设置，普通 CLI 与 `-C` 均传递正确 CWD，`npx` 包装未改变它。

真实 Codex + 新版 Nova debug 二进制 + 本地 HTTP mock 验收：

| 启动方式 | 正确工作区来源 | 有效 Nova 启动 / 自动预热成功 |
|---|---|---:|
| `codex` 交互 TUI（PTY，无用户提示输入） | `cwd_git`：启动项目 A | 1 / 1 |
| `codex -C project-b` 交互 TUI（PTY） | `cwd_git`：目标项目 B | 1 / 1 |
| `codex exec` | `cwd_git`：启动项目 A | 1 / 1 |
| `codex -C project-b exec` | `cwd_git`：目标项目 B | 1 / 1 |
| exec `-C` + `npx` stdio 包装新版 Nova | `cwd_git`：目标项目 B | 1 / 1 |

每个有效启动都在未配置工作区变量、未调用 Nova 工具的情况下自动上传对应项目的源码与 README，并完成一次 checkpoint，没有上传另一个项目的源码。不需要 SessionStart Hook。`npx` 测试是包装本地新二进制，不代表 npm 线上包已发布本次功能。

这些是五种启动组合各一个有效样本，不是生产总体成功率。交互式 `-C` 共尝试三次：前两次分别在宿主 loading 等待上限和宿主磁盘不足处中断，Nova 尚未启动；清理本次临时 Codex daemon 安装后第三次完成验收。没有通过修改系统或用户配置绕过问题。宿主登录、信任提示、首次 daemon 下载不属于 Nova MCP 就绪耗时。

exec 使用本地模型端点主动拒绝推理，因此测试命令最后退出码为 1；验收标准是此前已发生的 MCP 初始化、Nova 自动上传和 checkpoint，不是生成模型回答。未调用付费模型或 Embedding API。

本轮已再次调用只读子代理 review，并修复其两项发现：显式路径同样执行缓存目录拒绝检查；空显式路径按错误处理而非回退。对应回归包含在上述 100 项测试中。

**支持范围**：普通 Codex CLI 与 `-C` 已实测；IDE 扩展、Plugin MCP、其他 Agent 的真实启动未实测，不能由 CLI 结果推断其 CWD 一定正确。若其启动目录不可靠，自动预热跳过；可以提供显式 `NOVA_PROJECT_DIR`，或由首次工具调用的 `project_root` 兜底。本次未添加 Hook，也未执行 npm 发布/服务端部署。
