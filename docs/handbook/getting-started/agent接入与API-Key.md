# Agent 接入手册（API Key + `npx nova-client`）

> 状态：2026-09-14 编排者实测（本机 WSL2，逐条命令真实跑过）；
> **2026-09-15 TASK-110 更新：注册改为邀请制，API Key 支持自定义（拓荒者特权）。**
> 结论：**用户期望的接入形态已经可用**，无需改代码。
> 配套：`docs/handbook/getting-started/cloud-embedding.md`（embedding 配置）、`docs/handbook/getting-started/M2a-验收手册.md`（本地模式 demo）。

## 0. 一句话

管理员发邀请码 → 用户注册 → 建 API Key → 把 `npx nova-client --base-url <URL> --token <KEY>` 填进编辑器 → 即可用真实问题检索。

## 0.1 邀请码与身份分级（TASK-110）

**注册必须有邀请码**（`POST /api/auth/register` 的 `inviteCode` 字段）——不传会得到 400 `invalid_invite`。

码面是 **6 位大写字母/数字**，**首字母即类型**：

| 首字母 | 身份 | 头衔 | 索引空间 | 自定义 Key |
|---|---|---|---|---|
| `A` | 管理员 | 执炬者 | 5 GiB | ✅ |
| `B` | 内测玩家 | 拓荒者（前 100 名**带编号**） | 1 GiB | ✅ |
| `C` | 公测玩家 | 旅人 | 500 MiB | ❌ |

管理员在后台（`/admin`）的**邀请码**页创建/失效邀请码，可指定类型、可用次数与有效期。

```console
# 管理员建一张内测码（需管理员 Key 或登录会话）
$ curl -s -X POST http://127.0.0.1:8787/api/admin/invites \
    -H 'Content-Type: application/json' -H "Authorization: Bearer $ADMIN_KEY" \
    -d '{"kind":"B","maxUses":1,"expiresInDays":7}'
{"code":"B3NQ8W","kind":"B","createdAt":1789473787,"maxUses":1,"usedCount":0,"revokedAt":null}

# 用它注册（**必须**带 inviteCode）
$ curl -s -X POST http://127.0.0.1:8787/api/auth/register \
    -H 'Content-Type: application/json' \
    -d '{"name":"early-bird","password":"...","inviteCode":"B3NQ8W"}'
# → role=beta, title=拓荒者, earlyMemberNo=1, capabilities.canCustomKey=true
```

**首个账户**不需要邀请码：空库时 `POST /api/auth/bootstrap` 建的账户**直接是管理员**
（否则是死锁——邀请码只能由管理员创建）。已有库里谁是管理员由 `NOVA_ADMIN_NAME`
（默认 `xuwenzheng`）在服务启动时按**名字**指定，不依赖"最早创建者"。

### 自定义 API Key（拓荒者 / 管理员）

```console
$ curl -s -X POST http://127.0.0.1:8787/api/auth/tokens \
    -H 'Content-Type: application/json' -b cookies.txt \
    -d '{"name":"my-key","key":"nova_my-laptop-key-2026"}'
{"id":"...","token":"nova_my-laptop-key-2026","prefix":"nova_my-pro","isCustom":true}
```

规则：必须以 `nova_` 开头，其后**不能为空**；字符集与长度**不做限制**（`nova_1` 也合法），
唯一要求是库里没有一样的 Key（冲突 → 409）。

随机生成（不传 `key`）时是 `nova_` + **16 位字母数字**（不含符号）。
无特权身份传 `key` → **403 `custom_key_forbidden`**（不是静默忽略——静默会让人以为自定义成功、
拿到的却是随机 Key）。不传 `key` 时行为与以前**逐字一致**（服务端随机生成）。

### 配额（超限硬拒上传）

索引空间按身份生效（上表）。**超过上限时新的索引上传会被拒绝**（413 `quota_exceeded`），
**检索与已有项目不受影响**——删掉不再需要的项目即可释放空间。

## 1. 接入形态（编辑器配置）

```json
{
  "mcpServers": {
    "nova": {
      "command": "npx",
      "args": ["nova-client", "--base-url", "http://你的服务器地址", "--token", "<控制台里创建的 API Key>"]
    }
  }
}
```

`nova-client` 的参数（`npx -y nova-client --help` 实测输出）：

