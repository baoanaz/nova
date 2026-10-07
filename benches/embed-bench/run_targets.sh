#!/usr/bin/env bash
# TASK-102：三靶场 embedding 配置标定（端到端 ingest 墙钟）。
#
# 为什么是端到端而不是只测嵌入阶段：用户要的答案是"改配置能省多久索引时间"，
# 而 ingest 里除嵌入外还有解析、入库、建图（实测占比 0.2–2%）。本脚本用真实 CLI
# 拿墙钟，同时把 provider 的生效参数打印出来（防止"配了没生效"）。
#
# 纪律：
# - **串行执行**（不同时跑两个 ingest）：单 key 独占，避免配额互抢污染数据；
# - 每组配置用一个独立 data 根（避免指纹/缓存互相污染），跑完记录 data 目录体积；
# - `--full` 强制全量重解析，保证每组都是冷启动口径。
#
# 用法：
#   bash benches/embed-bench/run_targets.sh <tag>
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
TAG="${1:-run1}"
BENCH_ROOT="${BENCH_ROOT:-/home/xuwenzheng/2_github/AI/ACE/benchmark}"
OUTDIR="$ROOT/benches/results/raw/ingest-$TAG"
mkdir -p "$OUTDIR"

export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export NO_PROXY=127.0.0.1,localhost
export EMBED_MODE=api
export EMBED_MODEL=bge-m3
export EMBED_BASE_URL=https://api.siliconflow.cn
export EMBED_API_KEY="$(grep -m1 -E '^EMBED_API_KEY=sk-|^# EMBED_API_KEY=sk-' "$ROOT/.env" | sed -E 's/^#? ?EMBED_API_KEY=//')"
[ -n "$EMBED_API_KEY" ] || { echo "缺少 EMBED_API_KEY" >&2; exit 2; }

REPOS=(HelloAgents leveldb langchain)
# 配置矩阵：name|concurrency|max_input_tokens
CONFIGS=(
  'base|1|8192'
  'conc3|3|8192'
  'conc3-t2048|3|2048'
)

echo "生效参数自检："
EMBED_CONCURRENCY=1 uv run python -c "
from nova_core.embedding.factory import create_provider
p = create_provider()
print('  default   batch=%s budget=%s conc=%s maxTok=%s %s' % (
    p.batch_size, p.batch_token_budget, p.concurrency, p.profile.max_input_tokens, p.profile))
" 2>/dev/null

for cfg in "${CONFIGS[@]}"; do
  IFS='|' read -r name conc limit <<<"$cfg"
  export EMBED_CONCURRENCY="$conc"
  export EMBED_MAX_INPUT_TOKENS="$limit"
  for repo in "${REPOS[@]}"; do
    src="$BENCH_ROOT/$repo"
    [ -d "$src" ] || { echo "跳过 $repo：路径不存在"; continue; }
    data="$OUTDIR/$name-$repo"
    log="$OUTDIR/$name-$repo.log"
    echo "=== [$name] $repo  (conc=$conc maxTok=$limit)"
    start=$(date +%s.%N)
    uv run nova-core ingest --repo "$src" --data "$data" --full >"$log" 2>&1
    rc=$?
    end=$(date +%s.%N)
    printf '    rc=%s 墙钟=%.1fs\n' "$rc" "$(echo "$end - $start" | bc)"
    grep -E "^(files|chunks|vectors|elapsed)|error" "$log" | sed 's/^/    /' | head -8
    du -sh "$data" 2>/dev/null | sed 's/^/    data /'
  done
done
echo "全部完成，日志在 $OUTDIR"
