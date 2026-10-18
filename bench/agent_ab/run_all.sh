#!/usr/bin/env bash
S=$(cd "$(dirname "$0")" && pwd); export S R=/home/xuwenzheng/github/ace/bench-repos
xargs -a "$S/jobs.txt" -d '\n' -P 10 -n 1 bash -c '
IFS=$'"'"'\t'"'"' read -r arm id repo q <<< "$1"
f="$S/runs/$id.$arm.jsonl"
[ -s "$f" ] && grep -q "\"agent_end\"" "$f" && exit 0
"$S/run_one.sh" "$arm" "$id" "$R/$repo" "$q"' _
