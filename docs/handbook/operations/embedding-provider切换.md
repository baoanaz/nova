# embedding provider 切换手册（TASK-049）

> 适用版本：TASK-049 合并后（`main` @ `31baf91` 起）。
> 配套文档：`docs/handbook/getting-started/cloud-embedding.md`（TASK-046，**接入**与排障）；
> 本手册只管**切换**——换厂商、换模型、调批/并发参数、额度用完后怎么办。
> 本手册所有命令与输出均在 WSL2 本机实测过（2026-09-13）。

## 0. 一句话

**换模型 = 改两个环境变量**（`EMBED_MODEL` + `EMBED_BASE_URL`），批大小/并发/上下文上限会按模型自动适配；
需要微调时用 env 显式覆盖（**三级回落：env > 模型 > 厂商 > 全局默认**）。

## 1. 当前配置（2026-09-13：Voyage 为主）

```bash
# .env（本文件不入库；.env.example 是模板）
EMBED_MODE=api
EMBED_MODEL=voyage-4-lite
EMBED_BASE_URL=https://api.voyageai.com
EMBED_API_KEY=pa-xxxx          # Voyage key
NO_PROXY=127.0.0.1,localhost   # WSL 上设了 http_proxy 时必加
```

生效参数（实测输出）：

```text
voyage-4-lite    transport=voyage       batch= 500 budget= 300000 conc=8   maxTok=32000
```

## 2. 已登记的 provider 与模型（`registry.py`）

| provider | base_url | 已登记模型 | 条数上限 | 安全批 | 并发推荐 | ctx |
|---|---|---|---|---|---|---|
| `voyage` | `https://api.voyageai.com` | `voyage-4-lite`、`voyage-code-4` | 1000 | **500** | **8** | 32000 |
| `siliconflow` | `https://api.siliconflow.cn` | `bge-m3`（免费）、`bge-m3-pro`（付费） | 800 | **256** | **1** | 8192 |
| `openai` | `https://api.openai.com` | `text-embedding-3-small/large` | 2048 | 512 | 1 | 8191 |
| `generic` | （用户自填） | 未登记模型（需 `EMBED_DIM`） | 256 | 128 | 1 | 2048 |

**别名**（写法不同、指向同一模型，**不会触发重嵌**）：

| 别名 | 实际条目 |
|---|---|
| `BAAI/bge-m3` | `bge-m3` |
| `Pro/BAAI/bge-m3` | `bge-m3-pro` |

> ⚠️ `bge-m3` 与 `bge-m3-pro` 虽然实测**返回同一模型**（余弦 0.9999+），但**指纹不同**
> （`api:bge-m3` vs `api:bge-m3-pro`）→ 两档之间切换会触发 **D-07 全量重嵌**。
> 若经常切换，建议固定用其中一档。

## 3. 切换场景

### 3.1 额度用完 → 切回硅基流动免费档（最常用）

```bash
# 改 .env 两行即可
EMBED_MODEL=bge-m3
EMBED_BASE_URL=https://api.siliconflow.cn
EMBED_API_KEY=sk-xxxx          # 硅基流动 key

# 重新索引（必须：模型变了 → profile 变 → D-07 触发 reembed）
set -a; source .env; set +a
uv run nova-core ingest --repo <你的仓库> --data <数据根>
```

**预期行为**：`reembed` 档位——AST/符号/边**原样保留**，只重算向量（不重新解析）。
实测代价（hello-agents 9389 chunks）：voyage 43.6s → 硅基流动串行约 230s。

### 3.2 Voyage 内部换档（不用重嵌）

`voyage-4-lite` / `voyage-4` / `voyage-4-large` / `voyage-code-4` **共享同一向量空间**
（官方文档 + 实测确认），因此**理论上可混用**：用 lite 建索引、用 large 做查询。

> ⚠️ **当前实现仍未支持**：nova 的 `model_id` 会随模型名变化 → 切换仍触发重嵌。
> 若确实需要混用（例如查询用更强模型），需要一张新卡让 `model_id` 可显式声明为
> "共享空间标识"。**在实现前，换档位 = 全量重嵌。**

### 3.3 换到未登记的新厂商

```bash
EMBED_MODEL=my-model
EMBED_DIM=1024                 # 必填！否则报错（保护 D-07 指纹）
EMBED_BASE_URL=https://my-llm.internal
EMBED_PROVIDER=generic         # 可选；不填则按 base_url 推断
EMBED_BATCH_SIZE=128           # 建议显式给保守值
EMBED_CONCURRENCY=1            # 先用串行验证，再考虑并发
```

