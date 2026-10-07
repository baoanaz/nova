# 云端 embedding 接入手册（硅基流动 bge-m3）

> 适用版本：M2b / W6（TASK-046：F1 模型名可用、F2 上限对齐、F4 截断与分批）
> 本手册的命令与输出**除标注「引用编排者」外，均由实施 AI 在本机实测**（WSL2 Ubuntu / Python 3.12 / 测试日期见文末）。
> 数字（token 数、耗时）会因机器与网络不同而变化。

一句话目标：

> **用硅基流动的 `BAAI/bge-m3` 跑通 `ingest` + `search`，且源码文本会发往第三方（自觉选择，见 §5）。**

---

## 0. 为什么需要本手册

照硅基流动官方文档配置 nova，**默认跑不通**，且失败原因不明显：

| 现象 | 真实输出 |
|---|---|
| 用 registry 里登记的裸名 | API 直接拒绝：`{"code":20012,"message":"Model does not exist..."}` |
| 照官方文档填全名（旧版 nova） | registry 拒绝：`EmbeddingConfigError: 未登记的 API 模型 'BAAI/bge-m3'` |
| 旧版唯一可行路径 | 同时给 `EMBED_MODEL=BAAI/bge-m3` **且** `EMBED_DIM=1024` —— 这条路不在任何文档里 |

TASK-046 修掉后：

- `EMBED_MODEL=BAAI/bge-m3`（**不需要** `EMBED_DIM`）开箱可用；
- 旧的 `EMBED_MODEL=bge-m3` **仍然可用**（别名解析），两者指纹相同、互不触发重嵌；
- 上限默认取模型实际能力（8192），并新增按 token 的截断与分批（长文档不再失败）。

---

## 1. key 怎么拿（先解决这一条）

### 1.1 问题：`.bashrc` 里的 key，子进程拿不到

key 写在 `~/.bashrc`，而文件开头有非交互守卫：

```bash
case $- in
    *i*) ;;
      *) return;;
esac
```

**只有交互式终端才会执行到那一行之后**。实测（本机复现）：

```console
$ bash -c  'echo "${nova_embeding_API_KEY:-NO}"'   # 子进程 / 脚本 / CI 的真实处境
NO
$ bash -lc 'echo "${nova_embeding_API_KEY:-NO}"'   # 登录 shell 同样拿不到
NO
```

这就是"子 AI 按手册跑命令得到 401、并误判为 key 无效"的根因。

### 1.2 推荐做法：项目根 `.env` + `set -a`

本仓库已提供 `.env.example`，且 `.gitignore` 第 30–32 行已排除 `.env` / `.env.*`（只放行 `.env.example`）：

```
.env
.env.*
!.env.example
```

**为什么推荐它**：不依赖 shell 类型、对任何子进程可见、key 不进仓库、一条 `source` 对所有命令生效。
**为什么不推荐 `.bashrc`**：非交互进程拿不到（见 §1.1），且把 key 与项目绑定关系藏在 shell 配置里，换机器就丢。

```console
$ cd /path/to/nova-workspace
$ cp .env.example .env
$ # 把真实 key 填进 .env（或从 ~/.bashrc 提取，见下）
$ set -a; source .env; set +a
```

`.env` 里的 `EMBED_API_KEY` 可以从 `~/.bashrc` 提取（**该命令只出现变量名，key 本体不落任何文件**）：

```console
$ export EMBED_API_KEY=$(sed -n 's/^export nova_embeding_API_KEY=//p' ~/.bashrc | tr -d '"')
```

> 备选做法：每条命令前显式 `export`（适合一次性调试），或让 service 读进程环境（service 读的是**进程环境**，
> 因此也必须先 `export`/`source`，它不会自己去读 `.bashrc`）。

### 1.3 本机网络：`NO_PROXY` 必加

本机设了 `http_proxy`/`https_proxy`（`env | grep -i proxy` 可见）。这会**拦截对本机 127.0.0.1 的请求**，
让 service 连不上自己。跑 service 时必须：

```console
$ export NO_PROXY=127.0.0.1,localhost
```

（对 `api.siliconflow.cn` 的出站请求不受影响，继续走代理即可。）

---

## 2. 从零到跑通：完整命令序列

```console
# --- 0) 依赖 ---
$ cd /path/to/nova-workspace
$ uv sync --all-packages --all-extras

# --- 1) key（不写进仓库）---
$ export EMBED_API_KEY=$(sed -n 's/^export nova_embeding_API_KEY=//p' ~/.bashrc | tr -d '"')

# --- 2) 云端 embedding 配置 ---
$ export EMBED_MODE=api
$ export EMBED_MODEL=BAAI/bge-m3          # 官方全名；不需要 EMBED_DIM
$ export EMBED_BASE_URL=https://api.siliconflow.cn
$ export NO_PROXY=127.0.0.1,localhost     # 本机有代理时必须

# --- 3) 先验证配置与连通性（不打索引，秒级）---
$ uv run python - <<'PY'
from nova_core.embedding.factory import create_provider
p = create_provider()          # 读环境变量
print(p.profile)
PY
$ uv run nova-core status --repo /home/xuwenzheng/github/hello-agents --data /tmp/nova-ha   # 索引现状（首次可跳过）

# --- 4) 索引一个仓库（首次全量；长文档仓库需要几分钟）---
$ uv run nova-core ingest --repo /home/xuwenzheng/github/hello-agents --data /tmp/nova-ha

# --- 5) 检索验证 ---
$ uv run nova-core search "ReAct 范式是怎么实现的？" \
    --repo /home/xuwenzheng/github/hello-agents --data /tmp/nova-ha
```

