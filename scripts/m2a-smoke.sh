#!/usr/bin/env bash
# nova M2a 一键冒烟：起服务 → 等索引 → 调 MCP → 断言返回里有「文件:行号」。
#
# 用法（详见 docs/handbook/getting-started/M2a-验收手册.md §10）：
#   bash scripts/m2a-smoke.sh --repo /path/to/repo [--data-root /tmp/nova-smoke] [--port 8799]
#                             [--query "…"] [--keep] [--timeout 1800] [--api-key-env VAR]
#
# 本脚本把本环境的两个坑固化下来（见 docs/plan/phase2-m2b-w6.md §2.4 F3 与手册 §4.2）：
#   1. key 注入：~/.bashrc 的 export 只对交互式 shell 生效，脚本/子 AI 是**非交互**进程，
#      拿不到 nova_embeding_API_KEY。本脚本按下面的顺序自己找 key（找不到就明确报错并给出解决命令）；
#   2. NO_PROXY=127.0.0.1,localhost：本机有 http_proxy，否则客户端会走代理连本机端口而失败。
#
# 它**不修改被索引的仓库**，也**不写入 key**（只从环境/文件读取，不落盘、不回显）。
set -euo pipefail

# --------------------------------------------------------------------------
# 默认值（可用 CLI 覆盖；不硬编码任何本机绝对路径为唯一选项）
# --------------------------------------------------------------------------
REPO=""
DATA_ROOT="${NOVA_SMOKE_DATA_ROOT:-/tmp/nova-smoke}"
PORT="${NOVA_SMOKE_PORT:-8799}"
HOST="127.0.0.1"
QUERY="${NOVA_SMOKE_QUERY:-令牌过期后在哪里刷新？}"
TIMEOUT_S="${NOVA_SMOKE_TIMEOUT:-1800}"     # 等索引的上限（秒）；超时如实报当前进度
POLL_S="${NOVA_SMOKE_POLL:-5}"              # 轮询间隔（秒）
MAX_RETRIES="${NOVA_SMOKE_MAX_RETRIES:-3}"  # 被限流（429）中断后的重试次数
RETRY_WAIT_S="${NOVA_SMOKE_RETRY_WAIT:-90}" # 每次重试前的等待（秒），让 TPM 窗口过去
KEEP=0
KEY_VAR="${NOVA_SMOKE_KEY_VAR:-nova_embeding_API_KEY}"   # 从 ~/.bashrc 提取的变量名
BASHRC="${NOVA_SMOKE_BASHRC:-$HOME/.bashrc}"
ENV_FILE="${NOVA_SMOKE_ENV_FILE:-.env}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_LOG=""
SERVICE_PID=""

log()  { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

usage() {
  cat <<'EOF'
nova M2a 一键冒烟：起服务 → 等索引 → 调 MCP → 断言返回里有「文件:行号」。

用法（详见 docs/handbook/getting-started/M2a-验收手册.md §10）：
  bash scripts/m2a-smoke.sh --repo /path/to/repo [--data-root /tmp/nova-smoke] [--port 8799]
                            [--query "…"] [--keep] [--timeout 1800]

本脚本把本环境的两个坑固化下来（w6 §2.4 F3 与手册 §4.2）：
  1. key 注入：~/.bashrc 的 export 只对交互式 shell 生效，脚本/子 AI 是**非交互**进程，
     拿不到 nova_embeding_API_KEY。本脚本自己按顺序找 key（找不到就报错并给出解决命令）；
  2. NO_PROXY=127.0.0.1,localhost：本机有 http_proxy，否则客户端会走代理连本机端口而失败。

它**不修改被索引的仓库**，也**不写入 key**（只从环境/文件读取，不落盘、不回显）。
EOF
  cat <<'EOF'

参数：
  --repo PATH        要索引并查询的仓库绝对路径（必填）
  --data-root PATH   数据根（默认 /tmp/nova-smoke；不要指向被索引仓库内部）
  --port N           服务端口（默认 8799）
  --query TEXT       冒烟查询（默认「令牌过期后在哪里刷新？」；换成你仓库里的真实问题更好）
  --timeout N        等索引的上限秒数（默认 1800）
  --poll N           轮询间隔秒数（默认 5）
  --max-retries N    被 429 限流中断后的重试次数（默认 3；0 = 不重试）
  --retry-wait N     每次重试前等待秒数（默认 90，让 TPM 窗口过去）
  --keep             结束后不删数据根、不停服务（调试用）
  --key-var NAME     从 ~/.bashrc 提取的 key 变量名（默认 nova_embeding_API_KEY）
  --env-file PATH    优先读取的 .env 文件（默认 ./.env；不存在则跳过）
  -h, --help         显示本帮助

key 的来源顺序（第一个非空者胜）：
  1) 已 export 的 EMBED_API_KEY（推荐：调用方自己注入）
  2) --env-file 指定的 .env 里的 EMBED_API_KEY=
  3) ~/.bashrc 里的 --key-var 变量（非交互 shell 拿不到，所以由脚本代为提取）

