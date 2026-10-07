# nova M2a 验收手册（本地单用户：编辑器里用真实问题验证）

> 适用版本：M2a-2（TASK-034 本地单用户模式 + TASK-040 service 侧 MCP 端点）
> 手册中**每一条命令与输出都在本机真实跑过**（WSL2 Ubuntu / Python 3.12 / `mcp==2.2.0`）。
> 你照着敲，应当看到同样的形态；数字（文件数、耗时、命中证据）会因仓库不同而不同。

M2a 的验收目标只有一句话：

> **在编辑器里问一个真实问题，拿到带「文件:行号」证据的上下文。**

下面从零走到这一步；第 6 节是踩坑时的排查表。

---

## 0. 前提

| 项 | 要求 | 说明 |
|---|---|---|
| 平台 | **WSL2 / Linux / macOS**（本手册实测于 WSL2 Ubuntu） | Windows 原生路径（`C:\...`）在服务端不存在：`project_root` 必须是正斜杠绝对路径，否则工具会明确告诉你（见 §6.3） |
| Python | ≥ 3.12 | 由 `uv` 管；不需要手动装 |
| 依赖 | `uv sync --all-packages --all-extras` | M2a 新增的唯一依赖是 `service/pyproject.toml` 里的 `mcp>=2.2` |
| 模型 | 首次运行会从 HuggingFace 下载 `multilingual-e5-small`（约 470MB） | 缓存到 `~/.cache/huggingface`；离线环境见 §6.2 |
| 磁盘 | 数据根目录留足空间 | 索引体积随仓库大小增长（本手册示例：451 文件的仓库约 40–60MB；1382 文件的 C++ 仓库约 20MB DB + 10MB 向量） |

```console
$ uv sync --all-packages --all-extras
...
 + mcp==2.2.0
 + sse-starlette==3.4.11
 + python-multipart==0.0.32
 + truststore==0.10.4
 + pyjwt==2.13.0
```

---

## 1. 一条命令起服务

```console
$ uv run nova-service local --repo /home/xuwenzheng/nova-scratch/demo-repo \
      --data-root ~/nova-scratch/data-demo --port 8792
nova-service local 已启动（127.0.0.1:8792）
  projectId : e6fe81dbaebfb65d
  dataRoot  : /home/xuwenzheng/nova-scratch/data-demo
  repo      : /home/xuwenzheng/nova-scratch/demo-repo
  身份      : 非 git 仓库 → 绝对路径 hash（D-29）；换路径/换机器 projectId 会变
  索引      : 后台进行中（state=running，已处理 0/0 个文件）
             进度：GET http://127.0.0.1:8792/api/projects/e6fe81dbaebfb65d ｜ 服务现在已可响应，不必等索引完成
  检索接口  : POST http://127.0.0.1:8792/api/query/search
  MCP       : http://127.0.0.1:8792/mcp（Streamable HTTP）
  懒重扫    : 每 2s 一次（0=禁用，NOVA_LOCAL_RESCAN_INTERVAL 可改）

nova MCP 端点：http://127.0.0.1:8792/mcp（Streamable HTTP）

Cursor（项目内 .cursor/mcp.json 或全局 ~/.cursor/mcp.json）：
{ "mcpServers": { "nova": { "url": "http://127.0.0.1:8792/mcp" } } }

Claude Code / 其它支持 HTTP 传输的 harness：
{ "mcpServers": { "nova": { "url": "http://127.0.0.1:8792/mcp" } } }
（URL = http://127.0.0.1:8792/mcp；若编辑器要求 JSON 文件，直接写上面的对象即可）
只支持 stdio 的 harness 需要一个 stdio 代理（在本地把 stdio 转发到本 URL）——归 M2c 的 Rust client（TASK-040R），当前版本未提供。
```

要点：

- **`--repo` 必须是绝对路径**，且是**目录**（不是文件）。不存在 → 启动失败并给出可读错误。
- **`--data-root` 建议显式指定**（默认 `~/.nova`）：不同仓库用不同数据根，删除/重建互不影响。
- **`身份` 一行很重要**：带 git remote 的仓库走 D-29 的 `remoteUrl + 相对路径` 身份（换机器同
  projectId，索引可复用）；非 git 目录退化为**绝对路径 hash**（换路径/换机器 projectId 会变）。
- **服务立刻开始监听**，不等索引完成。`Ctrl+C` 停止（前台运行）。
- 只想要配置片段、不起服务：`uv run nova-service mcp-config --port 8787`。
- 纯服务模式（不绑仓库、走客户端上传）：`uv run nova-service serve`。

---

## 2. 等索引（进度怎么看，以及为什么不显示百分比）

查询进度：

