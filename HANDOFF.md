# nova 开发交接

> 当前环境更新：2026-10-07 UTC。公开使用入口是 [README.md](README.md)，
> 安装清单是 [ENVIRONMENT.txt](ENVIRONMENT.txt)，操作指南在 [docs/handbook/](docs/handbook/README.md)。
> 本文件记录开发现状；旧机器的环境、服务状态和历史成绩不代表当前 VPS。

## 2026-10-18 真实 Agent 场景优化轮（先读这一节）

目标：模拟真实使用——Agent 只看 MCP 工具描述、自己写 search/ask 参数——两个工具的返回都能让评审判“答得出大意”。
评测用新增的 `bench/run.py agentsim`（见 [bench/README.md](bench/README.md#模拟真实-agentagentsim与大意判定2026-10-18)），
评审与模拟 Agent 都是 `gpt-6-astra`，服务内 ask 用 `gpt-6-luna`；33 题 × 2 工具，留出集 13 题只验收不调参。

| 大意通过率（调参 / 留出） | search | ask | ask 耗时 |
|---|---|---|---|
| A0 基线：旧描述、单次 ask | 87% / 69% | 83% / 69% | 平均 24s，长尾 77~138s |
| **A4 本轮结果** | **94% / 85%**（全部 92%） | **94% / 92%**（全部 94%） | 中位 21s，最长 < 30s，0 降级 |
| A5 同代码复核 | 96% / 85%（全部 94%） | 92% / 69%（全部 88%） | 中位 21s，最长 31s，0 降级 |

留出集只有 13 题，ask 留出集两轮分别 12/13、9/13，失败题几乎不重叠（评审与 Agent 出题的随机性），两轮平均约 81%。

改了什么（按收益排序）：

1. **工具描述**（`docs/contracts/mcp-tools.json`，client 副本同步）：search 写明“零 LLM、3 秒内、一条 query 一个关注点，
   多问拆成多条并行查”；ask 写明“多轮调研、10~30 秒，适合跨函数调用链 / 多问合一 / 模糊问题”，删掉过时的“最多一次 LLM 调用”。
   Agent 从“把整句塞进一条 search”变成拆 2~3 条，search 通过率提升最大的一项。
2. **ask 多轮调研**（新 `service/nova_service/research.py`，`ASK_RESEARCH_ROUNDS` 默认 2，0 = 旧的单次总结）：
   首轮证据 → luna 规划缺口并给出检索词（优先证据里出现过但没展示定义的标识符）→ 并发补检 → 按 (路径, 行区间) 去重合并、
   统一重编 E/F 号 → 总结。调研预算 15 秒，超时的补检直接丢弃。
3. **宽回答格式**（`grounded.py`）：最多 12 条结论、要求逐一覆盖每个子问题/步骤；`ANSWER_MAX_TOKENS` 默认 1536 → 3072；
   证据只覆盖部分要点时先答已有部分再列缺口，不再整题“证据不足”。
4. **ask 尾延迟与稳定性**：LLM 调用对冲（规划 8 秒、总结 11 秒未返回就并发补发一份，取先到的；每个 ask 最多多占 1 个上游并发）；
   luna 偶尔把引用写进 claim 文本把 JSON 弄坏（`…结论。 [E1","E4"],"kind"`），现在按该形状修复，修不好在多轮模式下重问一次。
5. **core：并发检索的 expand 瓶颈**（与本轮其它改动无关的存量问题）：找 callers 时每次查询都把全仓未解析的 `js:` 调用边
   全部读出并逐条做 ESM 解析（数千条 × 十几次小查询），4 路并发时单条检索从 1 秒变成 14~18 秒。现在
   `Store.unresolved_edges(target_prefix=)` 在 SQL 里按前缀范围过滤，解析结果跨查询缓存、按 `Store.content_signature()` 失效。
   **检索输出逐字不变**（40 条查询对比），4 路并发单条约 1 秒。ask 补检因此改为 4 路并发。
6. core 检索小改（上一轮遗留，本轮 A/B 后保留）：图扩展邻居补“虚拟向量名次”（`vector.semanticize_expanded`）、
   成员调用解析与二跳、装填不足时逐级放宽。对 HEAD：大意 36 vs 35、参考文件命中 58 vs 56、单条 search +0.4s（仍 < 3s）。
7. 评测本身的修正：search 评审原来只看返回的前 20000 字符（34/45 被截断，误判集中在这里），现在 60000。

注意 / 未做：
- 评审噪声约 10%（luna 与 astra 对同一批输出的一致率 89%），单题翻转很常见，看整体。
- `benches/golden` 回归本轮没跑（需要 embedding 密钥环境）；第 5 项是纯性能改动且输出逐字一致。
- 本机没有 cargo，client 侧只改了契约 JSON 副本（Rust 测试校验两份一致，已确认字节相同）。
- 仍失败的题多是“流程中间一环没被补检到”（如 gkd `ResolvedRule.status` 的计算、cc-switch 新增 CLI 应用的适配模块面），
  可继续从规划提示或检索侧补。

## 2026-10-17 检索与 ask 优化轮

用新增的 [`bench/`](bench/README.md)（六个真实公开仓库 × 60 题，靶场见
[靶场清单](docs/handbook/benchmark/targets.md)）驱动，结果：

| | 基线 | 本轮 |
|---|---:|---:|
| search 命中@10 | 17/30 | **25/30** |
| ask 通过（自动粗筛） | 10/30 | **23/30** |

按“根因”而非“调参”改进，每一项都有 benchmark 证据：

1. **源码大文件被整体丢弃**（最大的一项）：索引有 128 KB 单文件上限，`fastapi/routing.py`
   （254 KB）、cc-switch 的 `forwarder.rs`（342 KB）等 24 个核心实现文件根本不在索引里，
   不是语义问题。现在**源码扩展名上限 1 MiB**，其余文件仍 128 KB。core
   `NOVA_MAX_SOURCE_FILE_BYTES` 可调；client 同口径（`client/src/blobref.rs::max_bytes_for`）。
2. **新增 Rust、Kotlin 解析器**（`core/nova_core/parsing/{rust,kotlin}.py`，节点表参考
   CodeGraph）：此前 `.rs/.kt` 只走兜底切分，大文件被切成无符号名的 `(module)` 块。
   依赖 `tree-sitter-rust`、`tree-sitter-kotlin` 已写入 `core/pyproject.toml` 和 `uv.lock`。
3. **路径先验**（`core/nova_core/retrieval/prior.py`，rerank 三个新特征）：多语言译文变体、
   变更日志、示例目录在查询没有明确要找它们时降权。设计文档 Module/02 §4.5 已同步。
4. **C/C++ 容忍函数名前的未知修饰宏**（FFmpeg 的 `attribute_align_arg` 之类），
   不再把整个函数压成 fallback 块。
5. **ask 不再轻易降级**（`service/nova_service/{answer,grounded,llmprotocol}.py`）：
   - 推理模型的 reasoning token 与正文共用 `max_tokens`，大上下文时正文为空或 JSON 被截断，
     过去被判“空回答 / 格式无效”。现在因长度截断会把额度放大 4 倍（上限 16384）重试一次。
   - 结构化回答解析容忍围栏变体、JSON 前后的说明文字、重复键。
   - **ask 只在“一条证据都没有”时才跳过 LLM**（HTTP 与 MCP 共用 `no_usable_evidence`）。
     `answerable=false` 是多通道共识的质量提示，对 ask 过严，且无关问题与真实问题的
     `confidence` 同为 low，分不开；有证据就交给 LLM，强制引用回验仍会移除无依据结论。
     实测“天气 / 番茄炒蛋 / 订单支付”这类无关问题会如实回答“证据不足”并列出缺口。
6. `PARSER_CONFIG_VERSION` 2 → 5，`Engine.rank_candidates` 提供候选池诊断。

**对老 benchmark 的回归检查**：`benches/golden/nova`（本仓自检，19 条）在路径先验开/关两种配置下
结果完全一致（recall@5=recall@10=0.684、MRR=0.575），说明 rerank 新特征对既有 golden 零回归
（该仓没有多语言译文，所以先验本就不应触发）。同一份结果里负例 `nova-0106` 仍判为可回答
（0/1），这是 backlog 里“负例可回答阈值”的既有缺口，本轮没有改动。
复现：`nova-core ingest --repo . --data ~/.nova/bench-golden` 后
`python benches/run.py --repo . --golden benches/golden/nova/nova.jsonl --data ~/.nova/bench-golden`，
用 `NOVA_W_TRANSLATION=0 NOVA_W_CHANGELOG=0 NOVA_W_EXAMPLE=0` 关闭先验做对照。

### 上线时会发生什么（VPS）

不需要手工迁移，按常规流程拉代码、`uv sync`、重启服务即可：

- **旧索引会在下一次索引时自动全量重解析**（解析版本号变了）。这是自动的、无感的，
  代价是 embedding 重新计算新增/变化的 chunk（有内容哈希缓存，未变的 chunk 不重复付费）。
  数据量参考：放开大文件让 ffmpeg/fastapi/pi/cc-switch 合计多约 5500 个 chunk（+4%）；
  Rust/Kotlin 解析器让 cc-switch 14582→27312、gkd 1209→5610（按符号切块后粒度更细）。
  大仓首次重建会多花几分钟（ffmpeg 约 2 分钟，其余不到 1 分钟）。
- 想先观察再放开，可设 `NOVA_MAX_SOURCE_FILE_BYTES=131072` 回到旧上限。
- **client 需要同步发版**：远程模式下 Rust client 在上传前按同样的规则过滤，只升级服务端
  不会让大文件进索引。
- **本机没有 cargo，client 的改动（`blobref.rs`/`index.rs`/`warmup.rs`）未编译、未测试**。
  请在有 Rust 的机器上跑 `cargo test`（`client/src/blobref.rs` 新增了
  `source_files_get_the_higher_limit`）后再发 npm 版本。

### 未做 / 下一步

- 剩下 5 个 search 未命中全是“自然语言问句 ↔ 代码名”的语义距离（如“放到线程池里执行”
  对 `run_endpoint_function`）。按约定**不在检索前加 LLM 步骤**，靠 Client 侧 Tool 描述引导
  Agent 把问句改写成更像代码的查询；这是产品文案问题，不是检索架构问题。
- `bench/` 与 [`benches/`](benches/README.md) 互补：后者是 golden 集上的 recall@k / MRR 回归，
  前者在真实仓库上暴露“根本没进索引”这类系统性问题。**两者都保留，别删 `benches/`。**
  下一步可把 `bench/` 的几个检索细节（译文去重、大文件、无解析器语言）补成 `benches/golden` 风格的
  细粒度用例，覆盖各个检索细节。
- **ask 模型统一为 `gpt-6-luna`**（非推理模型，约 3 秒返回）。模型完全由配置决定，代码里没有默认值：
  本机 `~/.key/nova/secrets.env`、部署文档示例（vps.md / wsl-live.md）、`.env.example` 已切换。
  **线上 VPS 需要同步**：把 `/root/.key/zace/secrets.env`（或实际生效的环境文件）里的
  `ANSWER_MODEL` 改为 `gpt-6-luna`，`ANSWER_API_KEY` 换成网关 **GPT 分组**的 key
  （`xiugou` 有 deepseek 与 GPT 两个分组，key 与分组绑定；DeepSeek 分组的 key 看不到 gpt 系列），
  然后重启 `nova-service`。设置页里用户自己配置的 LLM 不受影响。
  旧的 DeepSeek 推理模型会因推理 token 占满 `max_tokens` 而空回答，已有长度截断重试兜底，但不推荐。
- 提交 `bench/` 后本地跑法见 [bench/README.md](bench/README.md)；`bench/results/` 被忽略。

### 场景评测与嵌入模型（本轮后半）

- **评测方式改了**：随机抽函数出题会高估质量。新的 `bench/scenarios/*.jsonl` 是读完仓库、摸清某个功能的
  结构后手写的场景题（定位 / 排错 / 影响面 / 流程 / 设计 / 怎么做），每题带人工写的参考答案；
  `uv run python bench/run.py scenario [--repo X] [--tool search|ask]` 调真实工具，再让 LLM 对照参考答案
  评审（覆盖、可行动、噪声；ask 的正确/有依据/有用/幻觉）。参考文件命中是自动算的。
- **第一轮基线**（ffmpeg 除外较弱）：search 参考文件命中 23/36、噪声 2.35/5；ask 正确 2.79/5，多题答“证据不足”。
- **根因 1：嵌入模型**。`voyage-4-lite` 下 `toggle_app`、`solve_dependencies` 这类目标函数的向量名次是
  271 / 299 / 704，等于召回不到；换 `voyage-code-4` 后是 5 / 2 / 12，目标进向量前 50 的题从 18/22 到 22/22。
  **默认嵌入模型已改为 `voyage-code-4`**（价格约为 lite 的 6 倍，只在建索引时发生）。
  线上 VPS 还没改：把 `EMBED_MODEL=voyage-code-4` 写进环境文件并重启，下一次索引会按指纹自动整体重嵌。
- **根因 2：向量文本缺位置信息**。`embedding_text` 现在带 `path :: symbol` 前缀（`EMBEDDING_TEXT_VERSION=2`，
  并入向量缓存键，`PARSER_CONFIG_VERSION=6`）。
- **根因 3：同文件重载被合并**。`TcpConnection::send` 的三个重载只留一个，ask 因此答“证据不足”；
  现在同文件不同行段的同名符号不再聚合，只聚合跨文件的声明/实现。
- **仍待做**：召回已接近满分，但最终排名没吃到红利（目标向量第 12–16 名，最终排到 37–70）；
  返回里 `Related` 占约 35% token 而几乎只有 Core 在命中参考文件；ask 对“全流程”类问题证据偏窄。

## 当前工作区

本轮恢复 Python 3.12.3/uv 0.12.23、Node 22.23.3/npm 10.9.9、
Rust/Cargo 1.99.0 与 C/C++ 工具链。虚拟环境、缓存、索引和测试产物都留在仓库内的忽略目录。
旧失效虚拟环境与改动前重要文件备份于本机 `backups/`。

凭据统一在 `$HOME/.key/nova/secrets.env`，目录 0700、文件 0600；
仓库内 `.env` 只加载外部密钥。Voyage 与 LLM 凭据已恢复并通过实际请求验证；
LLM 使用用户指定的 DeepSeek 模型，网关地址与认证均不进入公共配置。
已有 SSH 部署密钥与供应商 API Key 不是同一类凭据。

已识别的 5 份内部用例/问答/报告及私有清单已迁至 `$HOME/.key/nova/benchmarks/`；
公开默认清单只保留公共靶场与自检，内部测试通过 `--targets-file` 显式选择。
Git 历史仍含迁移前内容，公开历史前需要另外处理。

本机 `nova-service` 未启动，旧持久基准索引没有出现在历史文档指定位置。
不假定新机器已经部署或已经有可复用索引。环境恢复不会自动启动生产服务。

## 新 VPS 复测

设备为 EPYC 7282、2 vCPU、1919 MiB RAM、1535 MiB swap。
LangChain 保持 `e75dae1f53c99c2b5ddb0c7bb36022c6aea25569`，
每轮 2986 文件 / 20931 chunks。并发 4、消费者 2、1024 维。

- 本地冷启动三轮 44.543 / 39.775 / 40.950s，中位数 40.950s，比旧机 45.462s 缩短 9.92%。
- CPU 累计中位数 36.656s，比旧机 44.965s 缩短 18.48%；未兑现早期“翻倍”的预测。
- OVH 直连单连接/四连接下载为 74.818/199.331 Mbps；Cloudflare 上传为 297.422 Mbps。
- Cloudflare 下载曾返回 403，已保留失败记录，未用错误响应计算吞吐。
- 真实 API full 单次 52.664s，比旧机 63.326s 缩短 16.84%；58 请求全部 HTTP 200，零索引错误。
- TTFB 并发 1/4/8，每档 8 请求均 HTTP 200；聚合响应速率 6.49/19.30/29.10 MiB/s，不能当作端口带宽。

证据与限制见 [设备报告](benches/results/index-perf-epyc-2c2g.md)。
入口为 `scripts/benchmark-vps.sh`，默认结果在 `.local/bench/`。
性能靶场版本与早期 golden 的 `41d3572` 不同，不混用文件数和耗时。

## 架构与工具语义

依赖方向为 `web, client → service → core`，由 CI 强制。

| 路径 | 职责 |
|---|---|
| `core/` | tree-sitter 解析、切片、SQLite/FTS、LanceDB、检索和 ContextPack |
| `service/` | REST/MCP、鉴权/租户、同步/索引、审计/配额、LLM 总结 |
| `client/` | Rust stdio MCP、扫描/忽略/哈希/增量上传 |
| `web/` | React/Vite 管理界面 |
| `npm/` | 客户端启动器和六个平台包清单 |

`search_context` 返回 ContextPack Markdown，不调用 LLM。
`ask_project` 先判断证据：不可回答时返回 insufficient_evidence；
总结模型未配置或调用失败时 degraded；成功时 answered，并回验引用。
`reason`、证据编号、Missing Evidence 和 Suggested Next Queries 都是重要返回语义。

## 产品开发与约束

任务板为 [docs/tasks/README.md](docs/tasks/README.md)；
新工作优先参考 [optimization-backlog.md](docs/plan/optimization-backlog.md)。
已登记的后续工作包括 TASK-093 真实数据采集、TASK-023 真实场景用例。

- 检索质量参数 R29/R30 仍冻结；不根据现有 smoke/golden 集拟合后直接调参。
- `docs/contracts/**`、`docs/design/**` 和 `core/nova_core/{types,interfaces,hashing}.py`
  属冻结面，变更遵循 [契约流程](docs/contracts/PROCESS.md)。
- TASK-109 已合并；不要把历史 pending 标记当作未完成。
- TASK-114/115 已优化 CLI 整仓冷启动，但实际客户端仍有串行分批上传和请求内 ingest，
  CLI 收益未必直接等于生产接入收益，见 backlog S1/S2。
- 多消费者要攒满整窗派发，不能按 K 切小窗口来维持并发。

## 已知缺口

下列问题来自既有审查，本轮环境与文档整理未修改业务代码：

- `/healthz` 的匿名项目进度信息需进一步收敛（backlog A2）。
- 符号级定位、负例可回答阈值、调用链命中仍需真实数据驱动。
- API 的 output_dimension 透传、chunk 参数配置与 full_reparse 成本仍待处理。
- 共享仓库认领、多租户组织模型、端到端测试覆盖仍有后续工作。
- 历史记录中的日志归属、契约 YAML 语法等缺口按对应任务卡核对，不在环境整理中顺手修改。
- 历史截图、旧证据及 Git 历史未做全量脱敏审计，不能宣称整个历史没有敏感内容。

## 验证与发布

验证命令见 [CONTRIBUTING.md](CONTRIBUTING.md)。
2 GiB VPS 串行运行 Python、Web、Rust 各组检查；core 按子目录、service 按测试文件拆进程释放内存。
整组长进程曾触发 MemoryHigh 回收限速，已终止后拆分重跑，不能把中断记为通过。
计时测试与构建、安装、常规测试分开执行。

本轮结果：Python 1362 通过、9 跳过（core 771、service 552、root 17、benches 22）；
Web 109 通过、3 跳过，lint/build 通过；Rust 61 项通过。
Ruff、依赖方向、版本一致性、隐私包往返恢复、改动文档链接检查均通过。
独立只读复审无阻塞问题；验证日志保存在本机 `.local/logs/checks/`。

仓库采用与既有 npm/Rust 声明一致的 MIT OR Apache-2.0。
当前变更包括原有未提交的性能手册；不要覆盖其他开发者改动。
提交、推送、发布 tag 和生产部署是不同动作，不把旧交接文档中的授权当成当前会话授权。
