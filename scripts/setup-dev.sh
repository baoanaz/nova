#!/usr/bin/env bash
# Install locked dependencies and create local configuration without overwriting secrets.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_WEB=0
WITH_CLIENT=0
for option in "$@"; do
  case "$option" in
    --with-web) WITH_WEB=1 ;;
    --with-client) WITH_CLIENT=1 ;;
    *) echo "Usage: bash scripts/setup-dev.sh [--with-web] [--with-client]" >&2; exit 2 ;;
  esac
done
cd "$ROOT"
command -v uv >/dev/null || { echo "Install uv first; see ENVIRONMENT.txt." >&2; exit 2; }
# Refuse to replace user-owned configuration links.
[ ! -L .env ] || { echo ".env is a symlink; review it before setup." >&2; exit 2; }
if [ -d .venv ] && [ ! -x .venv/bin/python ]; then
  echo "Broken .venv: review its symlinks and back it up before recreating it." >&2
  exit 2
fi
umask 077
mkdir -p .local/logs
if [ ! -e .env ]; then cp .env.example .env; fi
set -a
source .env
set +a
NOVA_SECRETS_FILE="${NOVA_SECRETS_FILE:-$HOME/.key/nova/secrets.env}"
case "$(realpath -m "$NOVA_SECRETS_FILE")" in
  "$ROOT"/*) echo "Credentials must be outside the repository." >&2; exit 2 ;;
esac
PRIVATE_DIR="$(dirname "$NOVA_SECRETS_FILE")"
[ ! -L "$PRIVATE_DIR" ] && [ ! -L "$NOVA_SECRETS_FILE" ] || {
  echo "Credential location is a symlink; review it before setup." >&2; exit 2;
}
mkdir -p "$PRIVATE_DIR"
chmod 700 "$PRIVATE_DIR"
if [ ! -e "$NOVA_SECRETS_FILE" ]; then
  printf '%s\n' '# Private API keys; obtain them from the provider.' 'EMBED_API_KEY=' 'ANSWER_API_KEY=' > "$NOVA_SECRETS_FILE"
fi
chmod 600 "$NOVA_SECRETS_FILE" .env
uv sync --frozen --all-packages --all-extras
if [ "$WITH_WEB" = 1 ]; then
  command -v npm >/dev/null || { echo "Install Node.js 22 and npm first." >&2; exit 2; }
  npm --prefix web ci --no-audit --no-fund
fi
if [ "$WITH_CLIENT" = 1 ]; then
  command -v cargo >/dev/null || { echo "Install Rust as described in ENVIRONMENT.txt." >&2; exit 2; }
  cargo fetch --locked --manifest-path client/Cargo.toml
fi
echo "Ready. From the repository root: set -a; source .env; set +a"
echo "Private API keys belong in $NOVA_SECRETS_FILE; never paste them into logs or issues."