```console
$ curl -s http://127.0.0.1:8792/api/projects/e6fe81dbaebfb65d | python3 -m json.tool
{
    "projectId": "e6fe81dbaebfb65d",
    "displayName": "demo-repo",
    "createdAt": 1789089930,
    "attachedRoot": "/home/xuwenzheng/nova-scratch/demo-repo",
    "indexProgress": {
        "state": "done",
        "startedAt": 1789089930,
        "finishedAt": 1789089946,
        "processedFiles": 2,
        "totalFiles": 2,
        "error": null
    },
    "sync": { "filesIndexed": 2, "chunks": 7, "symbols": 4, "edges": 1, ... },
    ...
}
```

同一个项目**第二次起服务**（索引数据根已存在、仓库无改动）时：

```json
"indexProgress": { "state": "done", "startedAt": 1789093918, "finishedAt": 1789093918,
                   "processedFiles": 0, "totalFiles": 2, "error": null }
```

**`processedFiles: 0` 不是"没索引"**——增量扫描发现没有任何变化，因此没有文件被重新解析，
库里仍有用上一次的 7 个 chunk（`sync.filesIndexed=2`）。这也是为什么不能用这两个数算百分比（§2）。

`indexProgress` 六个字段的口径（**这是设计上的诚实性要求，不是实现细节**）：

| 字段 | 含义 | 陷阱 |
|---|---|---|
| `state` | `idle` / `running` / `done` / `failed` | 服务重启后未重新 attach 的项目回到 `idle` |
| `startedAt` / `finishedAt` | Unix 秒 | `running` 时 `finishedAt` 为 `null` |
| `totalFiles` | 目录列举出的**文件总数**（只 walk，不读内容） | 含二进制/构建产物等解析层会跳过的文件 |
| `processedFiles` | **本次真正解析成 chunk 的文件数**（`IngestReport.files_parsed`） | 增量重扫时只数变化文件；**无改动时为 0**（正常）；与 `totalFiles` **不是同一量纲** |
| `error` | 摘要（脱敏） | **`state="done"` 时也可能非空**＝"索引完成，但这些文件有解析问题"（如 C/C++ 语法错误）；`state="failed"` 时才是致命失败 |

**为什么没有百分比**：core 的 `ingest_repo` 是全同步、无回调的，中间态拿不到真实完成度。
服务选择"只说状态与已处理文件数"，而**不伪造**一个会骗人的进度条（宁可信息少，不可信息假）。
索引期间 `processedFiles` 长时间为 `0` 是**正常**的——解析写库在开始后很快完成，剩余时间全在
embedding 与向量写入，而那一段没有回调点。

真实观感（451 文件的 Python 仓库，索引期间每 90 秒采样）：

```text
10:24:23 running 0/451
10:33:59 running 0/451
10:44:30 running 0/451
...
```

**索引期间服务已经可用**：`GET /healthz` 始终 200 且毫秒级；`GET /api/projects/{id}` 也正常
（实测 1382 文件的仓库索引期间 25 次轮询：全部 200，耗时 3.0–79.7ms）。
只是**检索会如实告诉你"还没就绪"**而不是给你半个结果（§6.1 有真实报错文本）。

`/healthz` 也会带上进度（纯内存读，不因此变慢）：

```console
$ curl -s http://127.0.0.1:8792/healthz | python3 -m json.tool
{
    "status": "ok", "version": "0.0.1",
    "dataRoot": "/home/xuwenzheng/nova-scratch/data-demo",
    "localMode": true, "auth": "disabled(local)",
    "core": { "importable": true },
    "projects": [ { "projectId": "e6fe81dbaebfb65d",
                    "attachedRoot": "/home/xuwenzheng/nova-scratch/demo-repo",
                    "indexProgress": { "state": "done", "processedFiles": 2, "totalFiles": 2, ... } } ]
}
```

---

## 3. 配编辑器

起服务时已经把片段打出来了（§1 末尾），也可以随时单独取：

```console
$ uv run nova-service mcp-config --port 8792
nova MCP 端点：http://127.0.0.1:8792/mcp（Streamable HTTP）

Cursor（项目内 .cursor/mcp.json 或全局 ~/.cursor/mcp.json）：
{ "mcpServers": { "nova": { "url": "http://127.0.0.1:8792/mcp" } } }

Claude Code / 其它支持 HTTP 传输的 harness：
{ "mcpServers": { "nova": { "url": "http://127.0.0.1:8792/mcp" } } }
（URL = http://127.0.0.1:8792/mcp；若编辑器要求 JSON 文件，直接写上面的对象即可）
只支持 stdio 的 harness 需要一个 stdio 代理（在本地把 stdio 转发到本 URL）——归 M2c 的 Rust client（TASK-040R），当前版本未提供。
```

- **Cursor**：把那段 JSON 写进 `.cursor/mcp.json`（项目内）或 `~/.cursor/mcp.json`（全局），
  重启/刷新 MCP 面板，应当看到 server `nova` 带着两个工具 `search_context` / `ask_project`。
