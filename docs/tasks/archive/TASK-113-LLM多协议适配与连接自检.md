# TASK-113：LLM 多协议适配 + 连接自检（保存即可用）

> 状态：**已合并 main（`061b3da`）**
> 建议分支：`feature/task-113-llm-protocols_xwz0916`
> 交付物所有权：
> - `service/zace_service/answer.py`（多协议适配器）
> - `service/zace_service/llmprobe.py`（**新建**：模型探测与协议推断）
> - `service/zace_service/config.py`（`ANSWER_PROTOCOL`）
> - `service/zace_service/llmconfig.py`（协议进用户级解析）
> - `service/zace_service/metadb.py`（`user_llm_config.protocol` 迁移）
> - `service/zace_service/mcp.py`（provider 构造接线）
> - `service/zace_service/routers/auth.py`（测试连接端点）
> - `web/src/api/client.ts`、`web/src/pages/SettingsPage.tsx`（协议下拉 + 测试连接）
> - `service/tests/test_llm_protocols.py`（**新建**）、`service/tests/test_answer.py`、`service/tests/test_chain_and_llm_config.py`、`web/src/pages/SettingsPage.test.tsx`
> - `docs/contracts/openapi.yaml`（**L2：用户 2026-09-16 明确授权**）
> - `docs/design/INDEX.md` §3（**L3：用户 2026-09-16 明确授权**）、`docs/design/Module/04-AI总结.md` §2

## 一句话

让 `ask_project` 不再假设上游一定是 OpenAI Chat Completions：支持 **OpenAI Chat Completions / OpenAI Responses / Anthropic Messages** 三种协议，用户可在设置页选择或由"测试连接"自动推断；保存前能验证"key 有效、模型存在、协议匹配、端到端能出内容"。

## 为什么需要它（真实故障驱动）

2026-09-16 用户实测：设置页保存 `https://ai.cviauto.cn/ai/transit` + `deepseek-v4-flash` 返回 200（**保存成功**），但 3 次 `ask_project` 全部降级：

```text
HTTP Request: POST https://ai.cviauto.cn/ai/transit/v1/chat/completions "HTTP/1.1 503"
ask 降级（LLM unavailable）：AnswerUnavailableError: 总结模型返回 HTTP 503
```

同一 key 查 `/v1/models` 返回 200，并如实声明：

```json
{"id": "deepseek-v4-flash", "supported_protocols": ["ANTHROPIC", "RESPONSES"], "availability": {"status": "available"}}
```

**根因**：key 有效、模型存在，但该模型不提供 OpenAI Chat Completions；而 `answer.py` 只实现了那一条路径（搜索全仓 `protocol` 零命中）。用户侧表现为"配置保存成功、功能静默失灵"，且失败原因（协议不匹配）在设置页与历史页都不可见。

次要缺陷（同一次实测暴露）：

1. **设置页文案误导**：`接口地址` 的 hint 写"OpenAI 兼容的 /chat/completions 端点"，而实现要求的是 base URL；用户照文案填完整端点会拼出 `.../chat/completions/v1/chat/completions`。
2. **协议永久不匹配时白烧 3 次外呼**：503 落在 `RETRY_STATUS` 里，重试 2 次后才降级。
3. **上游错误不可诊断**：`AnswerUnavailableError` 只留状态码，没有（脱敏后的）上游响应摘要，历史页看不到"为什么失败"。

## 设计要点

### 1. 协议枚举与配置（D-47）

```text
PROTOCOLS = openai | responses | anthropic
   openai    → POST {base}/v1/chat/completions   （TASK-088 既有实现，默认）
   responses → POST {base}/v1/responses          （OpenAI Responses API）
   anthropic → POST {base}/v1/messages           （Anthropic Messages API）
```

- 服务端默认：`ANSWER_PROTOCOL`（未配置 → `openai`，与今天行为逐字相同）；
- 用户级：`user_llm_config.protocol`（迁移只加列，旧行留 `NULL` → 回落服务端默认）；
- 别名归一（`openai-chat` / `chat-completions` / `responses` / `messages` / `claude` 等），
  非法值**显式报错**（与 `local_mode` 的既有纪律一致，不静默取默认）。

### 2. 适配器：`complete()` 签名不变

三种协议只在"请求体构造 / 响应体抽取"上不同，其余（超时、重试、退避、脱敏、endpoint 拼接）
必须只有一份实现。因此抽出 `HttpJsonProvider` 基类：

```python
class HttpJsonProvider:
    def endpoint_for(self, base_url) -> str: ...   # 子类给端点规则
    def _build_request(self, *, system, user, max_tokens, temperature) -> (payload, headers): ...
    def _extract_text(self, response) -> str: ...
    def complete(self, *, system, user, max_tokens, temperature) -> str: ...  # 唯一的重试循环
```