| 参数 | 说明 | env 等价 |
|---|---|---|
| `--base-url <URL>` | nova-service 地址（必填） | `NOVA_BASE_URL` |
| `--token <TOKEN>` | **API Key**（云端形态必填） | `NOVA_API_TOKEN` |
| `--cache-root <DIR>` | 本地缓存（默认 `~/.cache/nova`） | `NOVA_CLIENT_CACHE` |

## 2. 服务端准备（完整服务形态）

普通 `serve` 始终启用账户、注册、登录、API Key 与鉴权，不需要用环境变量打开功能：

```bash
uv run nova-service serve --host 0.0.0.0 --port 8787 --data-root <数据根>
```

只有显式执行 `nova-service local --repo <目录>` 才进入免账户的专用本地嵌入流程。

启动后自查：

```console
$ curl -s http://127.0.0.1:8787/api/meta
{"version":"0.0.1","localMode":false,"authRequired":true,"registerOpen":true,
 "needsBootstrap":true,"userCount":null}
```

`authRequired=true` + `needsBootstrap=true` = 鉴权已开、还没有任何账户。

## 3. 建用户与 API Key（实测完整流程）

### 3.1 首个用户（bootstrap）

全新部署的网页会自动显示初始化页；bootstrap 用原子方式创建第一个账户。
**TASK-110 起该账户直接是管理员**（空库时没有别的方式能造出第一张邀请码）。

```console
$ curl -s -X POST http://127.0.0.1:8787/api/auth/bootstrap \
    -H 'Content-Type: application/json' \
    -d '{"name":"me","password":"correct-horse-battery"}' -c cookies.txt
{"userId":"6dea905f5f061860ccccc713cba8910f","name":"me","role":"admin","title":"执炬者",...}
```

- 字段是 **`name`**（不是 email）；长度 ≤64。
- 成功即自动登录（写 httpOnly session cookie 到 `cookies.txt`）。
- 已有用户时返回 403 `already_initialized`——**不会**创建第二个，也不会覆盖。

**后续用户注册必须有邀请码**（见 §0.1）：该账户已是管理员，可立刻去后台建码。

### 3.2 创建 API Key

```console
$ curl -s -X POST http://127.0.0.1:8787/api/auth/tokens \
    -H 'Content-Type: application/json' -b cookies.txt \
    -d '{"name":"client-key"}'
{"id":"46e8089b...","token":"nova_9fK2mQ7xR4tLpZ1a","prefix":"nova_9fK2mQ",
 "name":"client-key","isCustom":false}
```

⚠️ **`token` 明文只在这里返回一次**。服务端只存 sha256 哈希（`hash_api_token`），
之后 `GET /api/auth/tokens` **不含明文**。丢了只能撤销重建。

### 3.3 用 Key 访问数据面

```console
$ curl -s http://127.0.0.1:8787/api/projects -H "Authorization: Bearer nova_9fK2mQ..."
[]

$ curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8787/api/projects     # 不带 key
401
```

**撤销后立即失效**（实测）：`DELETE /api/auth/tokens/{id}` → 204，之后同一 key 访问 → 401。

## 4. 端到端实测（`npx nova-client` + 真实 MCP 协议）

用 Python MCP SDK 的 `stdio_client` 拉起真实 `npx nova-client`：

```python
params = StdioServerParameters(
    command="npx",
    args=["-y", "nova-client", "--base-url", "http://127.0.0.1:8787", "--token", TOKEN],
)
# initialize → server=nova, protocol=2025-11-25
# tools/list → 2 个: search_context, ask_project
# tools/call search_context → isError=False
```

**真实返回**（带「文件:行号」证据，这就是接入成功的标志）：

```text
## Relevant Context
### Code
[E1] SessionStore.refresh_token — src/session.py:1-6
     reason: bm25 -0.9853 + bm25 rank 1 + vector 0.8691 + vector rank 1 + 相邻区间合并
     1 | """会话管理。"""
     4 | class SessionStore:
     5 |     def refresh_token(self, token: str) -> str:
     6 |         return token + "-refreshed"
### Meta
confidence: l...
```

**token 错误时的表现**（可读、不泄漏）：

