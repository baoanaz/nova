#!/usr/bin/env bash
# Export only zace credentials and local settings. The archive never belongs in Git.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_DIR="${ZACE_KEY_DIR:-$HOME/.key/zace}"
OUT="$KEY_DIR/zace-secrets-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
while getopts ":o:yh" option; do
  case "$option" in
    o) OUT="$OPTARG" ;;
    y) ;; # Compatibility: packaging itself is non-interactive.
    h) echo "Usage: bash scripts/pack-secrets.sh [-o /private/path/archive.tar.gz] [-y]"; exit 0 ;;
    *) echo "Unknown option; use -h." >&2; exit 2 ;;
  esac
done
[ ! -L "$KEY_DIR" ] || { echo "Review the credential directory symlink first." >&2; exit 2; }
umask 077
mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"
OUT="$(realpath -m "$OUT")"
mkdir -p "$(dirname "$OUT")"
if git -C "$(dirname "$OUT")" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Refusing to write a private archive inside a Git worktree." >&2
  exit 2
fi
if [ -e "$OUT" ] || [ -L "$OUT" ]; then
  echo "Output already exists; choose another filename." >&2
  exit 2
fi
WORK="$(mktemp -d "$KEY_DIR/.pack-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
PKG="$WORK/zace-secrets"
mkdir -p "$PKG/env"
if [ -f "$KEY_DIR/secrets.env" ]; then
  install -m 600 "$KEY_DIR/secrets.env" "$PKG/env/secrets.env"
fi
if [ -f "$ROOT/.env" ]; then
  python3 - "$ROOT/.env" "$PKG/env/project.env" "$ROOT" <<'PY'
from pathlib import Path
import sys
source, destination, root = sys.argv[1:]
Path(destination).write_text(Path(source).read_text().replace(root, "<REPO>"))
PY
fi
cat > "$PKG/README.txt" <<'EOF'
Private zace credentials and local settings. Never commit or share this archive publicly.
Restore from a trusted archive:
  tar xzf <archive> -C <private-directory>
  bash <private-directory>/zace-secrets/restore.sh <zace-repository>
Legacy invocation also accepts: restore.sh wsl|vps <zace-repository>
Restoration backs up existing files privately. It does not install dependencies,
configure nginx/systemd, create symlinks, start services, or restore indexes.
EOF
cat > "$PKG/restore.sh" <<'RESTORE'
#!/usr/bin/env bash
set -euo pipefail
PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "${1:-}" in
  wsl|vps|local) REPO="${2:-$PWD}" ;;
  *) REPO="${1:-$PWD}" ;;
esac
REPO="$(cd "$REPO" && pwd)"
[ -f "$REPO/pyproject.toml" ] || { echo "Not a zace repository." >&2; exit 2; }
KEY_DIR="${ZACE_KEY_DIR:-$HOME/.key/zace}"
for target in "$KEY_DIR" "$KEY_DIR/secrets.env" "$REPO/.env"; do
  [ ! -L "$target" ] || { echo "Review destination symlink first: $target" >&2; exit 2; }
done
umask 077
mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"
BACKUP="$KEY_DIR/backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
backup_file() {
  if [ -e "$1" ]; then
    mkdir -p "$BACKUP"
    cp -p "$1" "$BACKUP/$2"
  fi
}
if [ -f "$PKG/env/secrets.env" ]; then
  backup_file "$KEY_DIR/secrets.env" secrets.env
  install -m 600 "$PKG/env/secrets.env" "$KEY_DIR/secrets.env"
fi
if [ -f "$PKG/env/project.env" ]; then
  backup_file "$REPO/.env" project.env
  python3 - "$PKG/env/project.env" "$REPO/.env" "$REPO" <<'PY'
from pathlib import Path
import sys
source, destination, root = sys.argv[1:]
Path(destination).write_text(Path(source).read_text().replace("<REPO>", root))
PY
  chmod 600 "$REPO/.env"
fi
echo "Restored credentials and local settings. Backups, if any: $BACKUP"
echo "Next: cd to the repository, run scripts/setup-dev.sh, and source .env."
RESTORE
chmod 700 "$PKG/restore.sh"
# Exclusive creation avoids overwriting an existing file or following an output symlink.
python3 - "$OUT" "$WORK" <<'PY'
import os, sys, tarfile
output, work = sys.argv[1:]
fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(fd, "wb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as archive:
        archive.add(os.path.join(work, "zace-secrets"), arcname="zace-secrets")
except BaseException:
    os.unlink(output)
    raise
PY
echo "Created private archive: $OUT (0600)"