`HttpAnswerProvider` 改为该基类的子类，**公开行为逐字不变**（既有测试是回归护栏）。

### 3. 端点拼接（修真实缺陷）

先剥掉用户可能误粘的已知端点后缀（`/v1/chat/completions`、`/chat/completions`、
`/v1/responses`、`/responses`、`/v1/messages`、`/messages`），再按"是否以 `/v1` 结尾"拼接。
既有三条 `test_endpoint_join_is_idempotent` 用例必须继续通过。

### 4. 测试连接（两级，零成本优先）

`POST /api/auth/llm-config/test`：

| 级 | 做什么 | 成本 |
|---|---|---|
| L1（默认） | `GET {base}/v1/models`：验证 key、模型名是否精确存在、声明支持哪些协议，并给出 `suggestedProtocol` | 零 token |
| L2（`deep=1`） | 用最小 prompt（"回复 OK"）真发一次请求，走**当前选中的协议** | 少量 token |

响应**不含 key 任何部分**；错误摘要经 `redact_text` 脱敏。

### 5. 自动推断（用户勾选）

`detect_protocols(supported_protocols, model)` 归一上游声明 → `suggestedProtocol`：
优先级 `openai > responses > anthropic`（既有实现优先，减少行为变化）；
上游声明与当前选择不一致时响应给 `protocolMismatch: true`，设置页据此提示"该模型不支持你选的协议"。

## 冻结接口（本卡不得变更）

- `AnswerProvider.complete(*, system, user, max_tokens, temperature) -> str`（CF-09）**签名不变**；
- `ContextPack` / `Engine.search` / `ask_project` 返回形态不变；
- `GET /api/meta` 的 `config.llm` **只增字段**（`protocol` / `supportedProtocols`），不改既有字段语义。

## 验收标准（DoD）

- [ ] `service/tests/test_llm_protocols.py`（新建）全绿，覆盖：
  - 三种协议各自的**请求体形状**（`messages` / `instructions+input` / `system+messages`）与**响应抽取**；
  - Anthropic 的 `x-api-key` + `anthropic-version` 头、`max_tokens` 必填；
  - Responses 的 `output[].content[].text` 抽取，以及"只有 reasoning 无 content" → `AnswerResponseError`；
  - 端点拼接：三类 base（无 `/v1`、带 `/v1`、误粘完整端点）都得到正确端点；
  - 协议别名归一 + 非法协议显式报错；
  - 未知协议 / 未配置 → 不抛异常，回落或降级。
- [ ] 测试连接端点：`/v1/models` 命中模型时回 `modelFound=true` 与 `supportedProtocols`；
      模型不存在时回 `modelFound=false` 但**不报 500**；key 无效时 `ok=false` + 可操作文案；
      **key 不出现在响应任何字段**（逐字节断言）。
- [ ] 端到端（真实网关，手动）：设置页选 `responses` + `deepseek-v4-flash` → 测试连接 L2 通过 →
      `ask_project` 返回 `status=answered`。
- [ ] 基线三条全绿：`uv run ruff check .`、`uv run python scripts/check_dependency_direction.py`、
      `uv run pytest -o addopts="" -q`；前端 `npm run lint && npm test && npm run build`。
- [ ] 任务卡"执行记录"已回填；任务板状态改为 `review`。

## 明确不做

- 不做协议自动重试切换（失败后自动换协议重发）——一次 ask 只走一条协议，避免"两次计费 + 答案不可归因"；
- 不做流式输出（Module/04 §10 既有结论）；
- 不做 `/v1/models` 结果缓存（探测是显式用户动作）；
- 不实现 Gemini/Bedrock 等其它协议（只做本次实测需要的三种）。

## 参考源码锚点

- `service/zace_service/answer.py`（既有 OpenAI 实现 + 重试/脱敏纪律）
- `service/zace_service/llmconfig.py`（用户级配置解析与缓存键）
- `service/zace_service/routers/auth.py` `put_llm_config` / `_validate_llm_config`（保存端点的既有口径）
- `docs/design/Module/04-AI总结.md` §2（AnswerProvider 接口与配置）

## 执行记录

### 2026-09-16（branch `feature/task-113-llm-protocols_xwz0916`，lane-b）

#### 关键决策

1. **协议层独立成模块** `zace_service/llmprotocol.py`（纯函数、只有 stdlib 依赖，不 import `answer`
   以避免循环）：三种协议的"请求体形状 / 响应抽取 / 端点拼接"集中在一处，可**不发 HTTP** 穷举测试。