退出码：0 冒烟通过 ｜ 1 断言失败/超时 ｜ 2 参数或前置条件错误
EOF
}

# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)      REPO="${2:-}"; shift 2 ;;
    --data-root) DATA_ROOT="${2:-}"; shift 2 ;;
    --port)      PORT="${2:-}"; shift 2 ;;
    --query)     QUERY="${2:-}"; shift 2 ;;
    --timeout)   TIMEOUT_S="${2:-}"; shift 2 ;;
    --poll)      POLL_S="${2:-}"; shift 2 ;;
    --max-retries) MAX_RETRIES="${2:-}"; shift 2 ;;
    --retry-wait)  RETRY_WAIT_S="${2:-}"; shift 2 ;;
    --key-var)   KEY_VAR="${2:-}"; shift 2 ;;
    --env-file)  ENV_FILE="${2:-}"; shift 2 ;;
    --keep)      KEEP=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *) warn "未知参数：$1（用 --help 看用法）"; exit 2 ;;
  esac
done

if [ -z "$REPO" ]; then
  warn "[错误] 缺少 --repo。示例：bash scripts/m2a-smoke.sh --repo /home/me/hello-agents"
  exit 2
fi
if [ ! -d "$REPO" ]; then
  warn "[错误] --repo 不是目录：$REPO"
  exit 2
