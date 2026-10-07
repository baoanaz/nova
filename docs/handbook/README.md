# zace 手册（Handbook）

> 面向**做事**的文档：怎么装、怎么部署、怎么验证、怎么排障。
> 设计意图与决策在 [`../design/INDEX.md`](../design/INDEX.md)；任务卡在 [`../tasks/`](../tasks/)。
>
> **新机器请从这里开始**：先看 §1 的"从零到跑起来"，再按你的目标进分节。

## 1. 从零到跑起来（四步）

```text
① 代码    git clone https://github.com/baoanaz/zace.git
② 依赖    scripts/setup-dev.sh（清单与版本见根目录 ENVIRONMENT.txt）
③ 隐私    privacy/资产清单.md  （key / env / token，不能进 git）
④ 部署    deployment/ 对应篇章（VPS 或 WSL）
```

## 2. 按目标查找

| 我要做什么 | 看哪篇 |
|---|---|
| **安装完整开发环境** | [ENVIRONMENT.txt](../../ENVIRONMENT.txt) |
| **安装依赖、本地把 core 跑起来** | [getting-started/README.md](getting-started/README.md) |
| 配 embedding（Voyage / 硅基流动） | [getting-started/cloud-embedding.md](getting-started/cloud-embedding.md) |
| 让编辑器 / Agent 接入（MCP） | [getting-started/agent接入与API-Key.md](getting-started/agent接入与API-Key.md) |
| 本地单用户模式验收（M2a） | [getting-started/M2a-验收手册.md](getting-started/M2a-验收手册.md) |
| **发布到公网 VPS** | [deployment/vps.md](deployment/vps.md) |
| **本地跑一套跟生产同构的环境** | [deployment/wsl-live.md](deployment/wsl-live.md) |
| **发新版本 / 重建前端 / 重启服务** | [release/README.md](release/README.md) |
| **发布 npm 包（入口 / 流水线 / 排障）** | [release/npm.md](release/npm.md) |
| **改检索/排序后跑跨仓库回归** | [benchmark/README.md](benchmark/README.md) |
| **准备密钥与隐私资产** | [privacy/资产清单.md](privacy/资产清单.md) |
| 切换 embedding 参数 / 看实测数字 | [operations/embedding-provider切换.md](operations/embedding-provider切换.md) |
| **按机器留档/切换批参数、并发与消费者数（换 VPS）** | [../../configs/profiles/README.md](../../configs/profiles/README.md) |
| **换到新 VPS：量硬件上限与冷启动（上机三步 + 提示词）** | [perf/README.md](perf/README.md) |
| 让 `.gitignore` 排除的文档也能被索引 | [operations/索引白名单.md](operations/索引白名单.md) |
| 查一个 trace id / 看请求日志 | [operations/请求日志与trace-id报错手册.md](operations/请求日志与trace-id报错手册.md) |

**旧链接**：[部署指南.md](部署指南.md)（三章合集的跳转页，2026-09-15 已拆分）。

## 3. 分节说明

```text
handbook/
├── getting-started/   装依赖 → 配 key → 接入 Agent（本地第一次跑通）
├── deployment/        部署到真实环境（VPS 生产 / WSL 同构验证）
├── release/           发一次新版本（npm + Release 六平台 + 重启生效）
├── benchmark/         改动 core 检索后的回归测试台
├── perf/              换机/上新机：量硬件上限与冷启动基线（新增 2026-10-05）
├── privacy/           密钥与环境变量的统一管理（新增 2026-09-15）
└── operations/        日常运维：调参、排障、日志
```

### getting-started/

| 文档 | 内容 | 适用版本 |
|---|---|---|
| [README.md](getting-started/README.md) | 依赖、目录、第一次跑 core | 现行 |
| [cloud-embedding.md](getting-started/cloud-embedding.md) | 云端 embedding key 怎么配（含 `NO_PROXY` 坑） | 现行 |
| [agent接入与API-Key.md](getting-started/agent接入与API-Key.md) | API Key + `npx zace-client` 端到端 | 2026-09-14 实测 |
| [M2a-验收手册.md](getting-started/M2a-验收手册.md) | 本地单用户模式的验收流程（0-9 节 + §10 冒烟） | M2a-2 |

### deployment/

