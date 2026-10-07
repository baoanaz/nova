#!/usr/bin/env python3
"""生成 npm 平台子包（TASK-MCP-NPM）：把二进制随 npm 走，不再依赖 GitHub Release。

## 为什么 (背景，实测踩到)

旧形态是「`zace-client` 包装器 + GitHub Release 下载二进制」。两个真实故障：

1. **Node 默认不读 `https_proxy`**（只认 `NODE_USE_ENV_PROXY=1`，v20+ 才支持）。
   代理环境里包装器直连 GitHub → 命中共享出口 IP 的 API 限流：
   实测同一时刻 `curl`（走代理）200，而 `node`（直连）403 rate limit exceeded。
   用户看到的是「MCP server failed to start: connection closed」，
   既不像网络问题也不像版本问题，排查代价极高。
2. **版本对齐是人为纪律**：`npm publish` 必须等 Release 五个平台资产全绿。
   慢一步或漏一个平台，用户就是 404。

## 现在的形态（esbuild / swc / biome 的通行做法）

```text
zace-client                    ← 包装器（run.js）+ 6 个平台子包作为 optionalDependencies
├── zace-client-linux-x64      ← 内含 bin/zace-client
├── zace-client-linux-arm64
├── zace-client-darwin-x64
├── zace-client-darwin-arm64
├── zace-client-windows-x64
└── zace-client-windows-arm64
```

npm 按**子包自己的 `os`/`cpu` 字段**挑一个装上（其余跳过），包装器直接执行它。
收益：**无网络下载步骤、无版本对齐、无 GitHub 限流影响，用户零配置**。

## 用法

```bash
# ① 打包（CI 在每个平台矩阵里跑一次，把产物放进对应子包目录）
python3 scripts/make-platform-packages.py stage \
    --suffix linux-x64 --binary path/to/zace-client

# ② 生成/刷新 6 个 package.json（改版本号或平台表后跑；幂等）
python3 scripts/make-platform-packages.py generate

# ③ 发布（先子包、后主包——主包的可选依赖要能解析到已发布的版本）
python3 scripts/make-platform-packages.py publish --dry-run
```

平台表是**唯一事实来源**，`npm/run.js` 的 `PLATFORMS` 与 `.github/workflows/release.yml`
的矩阵都按它对齐；`check-version.sh` 负责守住版本号一致。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NPM_DIR = ROOT / "npm"
PLATFORMS_DIR = NPM_DIR / "platforms"

#: 平台表（唯一事实来源）。``os``/``cpu`` 必须与 Node 的 ``process.platform`` /
#: ``process.arch`` 取值一致——npm 就是靠这两个字段决定装哪个子包。
PLATFORMS = (
    {"suffix": "linux-x64", "os": "linux", "cpu": "x64", "binary": "zace-client"},
    {"suffix": "linux-arm64", "os": "linux", "cpu": "arm64", "binary": "zace-client"},
    {"suffix": "darwin-x64", "os": "darwin", "cpu": "x64", "binary": "zace-client"},
    {"suffix": "darwin-arm64", "os": "darwin", "cpu": "arm64", "binary": "zace-client"},
    {"suffix": "windows-x64", "os": "win32", "cpu": "x64", "binary": "zace-client.exe"},
    {"suffix": "windows-arm64", "os": "win32", "cpu": "arm64", "binary": "zace-client.exe"},
)

MAIN_PACKAGE_NAME = "zace-client"


def sub_package_name(suffix: str) -> str:
    return f"{MAIN_PACKAGE_NAME}-{suffix}"


def sub_package_dir(suffix: str) -> Path:
    return PLATFORMS_DIR / suffix


def main_version() -> str:
    return json.loads((NPM_DIR / "package.json").read_text(encoding="utf-8"))["version"]


def _payload(spec: dict, version: str) -> dict:
    """一个平台子包的 package.json 内容（确定性地生成，便于 diff 审查）。"""
    name = sub_package_name(spec["suffix"])
    return {
        "name": name,
        "version": version,
        "description": (
            f"Platform binary for {MAIN_PACKAGE_NAME} "
            f"({spec['os']}/{spec['cpu']}). Installed automatically by {MAIN_PACKAGE_NAME}; "
            "do not install directly."
        ),
        "license": "Unlicense",
        "repository": {
            "type": "git",
            "url": "git+https://github.com/baoanaz/zace.git",
        },
        "homepage": "https://github.com/baoanaz/zace#readme",
        # **没有 bin 字段**：这些包不是给用户直接用的命令，只是二进制载体。
        # 加 bin 会让 npm 在 6 个平台里都尝试建 shim（其余平台会因 os/cpu 不匹配而跳过，
        # 但本平台的 shim 会与主包的 bin 冲突）。由 run.js 按路径直接执行。
        "files": [spec["binary"]],
        "os": [spec["os"]],
        "cpu": [spec["cpu"]],
        # 平台包不开 lifecycle 脚本（安装即落文件，无可执行逻辑）。
        "scripts": {},
    }


def cmd_generate(args: argparse.Namespace) -> int:
    version = args.version or main_version()
    written: list[Path] = []
    for spec in PLATFORMS:
        directory = sub_package_dir(spec["suffix"])
        directory.mkdir(parents=True, exist_ok=True)
        payload = _payload(spec, version)
        target = directory / "package.json"
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
            written.append(target)
        # 子包目录里放一个 .gitkeep，保证「目录存在」这件事也进 git
        # （二进制本身不进 git，由 CI 在发布前 stage 进来）。
        keep = directory / ".gitkeep"
        if not keep.is_file():
            keep.write_text("", encoding="utf-8")
    print(f"版本 {version}；新写入 {len(written)} 个 package.json")
    for path in written:
        print(f"  {path.relative_to(ROOT)}")
    if not written:
        print("  （全部已是最新）")
    _sync_main_optional_dependencies(version, dry_run=False)
    return 0


def _sync_main_optional_dependencies(version: str, *, dry_run: bool) -> None:
    """把 6 个平台子包写进主包的 ``optionalDependencies``（唯一需要手改主包的地方）。"""
    path = NPM_DIR / "package.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    wanted = {sub_package_name(spec["suffix"]): version for spec in PLATFORMS}
    if payload.get("optionalDependencies") == wanted:
        print("  optionalDependencies 已是最新")
        return
    payload["optionalDependencies"] = wanted
    # 保持键顺序稳定：name, version, description, license, repository, bugs,
    # homepage, keywords, bin, files, engines, os, cpu, optionalDependencies。
    ordered = {}
    for key in (
        "name",
        "version",
        "description",
        "license",
        "repository",
        "bugs",
        "homepage",
        "keywords",
        "bin",
        "files",
        "engines",
        "os",
        "cpu",
        "optionalDependencies",
    ):
        if key in payload:
            ordered[key] = payload[key]
    for key, value in payload.items():
        ordered.setdefault(key, value)
    if dry_run:
        print(f"  [dry-run] 将把 optionalDependencies 设为 {len(wanted)} 个子包")
        return
    path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  已更新 {path.relative_to(ROOT)} 的 optionalDependencies（{len(wanted)} 个子包）")


def _preflight(suffix: str, source: Path) -> list[str]:
    """暂存前的**产物属性校验**（返回问题列表，空 = 通过）。

    为什么必做（真实缺陷模式）：CI 与本地可能编出**不同 flavour 的同名二进制**——
    实测 CI 用 `x86_64-unknown-linux-musl`（静态）、本地默认编的是
    `x86_64-unknown-linux-gnu`（动态）。两者名字都是 `zace-client`、都能跑，
    但打出来的包可移植性天差地别。若把本地那个 stage 进 `linux-x64` 子包，
    用户在一个较旧的发行版上会报 `GLIBC_2.xx not found`。

    同时校对**平台族**：把 ELF/Mach-O/PE 搞反了是灾难性的，
    而 `file(1)` 的输出在三种格式上足够可区分（含 Windows 的交叉编译产物）。
    """
    problems: list[str] = []
    result = subprocess.run(
        ["file", "-b", str(source)], capture_output=True, text=True, check=False
    )
    kind = (result.stdout or "").strip() if result.returncode == 0 else ""
    spec = next(item for item in PLATFORMS if item["suffix"] == suffix)
    os_family = spec["os"]

    if kind:
        actual = (
            "elf"
            if "ELF" in kind
            else "macho"
            if ("Mach-O" in kind or "universal binary" in kind)
            else "pe"
            if ("PE32" in kind or "MS-DOS" in kind)
            else "unknown"
        )
        expected = {"linux": "elf", "darwin": "macho", "win32": "pe"}[os_family]
        if actual != expected:
            problems.append(
                f"产物格式是 {actual}（{kind[:60]}），但 {suffix} 需要 {expected}——"
                "可能是交叉编译工具链选错，或 stage 错了文件"
            )
        if os_family == "linux" and "dynamically linked" in kind:
            problems.append(
                "linux 产物是**动态链接**的：CI 用 musl 目标（静态）以免依赖具体发行版的 glibc，"
                "动态产物在旧发行版上会 `GLIBC_2.xx not found`。"
                "请用 `cargo build --release --target x86_64-unknown-linux-musl`（需要 musl-tools）"
                "或 `cross build --release --target <musl target>`"
            )
        if os_family == "darwin":
            # 两架构**各自独立构建**（不用 lipo/universal）：子包机制下 x64 与 arm64
            # 本就是两个包，合并只会让用户多下另一个架构的代码（体积翻倍）。
            # 故这里校验架构对不对，而不是校验「是不是 universal」。
            want = {"darwin-x64": "x86_64", "darwin-arm64": "arm64"}[suffix]
            if want not in kind:
                problems.append(
                    f"macOS 产物架构不对：{kind[:60]}（期望 {want}）。"
                    "两个架构必须分别在对应目标上构建"
                    "（`cargo build --release --target {x86_64,aarch64}-apple-darwin`）"
                )
    return problems


def cmd_stage(args: argparse.Namespace) -> int:
    """把一个平台的二进制放进对应子包目录（CI 用；主包 version 必须已与子包一致）。"""
    spec = next((item for item in PLATFORMS if item["suffix"] == args.suffix), None)
    if spec is None:
        available = "、".join(item["suffix"] for item in PLATFORMS)
        print(f"✗ 未知平台：{args.suffix}（可选：{available}）", file=sys.stderr)
        return 2
    source = Path(args.binary).expanduser()
    if not source.is_file():
        print(f"✗ 二进制不存在：{source}", file=sys.stderr)
        return 2
    problems = _preflight(args.suffix, source)
    if problems and not args.allow_unsafe_binary:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "✗ 产物属性校验未通过。确认是有意为之就加 `--allow-unsafe-binary`。",
            file=sys.stderr,
        )
        return 1
    cmd_generate(argparse.Namespace(version=None))
    directory = sub_package_dir(spec["suffix"])
    target = directory / spec["binary"]
    shutil.copy2(source, target)
    if spec["os"] != "win32":
        target.chmod(0o755)
    size = target.stat().st_size
    print(f"已暂存 {source} → {target.relative_to(ROOT)}（{size} 字节）")
    return 0


def _registry_version(package: str) -> str | None:
    """该包在 registry 上的 latest 版本（查不到 / 无网络 / 未登录 → ``None``）。

    用途：“这个版本是否已经发过”。npm **不允许重发同一版本**，而发布途中可能因
    网络/鉴权在**第 3 个包**上失败；恢复时不能从头发一遍（前两个会报
    `cannot publish over` 而中断）。故发布前先探测，跳过已存在的。
    """
    try:
        result = subprocess.run(
            ["npm", "view", package, "versions", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        published = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    versions = published if isinstance(published, list) else [published]
    return versions[-1] if versions else None


def _already_published(package: str, version: str) -> bool:
    """该包的**这个版本**是否已在 registry 上（用于断点续发）。

    只把“已登录且能查到”当依据：查不到（未登录/无网络）时返回 ``False``，
    让真正的 ``npm publish`` 去报错——不把“探测失败”当成“已发布”。
    """
    try:
        result = subprocess.run(
            ["npm", "view", f"{package}@{version}", "version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and version in result.stdout


def cmd_check(args: argparse.Namespace) -> int:
    """**发布前**的硬门：6 个平台包都真有非空二进制，且平台字段一致。

    为什么必须做（真实故障模式）：缺一个平台 = 该平台用户装不上，
    而 npm 对解析不了的可选依赖是**静默跳过**的——用户侧无任何提示，
    只在 `npx zace-client` 时才表现为“没有二进制”。故必须在发布前失败，
    而不是发布后发现。
    """
    version = main_version()
    problems: list[str] = []
    for spec in PLATFORMS:
        name = sub_package_name(spec["suffix"])
        directory = sub_package_dir(spec["suffix"])
        binary = directory / spec["binary"]
        if not binary.is_file():
            if args.allow_missing_binaries:
                # 本地发布入口用：二进制只存在于 CI 的构建产物里，
                # 本地不可能（也不应该）有 6 平台产物。此时只校验元数据一致性，
                # 产物齐备由 CI 的 `check`（不带本开关）负责。
                pass
            else:
                problems.append(f"{name}：缺二进制 {spec['binary']}（跑 stage）")
            continue
        size = binary.stat().st_size
        if size < 64 * 1024:
            # 真二进制是数 MB 级；几十 KB 通常说明 stage 了错东西（或空文件）。
            problems.append(f"{name}：二进制仅 {size} 字节，不像是真产物")
        package_json = directory / "package.json"
        if not package_json.is_file():
            problems.append(f"{name}：缺 package.json")
            continue
        payload = json.loads(package_json.read_text(encoding="utf-8"))
        if payload.get("version") != version:
            problems.append(f"{name}：version {payload.get('version')} != {version}")
        if payload.get("os") != [spec["os"]] or payload.get("cpu") != [spec["cpu"]]:
            problems.append(f"{name}：os/cpu 字段与平台表不符")
    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "✗ 平台包检查未通过：不要发布。缺一个平台就是该平台用户**静默**装不上。",
            file=sys.stderr,
        )
        return 1
    print(f"✓ 6 个平台包均就位（版本 {version}）")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """分阶段发布：``--phase platforms``（6 个子包）或 ``--phase main``（主包）。

    **必须分两次调用**（而不是一个循环全发完），理由：

    - 与发布流水线一一对应（② 发子包 → ③ 验证 → ④ 发主包），CI 才能在
      “子包发完”与“主包发出去”之间插一道验证；
    - 发布不可逆。若一并发完，中途失败时无法区分“子包没发齐”与“主包已出去了”。

    ``--tag``：主包默认发到 **``next``** 而不是 ``latest``。

    为什么主包不直接发 latest：npm 发布不可逆，直接发 latest 意味着一旦子包有问题，
    所有 `npx zace-client@latest` 的用户**立刻**拿到坏包，只能靠升版本修。
    先发 next 则 latest 仍指向旧的好版本，留出验证与补救窗口（后续 ``promote`` 切换）。
    子包不支持 dist-tag 玩法（它们是被主包按版本精确引用的），故 ``--tag`` 仅对主包生效。

    断点续发：已存在于 registry 的包会**跳过**（npm 不允许重发同一版本，
    而失败重跑不应因为前几个已发过而卡死）。
    """
    cmd_generate(argparse.Namespace(version=None))
    version = main_version()
    if args.phase == "platforms":
        targets = [
            (sub_package_name(spec["suffix"]), sub_package_dir(spec["suffix"]))
            for spec in PLATFORMS
        ]
        tag = None
    else:
        targets = [(MAIN_PACKAGE_NAME, NPM_DIR)]
        tag = args.tag

    command = ["npm", "publish", "--access", "public"]
    if tag:
        command += ["--tag", tag]
    if args.dry_run:
        command.append("--dry-run")
    for name, directory in targets:
        if not args.dry_run and _already_published(name, version):
            print(f"=== {name}@{version} 已在 registry 上，跳过")
            continue
        suffix = f" --tag {tag}" if tag else ""
        print(f"\n=== {name}@{version}: {' '.join(command)}  （cwd={directory.name}）{suffix}")
        result = subprocess.run(command, cwd=directory, check=False)
        if result.returncode != 0:
            print(f"✗ {name} 发布失败（退出码 {result.returncode}）", file=sys.stderr)
            return result.returncode
    print(f"\n✓ {args.phase} 阶段完成")
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    """**真实安装 + 启动**的冒烟测试（``promote`` 前的最后一道门）。

    为什么要真跑一次 ``npx``：前面的 ``verify`` 只能证明「包存在」，
    证明不了「装上后能启动」——而真用户的路径就是 npx 起 MCP server。
    实测踩过的坑：zip 会丢可执行权限、artifact 混淆可能把别的平台二进制打进包，
    这两类问题**只有真跑一次**才能发现。

    做法：在一个空目录里 `npx --yes --prefer-online zace-client@<tag> --help`，
    以退出码与 stdout 判定（``--help`` 不连服务，不需要 token）。
    """
    version = main_version()
    spec = f"{MAIN_PACKAGE_NAME}@{args.tag or version}"
    workdir = ROOT / ".smoke"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "package.json").write_text('{"name":"smoke","private":true}\n', encoding="utf-8")
    command = ["npx", "--yes", "--prefer-online", spec, "--help"]
    print(f"=== 冒烟：{' '.join(command)}（cwd={workdir}）")
    result = subprocess.run(command, cwd=workdir, capture_output=True, text=True, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    print(output[-2000:] if output else "（无输出）")
    shutil.rmtree(workdir, ignore_errors=True)
    if result.returncode != 0:
        print(f"✗ 冒烟失败（退出码 {result.returncode}）——不要 promote", file=sys.stderr)
        # npx 找不到子包时会退出非 0，这正是要拦的情形。
        return 1
    if "Usage:" not in output and "--base-url" not in output:
        print("✗ 冒烟输出里没有 usage —— 启动的可能是别的东西", file=sys.stderr)
        return 1
    print(f"✓ 冒烟通过（{spec} 能安装并启动）")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """把 ``latest`` 切到当前版本（**发布后的唯一可控点**）。

    为什么单独一步而不在 publish 里带 ``--tag latest``：
    切 latest 之前必须先验证（``smoke``），而这需要是**一次独立的、可控的**动作；
    且它可重试（幂等），不像 publish 那样不可逆。
    """
    version = main_version()
    command = ["npm", "dist-tag", "add", f"{MAIN_PACKAGE_NAME}@{version}", "latest"]
    print(f"=== {' '.join(command)}")
    result = subprocess.run(command, cwd=NPM_DIR, check=False)
    if result.returncode != 0:
        print(f"✗ 切 latest 失败（退出码 {result.returncode}）", file=sys.stderr)
        return result.returncode
    print(f"✓ latest → {version}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """**分阶段**验证（registry 同步有秒级延迟，故带重试）。

    - ``--phase platforms``（主包发布**前**）：6 个子包都能查到该版本 ← 缺一就停
    - ``--phase latest``（全流程**后**）：7 个包 + ``latest`` 指向该版本
    """
    version = main_version()
    if args.phase == "platforms":
        expected = [sub_package_name(spec["suffix"]) for spec in PLATFORMS]
    else:
        expected = [MAIN_PACKAGE_NAME] + [
            sub_package_name(spec["suffix"]) for spec in PLATFORMS
        ]
    attempts = max(1, args.attempts)
    missing: list[str] = []
    for attempt in range(1, attempts + 1):
        missing = []
        for name in expected:
            if _already_published(name, version):
                print(f"  ✓ {name}@{version}")
            else:
                print(f"  ✗ {name}@{version} 查不到（第 {attempt}/{attempts} 次）")
                missing.append(name)
        if not missing:
            break
        if attempt < attempts:
            # registry 的**读端点缓存**传播可能长达数分钟：实测 0.0.6 一次发布里，
            # 6 个包在 02:47:47-02:51:52 陆续可读，而 pre-verify 在 02:47:55-02:49:08
            # 跑完（60s 预算）——于是它报"查不到"，而包其实**已经发布成功**
            # （publish-platforms 是绿的）。故默认给到约 6 分钟。
            time.sleep(args.interval)
    problems = list(missing)
    if args.phase == "latest":
        latest = _registry_version(MAIN_PACKAGE_NAME)
        if latest == version:
            print(f"  ✓ dist-tag latest = {latest}")
        else:
            print(f"  ✗ dist-tag latest = {latest}（期望 {version}）")
            problems.append("latest")
    if problems:
        print("\n✗ 验证未通过。", file=sys.stderr)
        if missing and args.phase == "platforms":
            print(
                "  ⚠️ 子包不齐：**不要发主包**（该平台用户会静默装不上）。",
                file=sys.stderr,
            )
            print(
                "  先区分两件事：① 真的没发出去（看 publish-platforms job 的日志）；"
                "② 只是 diff 读端缓存还没传播（publish 是绿的、几分钟后再查就有）。",
                file=sys.stderr,
            )
        if args.phase == "latest":
            print(
                f"  latest 不对：`npm dist-tag add {MAIN_PACKAGE_NAME}@{version} latest`",
                file=sys.stderr,
            )
        return 1
    print(f"✓ {args.phase} 验证通过（版本 {version}）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成/暂存/校验/发布 npm 平台子包")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="生成 6 个平台子包的 package.json（幂等）")
    generate.add_argument("--version", help="覆盖版本号（默认取 npm/package.json）")
    generate.set_defaults(func=cmd_generate)

    stage = sub.add_parser("stage", help="把某平台的二进制放进对应子包目录")
    stage.add_argument("--suffix", required=True, help="平台后缀，如 linux-x64")
    stage.add_argument("--binary", required=True, help="已构建的二进制路径")
    stage.add_argument(
        "--allow-unsafe-binary",
        action="store_true",
        help="跳过产物属性校验（静态链接 / universal / 平台族）；仅用于有意的本地试验",
    )
    stage.set_defaults(func=cmd_stage)

    check = sub.add_parser("check", help="发布前硬门：6 个平台包都真有二进制")
    check.add_argument(
        "--allow-missing-binaries",
        action="store_true",
        help="只校验元数据，不要求二进制就位（本地发布入口用；CI 必须不带本开关）",
    )
    check.set_defaults(func=cmd_check)

    publish = sub.add_parser("publish", help="分阶段发布（先 platforms、后 main）")
    publish.add_argument(
        "--phase",
        choices=("platforms", "main"),
        default="platforms",
        help="platforms = 6 个子包；main = 主包",
    )
    publish.add_argument(
        "--tag",
        default="next",
        help="主包的 dist-tag（默认 next，验证后再 promote 到 latest）",
    )
    publish.add_argument("--dry-run", action="store_true", help="只打印不发布")
    publish.set_defaults(func=cmd_publish)

    smoke = sub.add_parser("smoke", help="真实 npx 安装+启动冒烟（promote 前的门）")
    smoke.add_argument("--tag", default="next", help="要测的 dist-tag（默认 next）")
    smoke.set_defaults(func=cmd_smoke)

    promote = sub.add_parser("promote", help="把 latest 切到当前版本")
    promote.set_defaults(func=cmd_promote)

    verify = sub.add_parser("verify", help="分阶段验证（platforms / latest）")
    verify.add_argument(
        "--phase",
        choices=("platforms", "latest"),
        default="platforms",
        help="platforms = 主包发布前验子包；latest = 全流程后验 7 包 + latest",
    )
    verify.add_argument(
        "--attempts",
        type=int,
        default=24,
        help="registry 同步延迟导致的重试次数（默认 24 次 × 15s ≈ 6 分钟）",
    )
    verify.add_argument(
        "--interval",
        type=float,
        default=15.0,
        help="重试间隔秒数（默认 15s）",
    )
    verify.set_defaults(func=cmd_verify)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