- **本地模式没有 token**：URL 里不出现任何密钥（那是 M2c 才有的事）。
- **server 名是 `nova`**（`initialize` 返回 `serverInfo.name = "nova"`）。
- **URL 里的 `/mcp` 可以直接用，没有重定向**（见 §7 的实现说明）。

---

## 4. 验证：问一个真实问题

在编辑器里（或用一个最小 MCP 客户端，见 §4.2）调用 `search_context`：

```jsonc
// 工具参数（CF-06 冻结的两个工具）
search_context: { query: "令牌过期后在哪里刷新？",
                  project_root: "/home/xuwenzheng/nova-scratch/demo-repo",
                  max_tokens: 10000 }     // 可选，默认 10000，上限 16000
ask_project:    { question: "…同上的自然语言问题…", project_root: "…", max_tokens: 10000 }
```

### 4.1 期望看到什么（真实返回）

```text
[nova] answerable=true · confidence=medium · evidence=1 · docs=1 · mode=fast · channels=inferred,bm25,vector · degraded=false

## Relevant Context
### Code
[E1] SessionStore.refresh_token — session.py:1-13
     reason: inferred symbol refresh_token + inferred rank 1 + bm25 -1.3818 + bm25 rank 1 + vector 0.9333 + vector rank 1 + query symbol == chunk symbol +1.0 + 3-channel consensus +0.5 + 相邻区间合并
     1 | """会话管理模块。"""
     ... （省略 1 行）
     4 | class SessionStore:
     5 |     """内存会话存储。"""
     6 |     def create(self, user: str) -> str:
     7 |         """创建会话并返回 token。"""
     8 |         return f"tok-{user}"
     ... （省略 1 行）
    11 |     def refresh_token(self, token: str) -> str:
    12 |         """刷新会话 token：过期后由本方法负责续期。"""
    13 |         return token + "-refreshed"
### Docs
[E2] README.md > 演示仓库 > 会话（readme）
     reason: bm25 -0.0000 + bm25 rank 2 + vector 0.9021 + vector rank 2 + high-value doctype +0.8 + 相邻区间合并
     1 | # 演示仓库
     2 | 
     3 | ## 会话
     4 | 
     5 | `SessionStore.create` 创建会话，`refresh_token` 续期令牌。
### Meta
confidence: medium | index: fresh (58s ago) | budget: 605/10.0K
```

怎么判读：

| 看到的东西 | 含义 |
|---|---|
| 首行 `[nova] answerable=… confidence=…` | 服务端 meta 的一行摘要。`render_markdown` 本身只渲染 `confidence`，`answerable` 由这一行补上（诚实反映"证据够不够"，编辑器据此决定要不要声明证据不足） |
| `[E1] 符号 — 文件:行号` | 证据块。**行号是硬要求**：没有行号就没法在编辑器里跳转 |
| `reason: …` | 排序理由（哪几个通道命中、排名、图/同符号等信号）。出问题时可看它诊断 |
| `### Missing Evidence` | 缺失证据段（本例没有）。**`answerable=false` 时也照常返回已有证据**，不会给空结果 |
| `channels=bm25,vector` 或 `inferred,bm25,vector` | 本次用到的检索通道。只出现 `bm25` 通常意味着向量通道还没就绪（索引中） |
| `index: fresh (58s ago)` | 索引新鲜度；`stale (...)`/`indexing (...)` 会如实标出 |

真实仓库（1382 文件的 C++ 服务，非本手册的演示仓库）返回示例：

```text
$ curl -s -X POST http://127.0.0.1:8793/api/query/search -H 'Content-Type: application/json' \
    -d '{"projectId":"02f437a22ebbe713","query":"摄像头代理 MWPCameraProxy 的初始化流程在哪里实现？"}'
answerable=True confidence=medium channelsUsed=['bm25','vector'] evidenceCount=58 docsCount=3 degraded=False
[E2] MWPCameraProxy::~MWPCameraProxy — cameraservice/proxy/MWPCameraProxy.h:18-22
[E4] MWPCameraServer::MWPCameraServer — cameraservice/MWPCameraServer.cpp:9-12
[E5] MWPCameraProxy::MWPCameraProxy — cameraservice/proxy/MWPCameraProxy.cpp:8-23
     8 | MWPCameraProxy::MWPCameraProxy(){
     9 |     InitConfig();
    10 |     mPolicyManager=std::make_shared<MWPPolicyManager>(Car::PARK_TYPE_UART,this);
```

### 4.2 不装编辑器也能验证（最小 MCP 客户端）

装好依赖后，下面 40 行的脚本就是"真实编辑器协议客户端"的最小版（官方 SDK，Streamable HTTP）：

