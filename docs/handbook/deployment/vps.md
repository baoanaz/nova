# VPS 生产部署（公网发布）

> **读者**：要把 zace 发布到公网服务器的人。
> **产出**：`https://<域名>/zace-web/`（前端）+ `https://<域名>/zace-service`（后端 + MCP）。
> **前置**：先读 [`../privacy/资产清单.md`](../privacy/资产清单.md) 准备密钥，
> 再回来看本篇。密钥**绝不入库**。
>
> 本地模拟同一形态 → [`wsl-live.md`](wsl-live.md)（推荐先在本地跑通再来 VPS）。

## 1. 形态与规格

```text
浏览器 / Agent
   │ HTTPS 443
   ▼
nginx（占用 80/443）
   ├── /zace-web/       → 静态 SPA（web/dist）
   └── /zace-service/   → 反向代理 → 127.0.0.1:8787（zace-service）
```

- 规格参考：**2C4G 起步**（TASK-092 实测 2 vCPU / 1.9 GB 可跑）；
- 磁盘：约 10 KB/chunk，单项目 500 MB 上限 ≈ 50,000 chunks；
- **不用 Docker**：VPS 上是 `systemd + nginx + 静态产物`（本机已有 nginx 占用 80/443，
  且内存不足以再叠一层容器运行时）。

## 2. 从零到跑起来（六步）

> 目标：**基于 GitHub 干净代码 + 隐私包 → 生产环境可访问**。
> 隐私包见 [`../privacy/资产清单.md`](../privacy/资产清单.md)。

```bash
# ── ① 代码 ──────────────────────────────────────────
sudo mkdir -p /opt && cd /opt
git clone https://github.com/baoanaz/zace.git
cd /opt/zace

# ── ② 依赖（Python 3.12 + uv；见 ../getting-started/README.md）──
uv sync

# ── ③ 隐私资产（从你保存的包恢复）──────────────────
#    把 zace-secrets.tar.gz 拷到机器上，然后：
tar xzf zace-secrets.tar.gz -C ~/
bash ~/zace-secrets/restore.sh vps      # 交互式：填域名/路径/key

# ── ④ 前端构建 ─────────────────────────────────────
cd /opt/zace/web
ZACE_WEB_BASE=/zace-web/ VITE_ZACE_API_BASE=/zace-service npm run build

# ── ⑤ systemd + nginx ──────────────────────────────
sudo cp ~/zace-secrets/systemd/zace-service.service /etc/systemd/system/
sudo cp ~/zace-secrets/nginx/zace-vps.conf /etc/nginx/sites-available/zace
sudo ln -sf /etc/nginx/sites-available/zace /etc/nginx/sites-enabled/zace
sudo nginx -t && sudo systemctl daemon-reload
sudo systemctl enable --now zace-service nginx

# ── ⑥ 首次初始化（建账户 + API Key）───────────────
curl -s -X POST https://<域名>/zace-service/api/auth/bootstrap \
  -H 'Content-Type: application/json' \
  -d '{"name":"admin","password":"<强密码>"}' -c /tmp/cookie.txt
```

## 3. 环境变量（`/etc/zace/zace.env`）

权限必须是 `0600`（含 key 明文）。完整键名与取值见
[`../privacy/资产清单.md`](../privacy/资产清单.md) §3（密钥种类与来源），此处是生产取值：

