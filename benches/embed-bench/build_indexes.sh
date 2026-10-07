#!/usr/bin/env bash
# 三靶场「持久索引」的构建 / 复用入口（VPS 量产口径）。
#
# 为什么需要它：未来所有基准测试都**复用**这三个预建索引，不再索引。本脚本把
# 「配置 → 数据根目录 → projectId」的约定固化成代码，避免下次手敲参数时把索引建到别处、
# 或者用了不同模型/维度（D-07 会判定 reembed → 全量重嵌，白烧 token）。
#
# 目录约定（按模型 + 维度分目录，换档位即换根，互不污染）：
#   $NOVA_BENCH_ROOT/<model>-d<dim>/projects/<16 位 projectId>/
#   例：/root/.nova/bench/voyage-4-lite-d1024/projects/ca2050db0db5b1e2
#   实证与口径：benches/results/index-cost-model-vps.md
#
# 可配置项（默认 = VPS 量产实测值；env 覆盖即可，无需改脚本）：
#   EMBED_MODEL            默认 voyage-4-lite
#   EMBED_BASE_URL         默认 https://api.voyageai.com
#   EMBED_API_KEY          必填（默认从未传入时读 /etc/nova/nova.env，密钥不进仓库）
#   EMBED_CONCURRENCY      默认 4（**本机 VPS 冻结值**，2026-09-15 基线 v1）；
#                          换更大内存的机器可回调 8（registry 的厂商推荐值），见报告"内存"一节
#   EMBED_BATCH_SIZE       默认 500（实测 1000 条会因响应体过大被对端断连）
#   EMBED_BATCH_TOKEN_BUDGET 默认 300000
#   EMBED_MAX_INPUT_TOKENS 默认 32000（只影响截断，不进指纹）
#   NOVA_BENCH_ROOT        默认 ~/.nova/bench
#   BENCH_REPOS_ROOT       默认 /root/xuwenzheng/ACE/benchmark
#   BENCH_REPOS            默认 "leveldb HelloAgents langchain"
#   CHUNK 策略             当前实现默认口径（无开关；改切片规则要动 PARSER_CONFIG_VERSION，
#                          属 core 变更，会触发 full_reparse，不在本脚本范围内）
#
# 用法：
#   bash benches/embed-bench/build_indexes.sh --dry-run    # 只看要建什么、建到哪
#   bash benches/embed-bench/build_indexes.sh              # 缺哪个建哪个，已建的跳过（不重嵌）
#   bash benches/embed-bench/build_indexes.sh --force      # 强制重建（会重新嵌入，烧 token）
#   BENCH_REPOS="leveldb" bash benches/embed-bench/build_indexes.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

FORCE=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "未知参数：$arg（可用：--force / --dry-run）" >&2; exit 2 ;;
  esac
done

# ---- 配置（默认 = 量产口径）----
export EMBED_MODE="${EMBED_MODE:-api}"
export EMBED_MODEL="${EMBED_MODEL:-voyage-4-lite}"
export EMBED_BASE_URL="${EMBED_BASE_URL:-https://api.voyageai.com}"
export EMBED_CONCURRENCY="${EMBED_CONCURRENCY:-4}"  # VPS 冻结值：链路 96% 饱和 + 2 GiB 内存安全线
export EMBED_BATCH_SIZE="${EMBED_BATCH_SIZE:-500}"
export EMBED_BATCH_TOKEN_BUDGET="${EMBED_BATCH_TOKEN_BUDGET:-300000}"
export EMBED_MAX_INPUT_TOKENS="${EMBED_MAX_INPUT_TOKENS:-32000}"

NOVA_BENCH_ROOT="${NOVA_BENCH_ROOT:-$HOME/.nova/bench}"
BENCH_REPOS_ROOT="${BENCH_REPOS_ROOT:-/root/xuwenzheng/ACE/benchmark}"
REPOS="${BENCH_REPOS:-leveldb HelloAgents langchain}"

if [ -z "${EMBED_API_KEY:-}" ] && [ -r /etc/nova/nova.env ]; then
  set -a; . /etc/nova/nova.env; set +a
fi
[ -n "${EMBED_API_KEY:-}" ] || { echo "缺少 EMBED_API_KEY（set -a; source <env>; set +a）" >&2; exit 2; }

# ---- 数据根：按「模型-维度」分目录（维度是真变量，见用户 2026-09-15 口径）----
read -r MODEL_SLUG DIM < <(uv run python -c "
from nova_core.embedding.factory import create_provider
p = create_provider()
print(p.profile.model_id.removeprefix('api:').removeprefix('local:'), p.profile.dim)
")
DATA_ROOT="${DATA_ROOT:-$NOVA_BENCH_ROOT/${MODEL_SLUG}-d${DIM}}"

echo "生效配置："
echo "  provider   = $EMBED_MODE / $EMBED_MODEL（$EMBED_BASE_URL）"
echo "  维度       = $DIM"
echo "  批/预算    = $EMBED_BATCH_SIZE 条 / $EMBED_BATCH_TOKEN_BUDGET token（并发 $EMBED_CONCURRENCY）"
echo "  截断上限   = $EMBED_MAX_INPUT_TOKENS（不进指纹，只影响单条截断）"
echo "  数据根     = $DATA_ROOT"
echo "  仓库根     = $BENCH_REPOS_ROOT"
echo

mkdir -p "$DATA_ROOT/projects"

for repo in $REPOS; do
  src="$BENCH_REPOS_ROOT/$repo"
  if [ ! -d "$src" ]; then
    echo "跳过 $repo：仓库不存在（$src）"
    continue
  fi
  # projectId 由 D-29 身份（git remote + 仓库内相对路径）决定，本地可算，不需要 API
  read -r project_id < <(uv run python -c "
import sys
from pathlib import Path
from nova_core.engine import project_id_for, repo_identity
print(project_id_for(repo_identity(Path(sys.argv[1])).identity_key))
" "$src")

  index_dir="$DATA_ROOT/projects/$project_id"
  if [ -f "$index_dir/index.db" ] && [ "$FORCE" -eq 0 ]; then
    echo "[复用] $repo  project=$project_id  （索引已存在，跳过，不重嵌）"
    echo "        复用命令：uv run nova-core search \"<query>\" --project-id $project_id \\"
    echo "                    --repo $src --data $DATA_ROOT"
    continue
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[待建] $repo  project=$project_id  → $index_dir"
    continue
  fi

  echo "[构建] $repo  project=$project_id"
  uv run python benches/embed-bench/ingest_probe.py \
    --repo "$src" --data "$DATA_ROOT" --incremental \
    --tag "build_indexes/$EMBED_MODEL-d$DIM/conc$EMBED_CONCURRENCY" \
    --out "benches/results/raw/ingest-vps/$repo.json" || exit 1
done

echo
echo "完成。索引清单：$DATA_ROOT/INDEXES.json（由 scripts 或人工维护，见报告）"