```python
# mcp_client_demo.py —— 用法：uv run python mcp_client_demo.py <URL> <PROJECT_ROOT> <问题> [search_context|ask_project]
import asyncio, os, sys
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL, PROJECT_ROOT, QUERY = sys.argv[1], sys.argv[2], sys.argv[3]
TOOL = sys.argv[4] if len(sys.argv) > 4 else "search_context"
FIELD = "question" if TOOL == "ask_project" else "query"

async def main() -> None:
    async with streamable_http_client(URL) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            init = await session.initialize()
            print(f"== initialize == server={init.server_info.name} protocol={init.protocol_version}")
            tools = await session.list_tools()
            print(f"== tools/list == ({len(tools.tools)} 个)")
            for tool in tools.tools:
                print(f"  - {tool.name}({', '.join(tool.input_schema['properties'])}) "
                      f"required={tool.input_schema['required']}")
            result = await session.call_tool(TOOL, {FIELD: QUERY, "project_root": PROJECT_ROOT})
            print(f"== tools/call {TOOL} == isError={result.is_error}")
            print("\n".join(b.text for b in result.content if b.type == "text")[:2000])

asyncio.run(main())
```

真实运行：

```console
$ NO_PROXY=127.0.0.1,localhost uv run python mcp_client_demo.py \
    "http://127.0.0.1:8792/mcp" "/home/xuwenzheng/nova-scratch/demo-repo" "令牌过期后在哪里刷新？"
== initialize == server=nova protocol=2025-11-25
== tools/list == (2 个)
  - search_context(query, project_root, max_tokens) required=['query', 'project_root'] additionalProperties=False
  - ask_project(question, project_root, max_tokens) required=['question', 'project_root'] additionalProperties=False
== tools/call search_context == isError=False
[nova] answerable=true · confidence=medium · evidence=1 · docs=1 · mode=fast · ...
```

> **WSL 上的坑**：如果你设了 `http_proxy`（本机实测环境里设了），务必让 127.0.0.1 走直连
> （`NO_PROXY=127.0.0.1,localhost`，脚本里已设）。否则客户端会尝试用代理访问本机端口，
> 表现为连接失败。`curl` 同理要加 `--noproxy '*'`。

### 4.3 `ask_project` 在 M2a 是**降级包**，不是 LLM 总结

真实返回开头：

```text
Deep 模式（LLM 总结）尚未接入（Phase 3）；以下为检索与组装结果，可直接作为上下文使用。

[nova] answerable=true · confidence=medium · evidence=1 · docs=1 · mode=fast · channels=bm25,vector · degraded=true
```

- LLM 属 Phase 3；M2a 的 `ask_project` **不假装**给出了答案——第一行就写明，且
  `degraded=true`。你要的"直接结论"在 M2a 请自己读证据包（这也是它把 Markdown 渲染成
  带行号证据的原因）。
- 它**不会 500**，也不会返回空。

### 4.4 真实仓库（aibox-super-sdk）的完整验收实测

这是 M2 的验收实测：451 个文件的真实仓库，用**用户种子问题**调 `tools/call`（真实进程 + 官方 SDK 客户端）：

```console
$ uv run nova-service local --repo /home/xuwenzheng/4_AIBOX/gitlab/minicpm/aibox-super-sdk \
      --data-root ~/nova-scratch/data-aibox --port 8794
  projectId : 8f39057792cf72e8
  身份      : git remote（D-29）
{"logger": "nova_service.indexer", "msg": "索引完成：8f39057792cf72e8（parsed=434/451，added=434，modified=0，deleted=0，errors=2）"}
# 索引耗时：startedAt=1789093437 → finishedAt=1789096917，共 58 分钟（本机同时有其它进程占 CPU）

$ MCP_PRINT_LIMIT=0 NO_PROXY=127.0.0.1,localhost uv run python mcp_client_demo.py \
    http://127.0.0.1:8794/mcp /home/xuwenzheng/4_AIBOX/gitlab/minicpm/aibox-super-sdk \
    "workflow 在记忆系统里是怎么定义和使用的？"
== initialize == server=nova protocol=2025-11-25
== tools/list == (2 个)
  - search_context(query, project_root, max_tokens) required=['query', 'project_root'] additionalProperties=False
  - ask_project(question, project_root, max_tokens) required=['question', 'project_root'] additionalProperties=False
== tools/call search_context == isError=False
[nova] answerable=true · confidence=medium · evidence=6 · docs=5 · mode=fast · channels=bm25,vector · degraded=false

## Relevant Context
### Code
[E5] MEMORY_TOOLS — src/aibox/capabilities/memory/example/chatbot/agent.py:90-195
     reason: bm25 -16.7298 + bm25 rank 1 + 相邻区间合并
[E7] MemoryRepl — src/aibox/capabilities/memory/example/repl/session.py:49-51
[E8] AddEventRequest — src/aibox/capabilities/memory/types/evidence.py:89-98
[E9] BaseCapability — src/aibox/capabilities/base.py:86-98
[E10] MemoryService — src/aibox/capabilities/memory/service.py:56-60
[E11] ErrorCode — src/aibox/models/errors.py:11-42
### Docs
[E1] src/aibox/capabilities/memory/doc/记忆系统1.0详细设计.md > 记忆系统 1.0 详细设计 > 5. 长期记忆语义边界（guide）
[E2] src/aibox/capabilities/memory/doc/记忆系统1.0详细设计.md > … > 7.1 SQLite Job 是唯一事实队列（guide）
[E3] src/aibox/capabilities/memory/doc/记忆系统调研/记忆系统调研.md > … > 2.6 ByteRover > ⑧ 对车载场景的启发（guide）
[E4] src/aibox/capabilities/memory/doc/记忆系统调研/记忆系统设计精要.md > (preamble)（guide）
[E6] src/aibox/capabilities/memory/doc/V1.0/V1.0开发计划进展.md > … > 2.1 最小闭环（guide）
### Missing Evidence
- [unresolved_reference] 62 个符号引用无法解析（unresolved_refs status=failed），涉及这些符号的调用关系可能缺失。
- [retrieval_truncated] 候选池被预算裁剪：省略 83 个候选（其中 82 个因 spec 份额上限让位给代码证据），可能有相关但未展示的证据；可提高预算或收窄查询。
### Meta
confidence: medium | index: fresh (1 h ago) | budget: 2.8K/10.0K
```