fi
REPO="$(cd "$REPO" && pwd)"
case "$DATA_ROOT" in
  "$REPO"/*|"$REPO") warn "[错误] --data-root 不能落在被索引仓库内部：$DATA_ROOT"; exit 2 ;;
esac
if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then warn "[错误] --port 必须是整数：$PORT"; exit 2; fi

# --------------------------------------------------------------------------
# 0. key 解析（F3：非交互 shell 拿不到 ~/.bashrc 的 export）
# --------------------------------------------------------------------------
find_key() {
  if [ -n "${EMBED_API_KEY:-}" ]; then
    printf '%s' "$EMBED_API_KEY"; return 0
  fi
  if [ -f "$ENV_FILE" ]; then
    local from_env
    from_env="$(sed -n 's/^[[:space:]]*EMBED_API_KEY=//p' "$ENV_FILE" | tail -1 | tr -d '"' | tr -d "'")"
    if [ -n "$from_env" ]; then printf '%s' "$from_env"; return 0; fi
  fi
  if [ -f "$BASHRC" ]; then
    local from_rc
    from_rc="$(sed -n "s/^export ${KEY_VAR}=//p" "$BASHRC" | tail -1 | tr -d '"' | tr -d "'")"
    if [ -n "$from_rc" ]; then printf '%s' "$from_rc"; return 0; fi
  fi
  return 1
}

if ! EMBED_API_KEY="$(find_key)"; then
  warn "[错误] 找不到 embedding API key（云端索引需要）。"
  warn "  已尝试：① \$EMBED_API_KEY 环境变量 ② $ENV_FILE 里的 EMBED_API_KEY= ③ $BASHRC 里的 \$$KEY_VAR"
  warn "  解决（任选其一）："
  warn "    1) 显式注入：  export EMBED_API_KEY=sk-xxxx ; bash scripts/m2a-smoke.sh --repo $REPO"
  warn "    2) 写 .env：  cp .env.example .env  # 然后填 EMBED_API_KEY="
  warn "    3) 确认变量名/位置： grep -n '${KEY_VAR}' $BASHRC"
  warn "  注意：~/.bashrc 的 export 只对交互式 shell 生效；脚本/子进程是非交互的，拿不到它。"
  exit 2
fi
# 必须 export：nova-service 是子进程，只从**环境**读 EMBED_API_KEY。
# （实测踩过：只做 shell 赋值 `VAR=$(...)` 而不 export，服务会收到空 key 并以 HTTP 401 失败。
export EMBED_API_KEY
if [ "${EMBED_API_KEY:0:5}" = "sk-xx" ] || [ "${EMBED_API_KEY}" = "xxx" ]; then
  warn "[错误] EMBED_API_KEY 看起来还是占位符（未填真实 key）。请在 --env-file 或环境里给它真实值。"
  exit 2
fi
log "[1/5] key 已就绪（来源已解析，长度 ${#EMBED_API_KEY}，不回显内容）"

# 本机有 http_proxy 时必须让 127.0.0.1 直连（手册 §4.2 的 WSL 坑）。
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"

# 云端 embedding 配置：必须显式 export 给 nova-service 子进程（D-44 的代码默认仍是 local）。
# 可用环境变量覆盖；默认值 = W6 拍板的硅基流动 bge-m3。
export EMBED_MODE="${EMBED_MODE:-api}"
export EMBED_MODEL="${EMBED_MODEL:-BAAI/bge-m3}"
export EMBED_DIM="${EMBED_DIM:-1024}"
export EMBED_MAX_INPUT_TOKENS="${EMBED_MAX_INPUT_TOKENS:-8192}"
export EMBED_BASE_URL="${EMBED_BASE_URL:-https://api.siliconflow.cn}"
# 保守批次：API 默认 64，但 W6 实测（hello-agents 全量）批次大时会撞 TPM 限流（429），
# 而当前实现下一次 429 就会让整次 ingest 失败（TASK-046 §D 的 F4）。4 是本机实测跑通全量的值。
export EMBED_BATCH_SIZE="${EMBED_BATCH_SIZE:-4}"

# --------------------------------------------------------------------------
# 1. 起服务（local 模式：绑仓库 + 后台索引）
# --------------------------------------------------------------------------
mkdir -p "$DATA_ROOT"
SERVICE_LOG="$DATA_ROOT/nova-smoke-service.log"
log "[2/5] 起服务：nova-service local --repo $REPO --data-root $DATA_ROOT --port $PORT"
cd "$REPO_ROOT"
uv run nova-service local --repo "$REPO" --data-root "$DATA_ROOT" --port "$PORT" \
  >"$SERVICE_LOG" 2>&1 &
SERVICE_PID=$!

cleanup() {
  if [ -n "$SERVICE_PID" ] && kill -0 "$SERVICE_PID" 2>/dev/null; then
    kill "$SERVICE_PID" 2>/dev/null || true
    wait "$SERVICE_PID" 2>/dev/null || true
  fi
  # 泳道 C 纪律：脚本只碰 data-root，绝不动被索引的仓库。
  if [ "$KEEP" -eq 0 ]; then
    rm -rf "$DATA_ROOT"
    log "[5/5] 已清理数据根：$DATA_ROOT（--keep 可保留）"
  else
    log "[5/5] --keep：保留数据根 $DATA_ROOT 与服务日志"
  fi
}
trap cleanup EXIT

# 等服务端口就绪（最多 60s）
ready=0
for _ in $(seq 1 60); do
  if ! kill -0 "$SERVICE_PID" 2>/dev/null; then
    warn "[错误] 服务进程已退出，见日志：$SERVICE_LOG"
    tail -20 "$SERVICE_LOG" >&2 || true
    exit 1
  fi
  if curl -s --noproxy '*' "http://$HOST:$PORT/healthz" >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  warn "[错误] 服务 60s 内未就绪（$HOST:$PORT）。日志尾部："
  tail -20 "$SERVICE_LOG" >&2 || true
  exit 1
fi

PROJECT_ID=""
for _ in $(seq 1 30); do
  PROJECT_ID="$(curl -s --noproxy '*' "http://$HOST:$PORT/api/projects" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["projectId"] if d else "")' 2>/dev/null || true)"
  [ -n "$PROJECT_ID" ] && break
  sleep 1
done
if [ -z "$PROJECT_ID" ]; then
  warn "[错误] 服务已起但拿不到 projectId（GET /api/projects 为空）。日志尾部："
  tail -20 "$SERVICE_LOG" >&2 || true
  exit 1
fi
log "        projectId=$PROJECT_ID ｜ 服务日志：$SERVICE_LOG"

# --------------------------------------------------------------------------
# 2. 等索引（轮询 /api/projects/{id} 直到 state=done；带上限，超时如实报进度）
# --------------------------------------------------------------------------
log "[3/5] 等索引完成（上限 ${TIMEOUT_S}s，每 ${POLL_S}s 轮询一次；被 429 中断时最多重试 ${MAX_RETRIES} 次）"
elapsed=0
retries=0
log_offset=0          # 已检查过的服务日志行数（只对新出现的行判 429，避免重复触发重试）
state=""
while :; do
  # 不伪造百分比：只报服务端 indexProgress 的真实字段（手册 §2 的口径）。
  read -r state processed total <<<"$(curl -s --noproxy '*' "http://$HOST:$PORT/api/projects/$PROJECT_ID" \
    | python3 -c 'import json,sys
d=json.load(sys.stdin); p=d.get("indexProgress") or {}
print(p.get("state","?"), p.get("processedFiles","?"), p.get("totalFiles","?"))' 2>/dev/null || echo "? ? ?")"
  case "$state" in
    done) log "        索引完成：state=done，本次解析 $processed/$total 个文件（无改动时 processed=0 属正常）"; break ;;
    failed)
      # 本机实测：provider 有 TPM 限流，而当前实现（TASK-046 §D 的 F4，泳道 B 在修）下一次 429
      # 会让**整次 ingest 失败**（chunks 已入库但 vectors=0）。这里做有界重试：等一个 TPM 窗口后
      # POST /rescan 续跑——不碰 core，也不改靶场。
      new_log="$(tail -n "+$((log_offset + 1))" "$SERVICE_LOG" 2>/dev/null || true)"
      log_offset=$(wc -l <"$SERVICE_LOG" 2>/dev/null || echo "$log_offset")
      # 用 here-string 而非 `printf | grep -q`：grep -q 命中即退会让 printf 吃到 SIGPIPE，
      # 在 pipefail 下整条管道返回非 0，导致“明明匹配了却当成失败”。
      if grep -q 'HTTP 429\|rate limiting' <<<"$new_log" && [ "$retries" -lt "$MAX_RETRIES" ]; then
        retries=$((retries + 1))
        warn "        索引被限流中断（TPM 429）——第 $retries/$MAX_RETRIES 次重试：等 ${RETRY_WAIT_S}s 后 POST /rescan 续跑…"
        sleep "$RETRY_WAIT_S"
        elapsed=$((elapsed + RETRY_WAIT_S))
        if ! curl -s --noproxy '*' -X POST "http://$HOST:$PORT/api/projects/$PROJECT_ID/rescan" >/dev/null; then
          warn "        rescan 请求失败（服务可能已退出）；见 $SERVICE_LOG"
        fi
        continue
      fi
      warn "[错误] 索引失败（state=failed）。"
      if grep -q 'HTTP 401\|ApiAuthError' <<<"$new_log"; then
        warn "  原因：embedding 鉴权失败（401）——子进程没拿到有效 key。"
        warn "  下一步：export EMBED_API_KEY=sk-xxxx 后重跑（或在 --env-file 里填）。"
      elif grep -q 'HTTP 429\|rate limiting' <<<"$new_log"; then
        warn "  原因：TPM 限流（429），且已用尽 $MAX_RETRIES 次重试。"
        warn "  下一步：过几分钟再跑，或加大重试：--max-retries 6 --retry-wait 120。"
      else
        warn "  详情：GET http://$HOST:$PORT/api/projects/$PROJECT_ID"
      fi
      printf '%s\n' "$new_log" | grep -i 'error\|failed' | tail -6 >&2 || true
      exit 1 ;;
    idle) log "        state=idle（等待后台索引启动）…" ;;
    *)    log "        state=$state，本次已解析 $processed/$total 个文件…" ;;
  esac
  if [ "$elapsed" -ge "$TIMEOUT_S" ]; then
    warn "[错误] 等索引超时（${TIMEOUT_S}s），当前 state=$state，已解析 $processed/$total。"
    warn "       这是真实进度（服务端未提供百分比）；可加大 --timeout 后重跑，或先看 $SERVICE_LOG。"
    exit 1
  fi
  sleep "$POLL_S"
  elapsed=$((elapsed + POLL_S))
done

# --------------------------------------------------------------------------
# 3. 调 MCP（复用手册 §4.2 的最小客户端思路）
# --------------------------------------------------------------------------
MCP_CLIENT="$(mktemp)"
trap 'rm -f "$MCP_CLIENT"; cleanup' EXIT
cat >"$MCP_CLIENT" <<'PY'
# 最小 MCP 客户端（官方 SDK / Streamable HTTP）——思路同 docs/handbook/getting-started/M2a-验收手册.md §4.2。
import asyncio, os, sys
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL, PROJECT_ROOT, QUERY = sys.argv[1], sys.argv[2], sys.argv[3]

async def main() -> None:
    async with streamable_http_client(URL) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            result = await session.call_tool(
                "search_context", {"query": QUERY, "project_root": PROJECT_ROOT}
            )
            if result.is_error:
                print("MCP_TOOL_ERROR", flush=True)
            print("\n".join(b.text for b in result.content if b.type == "text"))

asyncio.run(main())
PY

log "[4/5] 调 MCP tools/call search_context（断言返回里有「文件:行号」）"
set +e
MCP_OUT="$(uv run python "$MCP_CLIENT" "http://$HOST:$PORT/mcp" "$REPO" "$QUERY" 2>&1)"
MCP_RC=$?
set -e
rm -f "$MCP_CLIENT"

if [ "$MCP_RC" -ne 0 ] || printf '%s' "$MCP_OUT" | grep -q 'MCP_TOOL_ERROR'; then
  warn "[错误] MCP 调用失败（exit=$MCP_RC）。输出尾部："
  printf '%s\n' "$MCP_OUT" | tail -20 >&2 || true
  exit 1
fi
printf '%s\n' "$MCP_OUT" | head -25 || true

# 断言：返回里有「路径:行号」形态的证据（只看 exit code 不算通过）。
# 用 here-string（同两个理由：pipefail + grep -q 的 SIGPIPE）。
if grep -Eq '([A-Za-z0-9_./-]+\.[A-Za-z0-9]+):[0-9]+' <<<"$MCP_OUT"; then
  log "[OK] 冒烟通过：MCP 返回包含「文件:行号」证据。"
  exit 0
fi

warn "[错误] 断言失败：MCP 返回里没有「文件:行号」（可能未命中/索引为空）。输出："
printf '%s\n' "$MCP_OUT" | head -30 >&2 || true
warn "  排查：① 换一个仓库里确实存在的 --query ② 看 GET http://$HOST:$PORT/api/projects/$PROJECT_ID 的 sync.chunks 是否 >0"
exit 1
