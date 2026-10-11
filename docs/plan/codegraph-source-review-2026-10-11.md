# CodeGraph 源码调研与 NOVA 借鉴清单

- 状态：只读源码调研完成；建议待实施与评测，不代表 NOVA 已具有这些能力。
- 调研日期：2026-10-11。
- 仓库：<https://github.com/colbymchenry/codegraph>，由用户指定。
- 固定 commit：`fc0da38f0f1a18147bd8c395ff78ae915d8de6e9`。
- `package.json` 版本：`1.6.2`；这是该源码快照字段，不等于确认 npm 已发布此快照。
- 方法：浅克隆后直接读取相关生产源码；没有安装依赖、运行第三方脚本、执行测试、调用模型或复现收益基准。
- 关联计划：[NOVA 检索与 search / ask 计划](retrieval-evidence-search-ask-plan.md)。

本次结论只适用于上述 commit。仓库现有 `docs/design/Background/02-codegraph.md` 是另一个历史研究快照，本轮未读取或修改，不将两份资料混为同一实现。

## 1. 核心结论

CodeGraph 最值得吸收的不是一个现成的自然语言问答工具，而是：

1. 统一结构抽取接口、语言适配与跨文件引用解析。
2. 符号关系在增量变化后的重新绑定，而不只是更新发生修改的文件。
3. 用明确锚点、有限桥接和调用点窗口把相关实现连接起来。
4. 将动态边界、启发式来源和新鲜度不确定性显示给调用方。
5. 输出预算优先保障目标实现，而不是平均切碎所有候选。

NOVA 的 BM25 / Vector / 文档证据体系应保留：CodeGraph 当前 explore 的相关性链路主要使用名称、FTS、词根和图关系，不是可直接替代自然语言语义召回的 embedding 检索器。

## 2. 当前工具形态与宣传描述需要区分

【源码核对·2026-10-11】`src/mcp/tools.ts` 的 `DEFAULT_MCP_TOOLS = new Set(['explore'])`：默认 MCP 只暴露 `codegraph_explore`，其余定义可通过配置重新启用。`src/mcp/server-instructions.ts` 引导 explore 返回带行号的原始源码、关系与影响摘要，也明确词法匹配、跨文件 best-effort 和无运行时正确性验证的局限。