```text
tools/call search_context → isError=True
无法在服务端定位项目（identityKey=769ff956...）：服务端返回 HTTP 401 Unauthorized：
{"error":{"code":"unauthorized","message":"缺少或无效的凭据：请在 Authorization: Bearer 头带上 API Key，或先登录"}}
```

## 5. 鉴权实现要点（为什么不用逐个路由加依赖）

服务端用**全局 HTTP 中间件**（`service/nova_service/app.py` 的 `_install_auth`）：

- **鉴权是全局不变量**：逐个路由挂 `Depends` 靠人工维护，漏一个就是越权面
  （TASK-051 A1 的 `auth: enabled` 自述就曾在骗人）；
- **免鉴权白名单**（`PUBLIC_PATHS`）：`/healthz`、`/api/meta`、`/api/auth/{login,register,bootstrap,logout}`；
  **其余全部要求凭据**；
- **凭据两种**：`Authorization: Bearer <API Key>`（客户端用）或 session cookie（浏览器用）；
- **显式 local 命令完全放行**：行为与 M2a 一致（R34），普通 serve 不会进入该模式；
- **401 不区分细节**（无效/已撤销/过期同一文案）——不给探测面。

客户端侧（Rust，`client/src/remote.rs`）：发 `Bearer`（若有 token），
错误文本**脱敏**（token 出现即替换为 `***`），响应体截断防止刷屏。

## 6. 排错表

| 现象 | 原因 | 处理 |
|---|---|---|
| 401 `unauthorized` | 没带 key / key 错 / key 已撤销 / **账户已被封禁** | 检查 `--token`；在控制台重建 key；若被封禁请联系管理员 |
| 400 `invalid_invite` | 注册没填邀请码、码形状不对、或码无效/已失效/已用尽 | 向管理员索要新码（**码不存在与已用尽的文案相同**，这是刻意的反枚举设计） |
| 403 `custom_key_forbidden` | 以公测身份传了自定义 Key | 自定义 Key 是拓荒者特权：不传 `key` 让服务端随机生成，或联系管理员提升身份 |
| 400 `invalid_custom_key` | 自定义 Key 不以 `nova_` 开头，或 `nova_` 后面为空 | 见 §0.1 的格式规则 |
| 409 `key_taken` | 该 Key 明文已被使用过 | 换一个；随机生成的 Key 不会撞（256 位随机） |
| 413 `quota_exceeded` | 索引空间超过当前身份的额度 | 在控制台删除不再需要的项目，或联系管理员调整配额 |
| 403 `admin_required` | 非管理员访问 `/api/admin/*` | 后台仅管理员可用 |
| 400 `project_id_required` | 非本地模式下检索必须显式给 `projectId` | 这是 R37 的设计（省略仅限本地模式）；client 会先 `resolve` 拿到 id |
| 403 `local_mode` | 对显式 `local` 命令启动的服务调用账户接口 | 改用普通 `nova-service serve` |
| 403 `already_initialized` | 已有用户还调 bootstrap | 改用登录或注册（注册需邀请码） |
| `npx nova-client` 无输出 | 它是 **MCP stdio 服务**，等 stdin 上的 JSON-RPC | 正常。用编辑器或 MCP SDK 客户端连它 |
| client 报 401 但 token 是对的 | Key 已撤销、输错或不属于当前服务 | 在网页控制台重建 Key |
| 连不上 127.0.0.1 | 本机 `http_proxy` 拦截 | `NO_PROXY=127.0.0.1,localhost` |

## 7. 尚未验证的部分（诚实声明）

| 项 | 状态 |
|---|---|
| 浏览器 UI 走完整流程（登录 → 建 key → 复制） | **未验证**（WebUI 在 TASK-070 推进中） |
| 编辑器（Cursor/Codex/Claude/pi）里真实接入 | **未验证**（本手册用 MCP SDK 模拟了协议层） |
| 多用户隔离（A 用户的 key 看不到 B 的项目） | **已实现**（TASK-061 租户双层） |
| TLS / 域名 / 反向代理 | **未实现**（TASK-063 部署，pending） |
| 远端场景下的完整同步（大仓库首次上传） | 部分验证（client 的 resolve/upload 路径已通，大规模未压测） |

**当前可用的边界**：HTTP（非 HTTPS）、本机或内网。
公网部署前必须先做 TASK-063（TLS）。
