# zace-web

zace 的**账户 console**：登录、账户面板、API Key、历史记录、接入指南。
静态 SPA，直连 `zace-service` REST API；**不做检索与渲染** —— 检索与 ContextPack → Markdown
由服务端与编辑器侧 Agent 完成（Module 07 / D-40），web 只做面板与元数据展示。

```text
浏览器 ── 静态 SPA（本目录产物）── REST /api ──► zace-service ──► zace-core
```

## 页面与后端依赖

| 页面 | 路径 | 后端端点 | 状态 |
|---|---|---|---|
| 登录 / 注册 / 初始化账户 | `/login` | `GET /api/meta`、`POST /api/auth/{login,register,bootstrap}` | ✅ |
| 账户面板（首页，含项目列表） | `/` | `GET /api/auth/me`、`GET /api/account/overview` | ✅ |
| API Key 管理 | `/keys` | `GET/POST/DELETE /api/auth/tokens` | ✅ |
| 历史记录（索引 / 使用） | `/history` | `GET /api/projects/{id}/index-runs`、`GET /api/usage/summary` | ✅ |
| 接入指南 | `/connect` | `GET /api/meta`（取部署形态） | ✅ |

> **TASK-082（2026-09-23）已删除 `/projects`、`/projects/:id`、`/playground` 三条路由与页面**（导航同步移除）。
> 项目列表仍保留在仪表盘首页；`/api/projects*` 与 `/api/query/{search,ask}` 端点未删，仍供客户端与本地模式使用。
> 随之失去全部引用的是 `components/Markdown.tsx` 与 `components/MetaPanel.tsx`（仅 Playground 使用）——
> 不在 TASK-082 文件所有权清单内，**本次保留**，已在卡片"未决问题"登记。

## 首屏行为（重要）

`src/app/App.tsx` 用 `GET /api/meta`（免鉴权）判断部署形态，再决定首屏：

| 部署状态 | 首屏 |
|---|---|
| 完整服务 + 有账户 + 未登录 | **登录页**（下方始终可切换注册） |
| 完整服务 + 无账户 | **初始化账户**页 |
| 本地模式 | 直接进入（`/api/auth/me` 返回隐式账户 `isLocal=true`） |

**不能**只看 `/api/auth/me` 的 401 来判定——那是"未登录"，不是"没有账户可登"。

## 开发

```bash
# 1) 起服务（终端 A）——本地模式最省事，无需账户
export NO_PROXY=127.0.0.1,localhost
uv run zace-service local --repo /绝对路径/你的仓库 --port 8787

# 或完整服务形态（验证登录/API Key/面板）：普通 serve 默认启用全部账户功能
ZACE_DATA_ROOT=/tmp/zace-ui uv run zace-service serve --port 8891

# 2) 前端（终端 B）
cd web && npm ci && npm run dev        # http://127.0.0.1:5173
```

Vite 把 `/api`、`/healthz` 代理到 `ZACE_WEB_API`（默认 `http://127.0.0.1:8787`）。

**为什么不需要 CORS**：生产由 Caddy 同源托管（`/` 静态、`/api` 反代 service），
开发期用 Vite 代理，因此 service 不必加 CORS 中间件。

## 品牌与界面主题

`site.config.ts` 是显示名称的唯一配置，默认 `name: "zace"`。修改 `name` 后运行
`npm run build`，侧栏、窄屏顶栏、登录页与浏览器标题会一起更新；品牌图标是通用代码符号，
不依赖名称首字母。`tagline` 控制品牌说明文字。

这只是显示品牌：接入指南中的真实 `zace-client` 包名、命令和 `zace_` API Key 前缀属于
现有接口契约，不随显示名称变更，避免复制配置后无法连接。若将来发布改名的客户端，需单独迁移这些契约。

界面采用暖白、珊瑚橙、方角边框和偏移阴影。颜色、圆角、阴影集中在
`tailwind.config.js`，通用组件样式在 `src/index.css`。桌面保持左侧导航、右侧内容，
窄屏使用可通过 Esc 关闭的导航抽屉。

## 命令

| 命令 | 作用 |
|---|---|
| `npm run dev` | 开发服务器（含代理） |
| `npm run build` | 类型检查 + 产物到 `dist/` |
| `npm run lint` | ESLint（`--max-warnings 0`） |
| `npm test` | vitest（单测；e2e 默认跳过） |
| `npx vitest run src/pages/console.e2e.test.tsx` | 账户 console 端到端（需真实服务） |

## 端到端测试（真实服务）

单测用 mock，只证明"按我以为的契约解析了"。要证明**真实返回被真实页面正确渲染**：

```bash
# 云端形态服务在 8891 运行，且已有账户
cd web
ZACE_E2E=1 ZACE_E2E_BASE=http://127.0.0.1:8891 \
ZACE_E2E_USER=owner ZACE_E2E_PASSWORD=<密码> \
VITE_ZACE_API_BASE=http://127.0.0.1:8891 \
  npx vitest run src/pages/console.e2e.test.tsx
```

该文件内含一个**cookie 转发器**：vitest 的 jsdom 里 `fetch` 是 Node undici，不与 jsdom 共享
cookie jar，而生产形态是同源（浏览器自动带 cookie）。转发器只在测试内补上这一环，**不改产品代码**。

## 目录

```text
src/
  api/         唯一请求出口（client.ts）+ CF-05 类型（types.ts）+ 错误码文案
  app/         路由与首屏门禁（App.tsx）、外壳（Layout.tsx）、接入片段（connect-info.ts）
  components/  展示组件（ui 基础件、indexProgress 口径、MetaPanel/Markdown 展示件）
  pages/       每页一个文件（请求 + 组合）
```

约定：

- `components/` 不发请求；所有错误经 `ApiError` 统一展示（文案集中在 `api/client.ts` 的 `errorHint`）；
- **`indexProgress` 不显示百分比**（core 无进度回调，宁可信息少不可信息假）；
- **统计不美化**：`avgDurationMs` 只统计成功索引；未测量显示 `—`（如 `citationCoverageAvg` 在 LLM
  接入前恒为 `null`，**不填 0**）；
- **不自研 ContextPack 渲染**（D-21/D-40）：只展示服务端给的 markdown 字符串，不解析 `[E1]` 语义。

## 与设计文档的对应

- `docs/design/Module/07-WebUI.md`：页面骨架与对接原则
- `docs/design/INDEX.md` §3 **D-40**：V1 = 管理面（Playground 已按用户要求于 TASK-082 删除）
- `docs/contracts/openapi.yaml`（CF-05）：唯一消费的接口契约
- `docs/tasks/TASK-070-web骨架与Playground.md`：任务卡与本阶段执行记录（含偏差与未决问题）
- `npm/README.md`：接入指南里三个 agent 配置片段的**事实来源**（改片段必须同步这里）
