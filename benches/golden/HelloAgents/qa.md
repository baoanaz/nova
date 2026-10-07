# HelloAgents 基准题库（20 题）

> 靶场：`HelloAgents` @ `93e77ea`｜projectId `06078cc80c7ce7d7`｜索引：`/root/.nova/bench/voyage-4-lite-d1024`（复用，不重建）
> 每题给出**建议工具**（`search` = 定位/事实型；`ask` = 需要跨文件综合的解释型）、**人工核实的参考答案**与**依据路径**。
> 机器可跑版本：同目录 `HelloAgents.jsonl`。

| ID | 建议工具 | 类别 | 问题 | 参考答案 | 依据 |
|---|---|---|---|---|---|
| H-01 | `search` | symbol | Which execution states does the `ToolResponse` protocol define, and what does each state mean? | ToolStatus 三种：SUCCESS（完全按预期）、PARTIAL（结果可用但有折扣，如截断/回退/部分失败）、ERROR（无有效结果）。 | `hello_agents/tools/response.py#ToolStatus` |
| H-02 | `search` | symbol | 要自己写一个工具类，必须实现哪个抽象方法，入参与返回值是什么？ | 继承 hello_agents/tools/base.py 的 Tool，实现 run(parameters: Dict[str, Any]) -> ToolResponse（另有 run_with_timing 包装计时）。 | `hello_agents/tools/base.py#Tool.run`<br>`hello_agents/tools/base.py#Tool` |
| H-03 | `search` | behavior | `ToolRegistry.register_tool` 的 `auto_expand` parameter 做什么？ | 当工具声明 `expandable=True` 且 `auto_expand=True` 时，调用 `get_expanded_tools()`，把返回的多个子工具分别注册进 name → Tool 映射，并且不再注册原始聚合工具；否则按普通单工具注册。它与“展开参数 schema”无关。 | `hello_agents/tools/registry.py#ToolRegistry.register_tool` |
| H-04 | `search` | behavior | What does `ToolRegistry.execute_tool` return, and how does it handle an exception raised by a tool? | 统一返回 ToolResponse（不是裸字符串或抛异常给调用方），工具执行失败被规约为 ToolStatus.ERROR 的响应。 | `hello_agents/tools/registry.py#ToolRegistry.execute_tool` |
| H-05 | `ask` | behavior | 一个工具的输出被截断了，按 ToolResponse 协议应该怎么表达？为什么不能直接返回 SUCCESS？ | 协议语义应使用 ToolStatus.PARTIAL，因为结果可用但不完整；`ObservationTruncator.truncate()` 实际返回含 `truncated`、`preview`、`full_output_path`、`stats` 的字典，并把全文写盘，调用方仍需把这些信息包装成 `ToolResponse.partial()`。当前实现没有自动完成这一步，不能把文档中的目标行为误写成已接线行为。 | `hello_agents/tools/response.py#ToolStatus`<br>`hello_agents/context/truncator.py#ObservationTruncator` |
| H-06 | `ask` | behavior | CircuitBreaker 在什么条件下 open，一个成功调用或 recovery timeout 后如何恢复？ | CircuitBreaker 按工具名累计连续失败次数，达到 failure_threshold（默认 3）后进入 Open（is_open 返回 True，调用被拒）；任一次成功会清零该工具的失败计数；Open 经过 recovery_timeout（默认 300 秒）后，在下一次 `is_open` 检查时惰性恢复为 Closed。也可手动 open/close。 | `hello_agents/tools/circuit_breaker.py#CircuitBreaker` |
| H-07 | `search` | behavior | TodoWrite 对任务状态有哪条硬性约束？ | 单线程强制：任意时刻最多只能有 1 个 in_progress 的任务（状态取值 pending / in_progress / completed）。 | `hello_agents/tools/builtin/todowrite_tool.py#TodoList` |
| H-08 | `search` | path | Which record categories does `DevLog` support? | decision（架构/技术选型）、progress、issue、solution、refactor、test、performance 七类，可按 category/tags 过滤读取。 | `hello_agents/tools/builtin/devlog_tool.py#DevLogTool` |
| H-09 | `search` | behavior | SkillLoader 扫描技能目录时只读取文件的哪一部分，为什么？ | 只解析 SKILL.md 的 YAML frontmatter 元数据（_parse_frontmatter_only），避免把正文全部读进内存；scripts/examples/references 作为属性按需列举。 | `hello_agents/skills/loader.py#SkillLoader` |
| H-10 | `ask` | behavior | 子代理 `TaskTool` 调用时，parent agent 和 subagent 的 tool set 是怎么隔离的？ | TaskTool.run 用 agent_factory 造出子代理，调用 run_as_subagent(..., max_steps_override=...)，并通过 _create_tool_filter(filter_type) 生成 ToolFilter 限定子代理可见的工具，防止子代理递归调用 TaskTool 之类的越界行为。 | `hello_agents/tools/builtin/task_tool.py#TaskTool`<br>`hello_agents/tools/tool_filter.py#ToolFilter` |
| H-11 | `search` | behavior | SessionStore 把会话保存在哪里、用什么格式？ | 默认目录 memory/sessions，按会话名存成 <session_name>.json（未命名则 session-<id>.json），内容为 JSON（ensure_ascii=False, indent=2）。 | `hello_agents/core/session_store.py#SessionStore` |
| H-12 | `ask` | behavior | 恢复一个历史会话时，会做哪些一致性检查，由谁触发？ | `BaseAgent.load_session(check_consistency=True)` 在读取 SessionStore 数据后触发两项检查：`check_config_consistency` 比对 llm_provider、llm_model、max_steps；`check_tool_schema_consistency` 比对工具 schema hash。差异只打印警告，不阻止恢复，随后历史、元数据和读缓存仍会恢复。 | `hello_agents/core/agent.py#BaseAgent.load_session`<br>`hello_agents/core/session_store.py#SessionStore.check_config_consistency`<br>`hello_agents/core/session_store.py#SessionStore.check_tool_schema_consistency` |
| H-13 | `search` | symbol | 上下文工程里，把历史/工具结果组装成上下文的入口方法是什么？ | ContextBuilder.build(...)，内部依次 _gather（收集）→ _select（选择）→ _structure（结构化）→ _compress（压缩），可用 token 由 ContextConfig.get_available_tokens 给出。 | `hello_agents/context/builder.py#ContextBuilder.build` |
| H-14 | `search` | behavior | `TokenCounter` 默认按哪个 model 计数，message token count 有没有 cache？ | 默认 model='gpt-4'，通过 _get_encoding 取 tiktoken 编码计数；`count_message` 以 role + content 为键缓存，`count_text` 不走缓存，并提供 clear_cache / get_cache_size / get_cache_stats。 | `hello_agents/context/token_counter.py#TokenCounter` |
| H-15 | `ask` | behavior | 工具输出特别长时，ObservationTruncator 怎么处理，原始输出会丢吗？ | 先按行截断（_truncate_lines）保留头尾，并把完整输出另存到磁盘（_save_full_output），返回给 LLM 的是截断版加上全文位置，因此不会丢原始输出。 | `hello_agents/context/truncator.py#ObservationTruncator` |
| H-16 | `search` | behavior | 可观测性 TraceLogger 的输出落到什么文件、什么格式？ | 每个会话同时写 `trace-<session_id>.jsonl` 和 `trace-<session_id>.html`：JSONL 每条事件写一行并立即 flush；HTML 增量写事件，`finalize()` 补统计面板与尾部后关闭两文件。 | `hello_agents/observability/trace_logger.py#TraceLogger` |
| H-17 | `search` | behavior | How does `StreamEvent.to_sse()` serialize an event? | StreamEvent.to_sse() 输出 `event: <type>` 与 `data: <json>`，再以空行结束事件；JSON 含 type、timestamp、agent_name、data。StreamBuffer 只是有界事件缓冲，不负责序列化。 | `hello_agents/core/streaming.py#StreamEvent.to_sse` |
| H-18 | `ask` | behavior | HelloAgentsLLM 是怎么决定用哪个厂商的适配器的？ | 根据 base_url 自动选择适配器（OpenAI/Anthropic/Gemini），base_url 可显式传入或从 LLM_BASE_URL 环境变量读取；没有 base_url 直接抛 HelloAgentsException。 | `hello_agents/core/llm.py#HelloAgentsLLM` |
| H-19 | `search` | behavior | 文件工具靠哪个 field 做 read-before-write 的 optimistic conflict detection？ | ReadTool 返回 file_mtime_ms（毫秒级 mtime），WriteTool/EditTool 写入时用它比对，检测文件是否被外部修改过。 | `hello_agents/tools/builtin/file_tools.py#ReadTool` |
| H-20 | `search` | negative | HelloAgents 里向量检索（Qdrant 向量库）的实现代码在哪个文件？ | 不存在：仓库里没有向量库/向量检索实现（qdrant 只出现在 __init__.py 中静音第三方日志的一行）。 | （无：负例） |

## 统计

- 共 20 题：`search` 13 题、`ask` 6 题、负例 1 题；语言：中文 11｜英文 4｜中英混合 5；
- 类别：symbol 3｜behavior 15｜path 1｜negative 1。