判读：`workflow` 是个词面上不存在的词（这个仓库把它表达为 tool 声明与设计文档里的“流程”），但 6 条
代码证据 + 5 条设计文档证据全部落在 `capabilities/memory/**`；**更重要的是 `Missing Evidence` 如实报出了
两条局限**（62 个引用未解析、83 个候选被预算裁掉）——这就是 M2 想要的形态：给可得的最好证据 +
明说自己缺什么，而不是编一个看起来完整的答案。

**索引未完成时问同一句**（同一次运行的中间态，供对比）：

```text
[nova] answerable=false · confidence=low · evidence=1 · docs=6 · mode=fast · channels=bm25 · degraded=false
```

`channels` 只剩 `bm25`（向量还没写完）、`answerable=false`：服务**不假装就绪**，但也不把已有结果丢掉。
所以“回答看起来不完整”时，第一件事是去看 §2 的 `indexProgress`，而不是怀疑检索质量。

---

## 5. 改了代码再问（懒重扫 / 手动重扫）

本地模式下服务与代码在同一文件系统，所以**不需要客户端上传**：服务端在检索前做一次增量重扫。

```console
$ # 往被索引的仓库里加一个函数
$ cat >> ~/nova-scratch/demo-repo/session.py <<'EOF'

def revoke_token(token: str) -> bool:
    """吊销会话 token（新增函数）。"""
    return bool(token)
EOF

$ sleep 4    # 默认间隔 2 秒，等过这个窗口
$ uv run python mcp_client_demo.py "http://127.0.0.1:8792/mcp" \
    "/home/xuwenzheng/nova-scratch/demo-repo" "revoke_token 吊销会话是怎么实现的？"
== tools/call search_context == isError=False
[nova] answerable=true · confidence=medium · evidence=1 · docs=1 · mode=fast · channels=inferred,bm25,vector · degraded=false

[E1] revoke_token — session.py:1-18
     reason: inferred symbol revoke_token + inferred rank 1 + bm25 -5.2784 + bm25 rank 1 + vector 0.9306 + vector rank 1 + query symbol == chunk symbol +1.0 + 3-channel consensus +0.5 + entry point / exported symbol +0.2 + 相邻区间合并
```

注意 `revoke_token — session.py:1-18`：**刚写的代码立刻就能被检索到**，无需手动同步。

调节/关闭：

| 手段 | 用法 |
|---|---|
| 改间隔 | 启动前设 `NOVA_LOCAL_RESCAN_INTERVAL=10`（秒；默认 2.0） |
| 禁用懒重扫 | `NOVA_LOCAL_RESCAN_INTERVAL=0`（测试或"只读演示"用） |
| 手动触发一次 | `curl -X POST http://127.0.0.1:8792/api/projects/e6fe81dbaebfb65d/rescan` → `202` + 当前进度 |

两条纪律（验收时值得知道，避免误判）：

1. **重扫失败不会让检索失败**：会记日志、如实标记，检索照常返回**已有索引**的结果。
2. **重扫是增量的**：只处理变化的文件（`processedFiles` 会是变化文件数，不是全仓文件数）。

---

## 6. 失败排查表（下面每条报错文本都是真实输出）

### 6.1 还没索引完 / 索引为空 → `isError=true` + 重试提示

