# 发布与重启（nova-client npm 包 / 服务 / 前端）

> **读者**：要发一次新版本、或改完代码要让环境生效的人。
> **产出**：npm 上新的 `nova-client@<版本>`，以及跑着新代码的服务与前端。

## 0. 一条命令看懂全流程

```text
bash scripts/release-client.sh 0.0.8（改版本号 → 提交 → 推 main → 打 tag）
                                          ↓
            CI: 构建 6 平台 → 发 6 个子包 → 验证 → 发主包(next) → 冒烟 → promote latest
                                          ↓
                              VPS 部署（拉代码 + 重启服务）
```

> npm 发布细节 → [`npm.md`](npm.md)。

**唯一硬约束**：发布 npm 时**先子包、后主包**。主包的 `optionalDependencies`
指向 6 个平台子包，子包没先上去，该平台用户就装不上。CI 里由
`scripts/make-platform-packages.py publish` 保证这个顺序。

## 1. 发布 nova-client（npm）

```bash
bash scripts/release-client.sh 0.0.8
```

这是唯一入口，脚本只做本地准备并 push tag，六平台构建与 npm 发布全部
由 GitHub Actions 完成。完整说明见 [`npm.md`](npm.md)。


## 2. 重建前端

```bash
cd web
NOVA_WEB_BASE=/nova-web/ VITE_NOVA_API_BASE=/nova-service npm run build
# 产物：web/dist → nginx 的 /nova-web/ 站点根
```

两个变量必须与 nginx 公开路径一致：`NOVA_WEB_BASE` 决定产物资源 URL 与 Router `basename`，
`VITE_NOVA_API_BASE` 决定前端请求后端的前缀。

> 漏掉 `NOVA_WEB_BASE=/nova-web/` → 产物资源路径变成 `/assets/...` → nginx 回落默认站点 → **前端白屏**。

## 3. 重启服务

```bash
sudo systemctl restart nova-live      # WSL 同构环境
# sudo systemctl restart nova-service # VPS 生产

# 确认
systemctl status nova-live --no-pager | head -3
curl -s http://localhost/nova-service/healthz
curl -sI http://localhost/nova-web/ | head -1
```

**重启后前几秒可能 502**（进程在初始化）：等 3–5 秒再 curl。
`healthz` 返回 `{"status":"ok",...}` 即为就绪。

## 4. 顺序与坑

| 场景 | 顺序 |
|---|---|
| 只改了后端/前端代码 | 重建前端（若涉及）→ 重启服务 |
| 只发了 client | 打 tag → 等 CI 发布 7 个 npm 包 → 服务**不用重启** |
| 改了 core 检索逻辑 | 跑 `benchmark/README.md` 的回归 → 重建前端 → 重启服务 |
| 改了工具 description | 改 `docs/contracts/mcp-tools.json` → **同步 `client/contract/`** → 发 client |
| 改了平台表（新增/删平台） | 改 `make-platform-packages.py` 的 `PLATFORMS` → `generate` → 同步 CI 矩阵 → `check-version.sh` |

| 现象 | 原因 |
|---|---|
| **某平台用户报「没有二进制」** | 该平台**子包没发或版本不一致**。这是**静默故障**（npm 跳过解析不了的可选依赖）；跑 `bash scripts/check-version.sh` 定位 |
| `npx` 仍是旧描述/旧行为 | 该版本没发，或 npx 缓存了旧包（配置里加 `--prefer-online`） |
| npm 报 `cannot publish over` | 该版本号已发布过，换 patch 版本号 |

## 相关文档

- 密钥与隐私 → [`../privacy/资产清单.md`](../privacy/资产清单.md)
- 部署（VPS / WSL）→ [`../deployment/vps.md`](../deployment/vps.md)、[`../deployment/wsl-live.md`](../deployment/wsl-live.md)
- Agent 接入 → [`../getting-started/agent接入与API-Key.md`](../getting-started/agent接入与API-Key.md)
- 改动检索后回归 → [`../benchmark/README.md`](../benchmark/README.md)
