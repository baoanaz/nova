# nova-web 交接说明（TASK-070 / 账户 console）

> 交接人：lane-b 实施会话 ｜ 日期：2026-09-14 ｜ 分支：`feature/task-070-webui_xwz0914`（工位 `nova-lane-b`）
> 基线：`main @ 401c045`（已含 TASK-060/062/064 后端）｜ 状态：**review，已提交本地，未 push**

## 0. 一句话现状

**UI 已可打开并端到端可用**：登录页 → 账户面板 → API Key / 历史记录 / Playground / 接入指南，
全部接的是 main 上已合并的真实后端；29 个前端单测 + 真服务 e2e + 746 个仓库测试全绿。

## 1. 用户要什么（原始需求，逐条对照）

| 用户原话 | 实现 | 位置 |
|---|---|---|
| "打开网页最先出现的应该是登入页面，输入账户密码登入，下面可以注册用户" | ✅ 首屏登录；注册开启时下方可切注册 | `web/src/pages/LoginPage.tsx`、`App.tsx` |
| "首页只展示账户资料，账户、状态、创建时间等个人信息、占用内存、平均耗时、索引成功、索引失败等数据面板" | ✅ 账户资料 + 四项指标 + 使用次数面板 | `web/src/pages/DashboardPage.tsx` |
| "大页面再来一个 API Key 管理，可以创建、删除 Key" | ✅ 创建（明文只显示一次）/列表/撤销 | `web/src/pages/ApiKeysPage.tsx` |
| "大页面再来一个历史记录，看到什么时候进行了什么索引，量是多少，token 多少，消耗多少等使用次数记录" | ✅ 索引记录 + 使用记录两个页签 | `web/src/pages/HistoryPage.tsx` |
| "接入指南这页太多垃圾信息，只要三个按键 codex、claude、pi + 一个代码框给出配置" | ✅ 三按键 + 单份可复制片段 | `web/src/pages/ConnectPage.tsx`、`app/connect-info.ts` |
| "你改完 UI，就去改后端吧" | ✅ 后端已由 TASK-060/062/064 落地并合入 main | `service/nova_service/{auth,metadb,stats}.py` |

## 2. 当前进展

### 已完成（本分支）

- 首屏门禁三分支：云端+有账户→登录；云端+无账户→**初始化账户**；本地模式→直接进入
- 账户面板：账户资料、索引成功/失败次数、平均耗时、磁盘占用、使用次数与 P95
- API Key 管理、历史记录（索引/使用）、接入指南（三按键）
- 删除全部"未就绪"占位页（原 TASK-070 §E/§F）
- 接入片段与 `npm/README.md` 逐字对齐（`npx nova-client --base-url/--token`）

### 后端（main 已合并，非本分支产出）

`POST /api/auth/{register,login,logout,bootstrap}`、`GET /api/auth/me`、
`GET/POST/DELETE /api/auth/tokens`、`GET /api/meta`、`GET /api/account/overview`、
`GET /api/projects/{id}/index-{runs,stats}`、`GET /api/index-stats`、
`GET /api/usage/{summary,projects/{id}}`。

> ⚠️ **路径纠正**：迁移通知里写的是 `/api/auth/keys`，**实际是 `/api/auth/tokens`**
> （CF-05 与 `service/nova_service/routers/auth.py`）。前端按实际实现对接。

### 验证记录

```console
uv run ruff check .                    # All checks passed!
uv run pytest -o addopts="" -q         # 746 passed, 2 skipped
cd web && npm run lint                 # exit 0
npm run build                          # dist 419 kB（gzip 133 kB）
npx vitest run                         # 29 passed | 4 skipped
# 真服务 e2e（云端模式 8891 + 真实页面）
npx vitest run src/pages/console.e2e.test.tsx   # 2 passed
```

### 未完成 / 已知缺口