```text
Error executing tool search_context: 项目 79043b7afde92ac6 暂无可用索引（chunks=0），当前索引状态：idle（本进程还没为它跑过索引）。
如果刚启动本地服务，后台索引可能还在跑：**稍后重试本查询**，或用 GET /api/projects/79043b7afde92ac6 查看 indexProgress；
如果一直是空，请确认仓库路径正确并重新 `nova-service local --repo <根目录>`（或 POST /api/projects/{id}/rescan 手动触发增量重扫）。
```

处理：等索引跑完（§2），或按提示 `rescan`。`state=running` 时这条消息会带
`（已处理 0/451 个文件）`——**不会有百分比**（§2 的理由）。

### 6.2 provider 不可用 → 报根因，别重试

配置坏掉（例如 `EMBED_MODE=api` 但没给 `EMBED_MODEL`/`EMBED_BASE_URL`）：

```text
Error executing tool search_context: 项目 … 的索引为空，且 embedding provider 当前不可用（EmbeddingConfigError: …）：
这不是「索引还没跑完」，重试不会好。请检查 embedding 配置（EMBED_MODE / EMBED_MODEL / EMBED_BASE_URL / EMBED_API_KEY）
与模型缓存目录（EMBED_CACHE_DIR / EMBED_MODEL_DIR）：本地模式下首次使用可能需要联网下载模型，离线环境请预先放置模型文件或显式设置 EMBED_OFFLINE=1 与 EMBED_MODEL_DIR。
```

要点：**根因优先**——provider 坏了就不再建议"重试"，否则你会陷入无限重试。
离线环境：预先放好模型 + `EMBED_OFFLINE=1` + `EMBED_MODEL_DIR=...`。

### 6.3 `project_root` 不对

**Windows 路径**（含反斜杠）：

```text
Error executing tool search_context: project_root 含反斜杠，必须是**正斜杠**的绝对路径（本地模式下服务跑在 WSL/Linux，Windows 路径如 C:\... 在这里不存在）：收到 'C:\\work\\repo'
```

**服务里没有这个项目**（没 attach 过 / 索引数据根不对）：

```text
Error executing tool search_context: 未知项目：/home/xuwenzheng/nova-scratch/no-such-indexed-dir 对应的 projectId df94e7b4d795324b 在本服务里没有索引记录（D-29 身份）。本地模式请用 `nova-service local --repo /home/xuwenzheng/nova-scratch/no-such-indexed-dir` 起服务，或先 POST /api/projects/attach；远端模式请先同步（POST /api/sync/batch-upload）。
```

注意报错里直接给了**你该敲的那条命令**（含正确的 project_root）。若同一个仓库被 attach 到
**不同的 data-root**，`projectId` 相同但本服务实例不认识它——用对 `--data-root` 即可。

### 6.4 参数错误

```text
# query 纯空白
query 不能为空或纯空白：请给出自然语言或符号混合的问题（中英均可）。
```

（`query: ""` 会被 CF-06 的 `minLength: 1` 先拦下，报的是 schema 校验错——同样是 `isError=true`。）

### 6.5 编辑器连不上 / 403

- **`Invalid Origin header`（HTTP 403）**：MCP 端点默认开了 DNS-rebinding / Origin 防护
  （本地模式下这是唯一挡住"浏览器里的任意网页访问本机服务"的机制）。实际表现：

  ```console
  $ curl -s --noproxy '*' -o /tmp/o.txt -w "status=%{http_code}\n" --max-redirs 0 -X POST http://127.0.0.1:8792/mcp \
      -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
      -H 'Origin: http://evil.example' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
  status=403
  $ cat /tmp/o.txt
  Invalid Origin header
  ```

  默认白名单是 `http://127.0.0.1:*` / `http://localhost:*` / `http://[::1]:*`（不带 Origin 的
  请求也放行）。**这是有意保留的**：不要为了让某个客户端连上就把它关掉；确需放行特定
  Origin 时用 SDK 的 `TransportSecuritySettings(allowed_origins=[...])` 显式加白名单。
- **一定要带 `Accept: application/json, text/event-stream`**：缺了会被协议层拒（MCP 规范要求）。
- **响应是 SSE**（`event: message` + `data: {...}`），不是纯 JSON。有些带 JSON-only 假设的
  简易客户端会解析失败——这是协议默认形态，不是 bug。
- **`curl`/客户端要走直连**（本机实测环境设了 `http_proxy`）：加 `--noproxy '*'` 或
  `NO_PROXY=127.0.0.1,localhost`，否则表现为连不上。

### 6.6 服务是活的吗

```console
$ curl -s http://127.0.0.1:8792/healthz
{"status":"ok", ...}
```

`/healthz` 恒为 200（即使索引在跑、即使 provider 坏了——那两件事不等于"服务挂了"）。
`?deep=1` 才会真的去探测 provider。

---

## 7. 实现要点（排障时你需要知道的形态）

