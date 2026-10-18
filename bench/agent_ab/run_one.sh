#!/usr/bin/env bash
# 用法：run_one.sh <nova|base> <id> <repo_dir> <question>
S=$(cd "$(dirname "$0")" && pwd); ARM=$1; ID=$2; DIR=$3; Q=$4
SYS=()
[ "$ARM" = nova ] && SYS=(--append-system-prompt "本仓库已接入 NOVA 代码检索（search_context 工具）。回答代码问题时先用 search_context 检索；结果里已有的代码直接用来回答，只在必要时再读文件核对。")
cd "$DIR"
start=$(date +%s.%N)
PI_CODING_AGENT_DIR=$S/agent-$ARM timeout 600 pi -p --mode json --no-session --no-skills --no-prompt-templates --no-context-files --offline \
  --provider xiugou --model gpt-6.1-sol --thinking medium --exclude-tools edit,write,mcp__nova__ask_project "${SYS[@]}" \
  "$Q 请基于当前仓库的代码回答，给出关键文件路径与行号，回答控制在 300 字以内。" < /dev/null > "$S/runs/$ID.$ARM.jsonl" 2> "$S/runs/$ID.$ARM.err"
echo "$ID $ARM exit=$? wall=$(echo "$(date +%s.%N) - $start" | bc)"
