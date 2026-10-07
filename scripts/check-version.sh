#!/usr/bin/env bash
# 版本一致性校验（发布前必须通过）。
#
# 为什么需要它：npm 包装器按 `v<package.json 版本>` 去 GitHub Release 找资产（旧路径），
# 而资产由 `.github/workflows/release.yml` 在 `v*` tag 时产出。三者一旦不一致，
# 用户 `npx nova-client` 会拿到 **HTTP 404** —— 这是最费时间排查的一类故障
# （表现是"包装器启动失败"，根因却在版本号）。
#
# TASK-MCP-NPM：主路径改为 **npm 平台子包**后，多个一类校验——6 个子包的版本号
# 必须与主包一致且列表不能缺平台（缺一个 = 该平台用户静默装不上）。
#
# 用法：
#   bash scripts/check-version.sh                 # 校验全部一致性
#   bash scripts/check-version.sh v0.1.0          # 额外校验与给定 tag 一致（CI/本地发布前用）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

cargo_version="$(grep -m1 '^version = ' client/Cargo.toml | sed 's/.*"\(.*\)".*/\1/')"
npm_version="$(node -p "require('./npm/package.json').version" 2>/dev/null || echo '')"
server_version="$(node -p "require('./server.json').version" 2>/dev/null || echo '')"

printf '%-22s %s\n' "client/Cargo.toml:" "$cargo_version"
printf '%-22s %s\n' "npm/package.json:" "$npm_version"
printf '%-22s %s\n' "server.json:" "$server_version"

status=0
if [ -z "$cargo_version" ] || [ -z "$npm_version" ] || [ -z "$server_version" ]; then
  echo "✗ 有文件读不到版本号（缺少 node？文件被改坏？）" >&2
  status=1
elif [ "$cargo_version" != "$npm_version" ] || [ "$cargo_version" != "$server_version" ]; then
  echo "✗ 三个版本号不一致：发布后 npm 包装器会去 GitHub 找一个不存在的 tag" >&2
  status=1
else
  echo "✓ 三个文件版本一致：$cargo_version"
fi

# npm 平台子包（TASK-MCP-NPM）：6 个子包的 version 与列表必须与主包一致，
# 否则主包的 optionalDependencies 会指向一个不存在的版本（该平台静默装不上）。
if command -v node >/dev/null 2>&1; then
  if node scripts/check-npm-platforms.js "$npm_version"; then
    echo "✓ npm 平台子包一致（6 个）"
  else
    echo "✗ npm 平台子包与主包不一致：跑 python3 scripts/make-platform-packages.py generate" >&2
    status=1
  fi
fi

if [ "$#" -ge 1 ]; then
  tag="${1#v}"  # 允许传 v0.1.0 或 0.1.0
  printf '%-22s %s\n' "待发布 tag:" "$tag"
  if [ "$tag" != "$cargo_version" ]; then
    echo "✗ tag（$tag）与版本号（$cargo_version）不一致：Release 资产会挂在另一个 tag 下" >&2
    status=1
  else
    echo "✓ tag 与版本号一致"
  fi
fi

if [ "$status" -ne 0 ]; then
  echo
  echo "提示：npm 不允许重复发布同一版本号；修好版本号后记得三处一起改。" >&2
fi
exit "$status"
