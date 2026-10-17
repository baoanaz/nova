#!/usr/bin/env bash
# 无界面的本地 benchmark 服务：nova-service 本地模式（无鉴权、只监听回环），索引落持久目录。
#
#   bench/stack.sh up       启动服务（已在运行则跳过）
#   bench/stack.sh down     停止服务（索引数据保留）
#   bench/stack.sh status   看进程与健康检查
#
# 数据根默认 ~/.nova/bench-data（不放 /tmp，重启不丢；索引只建一次，之后增量）。
# 密钥来自 ~/.key/nova/secrets.env（EMBED_*、ANSWER_*），不会被打印或写入仓库。
# 回环流量只在本脚本启动的子进程里设置 NO_PROXY，避免被宿主机代理接管；
# 不修改用户 shell 与任何全局代理配置。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${NOVA_BENCH_DATA:-$HOME/.nova/bench-data}"
PORT="${NOVA_BENCH_PORT:-8892}"
SECRETS="${NOVA_SECRETS_FILE:-$HOME/.key/nova/secrets.env}"
REPOS="${NOVA_BENCH_REPOS:-$HOME/github/ace/bench-repos}"
# local 模式启动时必须先绑定一个仓库；用最小的靶场，其余靶场由 runner 经 attach 接入。
SEED_REPO="${NOVA_BENCH_SEED_REPO:-$REPOS/muduo}"
BASE="http://127.0.0.1:$PORT"
RUN="$DATA/run"

export NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"

mkdir -p "$RUN"
chmod 700 "$DATA"

alive() { [ -f "$RUN/service.pid" ] && kill -0 "$(cat "$RUN/service.pid")" 2>/dev/null; }

wait_health() {
  for _ in $(seq 90); do
    curl -fsS "$BASE/healthz" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

up() {
  if alive; then echo "服务已在运行（pid $(cat "$RUN/service.pid")）：$BASE"; return; fi
  [ -r "$SECRETS" ] || { echo "缺少 $SECRETS（需要 EMBED_API_KEY；ask 另需 ANSWER_*）。" >&2; exit 1; }
  [ -d "$SEED_REPO" ] || { echo "缺少种子仓库 $SEED_REPO，请先按 docs/handbook/benchmark/targets.md 克隆靶场。" >&2; exit 1; }
  (
    set -a
    # shellcheck disable=SC1090
    . "$SECRETS"
    set +a
    # 默认值与 .env.example 一致；已有值不覆盖。
    export EMBED_MODE="${EMBED_MODE:-api}" EMBED_MODEL="${EMBED_MODEL:-voyage-code-4}"
    export EMBED_BASE_URL="${EMBED_BASE_URL:-https://api.voyageai.com}" EMBED_DIM="${EMBED_DIM:-1024}"
    export EMBED_BATCH_SIZE="${EMBED_BATCH_SIZE:-500}" EMBED_CONCURRENCY="${EMBED_CONCURRENCY:-4}"
    export EMBED_BATCH_TOKEN_BUDGET="${EMBED_BATCH_TOKEN_BUDGET:-300000}"
    export EMBED_MAX_INPUT_TOKENS="${EMBED_MAX_INPUT_TOKENS:-32000}" NOVA_EMBED_WORKERS="${NOVA_EMBED_WORKERS:-2}"
    export NOVA_ASK_RATE_PER_MIN=0 NOVA_DATA_ROOT="$DATA"
    # 基准的索引数据远超默认存储配额；0 = 不限，避免每条返回都带存储告警。
    export NOVA_STORAGE_LIMIT_PER_USER_BYTES=0 NOVA_STORAGE_LIMIT_PER_PROJECT_BYTES=0
    cd "$ROOT"
    exec nohup "$ROOT/.venv/bin/nova-service" local --host 127.0.0.1 --port "$PORT" \
      --data-root "$DATA" --repo "$SEED_REPO" --name muduo >"$RUN/service.log" 2>&1
  ) &
  echo $! >"$RUN/service.pid"
  wait_health || { echo "服务未就绪，见 $RUN/service.log" >&2; exit 1; }
  echo "服务已启动：$BASE（日志 $RUN/service.log）"
}

down() {
  if alive; then
    pkill -P "$(cat "$RUN/service.pid")" 2>/dev/null || true
    kill "$(cat "$RUN/service.pid")" 2>/dev/null || true
    echo "已停止服务"
  fi
  rm -f "$RUN/service.pid"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  status)
    if alive; then echo "服务：运行中（pid $(cat "$RUN/service.pid")）"; else echo "服务：未运行"; fi
    curl -fsS "$BASE/healthz" >/dev/null 2>&1 && echo "健康检查：正常（$BASE）" || echo "健康检查：不可用"
    echo "数据根：$DATA"
    ;;
  *) echo "用法：$0 {up|down|status}" >&2; exit 2 ;;
esac
