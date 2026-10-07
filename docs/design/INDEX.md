# others/research 文档索引与写作规范

> 本文件是整个 research 文档体系的入口。任何人（或 AI 会话）从这里开始，不要直接跳进具体文档。
> 建立日期：2025-09-09。
> 文档优先级：Demo.md 是原始需求来源，仅供参考；与本目录内容冲突时，以 Background/ 的调研事实和 Module/ 的设计决策为准。设计决策以本文件 §3 为准。
> 维护规则见 §4。
> 最后更新：2026-09-10（D-39 拍板 P1；新增 D-44 embedding 双实现、D-45 CJK 分词器）。

## 1. 文档地图

### Background/（快照类：调研事实，只读）

基于 2025-09 的参考项目源码快照调研。**只增不改**：参考项目后续演进不跟踪，发现错误以"勘误"节追加，不改正文。结论引用时必须带"快照 2025-09"语境。

| 文档 | 内容一句话 | 关键产出 |
|---|---|---|
| 01-notace-tool-rs.md | 本地同步协议与 local/remote 边界参考 | blob hash / verified cache hit / checkpoint / 错误分类学 |
| 02-codegraph.md | tree-sitter 抽取与跨文件解析参考 | unresolved_refs 两阶段解析 / Rust kernel / explore 工具契约 |
| 03-ragcode.md | ContextPack 合同参考 + 规则引擎反面教材 | 证据分层 / budget trace / 15 路专门化检索的失败教训 |
| 04-gitnexus.md | 图模型与认知诚实性参考 | epistemic envelope / Process-Flow / C++ 语义功能清单 |
| 05-implications-for-nova.md | 调研 → nova 的综合启示 | C/C++/Python 结论 / MCP 切片证据 / 技术选型佐证 |
| 06-core-engine-proposal.md | 核心引擎提案（检索管线总设计） | 四路召回 + RRF + ContextPack + AI 总结层 |

### Module/（活文档类：组件详细设计，随实现演进）

状态流转：草案 → 评审中 → 定稿 → 已实现。**实现后以代码为准**，文档负责"为什么这么设计"；代码与文档冲突时，改完代码回来更新文档并记录漂移。

| 文档 | 组件 | 状态 | 依赖 |
|---|---|---|---|
| （外部切片思考稿，观点已吸收） | （外部参考，非正式设计） | 参考（文件未随当前目录保留） | — |
| 01-切片存储.md | 切片策略 + 存储方案 + 增量失效 | 草案 v2.2（待评审） | — |
| 02-检索策略.md | 轻路由 + 四通道召回 + RRF + 图双角色 + 确定性 rerank + Gap 二轮 | 草案（待评审） | 01 |
| 03-上下文组装.md | ContextPack 合同 + 装填预算 + 双层渲染 | 草案（待评审） | 02 |
| 04-AI总结.md | AnswerProvider + grounded prompt + citation 回验 | 草案（待评审） | 03 |
| 05-MCP与同步.md | **nova-client**：MCP 适配 + 同步客户端 + checkpoint + 超时矩阵 | 草案（待评审） | 消费 02-04 |
| 06-服务化与部署.md | **nova-service** 外壳 + 租户双层 + 部署形态 + monorepo + runtime 选型 | 草案（待评审） | 横切（core=01-04） |
| 07-WebUI.md | **nova-web**：页面骨架 + API 对接（画风留白） | 骨架（画风待定） | 06 |

组件拆分总览（四物理单元，Module/06 §0）：**nova-core**（引擎纯库 = Module/01-04）/ **nova-service**（服务化外壳 = Module/06）/ **nova-client**（本地客户端含 MCP 模块 = Module/05，Rust 二进制）/ **nova-web**（SPA = Module/07）。依赖方向：web, client → service → core，core 零上层依赖。仓库：monorepo 单仓多目录（D-35）。

### 推荐阅读顺序

```text
新会话冷启动：本 INDEX → Background/00 → 05 → 06 → 目标 Module 文档
只查某个事实：按上面表格定位（同步协议→01；解析→02…）
只关心某个决策：直接查 §3 决策登记表
```