| 点 | 说明 |
|---|---|
| 端点 | 服务端直出 MCP **Streamable HTTP**，挂在 `/mcp`（R38：M2a 不需要 Rust client；远端场景才需要，属 M2c） |
| URL 形态 | 编辑器填 `http://127.0.0.1:8787/mcp` 即可，**不靠 307 重定向**：除了挂载，还额外注册了同端点的别名路由（否则 `POST /mcp` 会先 307 到 `/mcp/`，而跟随与否取决于编辑器用的 HTTP 客户端） |
| 会话 | 使用 SDK 的 session manager（服务进程内），`initialize` 返回 `mcp-session-id`，后续请求带上 |
| 工具数 | **恰好 2 个**（CF-06 冻结）：`search_context` / `ask_project`。不做 prompts/resources |
| 检索执行 | core 调用是阻塞的，工具内部走线程池，不占事件循环 |
| `/mcp` 与 CF-05 | `/mcp` **不在** REST 的 OpenAPI 路径集合里（路径快照测试不受影响） |
| 重启后的行为 | attach 关系只存内存：重启后 `POST /api/projects/{id}/rescan` 会返回 `409 local_root_unknown`——重新 `nova-service local --repo` 即可 |

---

## 8. 边界与"当前不做"

| 不做 | 何时 |
|---|---|
| 多项目 / 多仓库 workspace | M2c 或按需排期（一个 project 一个 repo，Module/01 §6-1） |
| 鉴权 / token / 多用户 | M2c（TASK-060/061）。本地模式绑 127.0.0.1、无鉴权（R34） |
| 远端部署 / TLS | M2c（TASK-063） |
| Rust client + stdio transport | M2c（TASK-040R）；只支持 stdio 的编辑器届时才可用 |
| `.gitignore` 解析 | **已知缺口**：扫描只用内置跳过规则（D-28），所以 `cmake-build-*/`、`.claude/skills/**` 等也会进索引。实测影响：某 C++ 仓库 1382 个列出文件里 1089 个是构建缓存 |
| 精确进度百分比 | 需要 core 支持回调（TASK-062）；现在只说状态与文件数 |
| LLM 总结 / citation 回验 | Phase 3 |

---

## 9. 15 分钟验收清单（照着打勾）

- [ ] `uv sync --all-packages --all-extras` 成功，`uv run nova-service --help` 有 `local` / `mcp-config` 子命令
- [ ] `uv run nova-service local --repo <你的仓库绝对路径> --data-root <数据根> --port 8787` 起服务，就绪信息里有 projectId / 身份 / MCP URL
- [ ] `curl -s http://127.0.0.1:8787/healthz` → `"status":"ok"`，且 `projects[0].indexProgress` 可见
- [ ] 轮询 `GET /api/projects/{id}` 直到 `state="done"`（期间服务一直可用）
- [ ] 把 `.cursor/mcp.json` 写好，编辑器能看到 `nova` 的两个工具
- [ ] 在编辑器里问一个**你自己仓库的真实问题**，返回里出现 `文件:行号` 证据
- [ ] 改一个文件、等 2 秒再问相关问题，能命中新代码
- [ ] 故意问一个不存在的路径 → 看到"未知项目"的可读报错（不是 500）
- [ ] 带 `Origin: http://evil.example` 发一次请求 → 403（防护生效）

---

## 10. 一键冒烟（可选）

第 1–4 节的步骤可以压缩成一条命令：`scripts/m2a-smoke.sh` 会**起服务 → 等索引 → 用 MCP 调一次
`search_context` → 断言返回里有「文件:行号」**，并在结束时停服务、清理数据根（默认不保留）。

```console
$ bash scripts/m2a-smoke.sh --repo /home/xuwenzheng/github/hello-agents \
      --data-root /tmp/nova-smoke --query "记忆工具如何实现多轮检索？"
[1/5] key 已就绪（来源已解析，长度 51，不回显内容）
[2/5] 起服务：nova-service local --repo /home/xuwenzheng/github/hello-agents --data-root /tmp/nova-smoke --port 8799
        projectId=e9ee9dd1d41a7d2c ｜ 服务日志：/tmp/nova-smoke/nova-smoke-service.log
[3/5] 等索引完成（上限 1800s，每 5s 轮询一次；被 429 中断时最多重试 3 次）
        state=running，本次已解析 0/1862 个文件…
        ...（约 5 分钟）
        索引完成：state=done，本次解析 1482/1862 个文件（无改动时 processed=0 属正常）
[4/5] 调 MCP tools/call search_context（断言返回里有「文件:行号」）
[nova] answerable=true · confidence=medium · evidence=22 · docs=4 · mode=fast · channels=bm25,vector · degraded=false

## Relevant Context
### Code
[E2] search_memory_demo — code/chapter8/01_MemoryTool_Basic_Operations.py:78-106
     reason: bm25 -14.9703 + bm25 rank 38 + vector 0.6394 + vector rank 6 + entry point / exported symbol +0.2
     78 | def search_memory_demo(memory_tool):
     79 |     """搜索记忆演示 - 实现语义理解的检索"""
     ...
[OK] 冒烟通过：MCP 返回包含「文件:行号」证据。
[5/5] 已清理数据根：/tmp/nova-smoke（--keep 可保留）
```