```bash
# ---- 数据根（持久化；只要这个目录不动，换代码不丢数据）
ZACE_DATA_ROOT=/root/.zace

# ---- 前端/后端公开路径（构建 web 时要用同一份 ZACE_WEB_BASE）
ZACE_WEB_BASE=/zace-web/
VITE_ZACE_API_BASE=/zace-service

# ---- 向量（Voyage）
EMBED_MODE=api
EMBED_MODEL=voyage-4-lite
EMBED_BASE_URL=https://api.voyageai.com
EMBED_DIM=1024
EMBED_CONCURRENCY=4
EMBED_BATCH_SIZE=500
EMBED_BATCH_TOKEN_BUDGET=300000
EMBED_MAX_INPUT_TOKENS=32000
EMBED_API_KEY=<你的 Voyage key>

# ---- 向量阶段消费者（TASK-115；不写也是 2，写出来便于留档与回滚）
# 窗口 = EMBED_BATCH_SIZE × EMBED_CONCURRENCY = 2000；K 个消费者同时在处理 K 个窗口。
ZACE_EMBED_WORKERS=2

# ---- LLM（OpenAI-compatible 网关）
ANSWER_BASE_URL=http://<网关地址>:8080/v1
ANSWER_MODEL=deepseek-flash
ANSWER_API_KEY=<你的网关 key>
ANSWER_TIMEOUT_S=120
ANSWER_MAX_TOKENS=16384

# ---- 展示用元数据（设置页「服务模型」卡；不参与任何调优）
ANSWER_PROVIDER=DeepSeek
ANSWER_MAX_CONTEXT_TOKENS=1048576
EMBED_TPM=16000000
EMBED_RPM=2000
```

> **按机器留档的调优档在 [`configs/profiles/`](../../../configs/profiles/README.md)**：
> 每个机器一份 `<机器标识>.env`（现役 VPS = `154.12.34.214.env`，计划中的 DMIT = `dmit-2c2g-200m.env`），
> 只放非密钥调优项（批大小 / 并发 / 消费者数）与 systemd 内存护栏留档；
> 用法与"窗口不能按消费者切小"的告警见该目录 README。

**三个必须知道的点**：

- **不设 `ZACE_LOCAL_MODE`**：`serve` 始终走完整鉴权；只有显式
  `zace-service local --repo <目录>` 才是免鉴权的单用户模式（R34）。
- **`ANSWER_MODEL` 必须显式给**：代码不写模型默认值——同一个名字在不同网关可以是
  不同模型，"用哪个"属部署决策。
- `ANSWER_MAX_CONTEXT_TOKENS` / `EMBED_TPM` / `EMBED_RPM` 只用于设置页展示；
  不配就显示 `—`（**不猜**）。

## 4. systemd 单元

`/etc/systemd/system/zace-service.service`（模板在隐私包 `systemd/`）：

```ini
[Unit]
Description=zace service
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/zace
EnvironmentFile=/etc/zace/zace.env
ExecStart=/opt/zace/.venv/bin/zace-service serve --host 127.0.0.1 --port 8787
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

只绑 `127.0.0.1`：对外由 nginx 终止 TLS 并转发，service 自身不直接暴露公网。

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now zace-service
systemctl status zace-service
```

## 5. 前端构建

```bash
cd /opt/zace/web
ZACE_WEB_BASE=/zace-web/ VITE_ZACE_API_BASE=/zace-service npm run build
# 产物：web/dist → 交给 nginx 的 /zace-web/ 站点根
```

两个变量必须与 nginx 的公开路径一致：`ZACE_WEB_BASE` 决定产物里的资源 URL 与
Router `basename`，`VITE_ZACE_API_BASE` 决定前端请求后端时的前缀。

> **踩过的坑**：漏掉 `ZACE_WEB_BASE=/zace-web/` 会让产物里的资源路径变成 `/assets/...`，
> nginx 找不到就回落到默认站点 → **前端白屏**。

## 6. nginx

```nginx
location /zace-web/ {
    alias /opt/zace/web/dist/;
    try_files $uri $uri/ /zace-web/index.html;   # SPA 深层路由回退
}
location /zace-service/ {
    proxy_pass http://127.0.0.1:8787/;           # 尾斜杠：剥掉 /zace-service 前缀
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_http_version 1.1;
    proxy_read_timeout 300s;                     # ask 会调用 LLM，需长于其超时
}
```

`proxy_pass` 末尾的斜杠**不可少**：没有它，后端会收到带 `/zace-service` 前缀的路径而 404。

## 7. 首次初始化

`bootstrap` 只在 `users` 表为空时可用（原子初始化，避免并发部署产生多个首任账户）；
之后用注册/登录。

```bash
curl -s -X POST https://<域名>/zace-service/api/auth/bootstrap \
  -H 'Content-Type: application/json' \
  -d '{"name":"admin","password":"<强密码>"}' -c /tmp/zace-cookie.txt
```