## 2. 可信度标记（全体系统一含义）

| 标记 | 含义 | 引用纪律 |
|---|---|---|
| 【已验证】 | 有参考项目生产背书，且调研时核对了源码（正文带 file 路径锚点） | 可放心引用，注明出处项目 |
| 【权衡】 | 存在合理替代方案，文中含对比表 | 引用时说明"已否决项见原文" |
| 【自研】 | 无外部参考，nova 独有设计 | 风险最高，验证前不要当定论引用 |
| 【快照 2025-09】 | 基于该日期源码的调研结论 | 引用时保留时间语境 |
| （无标记） | 一般性陈述 | 按普通内容对待 |

## 3. 决策登记表（轻量 ADR）

已拍板决策的跨文档汇总。变更任何一条：先在此表更新状态，再去原决策文档记录"变更理由与触发条件"。D 编号全局唯一不复用。

| # | 决策 | 出处 | 状态 |
|---|---|---|---|
| D-01 | 服务端保留完整源码镜像（Source Mirror），索引全部可从源码重算 | Module/01 §1-C4、§3.3 | 定稿（待评审） |
| D-02 | 切片采用 AST 符号级（非机械行切），客户端只传文件级 blob | Module/01 §2 | 定稿（待评审） |
| D-03 | 存储为 per-project 目录树：blobs/ + index.db(SQLite+FTS5) + vectors/(LanceDB) | Module/01 §3.2-3.3 | 定稿（待评审） |
| D-04 | chunk_id = path:fqn:start_line，只求代内唯一，不追求跨代稳定 | Module/01 §2.4 | 定稿（待评审） |
| D-05 | 同一 chunk 三通道差异化输入（存储全文 / FTS 全文 / embedding 截断 2048 token） | Module/01 §2.4 | 定稿（待评审） |
| D-06 | SpecBlock→code 关联永远 provenance=inferred 弱引用，禁止强事实关系 | Module/01 §2.2 | 定稿（待评审） |
| D-07 | 三级配置指纹分层失效（parser 变→重解析；embedding 变→只重嵌） | Module/01 §4.2 | 定稿（待评审） |
| D-08 | C++ V1 定位"尽力而为 + unresolved 如实标注"，不做两阶段查找/模板实例化 | Module/01 §2.2 | 定稿（待评审） |
| D-09 | 检索为四路并行召回 + RRF(K=60) 融合 + 图扩展；不做规则引擎 planner | Background/06 §2 | 已细化于 Module/02（部分被 D-16/D-17 修订） |
| D-10 | ContextPack 为统一返回合同，search_context/ask_project 共用一条检索管线 | Background/06 §0 | 已细化于 Module/02（§4.8） |
| D-11 | AI 总结层可插拔（OpenAI-compatible URL/KEY/MODEL 可配置），首选 deepseek-v4-flash | Background/06 §4 | 已细化于 Module/04（§2 参数表） |
| D-12 | MCP 面向 agent 暴露少量高层工具（search_context / ask_project 主力） | Background/05 §3 | 已细化于 Module/05（§2.1 schema） |
| D-13 | MD 文档为一等检索资产：doctype 分级 + 标题链 SpecBlock + spec_references 双向桥（symbol 级外键 + stale 级联）；doctype 加权只作为 rerank 信号，不开独立检索通道 | Module/01 §2.2 | 定稿（待评审） |
| D-14 | 轻路由四分支（Symbol/Path、Structural、Docs、General），分支数硬上限 4，只调通道配额不新增通道，新增分支需架构评审 | Module/02 §4.1 | 定稿（待评审） |
| D-15 | Exact 双档：Explicit（反引号/路径/::链）强种子 vs Inferred（正则抽取）普通种子，后者只是召回种子不是精确证据 | Module/02 §4.2-a | 定稿（待评审） |
| D-16 | 融合层纯 RRF(K=60) 不加通道权重；最终质量由确定性 Evidence Rerank 负责（V1 内置无模型，V1.5 可选 cross-encoder） | Module/02 §4.3/4.5 | 定稿（待评审） |
| D-17 | EvidenceTier = 可信度元数据 + 装填资格线 + rerank 特征，不强制决定最终排序（修订 Background/06 原表述） | Module/02 §4.6 | 定稿（待评审） |
| D-18 | 图双角色：普通查询 = 扩展器（calls 1-hop + spec_references 双向，tier3 配额≤30%，caller 爆炸按入口点截断）；结构型查询 = 主路径（符号解析→直查） | Module/02 §4.4 | 定稿（待评审） |
| D-19 | Deep 模式 Evidence-Gap 驱动二轮检索（G1-G5 规则表），最多 1 次定向补检，不预猜 intent；**2026-09-15 追加**：Repair 是受限回路而非第五级（§3 命名 + §4.7.1 的 R31 三条不变量）；G3 按 I1 不合法、未实现 | Module/02 §4.7 | 定稿（待评审） |
| D-20 | 中文/CJK 为一等查询场景：FTS 索引/查询双侧预分词（分词器独立模块）；已修正 01 的 unicode61 tokenizer 缺陷 | Module/02 §4.2-b、Module/01 v2.1 | 定稿（待评审） |
| D-21 | ContextPack 双层合同：内部结构化 JSON（evidence/docs 分列 + 单一 E 编号空间）+ 对外 Markdown，两层共用 formatter | Module/03 §2 | 定稿（待评审） |
| D-22 | 装填算法：rerank 分降序贪心 + 单文件预算上限 25% + tier3 配额 30% + spec 保底 1-2 块 + 去重三招（区间合并/同符号聚合/skeleton） | Module/03 §4 | 定稿（待评审） |
| D-23 | 预算默认值：Fast 14K / Deep 16K（TASK-MCP-BUDGET 2026-09-16 由 10K/12K 调档；仍拒绝 20-50K，flash 长输入注意力衰减）。单文件上限与 spec 份额锚定绝对值，不随 hardCap 缩放 | Module/03 §4.2 | 定稿（待评审） |
| D-24 | answerable=false 短路：Deep 模式不走 LLM，返回结构化证据不足包 + nextQueries | Module/03 §4.4、Module/04 §3 | 定稿（待评审） |
| D-25 | Citation 回验：服务端正则校验 [E*]/[F*] 引用存在性，无效只删标记不改写，coverage 入审计 | Module/04 §5 | 定稿（待评审） |
| D-26 | LLM 失败降级 never-empty-handed：超时/未配置均返回 ContextPack 渲染 + 故障说明 | Module/04 §6 | 定稿（待评审） |
| D-27 | 懒同步：tool call 自动保证工作区新鲜，无常驻 watcher，手动 sync 仅供 debug | Module/05 §3.1 | 定稿（待评审） |
| D-28 | 忽略规则三层：.novaignore > .gitignore（真实解析）> 内置默认，补齐 notace 缺陷 | Module/05 §3.1 | 定稿（待评审） |
| D-29 | project identity = sha256(git remote + repo 相对路径)，无 git 用路径 hash；跨机器同 repo 共享索引 | Module/05 §3.4 | 定稿（待评审） |
| D-30 | freshness 语义：上传完成 ≠ 索引完成，ContextPack 如实报告 indexingFiles，不阻塞等待 | Module/05 §3.6 | 定稿（待评审） |
| D-31 | 首同步断点续传：120s 转后台 + 返回进度反馈，拒绝 notace 的 180s 阻塞一把梭 | Module/05 §3.5 | 定稿（待评审） |
| D-32 | 分层超时矩阵：upload 30s / search 15s / ask 90s / 首次全量 120s 转后台 | Module/05 §4 | 定稿（待评审） |
| D-33 | 四物理单元拆分：nova-core（纯库）/ nova-service（外壳）/ nova-client（含 MCP 模块）/ nova-web；依赖方向 web,client→service→core，core 零上层依赖 | Module/06 §0-1 | 定稿（待评审） |
| D-34 | nova-core 纯库化：零 HTTP/零鉴权/零用户概念，字典里只有 project；CI 强制依赖清单，这是本地嵌入与云端部署同构的钥匙 | Module/06 §1 | 定稿（待评审） |
| D-35 | monorepo 单仓多目录（拒绝多 git），边界由包依赖 CI 检查强制；未来可 subtree split | Module/06 §5 | 定稿（待评审） |
| D-36 | 租户隔离双层：service 层授权映射（token→user→owns project）+ core 层物理隔离（per-project 目录） | Module/06 §2.3 | 定稿（待评审） |
| D-37 | REST API 合同（Module/06 §2.1 端点表），batch-upload/deletions 幂等语义 | Module/06 §2.1 | 定稿（待评审） |
| D-38 | 部署形态：A=VPS docker compose（V1 主形态）/ B=本地嵌入 client+core（V2）/ C=内网；部署只是配置 | Module/06 §4 | 定稿（待评审） |
| D-39 | runtime 分阶段：V1 = Python(core+service) + Rust(client) + SPA(web)；理由：V1 胜负手是检索质量不是性能，D-34 保住 V2 Rust 化可能 | Module/06 §6 | **定稿（2026-09-10 拍板 P1）** |
| D-40 | WebUI V1 = 管理面 + Playground（复用服务端 Markdown 渲染不自研渲染层）；Graph 可视化等分析面 V2+ | Module/07 | 定稿（待评审） |
| D-41 | V1 重点支持 Python、C、C++、Markdown；Kotlin 暂不适配，仅保留未来 Parser Provider 扩展路径 | 用户确认；Module/01 §2.2 | 定稿（待评审） |
| D-42 | SpecBlock 是一等证据类型：复用现有 BM25/Vector 检索，通过 doctype/rerank 与装填保底提高权重，不新增第五套 Planner 通道 | 用户确认；Module/01 §2.2、Module/02 §4.2 | 定稿（待评审） |
| D-43 | 文件 blob hash、文件 content hash、chunk content hash 分离定义；Module/01 作为存储与 hash 语义唯一依据 | 用户确认；Module/01 §2.4 | 定稿（待评审） |
| D-44 | embedding 双实现（EmbeddingProvider 接口）：本地 ONNX 小模型为默认（源码不出 VPS），OpenAI-compatible API 为可选配置；具体默认模型与维度由 Phase 1 bake-off（TASK-015）校准后钉死 | 用户确认；Module/01 §2.4、Module/06 §6 | 定稿 |
| D-45 | CJK 分词器 = jieba（Python 实现），模块化隔离（nova_core.text）；索引与查询双侧同库预分词，消费 D-20 | 用户确认；Module/02 §4.2-b、§7-2 | 定稿 |
| D-46 | 主链路命名 **Recall → Expand → Rank → Pack → Repair**（四级检索漏斗 + 单次 Evidence Repair Loop）；Repair 是受限反馈回路而非第五级，受 R31 三条不变量约束 | Module/02 §3、§4.7.1 | 定稿（2026-09-15） |
| D-47 | LLM 上游协议可配且多实现：`AnswerProvider` 接口不变，支持 **openai**（`/v1/chat/completions`，默认）/ **responses**（`/v1/responses`）/ **anthropic**（`/v1/messages`）；服务端默认走 `ANSWER_PROTOCOL`，用户级覆盖走 `user_llm_config.protocol`；并提供连接自检（`POST /api/auth/llm-config/test`，L1 探测 `/v1/models` / L2 真实最小请求）。理由：同一网关的不同模型可能只开放不同协议（实测 2026-09-16：`deepseek-v4-flash` 仅 `ANTHROPIC`/`RESPONSES`），单一 OpenAI 实现会让“配置保存成功但 ask 持续 503”成为静默失灵。否决项：① 只做协议探测不实现新协议（用户仍无可用模型）；② 失败后自动换协议重发（两次计费且答案不可归因）。触发重评：D-11 的模型选型变化、或新增第四种协议 | Module/04 §2；用户确认 2026-09-16 | 定稿（2026-09-16） |
| D-48 | **二进制分发走 npm 平台子包，不走 GitHub Release 下载**：主包 `nova-client`（仅启动器）+ 6 个平台子包 `nova-client-<os>-<arch>`（各含本平台二进制，作为主包 `optionalDependencies`）；npm 按子包 `os`/`cpu` 字段自动只装本平台那一个，启动器直接执行。理由（实测 2026-09-16）：① Node 默认**不读 `https_proxy`**（只认 `NODE_USE_ENV_PROXY=1`，v20+），代理环境里启动器直连 GitHub 命中共享出口 IP 的 403 rate limit（同一时刻 `curl` 走代理 200、`node` 直连 403），用户看到的是 `MCP server failed to start: connection closed`，极难排查；② 旧形态下 `npm publish` 必须等 Release 五平台资产就绪，人为纪律、错一步即 404。改为子包后**无网络下载步骤、无版本对齐**，用户零配置。否决项：① 给 `run.js` 加自设代理（仍要用户网络可达 GitHub，且 `NODE_USE_ENV_PROXY` 属实验特性）；② 只改文档让人“重试 403”（把设计缺陷当运维问题）；③ 把二进制直接打进主包（包体 ×6，且 npm 无按平台裁剪机制）。触发重评：npm 改变 optionalDependencies 的平台裁剪语义、或出现第二种分发渠道需求 | npm/README.md、docs/handbook/release/README.md；用户确认 2026-09-16 | 定稿（2026-09-16） |
| D-49 | **“以后都采用平台子包方式”是 nova 的既定分发规范**：今后任何面向用户的**预编译二进制**（客户端、CLI、后续可能的新工具）一律按 D-48 的形态分发——主包仅放启动器/胶水，二进制拆到 `<主包>-<os>-<arch>` 平台子包并列入主包 `optionalDependencies`；平台表以单一脚本的常量表为唯一事实来源，六平台（linux/darwin/win32 × x64/arm64）全量维护，发布顺序固定为**先子包、后主包**；一致性由 `scripts/check-version.sh` + `scripts/check-npm-platforms.js` 在 CI 与发布前守住。理由：D-48 的两个根因（Node 不读代理环境变量导致的下载失败、跨制品版本对齐的人为纪律）对**任何**二进制分发都成立，不是 nova-client 的特例。否决项：① 只把 nova-client 改了、后续新工具重新走下载（同一坑第二次踩）；② 允许“先发部分平台”过渡（缺失平台是**静默**故障——npm 跳过解析不了的可选依赖，用户侧无任何提示） | npm/README.md、docs/handbook/release/README.md；用户确认 2026-09-16 | 定稿（2026-09-16） |

