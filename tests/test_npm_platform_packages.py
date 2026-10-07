"""平台子包与包装器平台表一致性（D-48/D-49 的护栏）。

两项“静默故障”类约定都在这里守住：

- 主包 ``optionalDependencies`` 与 6 个子包的版本/平台字段不一致 → 该平台用户**静默**装不上
  （npm 不报错，只是跳过解析不了的可选依赖）；
- ``npm/run.js`` 的平台表、CI 矩阵的 ``npm_suffixes`` 与生成脚本的 ``PLATFORMS`` 漂移 →
  包装器按 A 名字找、npm 按 B 条件装，两边对不上。

因此断言直接读三个真实文件（不复制一份常量表），漂移会立刻失败。
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]



def _code_lines(text: str) -> str:
    """去掉注释行，只留可执行内容。

    为什么需要：这些断言守的是"行为"，而注释里**解释**"我们为什么不做 X"是好事
    （例如脚本开头写明"不使用 --force"）。若连注释一起禁字，就会把正确的文档判成违规
    —— 实测踩到过（lipo / NPM_TOKEN / npm publish 三处假阳性）。
    """
    kept = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _load_generator():
    """加载 ``scripts/make-platform-packages.py``（文件名带连字符，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location(
        "make_platform_packages", ROOT / "scripts/make-platform-packages.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mk = _load_generator()


def test_platforms_and_packages_agree():
    main = json.loads((mk.NPM_DIR / "package.json").read_text(encoding="utf-8"))
    optional = main.get("optionalDependencies", {})
    suffixes = {s["suffix"] for s in mk.PLATFORMS}
    assert set(optional) == {f"{mk.MAIN_PACKAGE_NAME}-{s}" for s in suffixes}
    for value in optional.values():
        assert value == main["version"]
    for item in mk.PLATFORMS:
        pkg = json.loads(
            (mk.sub_package_dir(item["suffix"]) / "package.json").read_text(encoding="utf-8")
        )
        assert pkg["os"] == [item["os"]]
        assert pkg["cpu"] == [item["cpu"]]
        assert pkg["version"] == main["version"]
        assert "bin" not in pkg


def test_run_js_platform_table_covers_script_table():
    text = (mk.NPM_DIR / "run.js").read_text(encoding="utf-8")
    for item in mk.PLATFORMS:
        assert f'"{item["suffix"]}"' in text
        assert f'"{item["os"]}"' in text


def test_platforms_match_node_conventions():
    assert {s["os"] for s in mk.PLATFORMS} == {"linux", "darwin", "win32"}
    assert {s["cpu"] for s in mk.PLATFORMS} == {"x64", "arm64"}


def test_workflow_matrix_covers_every_suffix():
    """CI 矩阵必须覆盖平台表的每一项（每个平台一个 matrix 条目 + 一个 artifact）。"""
    text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s+- suffix:\s*(\S+)\s*$", text, flags=re.MULTILINE))
    assert declared == {s["suffix"] for s in mk.PLATFORMS}
    # artifact 也要按平台分（同名文件不能用 merge-multiple 汇总）
    assert "name: binary-${{ matrix.suffix }}" in text


def test_check_version_script_passes():
    result = subprocess.run(
        ["bash", "scripts/check-version.sh"], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------- #
# 下面几条守的是“发布形态”本身（D-48/D-49）。
# 它们都是**静默故障**类约定：破了不会报错，只会在用户侧表现为“装不上/拿错二进制”。
# --------------------------------------------------------------------------- #


def test_run_js_has_no_github_download_fallback():
    """包装器不得出现任何 GitHub 下载路径（D-49：npm 是唯一分发渠道）。

    为何要守：保留下载回退 = 两条分发渠道并存，出错时无法判断用户拿到的是哪个二进制。
    且 Node 默认不读 `https_proxy`，下载在代理环境下会以 403 限流形式静默失败。
    """
    text = _code_lines((mk.NPM_DIR / "run.js").read_text(encoding="utf-8"))
    for banned in ("api.github.com", "releases/tags", "browser_download_url", "downloadToFile"):
        assert banned not in text, f"run.js 里还有 GitHub 下载残留：{banned}"
    # 不应 require https（下载专用）；也不应再写缓存目录
    assert "require(\"https\")" not in text
    assert "cacheDir" not in text, "缓存目录机制应一并删除（它是旧下载路径的一部分）"


def test_main_package_publishes_to_next_not_latest():
    """主包默认发 `next`（不是 `latest`）——npm 发布不可逆，latest 必须是最后一个可控点。"""
    text = (ROOT / "scripts/make-platform-packages.py").read_text(encoding="utf-8")
    # 默认 tag 必须是 next
    assert 'default="next"' in text
    # 不得有直接发 latest 的写法
    assert '"--tag", "latest"' not in text
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "--phase main --tag next" in workflow


def test_promote_is_separate_and_after_smoke():
    """⑤ 必须拆成 pre-verify →（publish-main）→ smoke → promote → post-verify。"""
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    for job in ("pre-verify:", "publish-main:", "promote:", "post-verify:"):
        assert job in workflow, f"缺少发布阶段：{job}"
    # job 依赖顺序必须是 子包 → 前置验证 → 主包 → promote → 后置验证
    assert "needs: publish-platforms" in workflow
    assert "needs: pre-verify" in workflow
    assert "needs: publish-main" in workflow
    assert "needs: promote" in workflow
    # promote job 里必须先冒烟、再 promote（顺序写在同一个 job 的 steps 里）
    promote_job = workflow[workflow.index("\n  promote:") : workflow.index("\n  post-verify:")]
    smoke_idx = promote_job.index("smoke --tag next")
    promote_idx = promote_job.index("make-platform-packages.py promote")
    assert smoke_idx < promote_idx, "promote 之前必须先跑冒烟"


def test_macos_builds_per_arch_without_lipo():
    """macOS 两架构**独立构建**，不得再用 universal/lipo（D-48 补充）。"""
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    code = _code_lines(workflow)
    assert "lipo" not in code, "不应再用 lipo 合并 universal"
    assert "universal-apple-darwin" not in code
    assert "x86_64-apple-darwin" in workflow
    assert "aarch64-apple-darwin" in workflow
    # 两个 macOS 架构各占一个矩阵条目
    assert "suffix: darwin-x64" in workflow
    assert "suffix: darwin-arm64" in workflow


def test_verify_has_platforms_and_latest_phases():
    """验证必须分阶段：子包发布前验 platforms，全流程后验 latest。"""
    text = (ROOT / "scripts/make-platform-packages.py").read_text(encoding="utf-8")
    assert '"platforms", "latest"' in text.replace("\n", " ") or "platforms" in text
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "verify --phase platforms" in workflow
    assert "verify --phase latest" in workflow


def test_no_win32_in_package_names():
    """npm 包名不得含 `win32-`（真实故障：`Package name triggered spam detection`）。

    实测（2026-09-17）：`nova-client-win32-x64` 发布被 registry 拒绝，
    完整错误 `403 Forbidden - PUT .../nova-client-win32-x64 - Package name triggered
    spam detection`。同一次发布里前 4 个包（linux/darwin）全部成功，
    换成 `nova-client-windows-x64` 后正常——`win32` 是恶意软件命名的常见特征词。

    注意：**npm 包名**用 `windows`，而 package.json 的 `os` 字段必须仍是 `win32`
    （那是 Node `process.platform` 的取值，不能被"统一"掉）。
    """
    main = json.loads((mk.NPM_DIR / "package.json").read_text(encoding="utf-8"))
    for name in list(main.get("optionalDependencies", {})) + [main["name"]]:
        assert "win32" not in name, f"npm 包名不得含 win32（会被 spam detection 拦）：{name}"
    for spec in mk.PLATFORMS:
        assert "win32" not in f"{mk.MAIN_PACKAGE_NAME}-{spec['suffix']}"
        if spec["os"] == "win32":
            # os 字段必须仍是 win32（Node 的取值），别跟着包名一起改名
            assert spec["suffix"].startswith("windows-")
    # run.js 的 platform 字段也必须是 win32
    text = (mk.NPM_DIR / "run.js").read_text(encoding="utf-8")
    assert 'platform: "win32"' in text


# --------------------------------------------------------------------------- #
# 发布入口（scripts/release-client.sh）：它才是"每次做法一致"的保证。
# 文档只能防遗忘，所以这里把它写死的几条纪律也钉成断言。
# --------------------------------------------------------------------------- #


def _release_script() -> str:
    return (ROOT / "scripts/release-client.sh").read_text(encoding="utf-8")


def test_release_entry_script_exists_and_is_executable():
    path = ROOT / "scripts/release-client.sh"
    assert path.is_file(), "缺少统一发布入口 scripts/release-client.sh"
    import os

    assert os.access(path, os.X_OK), "发布入口必须可执行（chmod +x）"


def test_release_script_never_forces_or_moves_tags():
    """硬约定：禁止 force push、禁止移动/重写已有 tag。"""
    text = _code_lines(_release_script())
    for banned in ("--force", "push --delete", "tag -f", "tag --force"):
        assert banned not in text, f"发布入口不得出现 {banned!r}"
    # 必须显式拒绝已存在的 tag（本地与远端都要查）
    assert "refs/tags/v$VERSION" in text
    assert "ls-remote --exit-code --tags origin" in text


def test_release_script_never_touches_npm_token():
    """发布凭据只存在于 GitHub Actions secret；本地脚本不得读取或打印。"""
    text = _code_lines(_release_script())
    assert "NPM_TOKEN" not in text, "本地发布入口不得引用 NPM_TOKEN"
    assert "npm publish" not in text, "本地发布入口绝不直接发布平台包（由 CI 负责）"
    assert "npm login" not in text


def test_release_script_defers_publishing_to_ci():
    """六平台构建与发布全部交 CI：本地脚本只 push tag，不等 CI、不查 registry。"""
    text = _code_lines(_release_script())
    assert "make-platform-packages.py publish" not in text
    # 本地不等待 CI：没有 gh watch / sleep 轮询 / npm 复核
    for banned in ("gh run watch", "gh run list", "sleep ", "npm view", "dist-tags"):
        assert banned not in text, f"发布入口不应包含 {banned!r}（CI 职责，见 npm.md）"
    # 必须真的把 tag 推出去（这是触发 CI 的唯一动作）
    assert 'git push origin "v$VERSION"' in text


def test_release_script_runs_version_check():
    text = _release_script()
    assert "check-version.sh" in text
    assert "make-platform-packages.py generate" in text
    assert "make-platform-packages.py check" in text


def test_verify_retry_budget_tolerates_registry_propagation():
    """verify 的重试预算必须够长，否则**成功的发布会判成失败**。

    真实故障（0.0.6）：`pre-verify` 在 60s 内宣告「查不到」，而包其实已发布成功
    （publish-platforms 是绿的）—— registry 读端缓存传播了约 4 分钟
    （6 个子包在 02:47:47-02:51:52 陆续可读）。
    预算太短会导致错误处置（"放弃版本号"），故钉住下限。
    """
    import re

    text = (ROOT / "scripts/make-platform-packages.py").read_text(encoding="utf-8")
    attempts = int(
        re.search(r'"--attempts",\s*\n\s*type=int,\s*\n\s*default=(\d+)', text).group(1)
    )
    interval = float(
        re.search(r'"--interval",\s*\n\s*type=float,\s*\n\s*default=([0-9.]+)', text).group(1)
    )
    seconds = attempts * interval
    assert seconds >= 300, f"verify 重试预算仅 {seconds:.0f}s，不足以覆盖 registry 传播（应 ≥300s）"