> 上面是本机**真实输出**（2026-09-13，`hello-agents` 全量 1862 文件 / 9971 chunks）。
> 索引阶段约 5 分钟；输出的文件数（1862/1482）会随仓库不同而变，但形态一致。

**它替你固化了本环境的两个坑**（这两条是实测踩过的，见 `docs/plan/phase2-m2b-w6.md` §2.4）：

| 坑 | 脚本的处理 |
|---|---|
| **F3：非交互 shell 拿不到 key**——`~/.bashrc` 的 `export nova_embeding_API_KEY=` 只对**交互式** shell 生效；脚本、子 AI、CI 都是非交互进程 | 脚本按 `$EMBED_API_KEY` → `--env-file`（默认 `./.env`）→ `~/.bashrc` 的 `--key-var`（默认 `nova_embeding_API_KEY`）顺序自己解析；**找不到时明确报错并打印三条解决命令**（不静默继续、不回显 key） |
| **本机有 `http_proxy`** → 客户端把 `127.0.0.1:8799` 也走代理，连接失败 | 脚本 `export NO_PROXY=127.0.0.1,localhost`（MCP 客户端进程内也再设一次，同 §4.2） |

**用法**

```bash
bash scripts/m2a-smoke.sh --help          # 全部参数
bash scripts/m2a-smoke.sh --repo <仓库绝对路径> [--data-root /tmp/nova-smoke] [--port 8799]
#   --query  "你自己的真实问题"   # 默认是一个演示问题，换掉更贴近你的仓库
#   --timeout 1800                # 等索引上限（秒）
#   --keep                        # 结束后保留数据根与服务日志，便于排错
```

**前置**

- 依赖已装：`uv sync --all-packages --all-extras`（MCP 客户端用 `mcp` 官方 SDK）；
- **云端 embedding 的 key**（脚本会把 `EMBED_MODE=api` / `EMBED_MODEL=BAAI/bge-m3` / `EMBED_DIM=1024`
  / `EMBED_BATCH_SIZE=4` 等导出给 `nova-service` 子进程，可用同名环境变量覆盖）——不想给 key 就先用
  `EMBED_MODE=local` + 本地模型（见 `.env.example` 末段）；
- **`--data-root` 不要落在被索引仓库内部**（脚本会拒绝，避免污染仓库）。

> **为什么要指定批大小**：脚本默认 `EMBED_BATCH_SIZE=4`。API 的默认批次是 64，实测在真实仓库上会撞
> provider 的 **TPM 限流（429）**，而当前实现里**一次 429 会让整次 ingest 失败**（TASK-046 §D 的 F4，
> 泳道 B 在修）。脚本对 429 有**有界重试**（默认 3 次 × 等 90s 后 `POST /rescan`），可用
> `--max-retries` / `--retry-wait` 调整；若反复 429，先把 `EMBED_BATCH_SIZE` 调小。

> **注意**：同一 `--data-root` **不要并发跑两个服务/两个脚本**（会触发向量表重建，把已嵌入的向量清空）。

**失败时怎么排查**

| 现象 | 先看哪里 |
|---|---|
| `[错误] 找不到 embedding API key` | 按提示三选一注入；确认 `~/.bashrc` 里的变量名（`grep -n nova_embeding_API_KEY ~/.bashrc`） |
| 服务 60s 内未就绪 | 脚本会打印服务日志尾部；手动起一次看完整报错：`uv run nova-service local --repo … --port …` |
| 等索引超时（state 一直 `running`） | 这是**真实进度**（服务端没有百分比，§2）；加大 `--timeout`，或检查是否撞了 provider 限流（日志里有 `429`） |
| 日志里反复 `HTTP 429` / `TPM limit reached` | 降低批次后重跑：`EMBED_BATCH_SIZE=2 bash scripts/m2a-smoke.sh …`；或加大重试 `--max-retries 6 --retry-wait 120`（根因属 TASK-046 §D） |
| 断言失败（没有「文件:行号」） | ① `--query` 换成仓库里确实存在的概念；② 查 `GET /api/projects/{id}` 的 `sync.chunks` 是否 > 0；③ 若 `channels` 只有 `bm25`，说明向量还没就绪 |
| 想留着现场 | 加 `--keep`，然后按第 1–4 节手工继续查 |

> **与第 9 节的关系**：第 9 节是**人工**逐项验收（含编辑器、改代码重扫、安全防护）；本脚本只做
> **自动化的最小闭环**（服务可用 + 索引完成 + MCP 有带行号的证据）。两者互补，不要用脚本替代第 9 节。
