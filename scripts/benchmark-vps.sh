#!/usr/bin/env bash
# Reproduce LangChain's TASK-115 baseline; local outputs never enter Git.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-local}"
REPO="${2:-$ROOT/../benchmark/langchain}"
EXPECTED_COMMIT=e75dae1f53c99c2b5ddb0c7bb36022c6aea25569
cd "$ROOT"
case "$MODE" in
  local|full|ttfb|hardware) ;;
  *) echo "Usage: bash scripts/benchmark-vps.sh [local|full|ttfb|hardware] [langchain-path]" >&2; exit 2 ;;
esac
[ -x .venv/bin/python ] || { echo "Run scripts/setup-dev.sh first." >&2; exit 2; }
set -a
if [ -f .env ]; then
  source .env
else
  source .env.example
fi
# Keep performance measurements tied to the baseline, irrespective of local settings.
source configs/profiles/epyc-2c2g.env
set +a
ZACE_SECRETS_FILE="${ZACE_SECRETS_FILE:-$HOME/.key/zace/secrets.env}"
OUT="${ZACE_BENCH_OUT:-$ROOT/.local/bench/$(date -u +%Y%m%dT%H%M%S%NZ)-$MODE}"
umask 077
mkdir -p "$(dirname "$OUT")"
mkdir "$OUT" || { echo "Output must be a new directory; refusing to reuse a benchmark." >&2; exit 2; }
git rev-parse HEAD > "$OUT/zace-commit.txt"
ZACE_DIRTY=false
[ -z "$(git status --porcelain --untracked-files=normal)" ] || ZACE_DIRTY=true
printf '%s\n' "$ZACE_DIRTY" > "$OUT/zace-dirty.txt"
if [ "$MODE" = hardware ]; then
  exec .venv/bin/python benches/embed-bench/hardware_probe.py --out "$OUT/hardware.json"
fi
[ "$(git -C "$REPO" rev-parse HEAD)" = "$EXPECTED_COMMIT" ] || {
  echo "LangChain must be at $EXPECTED_COMMIT for the 45.462s / 63.326s baseline." >&2
  exit 2
}
if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=normal)" ]; then
  echo "LangChain checkout must be clean; do not modify the benchmark target." >&2
  exit 2
fi
if [ "$MODE" != local ] && { [ -z "${EMBED_API_KEY:-}" ] || [ "${EMBED_API_KEY:-}" = pa-xxxx ]; }; then
  echo "Missing Voyage EMBED_API_KEY in $ZACE_SECRETS_FILE; no API request was made." >&2
  exit 2
fi
git -C "$REPO" rev-parse HEAD > "$OUT/langchain-commit.txt"
if [ "$MODE" = ttfb ]; then
  exec .venv/bin/python benches/embed-bench/ttfb_probe.py --repo "$REPO" \
    --requests 8 --concurrency 1 --concurrency 4 --concurrency 8 --out "$OUT/ttfb.json"
fi
command -v systemd-run >/dev/null || { echo "systemd-run is required for the memory limit." >&2; exit 2; }
PROBE_MODE=full
[ "$MODE" != local ] || PROBE_MODE=local-only
# local-only must not try downloading models/tokenizers during the timed run.
if [ "$MODE" = local ]; then export HF_HUB_OFFLINE=1; fi
systemd-run --scope --quiet -p MemoryHigh=1200M -p MemoryMax=1500M -p MemorySwapMax=512M -- \
  timeout 600 .venv/bin/python benches/embed-bench/coldstart_probe.py \
  --repo "$REPO" --data "$OUT/index" --out "$OUT/coldstart.json" \
  --mode "$PROBE_MODE" --tag "epyc-2c2g-$MODE"