创建 Agent 用的 API Key（**明文只返回这一次**）：

```bash
curl -s -X POST https://<域名>/zace-service/api/auth/tokens \
  -H 'Content-Type: application/json' -b /tmp/zace-cookie.txt \
  -d '{"name":"agent-key"}'
```

## 8. 验证清单

```bash
curl -s https://<域名>/zace-service/healthz          # {"status":"ok",...}
curl -s https://<域名>/zace-service/api/meta         # needsBootstrap=false
curl -sI https://<域名>/zace-web/ | head -1          # 200
```

端到端（真实 MCP 客户端）：

```toml
[mcp_servers.zace]
command = "npx"
args = ["-y", "zace-client", "--base-url", "https://<域名>/zace-service", "--token", "<API KEY>"]
startup_timeout_ms = 60000
```

`--base-url` 填**根地址**（不带 `/mcp`），client 会自己拼 MCP 路径。

> **client 版本**：`npx zace-client` 会按 npm 上的 latest 取包；二进制由**同版本的 npm 平台
> 子包**（`zace-client-<os>-<arch>`）随包提供，**无网络下载步骤**。
> **发新版本**（含工具描述变更）请看 [`../release/README.md`](../release/README.md)——
> 尤其是“先发 6 个子包、再发主包，缺一个平台就是静默故障”这条硬约束。

## 9. 备份与升级

```bash
# 备份：只要这两个（代码不含数据）
tar czf zace-backup-$(date +%F).tgz -C /root/.zace projects zace-meta.db

# 升级：换代码 → 重建 web → 重启 service；数据根不动
cd /opt/zace && git pull && uv sync
cd web && ZACE_WEB_BASE=/zace-web/ VITE_ZACE_API_BASE=/zace-service npm run build
sudo systemctl restart zace-service
```

**注意**：core 的切片/嵌入指纹变化（如 parser 版本升级）会触发索引重建，
表现为首次检索变慢（`PARSER_CONFIG_VERSION` 变更时全量重解析）。数据不丢，但需要等重建完成。

**换分支或换 checkout 后索引失效**（projectId 随“remote + 仓库内路径 + 分支名”变化）：
现象是项目还在但检索 0 命中。重建方式与实测耗时见
[`wsl-live.md` §10.1](wsl-live.md#101-索引看不见了projectid-随身份变化)。

## 10. 常见故障

| 现象 | 原因与处置 |
|---|---|
| `/zace-service/api/...` 404 | `proxy_pass` 少了尾斜杠，前缀没被剥掉 |
| 前端白屏、资源 404 | 构建时 `ZACE_WEB_BASE` 与 nginx 的 `location` 前缀不一致 |
| 深层路由刷新 404 | 缺 `try_files ... /zace-web/index.html` |
| `ask` 返回"证据不足" | `answerable=false` 按 D-24 短路，**不调 LLM**；换更具体的符号/路径重问 |
| `ask` 返回"总结模型不可用" | `ANSWER_*` 配置错或网关不可达；`/api/meta` 的 `config.llm.configured` 可确认 |
| `needsBootstrap` 恒 true | `zace-meta.db` 不可写，或 `ZACE_DATA_ROOT` 指向了临时目录 |
| 设置页模型项显示 `—` | 未配 `ANSWER_MAX_CONTEXT_TOKENS` / `EMBED_TPM` / `EMBED_RPM`（属展示项，不影响功能） |
| 内网请求被代理劫持 | 设了 `http_proxy` 但没绕过：`export no_proxy='*'` |

## 相关文档

- 密钥与隐私包 → [`../privacy/资产清单.md`](../privacy/资产清单.md)
- 本地同构环境（推荐先跑） → [`wsl-live.md`](wsl-live.md)
- Agent 接入手册 → [`../getting-started/agent接入与API-Key.md`](../getting-started/agent接入与API-Key.md)
- 请求排障 → [`../operations/请求日志与trace-id报错手册.md`](../operations/请求日志与trace-id报错手册.md)