| 文档 | 读者 | 产出 |
|---|---|---|
| [vps.md](deployment/vps.md) | 发布到公网的人 | `https://<域名>/zace-web/` |
| [wsl-live.md](deployment/wsl-live.md) | 要本地同构验证的人 | `http://localhost/zace-web/` |

按需启停（**不设开机自启**）：收到部署指令 `systemctl start`，收到暂停指令 `stop`。

### release/

| 文档 | 内容 |
|---|---|
| [README.md](release/README.md) | **发一次新版本**：版本号一致 → tag → 重建前端 / 重启服务 / VPS 部署；含顺序与坑 |
| [npm.md](release/npm.md) | **npm 发布专篇（唯一 SOP，D-48/D-49）**：入口 `bash scripts/release-client.sh x.y.z`、6 平台子包架构、CI 自动发布流水线、必要约束与最小排障 |

### benchmark/

[README.md](benchmark/README.md) —— **改了 `core/zace_core/{retrieval,contextpack}/` 必须跑**。
默认公共靶场的版本、质量护栏与重建条件都在里面；内部资料只通过仓库外的私有清单显式加载。

### perf/

换到新机器（新 VPS）时量本地处理、CPU、直连网络及真实 API 冷启动，
外加 DoD 回填与可直接复制的 AI 提示词。

| 文档 | 内容 |
|---|---|
| [README.md](perf/README.md) | 固定版本与可比基线、免费硬件测试、付费 API 测试、指标与留档 |
| [PROMPT.md](perf/PROMPT.md) | 四段可整段复制的提示词（P0 自检 → P1 免费基线 → P2 真实冷启动 → P3 报告回填） |

### privacy/

[资产清单.md](privacy/资产清单.md) —— 密钥种类、来源、存放位置、权限要求、轮转步骤。
配套脚本：`scripts/pack-secrets.sh`（打包）+ 包内 `restore.sh`（恢复）。

### operations/

| 文档 | 内容 |
|---|---|
| [embedding-provider切换.md](operations/embedding-provider切换.md) | Voyage ↔ 硅基流动切换与实测数字 |
| [索引白名单.md](operations/索引白名单.md) | 让被 `.gitignore` 排除的 AI 文档也进索引 |
| [请求日志与trace-id报错手册.md](operations/请求日志与trace-id报错手册.md) | 用 trace id 定位一次请求 |

## 4. 文档地图（全仓库）

| 位置 | 放什么 | 谁维护 |
|---|---|---|
| `docs/handbook/`（本篇） | 怎么做（操作指南） | 实施 AI |
| `docs/design/` | 设计成什么样（架构 + 决策登记） | 编排者 |
| `docs/contracts/` | **冻结契约**（改要走流程，见 PROCESS.md） | 编排者 |
| `docs/plan/` | 编排、路线图、工作区规程 | 编排者 |
| `docs/tasks/` | 任务卡（活跃）+ `archive/`（已完成） | 实施 AI 回填 |
| `docs/archive/` | 历史证据（含截图与脚本） | 只读 |

## 5. 已知过期/历史内容

| 内容 | 状态 | 说明 |
|---|---|---|
| [M2a-验收手册.md](getting-started/M2a-验收手册.md) | 部分过期 | 适用 M2a-2；后续形态变化见部署篇 |
| [cloud-embedding.md](getting-started/cloud-embedding.md) | 部分过期 | 主路径已从硅基流动改为 Voyage，见文件头部说明 |
| `docs/archive/evidence-task-098/` | 历史证据 | WebUI 复古风改版的截图与脚本，`review` 状态留存 |
| `docs/evidence/task-051-cloud-mcp-readiness.md` | 历史报告 | 2026-09-13 的只读诊断（被测版本 `main@42587cf`） |

设计类历史见 [`../design/Background/`](../design/Background/)（外部项目调研）。

## 6. 维护约定

- 改文档后：确认交叉链接仍有效（`grep -rn 'handbook/'` 自查）；
- 新增手册：放进对应分节，并在 §2 表格登记；
- 拆分/移动文档：**保留旧路径为跳转页**（避免外部链接与任务卡引用失效）；
- 版本敏感内容：在文头写"适用版本"；
- 过期但不删：移到 `docs/archive/` 或在本篇 §5 登记。