### 2.1 我实测的输出

第 3 步（profile）：

```console
$ uv run python -c "from nova_core.embedding.factory import create_provider; print(create_provider().profile)"
EmbeddingProfile(model_id='api:bge-m3', dim=1024, max_input_tokens=8192)
```

> **`api:bge-m3` 而不是 `api:BAAI/bge-m3`**：`model_id` 用注册表 key（稳定标识），
> 别名只影响**发给 API 的 `model` 字段**。这样换写法不会触发无谓重嵌（见 §4.2）。

第 4 步（`hello-agents` 全量首次索引，本机实测）：

```console
project: e9ee9dd1d41a7d2c (created)
repo: /home/xuwenzheng/github/hello-agents
identity: git remote https://github.com/datawhalechina/hello-agents.git (path .)
mode: incremental (invalidation=none)
files: added=1482 modified=0 deleted=0 parsed=1482
chunks: new=9971 reused=0 removed=0
vectors: upserted=9971 deleted=0
graph: edges_retargeted=3768 unresolved_resolved=69 spec_refs=20358 ambiguous=4081
skipped: 380 个二进制/不可解码文件
elapsed: 290.2s
```

第 5 步（`search`）：无 `warning: 向量索引为空`，命中项带 `vector 0.6316 + vector rank 13` 证据 —— 说明
**向量通道真的生效**（修复前该仓库 `vectors=0`，检索降级为仅 BM25）。

---

## 3. 实测数字（区分来源）

### 3.1 实施 AI 本机实测（2026-09-13）

| 探测 | 结果 |
|---|---|
| 裸名 `bge-m3` | `{"code":20012,"message":"Model does not exist..."}` |
| 全名 `BAAI/bge-m3` | `200 OK`，dim=1024，0.22s |
| 单条输入 token 上限 | **8192 token 成功**（0.37s）/ **8193 token → `400 code=20015`** |
| 2000 词真实文档 | OK 0.30s；4000 词 → `400 code=20015` |
| 全库嵌入（hello-agents，按 8192 截断） | 792 批 / 约 4.7M token，全量 290.2s |
| 前缀重复输入（`'word ' * N`） | 800 词以上会出现**挂起/500** —— 探针假象，与真实文本行为不同，勿据此判断上限 |

> **口径校正**：卡内 §2.4 记「8000 token 输入 0.27s 成功」与「默认 2048 导致长文档静默截断」。
> 本机复测确认 **8192 是真实可用上限**（8192 OK / 8193 400），因此 §B 把默认值对齐到 8192 是对的；
> 但"2048 会静默截断"在修复后已不成立（截断真实发生且按 8192）。

### 3.2 引用编排者的数字（未由实施 AI 独立复现）

| 项 | 数值 | 来源 |
|---|---|---|
| 免费档 bge-m3 速率 | 2000 RPM / 500,000 TPM | 硅基流动公开资料（见 §4.3） |
| 付费档价格 | ¥0.07 / M tokens 量级 | 卡内 §2.4 |
| `hello-agents` chunk token 分布 | 卡内口径是 `content`：p50=147 / p90=1052 / p99=12978 / max=64138；**实施 AI 复测 `embedding_text()`（真正送嵌入的文本）：p50=162 / p90=1080 / p99=3814 / max=4882** | 卡内 §D；实施 AI 复测见 §3.1 |
| 超长 chunk 数 | 卡内口径 182 / 9971 个 >8192 token（**按 `content` 计**）；按 `embedding_text()` 计 **0 个**（受字符级 `EMBEDDING_BODY_MAX_CHARS=8000` 截断） | 卡内 §D；§4.4 |

---

## 4. 行为说明（避免踩坑）

### 4.1 接受哪些写法

| `EMBED_MODEL` | 结果 |
|---|---|
| `BAAI/bge-m3` | 开箱可用，无需 `EMBED_DIM`（**推荐**） |
| `bge-m3` | 仍可用（别名解析到同一条目） |
| 其它未登记值 | 必须显式给 `EMBED_DIM`，否则报错（D-07 指纹保护，`dim` 猜错会让失效判定失真） |

### 4.2 上限与指纹

