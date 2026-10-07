#!/usr/bin/env bash
# nova-client 的唯一发布入口。
#
# 用法：bash scripts/release-client.sh 0.0.8
#
# 它只做本地准备，然后把 tag 推出去：
#   ① main + 工作区干净 + tag 不存在
#   ② 改版本号，同步 6 个平台子包，一致性检查
#   ③ commit → push main → push tag
#
# 推完 tag 就结束。六平台构建与 npm 发布**全部由 GitHub Actions 完成**
# （.github/workflows/release.yml）：build → 平台子包 → verify → 主包 next
# → smoke → latest → verify。CI 结果直接在 GitHub Actions 页面看。
#
# 边界：不构建二进制、不发布任何 npm 包、不等 CI、不轮询 registry、不读取 NPM_TOKEN。
# 失败处置：版本号不可撤回、tag 不移动 —— 放弃当前版本号，修复后递增新版本重跑。
#
# 详见 docs/handbook/release/npm.md。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NPM_PACKAGE="nova-client"

die() { echo "✗ $*" >&2; exit 1; }

usage() {
  cat <<EOF
用法：bash scripts/release-client.sh <version>

  <version>   要发布的版本号，如 0.0.8（不带 v 前缀）

例：bash scripts/release-client.sh 0.0.8
EOF
}

[ "$#" -eq 1 ] || { usage; exit 2; }
VERSION="$1"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "版本号必须是 x.y.z 形式：$VERSION"

# ① 前置检查
[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || die "必须在 main 上发布"
[ -z "$(git status --porcelain)" ] || die "工作区不干净，先提交或清理"
git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null && die "tag v$VERSION 已存在（禁止移动已有 tag，请递增版本号）"
git ls-remote --exit-code --tags origin "v$VERSION" >/dev/null 2>&1 && die "远端已存在 tag v$VERSION（禁止移动已有 tag，请递增版本号）"
echo "✓ 在 main、工作区干净、tag v$VERSION 不存在"

# ② 改版本号 + 同步平台子包 + 一致性检查
python3 - "$VERSION" <<'PY'
import json, pathlib, re, sys
version = sys.argv[1]
root = pathlib.Path(".")

cargo = root / "client/Cargo.toml"
text = cargo.read_text(encoding="utf-8")
# 只替换 [package] 段的 version（文件里还有依赖的 version = "..."）
new, count = re.subn(r'(?m)^(version\s*=\s*)"[^"]*"', rf'\g<1>"{version}"', text, count=1)
assert count == 1, "client/Cargo.toml 里没找到 [package] 的 version 行"
cargo.write_text(new, encoding="utf-8")
print(f"  client/Cargo.toml → {version}")

for name in ("npm/package.json", "server.json"):
    path = root / name
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = version
    if "packages" in payload:  # server.json 的 packages[0].version 也要改
        payload["packages"][0]["version"] = version
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  {name} → {version}")
PY

python3 scripts/make-platform-packages.py generate >/dev/null
# Cargo.lock 里记录了 client 自身版本，用离线 workspace 更新刷新它。
( cd client && cargo update --workspace --offline >/dev/null 2>&1 ) || true
bash scripts/check-version.sh "v$VERSION"
python3 scripts/make-platform-packages.py check --allow-missing-binaries

# ③ 提交 → push main → push tag
git add client/Cargo.toml client/Cargo.lock npm/package.json server.json npm/platforms
git diff --cached --quiet && die "版本号已是 $VERSION，没有可提交的改动"
git commit -q -m "release: $NPM_PACKAGE v$VERSION"
git push origin main
git tag -a "v$VERSION" -m "$NPM_PACKAGE v$VERSION"
git push origin "v$VERSION"

echo
echo "✓ 已完成本地准备并 push tag v$VERSION"
echo "  CI 已触发，请查看 GitHub Actions。"