## 4. 写作纪律（新文档必须遵守）

1. **事实三要素**：结论 + 来源锚点（源码 file 路径 / 参考项目名 / URL）+ 日期。无锚点的关键事实不许写。
2. **决策四要素**：决策 + 理由 + 否决项（对比表）+ 触发重评条件。决策必须登记进 §3。
3. **开放问题显式成节**（如 Module/01 §6），不埋在正文段落里。
4. **术语与 §5 术语表保持一致**，新术语先入表。
5. **文头标注**：状态（草案/评审中/定稿/已实现）+ 最后更新日期。
6. Background 只增不改（勘误追加）；Module 修订必须更新日期。
7. 密度要求：每句话承载信息。对比用表、流程用图、理由用列表；宁短勿水。
8. **MANIFEST.sha256 语义**（2026-09-10 明确）：它是设计文档迁入 nova 仓库时的**快照记录**（用于核对“研究目录快照”与仓库版本的对应关系），不是持续校验门禁。设计文档的后续变更由编排者执行并经 git 历史追踪；变更后不强制重算 manifest（如需校对快照，用 `git log -- docs/design/` 对照）。实现期的口径与漂移记录统一写在 `docs/contracts/PROCESS.md` §3。

## 5. 术语表（跨文档统一）

| 术语 | 定义 | 首次定义于 |
|---|---|---|
| Source Mirror | 服务端保留的完整源码内容寻址镜像（blobs/） | Module/01 |
| Chunk | 检索单元 = 一个符号的完整切片；三索引共同主体 | Module/01 |
| Symbol | 图节点（函数/类/方法/结构体/宏…），跨文件解析主体 | Module/01 |
| Edge | 符号间关系（calls/imports/extends…），带 provenance | Module/01 |
| provenance | 边/引用的来源标注：parsed（真实解析）/ synthesized（合成）/ inferred（推断） | Module/01 |
| unresolved_refs | 两阶段解析的暂存表：pending→resolved(删)/failed(保留重试) | Module/01 |
| Explicit / Inferred Exact | Exact 证据双档：反引号/路径/::链为 Explicit（强种子）；正则抽取为 Inferred（普通召回种子） | Module/02 |
| Evidence Gap | Deep 模式的确定性缺口信号（G1-G5），驱动最多一次定向补检 | Module/02 |
| 双层合同 | ContextPack 内部 JSON + 对外 Markdown，两层共用同一 formatter（D-21） | Module/03 |
| Citation 回验 | 服务端校验 LLM 回答中引用 id 的存在性，无效只删标记不改写（D-25） | Module/04 |
| 懒同步 | tool call 自动保证新鲜度，无常驻 watcher（D-27） | Module/05 |
| project identity | sha256(git remote + repo 相对路径)，跨机器同 repo 命中同一服务端项目（D-29） | Module/05 |
| nova-core / -service / -client / -web | 四物理单元：引擎纯库 / 服务化外壳 / 本地客户端（含 MCP 模块）/ Web 管理面（D-33） | Module/06 |
| 服务化外壳 | nova-service 的全部职责：HTTP API、鉴权、租户映射、索引 job、审计、可观测——鉴权归外壳而非 client | Module/06 |
| 纯库纪律 | nova-core 的依赖清单禁止 HTTP/用户体系；输入 ChangeSet+查询，输出 ContextPack（D-34） | Module/06 |
| 轻路由 | 检索前的四分支线性判断（分支数硬上限 4），只调通道配额 | Module/02 |
| 入口点 | 无内部调用者且被导出的符号；caller 爆炸时的截断排序依据 | Module/02 |
| SpecBlock | Markdown 标题树下的完整小节，spec 检索基本单位；heading_path（标题链）为其 fqn | Module/01 |
| doctype | 文档类型分级（agent-instructions/readme/design/adr/api/changelog/guide），rerank 与预算优先级信号 | Module/01 |
| spec_references | spec↔code 双向桥：symbol 级外键弱引用，检索层第二种图扩展边；含 stale 过时标记 | Module/01 |
| 四层模型 | SourceFile → Symbol → Chunk → Index Views（FTS/向量/图） | Module/01 |
| 三级指纹 | parser_config_hash / embedding_model / schema_version，各触发不同层级重建 | Module/01 |
| ContextPack | 检索输出的结构化证据包（snippets+callPaths+missingEvidence+budget） | Background/06 |
| evidenceTier | 证据分层 0-3：exact > keyword > semantic > graph-expanded | Background/06 |
| RRF | Reciprocal Rank Fusion，K=60，按排名融合多通道 | Background/06 |
| AnswerProvider | AI 总结层的可插拔接口（OpenAI-compatible） | Background/06 |
| epistemic envelope | "可计数的无知模型"：缺什么/为什么缺/缺多少/怎么补 | Background/04 |

## 6. AI 会话使用指引

新会话（任何 agent）进入本仓库处理 nova 相关任务时：

1. **先读本 INDEX**，按 §1 阅读顺序取所需文档，不要全量读所有文档。
2. 引用调研结论时**必须带可信度标记与时间语境**（例："codegraph 的 C 覆盖率 92.2%【已验证·快照 2025-09】"）。
3. 设计决策以 **§3 决策登记表 + 对应 Module 文档**为准；Background 是事实来源不是决策来源。
4. 实现阶段代码与 Module 文档冲突：**以代码为准**，并在对应 Module 文档记录漂移。
5. 修改 Module 文档：更新文头状态与日期；新增/变更决策：同步更新 §3 登记表。