- 不给 `EMBED_MAX_INPUT_TOKENS` → 用**模型登记能力**（bge-m3 = 8192）；
- 给更小值 → 尊重（`min` 语义，不被抬高）；
- 给**大于**模型能力的值 → 钳回 8192 并发 `UserWarning`；
- 指纹（`index_config`）用的是**生效值**，所以"配置不同但生效值相同"不会触发无谓重嵌。

### 4.3 成本与限流

- BAAI 系列在硅基流动 L0 免费档可用（bge-m3：2000 RPM / 500,000 TPM）；
- 付费档为 ¥0.07/M tokens 量级，10 人日常用量远低于"月 10 元"预算；
- 429 处理：`api.py` 已有**指数退避 + `Retry-After`**（上限 30s），最多重试 2 次，最终抛 `ApiRateLimitError`（不会无限重试）；
- **本机实测到的注意点**：多个进程/多个泳道**共用同一个 key** 时会互相抢配额，表现为远低于 500K TPM 就 429。
  排查顺序：① 确认没有别的 `nova-core ingest` 在跑；② 等一个配额窗口（约 1 分钟）再试；③ 缩小 `EMBED_BATCH_SIZE`。

### 4.4 分批与截断（为什么长仓库现在能跑完）

- 每个输入在**发送前**按 token 截到 `max_input_tokens`（拿不到 tokenizer 时按 UTF-8 字节数保守截断）；
- 批次切分依据是**累计 token 预算**（默认 8192）与条数上限（默认 64）两者取先到者；
- 单条超预算的输入仍单独成批（不拆、不丢）。

**分层截断的口径（重要）**：真正送嵌入的是 `embedding_text()` = signature + docstring + 正文，
而正文先受 `EMBEDDING_BODY_MAX_CHARS = 8000`（**字符级**）截断，之后才轮到本卡的 token 级截断。
实测 `hello-agents`：`embedding_text()` 的 token 上界是 **4882**（最大），尚未达到 8192。
也就是说：**在 bge-m3（8192）下，实际生效的截断层是字符上界 8000，不是模型能力**。
换上限更小的模型，或直接调 provider 的调用方，就会轮到 token 级截断生效。

---

## 5. 隐私告知（重要）

与本地 ONNX 路线（`local.py`）相反：

> **云端 embedding 会把源码文本发送到第三方服务（硅基流动）。**

这是当前开发期的自觉选择（免费、1024D、8192 token）。若你要求"源码不出本机"，
需要切回本地路线（`EMBED_MODE=local`，当前暂缓完善），或自建 OpenAI-compatible 服务
（`EMBED_BASE_URL` 指向内网地址即可，无需改代码）。

---

## 6. 排查表（下面每条都是真实输出）

### 6.1 `Model does not exist`

```
{"code":20012,"message":"Model does not exist. Please check it carefully.","data":null}
```

原因：`model` 字段发了裸名。修复后 nova 会自动替换为 `BAAI/bge-m3`；
若你手工调 API，请直接用全名。

### 6.2 `未登记的 API 模型 ... 必须显式提供 dim`

```
EmbeddingConfigError: 未登记的 API 模型 'xxx'：必须显式提供 dim（EMBED_DIM）...
已登记模型：['bge-m3', 'text-embedding-3-large', 'text-embedding-3-small']；
已登记别名：['BAAI/bge-m3']
```

原因：填了一个 nova 不认识的模型名。要么改成 `BAAI/bge-m3` / `bge-m3`，
要么**明确**给出 `EMBED_DIM`（例如自建服务）。

### 6.3 `The parameter is invalid`（400 code=20015）

```
{"code":20015,"message":"The parameter is invalid. Please check again.","data":null}
```

原因：某条输入超过 8192 token。修复后 nova 会截断；若仍出现，说明你在手工调 API，
或 `EMBED_MAX_INPUT_TOKENS` 被设成了模型不支持的值。

### 6.4 `Request was rejected due to rate limiting ... TPM limit reached`

```
ApiRateLimitError: embedding 被限流（HTTP 429，...，已尝试 3 次）；
响应体片段：{"message":"Request was rejected due to rate limiting. Details: TPM limit reached.","data":null}
```

原因：配额被吃满。先查是否有别的进程共用同一 key（见 §4.3），再考虑缩小批次。

### 6.5 连不上 `127.0.0.1`（跑 service 时）

原因：本机代理拦截了本机回环请求。加 `export NO_PROXY=127.0.0.1,localhost`。

---

## 7. 一键自检

```console
$ set -a; source .env; set +a
$ uv run python - <<'PY'
import os
from nova_core.embedding.factory import create_provider
print("EMBED_MODE   =", os.environ.get("EMBED_MODE"))
print("EMBED_MODEL  =", os.environ.get("EMBED_MODEL"))
print("key 已注入   =", bool(os.environ.get("EMBED_API_KEY")))   # 只打印布尔值，绝不打印 key
print(create_provider().profile)
PY
```

期望：`key 已注入 = True` 且 `EmbeddingProfile(model_id='api:bge-m3', dim=1024, max_input_tokens=8192)`。
