# WSL 本地测试环境（与生产同构）

> **读者**：要在本地用**和生产一样的 URL 形态**验证真实 Agent 接入的人。
> **前提**：已按 [`../getting-started/README.md`](../getting-started/README.md) 装好依赖。
> **产出**：`http://localhost/nova-web/` + `http://localhost/nova-service/`（含 `/mcp`）。
>
> **为什么不用 `local` 模式**：`nova-service local --repo <目录>` 是免鉴权的单用户模式，
> 与生产的鉴权链路不同，验证不了接入配置能否平移到生产。

## 1. 与生产的一致性

| 项 | 生产（VPS） | 本地（WSL） |
|---|---|---|
| 前端路径 | `/nova-web/` | 同 |
| 后端路径 | `/nova-service` | 同 |
| 模式 | `serve`（完整鉴权） | 同 |
| 数据根 | `/root/.nova` | `~/.nova/live` |
| 静态托管 | nginx | nginx |
| 访问地址 | `https://<域名>/...` | `http://localhost/...` |

**关键约束**：数据根独立于代码——换代码、重建前端、重启服务都不影响已索引的数据与账户。

**启停策略**：**按需启停，不开机自启**。收到部署指令才 `start`，收到暂停指令就 `stop`。

## 2. 从零到跑起来（五步）

```bash
# ── ① 依赖 ─────────────────────────────────────────
sudo apt-get update && sudo apt-get install -y nginx
sudo systemctl disable nginx          # 关键：关闭开机自启

# ── ② 隐私资产（env 文件）──────────────────────────
tar xzf nova-secrets.tar.gz -C ~/
bash ~/nova-secrets/restore.sh wsl   # 交互式：填用户名/路径/key

# ── ③ nginx + systemd ──────────────────────────────
sudo cp ~/nova-secrets/nginx/nova-wsl.conf /etc/nginx/sites-available/nova-live
sudo ln -sf /etc/nginx/sites-available/nova-live /etc/nginx/sites-enabled/nova-live
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t

sudo cp ~/nova-secrets/systemd/nova-live.service /etc/systemd/system/
sudo systemctl daemon-reload

# ── ④ 前端构建 ─────────────────────────────────────
cd ~/2_github/AI/ACE/nova/web
NOVA_WEB_BASE=/nova-web/ VITE_NOVA_API_BASE=/nova-service npm run build

# ── ⑤ 启动 ─────────────────────────────────────────
sudo systemctl start nova-live nginx
curl -s http://localhost/nova-service/healthz     # {"status":"ok",...}
curl -sI http://localhost/nova-web/ | head -1     # 200
```

## 3. 固定 URL 与端口

```text
http://localhost/nova-web/         前端
http://localhost/nova-service/     后端（含 /mcp）
```

| 端口 | 用途 |
|---|---|
| 80 | nginx（对外唯一入口） |
| 8787 | nova-service（仅监听 `127.0.0.1`，由 nginx 转发） |

## 4. 环境变量文件

位置固定在 `~/.config/nova/live.env`，权限 `0600`。

**这是数据根的配置文件，不是临时变量**：所有服务都从它读配置，写一次就长期有效，
不要用 `export` 在终端里临时设（那样每次都要重设，且换终端就丢）。

```bash
mkdir -p ~/.config/nova
cat > ~/.config/nova/live.env <<'EOF'
NOVA_DATA_ROOT=/home/<USER>/.nova/live
NOVA_LOCAL_RESCAN_INTERVAL=0

EMBED_MODE=api
EMBED_MODEL=voyage-4-lite
EMBED_BASE_URL=https://api.voyageai.com
EMBED_DIM=1024
EMBED_CONCURRENCY=4
EMBED_BATCH_SIZE=500
EMBED_BATCH_TOKEN_BUDGET=300000
EMBED_MAX_INPUT_TOKENS=32000
EMBED_API_KEY=<Voyage key>

ANSWER_BASE_URL=http://<gateway>:8080/v1
ANSWER_MODEL=deepseek-flash
ANSWER_API_KEY=<gateway key>
ANSWER_TIMEOUT_S=120
ANSWER_MAX_TOKENS=16384

ANSWER_PROVIDER=DeepSeek
ANSWER_MAX_CONTEXT_TOKENS=1048576
EMBED_TPM=16000000
EMBED_RPM=2000

# TASK-110 §7.1：首个管理员按**名字**指定（默认值就是 xuwenzheng，显式写出便于换部署人）。
# 服务启动时把它提为 role='admin'；账户不存在时只记一行日志，不报错。
NOVA_ADMIN_NAME=xuwenzheng
EOF
chmod 600 ~/.config/nova/live.env
```

写入后自查（确认文件确实落盘）：

```bash
ls -l ~/.config/nova/live.env
grep -E '^(NOVA_DATA_ROOT|ANSWER_MODEL|NOVA_ADMIN_NAME)=' ~/.config/nova/live.env
```

> 隐私包里已带这份文件的模板与恢复脚本，见
> [`../privacy/资产清单.md`](../privacy/资产清单.md)。

## 5. nginx 站点配置

`/etc/nginx/sites-available/nova-live`（`<USER>` 换成实际用户名）：

```nginx
server {
    listen 80;
    server_name localhost;

    location /nova-web/ {
        alias /home/<USER>/2_github/AI/ACE/nova/web/dist/;
        try_files $uri $uri/ /nova-web/index.html;
    }

    location /nova-service/ {
        proxy_pass http://127.0.0.1:8787/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_read_timeout 300s;
    }
}
```

启用并移除默认站点：

