#!/usr/bin/env bash
# TASK-015A：embedding bake-off 全矩阵驱动（可断点续跑；产物落在 ~/.cache/nova-bakeoff/）
#
# 用法（靶场 checkout 用环境变量给；没给或路径不存在 → 跳过该靶场）：
#   NOVA_REPO=$PWD \
#   HELLO_AGENTS_REPO=<hello-agents 检出> \
#   COCKPIT_REPO=<cockpit-agents-py 检出（内部仓库，需要权限）> \
#     bash benches/bakeoff/run_matrix.sh lane1     # e5-small 基线 + 截断 A/B + bge-m3
#   ... lane2                                       # bge-small-zh-v1.5 + arctic-embed-xs
#
# 纪律：**唯一变量是 embedding provider**；本脚本不传任何排序/装填参数
# （`run` 子命令只用 --model / --repo / --golden / --max-input-tokens）。
#
# 2026-09-14：旧靶场 aibox-super-sdk / linux-mtk-mw-cameraservice 已下线（用例与报告一并清理，
# 见 benches/README.md「靶场变更」），矩阵改为按环境变量选靶场，不再硬编码内部路径。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$ROOT/.venv/bin/python"
SCRIPT="benches/bakeoff/embed_compare.py"
LOGDIR="$HOME/.cache/nova-bakeoff/logs"
mkdir -p "$LOGDIR"

# 靶场表：name|repo_path|golden_dir。repo 为空或路径不存在 → 跳过（并说明原因）。
TARGETS=()
add_target() {  # name repo golden
  if [ -z "$2" ]; then
    echo "跳过 $1：未设置对应环境变量" >&2
    return 0
  fi
  if [ ! -d "$2" ]; then
    echo "跳过 $1：路径不存在（$2）" >&2
    return 0
  fi
  TARGETS+=("$1|$2|$3")
}
add_target nova              "${NOVA_REPO:-$ROOT}"            benches/golden/nova
add_target hello-agents      "${HELLO_AGENTS_REPO:-}"         benches/golden/hello-agents
add_target cockpit-agents-py "${COCKPIT_REPO:-}"              benches/golden/cockpit-agents-py

if [ "${#TARGETS[@]}" -eq 0 ]; then
  echo "没有可用靶场：至少设置 NOVA_REPO / HELLO_AGENTS_REPO / COCKPIT_REPO 之一" >&2
  exit 2
fi

run_one() {  # model repo_path repo_name golden [extra args...]
  local model="$1" repo="$2" name="$3" golden="$4"; shift 4
  echo "=== [$(date +%H:%M:%S)] $model @ $name $*"
  ( cd "$ROOT" && "$PY" "$SCRIPT" run --model "$model" --repo "$repo" --repo-name "$name" \
      --golden "$golden" "$@" ) 2>&1 | tee -a "$LOGDIR/${LANE}.log"
  echo "--- exit=$? [$(date +%H:%M:%S)]"
}

run_model() {  # model [extra args...]
  local model="$1"; shift
  local row name repo golden
  for row in "${TARGETS[@]}"; do
    IFS='|' read -r name repo golden <<< "$row"
    run_one "$model" "$repo" "$name" "$golden" "$@"
  done
}

case "${1:-}" in
  lane1)
    LANE=lane1
    run_model multilingual-e5-small
    # A2 截断 A/B（只对主模型做，验证 2048 截断是否伤召回）
    run_model multilingual-e5-small --max-input-tokens 2048
    # 上界参考（多语言大模型，代价优先）
    run_model bge-m3-int8
    ;;
  lane2)
    LANE=lane2
    run_model bge-small-zh-v1.5
    run_model arctic-embed-xs
    ;;
  *)
    echo "usage: bash benches/bakeoff/run_matrix.sh {lane1|lane2}" >&2
    echo "  靶场用环境变量给：NOVA_REPO / HELLO_AGENTS_REPO / COCKPIT_REPO" >&2
    exit 2
    ;;
esac
echo "=== lane done [$(date +%H:%M:%S)]"
