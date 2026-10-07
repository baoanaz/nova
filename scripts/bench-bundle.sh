#!/usr/bin/env bash
# benchmark bundle 打包/解包（TASK-101 §F）：让任意主机（新 WSL / 另一台机器）能直接跑分。
#
# 设计（详见 benches/README.md「跨主机复现」）：
#   bundle = 索引（index.db + vectors/） + 查询向量侧车（query-vectors.json）
#   索引解决"不重新索引"，侧车解决"目标机没有 embedding key / 没有本地模型"。
#   blobs/ 与 sync-state.json **不进包**：eval/search 不读它们（只用于增量同步）。
#
# 用法：
#   bash scripts/bench-bundle.sh pack   <projectId> <repo-path> <out.tar.zst> [--data <data-root>]
#   bash scripts/bench-bundle.sh unpack <bundle> <data-root> [--project-id <id>]
#   bash scripts/bench-bundle.sh verify <bundle>
#
# bundle 含源码正文；内部资料及 manifest 只保存在授权的私有位置，不放公开 Release。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
[ -f "$ROOT/.env" ] && { set -a; . "$ROOT/.env"; set +a; }

DATA_ROOT_DEFAULT="${ZACE_DATA_ROOT:-$HOME/.zace}"

die() { echo "bench-bundle: $*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"; }

# --------------------------------------------------------------------------- verify

verify_bundle() {
  local bundle="$1"
  [ -f "$bundle" ] || die "bundle 不存在：$bundle"
  local tmp
  tmp="$(mktemp -d)"
  tar -xf "$bundle" -C "$tmp"
  [ -f "$tmp/manifest.json" ] || die "bundle 缺 manifest.json"
  [ -f "$tmp/project/index.db" ] || die "bundle 缺 project/index.db"
  [ -d "$tmp/project/vectors" ] || die "bundle 缺 project/vectors/"
  ( cd "$tmp" && sha256sum -c SHA256SUMS >/dev/null ) || die "校验和不匹配（bundle 损坏或传输不完整）"
  python3 - "$tmp/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
need = ("project_id", "repo", "commit", "files", "chunks", "embedding_model", "dim",
        "vector_cache", "vector_queries")
missing = [k for k in need if k not in m]
if missing:
    sys.exit(f"manifest 缺字段：{missing}")
print(f"  projectId      : {m['project_id']}")
print(f"  repo / commit  : {m['repo']} @ {m['commit']}")
print(f"  规模           : {m['files']} 文件 / {m['chunks']} 切片")
print(f"  embedding      : {m['embedding_model']} ({m['dim']} 维)")
print(f"  查询向量侧车   : {m['vector_cache']}（{m['vector_queries']} 条查询）")
PY
  rm -rf "$tmp"
  echo "bench-bundle: verify 通过"
}

# --------------------------------------------------------------------------- pack