1. **没有跑过真浏览器**（无 Playwright）；只做到 jsdom + 真实服务。
2. **CI 未覆盖 e2e**（需起服务造数据，属独立任务）。
3. **TASK-061 租户隔离未落地**：已登录用户能看到服务端全部项目。单人部署无影响，
   **多用户上云前必须先合 TASK-061**。
4. 历史页逐项目拉取明细（跨项目端点只回聚合），项目多时是 N 次请求。

## 3. 未来任务（建议编排者按此派活）

| 优先级 | 任务 | 内容 | 依赖 |
|---|---|---|---|
| **P0** | TASK-061 | 租户双层（token→user→owns project）——多用户上云的前置 | 无 |
| **P1** | TASK-071 | 浏览器验收与视觉打磨：Playwright 冒烟六页；空/错/加载态；窄屏；深色可选 | 无 |
| **P1** | TASK-072 | 用量图表化：索引成功/失败趋势、耗时分位、token 消耗曲线（现为表格） | TASK-062 |
| **P2** | TASK-073 | 跨项目 `index-runs` 明细端点（L2 契约），消除 N 次请求 | 编排者批契约 |
| **P2** | TASK-074 | Playground 可分享 / 历史回放（基于 `query_audit`） | TASK-064 |
| **P2** | TASK-075 | 部署：Caddy 托管 `web/dist` + compose 接入（TASK-063 的一部分） | TASK-063 |
| **P3** | — | 设置页（只读展示 EMBED_*/ANSWER_* 配置状态） | 后端只读端点 |
| **P3** | — | i18n / 移动端专项（当前只保证窄屏不破版） | — |

## 4. 关键约定（接手必读，别踩）

1. **只在工位里干活**：`cd /home/xuwenzheng/github/ACE/nova-lane-b`，不要回主仓库改代码。
2. **文件所有权**：本卡只拥有 `web/**`；**不要碰 `service/**`**（后端会话领地）。
3. **改接入片段要同步两处**：`web/src/app/connect-info.ts` ↔ `npm/README.md`。
4. **不自研 ContextPack 渲染**（D-21/D-40）：只渲染服务端 `markdown`。
5. **统计不美化**：`avgDurationMs` 只统计成功索引；未测量显示 `—`（不填 0）；
   `indexProgress` **不显示百分比**。
6. **e2e 的 cookie 转发器**只在测试内模拟同源 cookie（jsdom 的 undici 不共享 cookie jar），
   **不是**产品代码的缺陷，别去"修"产品代码。
7. 不 push、不切 main、不 force push；改契约走 L2 流程。

## 5. 本地跑起来（2 分钟）

```bash
cd /home/xuwenzheng/github/ACE/nova-lane-b
export NO_PROXY=127.0.0.1,localhost

# 终端 A：本地模式（免账户，最快看到界面）
uv run nova-service local --repo /home/xuwenzheng/github/ACE/nova-lane-b/web --port 8787

# 终端 B
cd web && npm ci && npm run dev     # 打开 http://127.0.0.1:5173
```

要看登录页 / API Key / 面板，用云端形态（需先建首个账户）：

```bash
NOVA_DATA_ROOT=/tmp/nova-ui \
  uv run nova-service serve --port 8891
# 打开 http://127.0.0.1:8891/api/meta 确认 needsBootstrap=true，然后：
curl -c /tmp/c -X POST http://127.0.0.1:8891/api/auth/bootstrap \
  -H 'Content-Type: application/json' -d '{"name":"owner","password":"correct-horse-battery"}'
cd web && NOVA_WEB_API=http://127.0.0.1:8891 npm run dev
```

> ⚠️ 后端默认 embedding 已切到 **Voyage**（`.env.example`：`EMBED_MODEL=voyage-4-lite`），
> 本机没有 Voyage key，因此要索引真实仓库需 `EMBED_MODE=local` 或自备 key；
> 只验证 UI 与账户流程则不需要 embedding。