2. **抽出 `HttpJsonProvider` 基类**：超时/重试/退避/脱敏/注册 secret 只有一份实现，
   协议差异只在 `build_request` 与 `extract_text` 两处。`HttpAnswerProvider` 成为 `openai` 特例，
   **公开行为逐字不变**（`test_answer.py` 的 71 个用例是回归护栏）。
3. **默认协议 `openai`**：未配 `ANSWER_PROTOCOL` / 未选协议时，行为与升级前完全一致——
   升级不改变任何既有部署的行为。
4. **错误可见性优先于兼容性**：非法协议在三个入口都显式报错（启动 `Settings.from_env`、
   保存 400 `invalid_llm_protocol`、构造 provider），**不静默回落**。
5. **不自动换协议重发**（卡内"明确不做"）：一次 ask 只走一条协议，避免两次计费与答案不可归因。

#### 实现过程中发现的真实缺陷（本卡顺带修掉）

**推理类模型的 `max_tokens` 是总预算**：L2 自检最初用 `PROBE_MAX_TOKENS = 16`，实测该网关的
`deepseek/deepseek-v4.1-flash` 回 HTTP 200 + `content` 为空 + `finish_reason=length` +
`usage.completion_tokens_details.reasoning_tokens=16`——16 个 token 全被推理过程吃光。
即"自检报了一个与配置无关的假失败"。已改为 512 并加测试钉住下限，同时在空回答失败时
补一条本地成因提示（指向 `ANSWER_MAX_TOKENS`）。

#### 验收命令与结果

```bash
uv run ruff check .                                  # All checks passed!
uv run python scripts/check_dependency_direction.py   # 依赖方向检查通过
uv run pytest -o addopts="" -q                         # 1243 passed, 9 skipped（144s）
cd web && npm run lint                                # 0 warnings
cd web && npx vitest run                              # 108 passed, 3 skipped
cd web && npm run build                               # ✓ built in 3.12s
```

**真实网关端到端验证**（本次故障的原始场景，`https://ai.cviauto.cn/ai/transit` +
`deepseek-v4-flash`）：

| 协议 | L1 | L2（真实请求） |
|---|---|---|
| `openai` | `ok=false`，`protocolMismatch=true`，`suggested=responses` | — |
| `responses` | `ok=true` | `ok=true`，回显"可用" |
| `anthropic` | `ok=true` | `ok=true`，回显"可用" |

即：**原故障（保存成功但 ask 持续 503）现在会被 L1 直接指出根因并给出建议协议**，
而切到 `responses` 后真实请求成功。另对服务端默认网关
（`http://154.12.34.214:8080/v1` + `deepseek/deepseek-v4.1-flash`）验证 L1/L2 均 `ok=true`。

全程断言 key 不出现在结果任何字段（`key leaked: False`）。

#### 与设计的偏差

| # | 偏差 | 理由与处置 |
|---|---|---|
| 1 | 新增文件 `llmprotocol.py` / `llmprobe.py`（卡内交付物清单未列，清单写的是"协议实现放 answer.py"） | 单一职责：协议形状可零成本穷举测试，探测逻辑与 provider 生命周期无关。已在清单补登 |
| 2 | `ANSWER_MAX_TOKENS` 默认值未改（3072） | 3072 对 reasoning 模型偏紧，但改默认会影响既有部署的输出长度与成本；本卡只在自检里给提示，改默认值需单独评估 |
| 3 | `openapi.yaml` / `INDEX.md` / `Module/04` 由本卡直接修改 | 用户 2026-09-16 明确授权（AGENTS.md §2 的 L2/L3 流程：默认禁止实施 AI 改这两类文件） |

#### 未决问题

1. **`meta.llmProtocol` 只在 answered 分支写入**：降级路径（insufficient/degraded）看不到
   实际协议。用户诊断"为什么 503"时仍需看服务端日志。是否要进 `query_audit` 表需单独评估
   （加列属表结构扩展）。
2. **协议不匹配时的重试仍会发 3 次**：503 在 `RETRY_STATUS` 里，协议级永久错误与
   上游过载走同一条重试路径。若把"协议不被支持"单独识别可省 2 次外呼，但需要区分 503 的成因
   （上游不回结构化错误码），本卡未做。
3. **未做 `/v1/models` 结果缓存**：自检是显式动作，当前每次真发请求（15s 超时）。
4. **未验证 `responses` 协议在真实 ask 全链路**：自检（L2）已证明出内容，但
   `ask_project` 的完整链路（grounded prompt + citation 回验）未用该协议跑过真实问题——
   需用户在设置页切到 `responses` 后实测确认。