- [默认工具集合与筛选](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/mcp/tools.ts#L1828-L1855)
- [Agent 指引与限制](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/mcp/server-instructions.ts)

因此不能将 README 中 callers、callees、impact 等能力列表理解为当前默认全部暴露的独立工具，也不能据此直接设计 NOVA 的 MCP 表面。

`AGENTS.md` 的维护说明提醒：仅更换指引或工具描述不保证 Agent 改变选择，能力应适配 Agent 已有输入。该文属于维护者经验和评测陈述，本轮未复现。对 NOVA 的启示是：双工具优势区间仍可引导，但不把理想工具选择或理想符号输入作为结果可用的前提。

“留在上下文中的检索内容平均多约 80%”未核验来源、版本和测量口径，本轮不作为判断 CodeGraph 缺点的证据；不引用其他吞吐、准确率或节省 token 数字作为 NOVA 收益预测。

## 3. 实现发现与吸收方式

### 3.1 统一抽取底座 + 语言配置 / hook

【源码核对·2026-10-11】`src/extraction/tree-sitter-types.ts` 定义 `LanguageExtractor`：AST 节点类别、名称 / 正文 / 参数字段及语言 hook；`ExtractorContext` 提供创建节点、遍历正文、维护作用域和追加 unresolved reference 的受控 API。`languages/index.ts` 注册 TS/JS、Python、Go、Rust、Java、C/C++ 等多语言适配器。

- [抽取接口](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/extraction/tree-sitter-types.ts#L44-L164)
- [语言注册表](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/extraction/languages/index.ts)

**NOVA 借鉴**：继续使用 Parser 边界，将公共位置 / 作用域 / 引用抽取与语言语义适配分开；先 TS/JS，再按目标仓库分布补语言。能力表分别记录“能切片、能找定义、能跨文件解析、能产调用边”，不要只用语言数量验收。

**不照搬**：不移植整个 Node / Rust kernel 双栈，也不因注册了语言就声称调用关系完整准确。本轮没有逐语言评测。

### 3.2 引用解析有策略、作用域门禁与 unresolved

【源码核对·2026-10-11】`ReferenceResolver` 将 unresolved refs 转为 resolved / unresolved 结果；策略包括框架、import、名称匹配，并结合语言、文件可见性和链式接收者规则限制误连。`ResolvedRef` 带 confidence 和 resolvedBy；某些高置信结果提前返回，其余选择较高 confidence 的候选。

- [解析结果接口](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/resolution/types.ts#L36-L70)
- [批量解析](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/resolution/index.ts#L1035-L1097)
- [策略与可见性约束](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/resolution/index.ts#L1430-L1582)

**NOVA 借鉴**：先依据 import / alias / scope 再尝试弱名称候选；存解析依据和调用点，保留无法解析的引用。引入同名、不可见符号、外部依赖、链式调用等负例。

**边界**：源码中的 confidence 是规则分值，不是经统计校准的正确率。“resolved”也不能自动等同编译器已证明；NOVA 不直接复制这些阈值作为真实可信概率。

### 3.3 增量索引要重新打开失效关系

【源码核对·2026-10-11】`reconcileCrossFileNodes` 删除不再需要的框架节点前，会将相关入边恢复为待解析引用；新增节点也加入 unresolved refs。`reopenRouteModuleReaders` 根据变化模块重新打开受影响的路由引用，避免一个曾经成功解析的关系永久指向旧实现。

- [跨文件节点协调与路由引用重开](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/resolution/index.ts#L406-L487)

**NOVA 借鉴优先级：高。** 新语言支持不能只做“改文件 → 更新自己的节点”。还要确定改名、删除、重导出和模块新增后，哪些未改文件的关系必须失效和重解析。

验收：解析结果应随变化更新；删除不得留下悬空确定边；未修改调用方也应重新绑定。此项帮助 search 的连接准确性，也帮助 ask 避免基于旧关系总结。

### 3.4 框架规则与动态合成边可补连接，但必须带来源

【源码核对·2026-10-11】`frameworks/python.ts` 独立实现 Django / Flask / FastAPI 检测、路由抽取和特定引用解析。FastAPI 部分使用装饰器 regex，路由与普通符号解析不在同一层；其中依赖函数名称 / 目录等规则属于启发式。

`callback-synthesizer.ts` 通过 `SYNTH_PASSES` 管理多种合成规则；callback、event-emitter、react-render 等边可带 `provenance:'heuristic'`、`synthesizedBy`、`registeredAt`。部分结构性合成另有来源，不能把所有合成都当作同一种可信度。

- [Python 框架适配](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/resolution/frameworks/python.ts#L235-L288)
- [callback / emitter / render 合成来源](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/resolution/callback-synthesizer.ts#L190-L422)
- [合成规则注册与执行](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/resolution/callback-synthesizer.ts#L4415-L4732)

**NOVA 借鉴**：从 FastAPI 路由、TS/JS import 与常见事件绑定等窄规则开始；边携带绑定位置、适配器与规则版本；将直接抽取、解析推导和启发式关系分开展示。

**不照搬**：不以“把断掉的图补连通”为唯一目标，不把 regex / 名称习惯当完整语义模型，不一次移植所有框架。规则需误连反例和增量失效验证，结构路径不是运行时必然路径。

### 3.5 命名符号之间有限桥接，而不是无限扩邻居

【源码核对·2026-10-11】`named-symbol-flow.ts` 共用一个路径实现：named 模式以已命名符号作锚点，允许至多一个连续未命名桥接节点，寻找较长命名链；directed 模式固定两端并寻找较短路径，限制不同。精确符号查询失败不会将模糊 FTS 第一名冒充端点；存在歧义时保留定义和提示。

- [命名 / 定向模式与限制](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/graph/named-symbol-flow.ts#L1-L180)
- [查询与路径实现](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/graph/named-symbol-flow.ts#L283-L679)

**NOVA 借鉴**：围绕明确符号 / 路径和可信召回锚点发现少量连接证据，避免高扇出节点吞掉上下文。对自然语言召回产生的锚点，应另外保留锚点不确定性，不能认为与用户直接命名同样可靠。

**不照搬**：不要求 Agent 必须写符号袋；不把“最长链”或“最短链”直接定义成最佳答案；无路径时报告静态分析边界而非证明不存在关系。

### 3.6 先保障精确目标，围绕调用点选择源码窗口

【源码核对·2026-10-11】`handleExplore` 会将明确路径文件置于 pinned 集合，纳入精确目标及行号锚点；`questionIds` 合并精确目标、命名符号和 flow spine。`bodyFocusLines` 优先考虑行号锚点、主链调用点及指向相关符号的调用点。

输出中存在紧凑 Blast radius、Relationships、Flow 与按文件组织的源码。`ExploreOutputBudget` 限定整体和单文件字符量，分档阈值面向该工具和宿主经验，不是 token 通用标准。

- [预算定义与分档](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/mcp/tools.ts#L263-L365)
- [路径优先、精确目标、行号锚点](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/mcp/tools.ts#L3998-L4216)
- [关系组织与源码焦点](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/mcp/tools.ts#L5043-L5119)

**NOVA 借鉴优先级：高。** 核心实现先分配预算；图邻居用于补连接而非抢目标正文；大函数片段尽量覆盖相关调用点和必要条件，并报告省略。search 按“核心实现 / 连接 / 边界”组织，ask 使用同样焦点与关系元数据构造更宽的模型证据。

**不照搬**：不采用仓库大小直接决定响应质量，不复制字符上限作为 token 上限；不默认每次加入全部影响摘要，也不向 Agent 宣称截取源码足以直接修改所有相关位置。

### 3.7 动态边界是结果，而不是失败或强行猜边

【源码核对·2026-10-11】`dynamic-boundary-report.ts` 是只读查询时派生层：扫描静态路径停止处，针对可见 dispatch key 给出候选目标与未跟随的 continuation，不修改图。扫描次数、字符量和候选数量均有上限。

- [边界模型与资源限制](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/graph/dynamic-boundary-report.ts#L1-L85)
- [边界查询](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/graph/dynamic-boundary-report.ts#L132-L350)

**NOVA 借鉴优先级：高。** search 可以明确指出“静态连接到这里停止，以下只是候选”；ask 将其作为不确定性证据。图边、候选与边界分开，不让 LLM 自动把候选拼成真实链。

扫描器本身也是受限检测，不代表所有动态边界都能发现。本轮没有测试其覆盖率。

### 3.8 Impact / affected tests 是静态潜在关联，不是测试覆盖证明

【源码核对·2026-10-11】`GraphTraverser.getImpactRadius` 反向遍历依赖；容器节点额外展开成员，向上过滤 contains，避免从一个方法扩散所有兄弟成员。文件依赖来自已解析的跨文件符号边，不是单纯 import 声明。

CLI `affected` 对文件依赖 BFS，识别测试文件并受 maxDepth 限制；这不是执行测试，也不是动态覆盖映射。

- [影响遍历](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/graph/traversal.ts#L530-L634)
- [文件依赖投影](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/graph/queries.ts#L112-L142)
- [受影响测试 BFS](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/bin/codegraph.ts#L2612-L2664)

**NOVA 借鉴**：为修改前 search 提供紧凑候选影响 / 测试位置，必要时给 ask 解释依据；显示关系类型、深度和覆盖局限。不能由“没找到测试”推导“没有测试”，不能承诺推荐集合足够验证修改。

### 3.9 新鲜度由启动补偿、监听与查询校验共同保证

【源码核对·2026-10-11】`engine.ts` 的 `catchUpSync` 处理离线期间变化，并将 promise 作为首轮工具调用 gate。Watcher 使用原生 `fs.watch`，区分平台、合并事件、有监听数量 / 重试上限并暴露降级。

`answer-freshness.ts` 检查贡献文件的 indexed contentHash，受总文件数、字节量和时间限制；返回 stale / unchecked。`ToolHandler.execute` 在 watcher 降级等条件下使用该校验，验证不通过拒绝提供基于旧索引的回答。不能简化为“每个查询都全仓库 hash”。

- [启动补偿与 gate](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/mcp/engine.ts#L420-L452)
- [Watcher 资源与退化策略](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/sync/watcher.ts#L1-L165)
- [回答贡献文件校验](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/mcp/answer-freshness.ts)
- [查询校验调用条件与拒绝回答](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/mcp/tools.ts#L2855-L2878)

**NOVA 借鉴**：保留现有预热，用监听加快变化感知，查询核验相关文件版本；unknown 不能当 fresh。监听失败、部分范围未覆盖和恢复中必须可见。

**架构差异**：CodeGraph 本地读盘；NOVA 远端只能通过客户端版本、上传内容和服务端索引状态协作。移植的是一致性原则，不是本地 fs API 或其符号链接策略。

### 3.10 跨调用去重默认关闭，原因值得直接吸收

【源码核对·2026-10-11】`explore-dedup.ts` 用内容指纹与源码范围判断已发送内容，只在明确开启时省略；未设置 `CODEGRAPH_EXPLORE_DEDUP` 默认 false。原因是 MCP 连接不能证明调用者属于相同持续上下文，子代理和 compaction 可能没有先前源码。

- [去重规则、默认值与内容指纹](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/mcp/explore-dedup.ts#L1-L110)

**NOVA 借鉴优先级：高（安全约束）。** 首阶段只做单次请求内部去重；不能用连接 ID / 项目 ID 证明 Agent 已经持有源码。后续只有宿主提供上下文身份和生命周期，才能评审跨调用省略。

### 3.11 词法＋图与 NOVA 语义检索是互补，不是替代

【源码核对·2026-10-11】`ContextBuilder.findRelevantContext` 从查询符号、词根种子、精确名称、定义前缀和 FTS 词项取候选，对多词共现、文件共址等做分数调整。源码注释使用 HYBRID 一词，但本次核对的该函数和默认指引体现的是词法与结构组合，不能据此称为 embedding 混合检索。

- [精确与词根候选](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/context/index.ts#L457-L582)
- [FTS 与合并](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/src/context/index.ts#L632-L731)

**NOVA 借鉴**：命名符号同文件共现、词根分段、精确目标与真实关系可以作为可解释信号。保留 Vector 解决用户表达与实现命名不一致的问题，不复制大量语言特例到通用检索层。

## 4. 对 search / ask 的具体贡献

| 参考能力 | search 的收益方向 | ask 的收益方向 | 优先级 |
|---|---|---|---|
| 精确目标与调用点焦点窗口 | 少读无关源码，避免丢目标正文 | 让模型看到真正决定行为的片段 | 第一批 |
| 引用失效重解析 | 减少旧边和错误定位 | 减少基于旧连接的总结 | 第一批结构质量 |
| 动态边界与候选分离 | 显示下一步调查位置 | 提供诚实缺口与局限 | 第一批 |
| 统一语言适配与 resolver | 多语言功能定位与跨文件连接 | 扩大机制解释可依据的代码范围 | 按语言分期 |
| 框架绑定与有限桥接 | 补齐相关实现之间的连接 | 解释入口、绑定和协作机制 | 先窄规则验证 |
| 潜在影响与候选测试 | 修改前的紧凑关联位置 | 解释潜在影响及依据 | 第二批 |
| 三层新鲜度与退化状态 | 不把旧位置当当前源码 | 不把未知版本当当前实现 | 后续一致性专项 |
| 默认关闭跨调用省略 | 子代理 / 压缩后仍拿到证据 | 不缺关键上下文 | 立即保留约束 |

以上是设计收益方向，不是本轮测得的效果。具体实现顺序以主计划为准，不在本轮写入 NOVA 代码。

## 5. 明确不采用的方向

- 不移植 CodeGraph 单一 explore 的 MCP 表面；NOVA 保留 search / ask 双工具。
- 不依赖 Agent 必须输入符号袋；自然语言语义与文档继续是 NOVA 的基础能力。
- 不以图连通率替代调用边准确率，不把启发式边渲染为事实。
- 不默认强迫 Agent 放弃 read / grep；精确文件读取、全量引用和未覆盖区域仍应使用合适工具。
- 不照搬大源码响应，也不证明“更少返回”天然更优；衡量任务正确性与全链路成本。
- 不用连接作为持续上下文身份，不默认跨调用省略源码。
- 不新增完整图数据库、不替换 NOVA 语言栈、不复制第三方 daemon / installer。
- 不将未复现的维护者评测数字写成已验证的性能结论。

## 6. 许可与研究边界

该快照 [LICENSE](https://github.com/colbymchenry/codegraph/blob/fc0da38f0f1a18147bd8c395ff78ae915d8de6e9/LICENSE) 为 MIT，版权为 Colby Mchenry。此次只记录设计思路与源码链接，没有复制实现进入 NOVA。后续若直接复制或改编实质代码，须保留对应版权和 MIT 许可声明，并核对 grammar / WASM 等资产各自许可证；NOVA 自身许可不能替代这些义务。

本轮未验证：逐语言调用边准确率、全部框架语义、动态边界覆盖率、真实 watcher 平台行为、完整测试套件、Agent 工具选择 / token 成本收益及所谓“80%”数字。需要运行或付费评测时另行确认范围。

下一步首选：按主计划阶段 A/B 解耦内部证据集合与两种输出；再把精确目标优先、源码焦点窗口、关系来源 / 动态边界逐项做消融验证。