未登记厂商回落 `generic` 的**保守默认**（批 128 / token 64K / 并发 1）。

## 4. 参数含义与调优

| env | 含义 | 默认 | 何时调 |
|---|---|---|---|
| `EMBED_BATCH_SIZE` | 单请求**条数** | 按厂商安全值 | 加大可提速，但受响应体量限制（见下） |
| `EMBED_BATCH_TOKEN_BUDGET` | 单请求累计 **token** | 按厂商 | 与上者取先到者 |
| `EMBED_CONCURRENCY` | **并发批数** | 模型/厂商推荐（Voyage 8 / 硅基流动 1） | 提速主要靠它 |
| `EMBED_MAX_INPUT_TOKENS` | 单条截断上限 | 模型上限 | 一般不用改 |
| `EMBED_PROVIDER` | 显式指定厂商 | 自动推断 | 自建服务时 |

### 实测依据（本机，hello-agents 9389 chunks）

| 配置 | 耗时 |
|---|---|
| 硅基流动 bge-m3（串行） | 230.9s |
| voyage-4-lite（串行） | 80.2s |
| **voyage-4-lite（并发 8）** | **43.6s** |

**两条硬约束**（实测得出，别踩）：

1. **批不能太大**：dim=1024 下 1000 条 → 12.7 MB 响应 → 对端**传输中途断连**
   （`RemoteProtocolError: peer closed connection without sending complete message body`）。
   安全线是 **500 条**（约 6.6 MB）。
2. **并发的真正瓶颈是本机带宽**：8 路已饱和（~2.2 M tok/min），再往上无收益。
   官方 TPM 1600 万在本机**用不满**——降维测试（`output_dimension=256`）吞吐翻倍到 4.74 M，
   证明瓶颈在响应体传输而非配额。

### 耗时模型与设备绑定基准（TASK-102，2026-09-15）

> **耗时 ≈ chunk 数 × 21 ms**（1024 维 @ 本机 ~1 MB/s）。主导项是**下载响应体**
> （21.3 KB/chunk），不是模型算力、也不是 TPM 配额。完整数据（含量化公式、三靶场基准、
> 并发安全边界）见 `benches/results/index-cost-model-company-wsl.md`。

两条与本节旧表述不同的实测结论：

| 旧表述 | 实测（company-wsl，2026-09-15） |
|---|---|
| "加大 batch 可提速" | **无变化**：batch 256/512/768 → 54.9s / 55.6s / 54.6s（同一 2000 chunk 样本） |
| （未提）`max_input_tokens` 调小 | **调小无收益**：2048 与 8192 耗时相当（111.6s vs 109.4s），响应体逐字节相同 |

**并发安全边界（免费 bge-m3）**：`concurrency=1` 是唯一可保证成功的档位——
实测 langchain（20.7K chunks）在 `conc=3` 下 **429 整次失败**（无部分成功，vectors 全丢）；
小/中仓库 conc=3 可提速约 1.85×。

## 5. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `未登记的 API 模型 ... 必须显式提供 dim` | 模型没登记且没给 `EMBED_DIM` | 加 `EMBED_DIM`（保护指纹） |
| `batch_size=... 超过 voyage 的条数上限` | 批参数超过厂商上限 | 降到安全值（Voyage 500） |
| `RemoteProtocolError`（传输中断） | 批太大，响应体过大 | 减小 `EMBED_BATCH_SIZE` |
| `429 TPM limit reached` | 限流 | 降并发/降批（硅基流动免费档建议并发 1） |
| 切换后检索结果没变 | 没重新索引（指纹未变？） | 确认 `EMBED_MODEL` 真的改了；跑一次 ingest |
| 切换后大量重嵌 | 正常：模型指纹变了 | 属预期（D-07 二级失效） |

## 6. 隐私提醒

云端 embedding 会把**源码文本发往第三方**（Voyage / 硅基流动）。
若需"源码不出本机"，走本地 ONNX 路线（当前暂缓，见 `docs/plan/phase2-m2b-w6.md` §5）。

## 7. 未来任务锚点

| 需求 | 要做什么 |
|---|---|
| Voyage 系列混用（lite 建索引 + large 查询） | 让 `model_id` 可显式声明为共享空间标识（新卡） |
| 真正的额度用完自动降级 | provider 故障切换卡（当前需手动改 env） |
| 本地 ONNX 兜底 | TASK-038（钳制）+ 完善本地配置 |
| 用满官方 TPM | 换更快的网络链路（本机带宽是瓶颈） |