```bash
sudo ln -sf /etc/nginx/sites-available/nova-live /etc/nginx/sites-enabled/nova-live
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
```

## 6. systemd 单元（按需启停，不自启）

`/etc/systemd/system/nova-live.service`：

```ini
[Unit]
Description=nova service (WSL live environment)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/<USER>/2_github/AI/ACE/nova
EnvironmentFile=/home/<USER>/.config/nova/live.env
ExecStart=/home/<USER>/2_github/AI/ACE/nova/.venv/bin/nova-service serve --host 127.0.0.1 --port 8787
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo cp <上面的文件> /etc/systemd/system/nova-live.service
sudo systemctl daemon-reload
```

**不要执行 `systemctl enable`**——那会让它在 WSL 启动时自动运行。启用用 `start`，停止用 `stop`。

## 7. 启动与停止

```bash
# 启动
cd ~/2_github/AI/ACE/nova/web
NOVA_WEB_BASE=/nova-web/ VITE_NOVA_API_BASE=/nova-service npm run build   # 代码有更新才需要
sudo systemctl start nova-live
sudo systemctl start nginx

# 停止（数据保留在 ~/.nova/live，下次启动即恢复）
sudo systemctl stop nova-live
sudo systemctl stop nginx

# 看状态与日志
systemctl status nova-live
journalctl -u nova-live -n 50 --no-pager
```

## 8. 首次初始化与 API Key

仅首次部署需要（之后账户已存在，跳过）：

```bash
curl -s -X POST http://localhost/nova-service/api/auth/bootstrap \
  -H 'Content-Type: application/json' \
  -d '{"name":"admin","password":"<密码>"}' -c /tmp/nova-cookie.txt
```

创建 Agent 用的 API Key（**明文只返回这一次**）：

```bash
curl -s -X POST http://localhost/nova-service/api/auth/tokens \
  -H 'Content-Type: application/json' -b /tmp/nova-cookie.txt \
  -d '{"name":"agent-key"}'
```

## 9. Agent 接入配置

```toml
[mcp_servers.nova]
command = "npx"
args = ["-y", "nova-client", "--base-url", "http://localhost/nova-service", "--token", "<API KEY>"]
startup_timeout_ms = 60000
```

`--base-url` 填**根地址**（`http://localhost/nova-service`），不带 `/mcp`，也不能只写 `http:`。

验证客户端可用：

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
| npx -y nova-client --base-url http://localhost/nova-service --token <API KEY> \
| head -3
```

预期 `tools/list` 返回 `search_context` 与 `ask_project`。
完整接入手册见 [`../getting-started/agent接入与API-Key.md`](../getting-started/agent接入与API-Key.md)。

## 10. 数据持久化边界

```text
~/.nova/live/                  ← 数据（换代码不动它）
├── nova-meta.db               ← 账户 / API Key / 审计 / 项目归属
└── projects/<projectId>/      ← 每个项目的索引

~/.config/nova/live.env        ← 配置（写一次长期有效）
/etc/systemd/system/nova-live.service   ← 启动定义
/etc/nginx/sites-available/nova-live    ← 路由定义
```

- 换代码（`git pull`）、重建前端、重启服务，数据都在；
- 客户端缓存按 endpoint 分片（`~/.cache/nova/<endpointHash>/<projectId>/`），
  换 `--base-url` 会自然作废旧缓存；
- core 的切片或嵌入指纹变化会触发索引重建（首次检索变慢），数据不丢。

### 10.1 索引“看不见了”：projectId 随身份变化

**现象**：重启后项目列表里还在，但检索全部 0 命中，或客户端反复“仓库初始化”。

**根因**：`projectId = sha256(identity_key)`，而 `identity_key` 由
**git remote + 仓库内相对路径 + 分支名**决定（D-29）。“分支名”在 TASK-111 加入后,
**切换分支或用旧索引时代的 checkout 都会算出不同的 projectId**——旧索引还在盘上，
但已经挂不到新身份上。

```bash
# 看当前 checkout 会算出哪个 projectId
uv run python -c "
from nova_core.engine import repo_identity, project_id_for
from pathlib import Path
import sys
i = repo_identity(Path(sys.argv[1])); print(project_id_for(i.identity_key), i.display_name)
" /path/to/repo
```

**处置**：重建索引（这是个可重复的操作，不是故障）：

```bash
# 服务以 root 跑，用 sudo 保证索引目录权限一致
# -E 不可省：ingest 要读 EMBED_* （source 后才能透传）
set -a; source ~/.config/nova/live.env; set +a
export no_proxy='*'
sudo -E .venv/bin/nova-core ingest --repo /path/to/repo \
  --data /home/<USER>/.nova/live --full
```

`--full` 忽略增量、全量重解析 + 重建向量表（实测 langchain 3125 文件 / 20673 切片约 7 分钟）。
完成后服务**无需重启**（每次检索重新打开索引，见 `Engine._open_project`）。

> **为什么不用客户端上传重建**：`nova-core ingest` 直接写服务端数据根，省一轮上传；
> 但注意它**不写客户端缓存**，所以客户端下次调用仍会从零全量上传一遍
> （服务端按 `blobHash` 幂等去重，不会重复索引，但白花一轮时间）。
> 客户端上传一次后缓存就齐了，之后恢复增量。

## 相关文档

- 生产部署 → [`vps.md`](vps.md)
- 密钥与隐私包 → [`../privacy/资产清单.md`](../privacy/资产清单.md)
- 检索改动后的回归 → [`../benchmark/README.md`](../benchmark/README.md)
- 排障 → [`../operations/请求日志与trace-id报错手册.md`](../operations/请求日志与trace-id报错手册.md)