pack_bundle() {
  local project_id="$1" repo="$2" out="$3" data_root="${4:-$DATA_ROOT_DEFAULT}"
  local project_dir="$data_root/projects/$project_id"
  [ -d "$project_dir" ] || die "找不到项目索引：$project_dir"
  [ -f "$project_dir/index.db" ] || die "索引缺失：$project_dir/index.db（先 ingest）"
  [ -d "$project_dir/vectors" ] || die "向量库缺失：$project_dir/vectors（先 ingest）"

  # 查询向量侧车留在被 Git 忽略的本机目录，靶场始终只读。
  local cache="${ZACE_BENCH_VECTOR_CACHE:-$ROOT/.local/bench/query-vectors.json}"
  local work
  work="$(mktemp -d)"

  echo "bench-bundle: 暂存索引 …"
  mkdir -p "$work/project"
  cp "$project_dir/index.db" "$work/project/"
  cp -r "$project_dir/vectors" "$work/project/"

  if [ -f "$cache" ]; then
    cp "$cache" "$work/query-vectors.json"
    echo "bench-bundle: 带上查询向量侧车（目标机可离线回放）"
  else
    echo "bench-bundle: 警告：没有 $cache —— 目标机必须有 embedding key，否则向量通道会降级" >&2
  fi

  echo "bench-bundle: 生成 manifest 与校验和 …"
  python3 - "$work" "$project_id" "$repo" "$cache" <<'PY'
import hashlib, json, os, subprocess, sys
from pathlib import Path

work, project_id, repo, cache_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
work_p = Path(work)
repo_p = Path(repo)

# 索引规模与指纹：直接读索引，避免手填（手填的值一定会漂）。
import sqlite3  # noqa: E402

con = sqlite3.connect(work_p / "project" / "index.db")
counts = con.execute(
    "SELECT (SELECT COUNT(*) FROM files), (SELECT COUNT(*) FROM chunks)"
).fetchone()
config = dict(con.execute("SELECT key, value FROM index_config"))
con.close()

def git(args):
    try:
        return subprocess.run(["git", "-C", str(repo_p), *args], capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return ""

vector_cache = "query-vectors.json" if (work_p / "query-vectors.json").is_file() else None
queries = 0
if vector_cache:
    data = json.loads((work_p / vector_cache).read_text(encoding="utf-8"))
    queries = len(data.get("queries", {}))

manifest = {
    "schema": 1,
    "project_id": project_id,
    "repo": repo_p.name,
    "repo_path_hint": str(repo_p),
    "commit": git(["rev-parse", "HEAD"]),
    "files": counts[0],
    "chunks": counts[1],
    "embedding_model": config.get("embedding_model"),
    "dim": int(config.get("embedding_dim") or 0),
    "vector_cache": vector_cache,
    "vector_queries": queries,
    "notes": "eval/search 只需 index.db + vectors/；blobs 与 sync-state 用于增量同步，未打包。",
}
(work_p / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

  ( cd "$work" && find . -type f ! -name SHA256SUMS -print0 \
      | sort -z | xargs -0 sha256sum > SHA256SUMS )

  echo "bench-bundle: 压缩 …"
  if command -v zstd >/dev/null 2>&1 && case "$out" in *.zst) true;; *) false;; esac; then
    tar --zstd -cf "$out" -C "$work" .
  else
    case "$out" in
      *.zst) die "输出名以 .zst 结尾但系统没有 zstd：请改成 .tar.gz，或安装 zstd" ;;
    esac
    tar -czf "$out" -C "$work" .
  fi
  echo "bench-bundle: 完成 → $out ($(du -h "$out" | cut -f1))"
  verify_bundle "$out"
  rm -rf "$work"
}

# --------------------------------------------------------------------------- unpack

unpack_bundle() {
  local bundle="$1" data_root="$2" forced_id="${3:-}"
  verify_bundle "$bundle" >/dev/null
  local tmp
  tmp="$(mktemp -d)"
  tar -xf "$bundle" -C "$tmp"

  local project_id="$forced_id"
  [ -n "$project_id" ] || project_id="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['project_id'])" "$tmp/manifest.json")"

  local dest="$data_root/projects/$project_id"
  if [ -d "$dest" ]; then
    echo "bench-bundle: 目标已存在，先备份到 ${dest}.bak"
    rm -rf "${dest}.bak"
    mv "$dest" "${dest}.bak"
  fi
  mkdir -p "$dest"
  cp "$tmp/project/index.db" "$dest/"
  cp -r "$tmp/project/vectors" "$dest/"
  [ -f "$tmp/project.json" ] && cp "$tmp/project.json" "$dest/" || true

  echo "bench-bundle: 已解包到 $dest"
  echo "  projectId      : $project_id"
  python3 - "$tmp/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
if m.get("vector_cache") is None:
    print("  提示：此 bundle 不含查询向量侧车 → 目标机必须有 embedding key，否则向量通道降级")
else:
    print(f"  查询向量侧车   : {m['vector_cache']}（{m['vector_queries']} 条）"
          " → --replay 可离线跑分")
PY
  echo
  echo "下一步（任意 checkout，无需重新索引）："
  echo "  uv run zace-core eval --repo <任意 checkout> --data $data_root \\"
  echo "    --project-id $project_id --golden benches/golden/<repo_hint> --report benches/results/<name>.md"
  echo "  # 离线（无 key）：再加 --vector-cache <侧车路径> --replay"
  rm -rf "$tmp"
}

# --------------------------------------------------------------------------- main

case "${1:-}" in
  pack)
    [ $# -ge 4 ] || die "用法：pack <projectId> <repo-path> <out.tar.zst> [data-root]"
    need tar; need sha256sum
    pack_bundle "$2" "$3" "$4" "${5:-}"
    ;;
  unpack)
    [ $# -ge 3 ] || die "用法：unpack <bundle> <data-root> [project-id]"
    need tar
    unpack_bundle "$2" "$3" "${4:-}"
    ;;
  verify)
    [ $# -ge 2 ] || die "用法：verify <bundle>"
    need tar; need sha256sum
    verify_bundle "$2"
    ;;
  *)
    sed -n '2,20p' "$0"
    exit 2
    ;;
esac
