# 请求日志与 trace id 报错手册（TASK-090）

> 状态：2026-09-14 泳道 D 实施并实测（本机 WSL2，逐条命令真实跑过）。
> 配套：`docs/handbook/getting-started/agent接入与API-Key.md`（拿到 API Key）、`docs/handbook/getting-started/M2a-验收手册.md`（本地模式起服务）。
> 本手册只讲**报错之后怎么查**，不含部署与 embedding 配置。

## 0. 一句话

**报错时把响应头里的 `X-Request-Id` 抄下来发给我**，我就能查到那一次请求的完整日志
（路径、状态、耗时、错误码、5xx 的堆栈、涉及的项目）。

## 1. 你怎么拿到 requestId

**每一个** HTTP 响应都带 `X-Request-Id` 头——包括 4xx/5xx（失败的那些）在内。取值有三条路：

```bash
# 1) 命令行/脚本：只看响应头
curl -s -D - -o /dev/null http://127.0.0.1:8787/api/projects | grep -i x-request-id

# 2) 浏览器 F12 → Network → 点那条红了的请求 → Response Headers → X-Request-Id

# 3) 编辑器里的 MCP 工具报错时：错误信息里也会带上这个 id（客户端透传服务端响应头）
```

拿到的是 16 位十六进制串，例如：

```text
X-Request-Id: 6e5ca90b6db44feb
```

> 也可以**自己指定**：请求时带上 `X-Request-Id: <你的任意串>`，服务端会原样回写并记进日志
> （便于把一次操作的多条请求串起来）。不指定时服务端自动生成。

## 2. 把它发给管理员（或自己查）

- **自己查**（需要登录，本地模式免登录）：

  ```bash
  curl -s http://127.0.0.1:8787/api/request-log/6e5ca90b6db44feb \
       -H "Authorization: Bearer <你的 API Key>"
  ```

- **发给我**：直接把那 16 位串贴过来即可，例如
  `请求失败了，X-Request-Id: 6e5ca90b6db44feb`。

## 3. 查到的内容长什么样

```json
{
  "requestId": "6e5ca90b6db44feb",
  "ts": "2026-09-14T04:29:21.882+00:00",
  "method": "GET",
  "path": "/api/projects/abc123/index-stats",
  "status": 500,
  "durationMs": 42.19,
  "userId": "327c9b36...",
  "projectId": "abc123",
  "errorCode": "internal_error",
  "errorMessage": "服务内部错误，请稍后重试；服务端日志含完整堆栈",
  "traceback": "Traceback (most recent call last): …",
  "relatedLogs": [],
  "relatedLogCount": 0
}
```

字段含义：

| 字段 | 说明 |
|---|---|
| `path` / `method` / `status` / `durationMs` | 哪条接口、什么方法、什么状态码、耗时多少毫秒 |
| `userId` / `projectId` | 谁发的、涉及哪个项目（有则记） |
| `errorCode` | CF-05 错误码（`unauthorized` / `project_not_found` / `internal_error`…） |
| `errorMessage` | 给用户看的那句文案（与响应体一致） |
| `traceback` | **仅 5xx**：服务端堆栈（排查的核心）；4xx 是业务预期，不记栈 |
| `relatedLogs` | 同一次请求的旁路日志（如已处理的 503 的堆栈就在这里） |

## 4. 两个必须知道的边界

**① 只能查自己的。** 普通用户按 `requestId` 只能查到**自己发起的**请求；
查别人的 id 与查不存在的 id **返回完全相同的 404**（`request_log_not_found`）。
这样做是为了不给探测面——否则任何人拿一个 id 就能试出"这个请求存在吗、是谁的"。
V1 没有管理员角色，所以**没有"帮别人查"的口子**。

**② 未认证的请求谁都查不到。** 未带凭据被拒（401）的请求**没有归属者**，
它的日志谁都不能查（包括把它报给你的人）——因为记录里的 `path` 可能含别人的 projectId。
如果对方能拿到 401 的 `X-Request-Id`，请让 TA **带上凭据重试一次**，用新 id 再查。

## 5. 日志保存在哪、留多久（窗口机制）

- **落盘位置**：`$NOVA_DATA_ROOT/logs/request.log`（JSONL，一行一条请求）。
  默认 `NOVA_DATA_ROOT=~/.nova`，即 `~/.nova/logs/request.log`。
- **服务重启后仍在**（这是落盘而非内存的意义）；同时 stderr 也仍有一份实时输出。
- **有界保留（窗口）**：两个维度取交集，不会无限增长——

  | 维度 | 变量 | 默认 |
  |---|---|---|
  | 单文件上限 | `NOVA_LOG_MAX_BYTES` | 8388608（8 MiB）；写满即轮转 |
  | 轮转备份数 | `NOVA_LOG_BACKUP_COUNT` | 9（连同当前文件共 10 个） |
  | 保留天数 | `NOVA_LOG_RETENTION_DAYS` | 14（启动时清理超期文件） |

  默认上界约 `8 MiB × 10 = 80 MiB`、14 天。超窗的旧请求会查不到（返回 404），这是设计行为。
- **脱敏**：`Authorization` 头、cookie、API key（含裸 `sk-` / `nova_` 串）**一律不落盘**；
  日志只记 method/path/status/耗时/身份/错误码，**不记请求体**。

## 6. 快速排查表

| 现象 | 含义 | 怎么办 |
|---|---|---|
| 查日志返回 404 `request_log_not_found` | 四种可能：id 打错 / 不是你发的 / 该请求未认证 / 已被窗口清理 | 核对 id；确认是本人操作；让对方带凭据重试拿新 id；超过 14 天的日志本就查不到 |
| 404 但状态码是 401 `unauthorized` | 你自己没带凭据（未登录） | 带上 `Authorization: Bearer <KEY>` 再查 |
| `traceback` 为 `null` 但 `errorCode` 非空 | 4xx 业务错误（不记栈，属预期） | 看 `errorCode` 与 `errorMessage` 即可定位；如 `project_not_found` = 项目 ID 不对或不是你的 |
| `traceback` 空但 `relatedLogCount > 0` | 5xx 已被映射处理（如 503），堆栈在 `relatedLogs` 里 | 看 `relatedLogs[].traceback` |
| 完全查不到且确定刚发生 | 可能请求根本没到达服务（端口/代理问题） | 先确认 `curl http://127.0.0.1:8787/healthz` 通；WSL 上注意 `NO_PROXY=127.0.0.1,localhost` |
