"""TASK-013 §A/DoD：D-29 project identity 与项目数据目录。

DoD 原文：同一 repo 路径两次 resolve → 同 project_id；`.git` 目录存在但无 remote → 路径 hash 分支。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from nova_core.engine import (
    DATA_ROOT_ENV,
    DEFAULT_DATA_ROOT,
    PROJECT_META_FILENAME,
    Engine,
    EngineError,
    project_id_for,
    repo_identity,
    resolve_data_root,
)
from nova_core.interfaces import ContextEngine


def _git_repo(path: Path, *, remote: str | None) -> Path:
    """建一个真 git 仓库（可选配 origin）；``git init`` 失败则跳过测试。"""
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("# fixture\n", encoding="utf-8")
    try:
        subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - 环境无 git
        pytest.skip(f"环境不可用 git：{exc}")
    if remote:
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", remote],
            check=True,
            capture_output=True,
        )
    return path


def test_same_repo_path_resolves_to_same_project(repo: Path, engine: Engine) -> None:
    first, first_identity = engine.resolve_repo(repo)
    second, second_identity = engine.resolve_repo(repo)

    assert first.project_id == second.project_id
    assert first_identity.identity_key == second_identity.identity_key
    assert first.created is True and second.created is False

    meta_path = engine.data_root / "projects" / first.project_id / PROJECT_META_FILENAME
    assert meta_path.is_file(), "项目元数据必须落在 {data_root}/projects/{project_id}/"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["project_id"] == first.project_id
    assert meta["identity_key"] == first_identity.identity_key
    assert meta["display_name"] == repo.name
    assert project_id_for(first_identity.identity_key) == first.project_id


def test_git_remote_identity_is_shared_across_checkout_paths(tmp_path: Path) -> None:
    """D-29 的核心价值：两台机器/两个路径 clone 同一 repo → 同一个项目。"""
    remote = "https://example.com/team/nova.git"
    left = _git_repo(tmp_path / "machine-a" / "nova", remote=remote)
    right = _git_repo(tmp_path / "machine-b" / "workspace" / "nova", remote=remote)

    assert repo_identity(left).identity_key == repo_identity(right).identity_key
    assert project_id_for(repo_identity(left).identity_key) == project_id_for(
        repo_identity(right).identity_key
    )

    # 同一 monorepo 的不同子目录是不同项目（identity 含 git 根内相对路径）。
    sub = left / "core"
    sub.mkdir()
    sub_identity = repo_identity(sub)
    assert sub_identity.repo_path == "core"
    assert sub_identity.identity_key != repo_identity(left).identity_key
    expected = hashlib.sha256((remote + "core").encode("utf-8")).hexdigest()
    assert sub_identity.identity_key == expected


def test_git_dir_without_remote_falls_back_to_path_hash(tmp_path: Path) -> None:
    """.git 存在但无 remote → 路径 hash 分支（DoD 明文要求）。"""
    bare = _git_repo(tmp_path / "no-remote", remote=None)
    identity = repo_identity(bare)
    assert identity.remote_url is None
    assert identity.identity_key == hashlib.sha256(str(bare.resolve()).encode("utf-8")).hexdigest()

    plain = tmp_path / "plain"
    plain.mkdir()
    assert repo_identity(plain).identity_key == hashlib.sha256(
        str(plain.resolve()).encode("utf-8")
    ).hexdigest()

    # 不同路径 → 不同项目（无 remote 时没有跨机共享语义）。
    other = tmp_path / "plain-2"
    other.mkdir()
    assert repo_identity(plain).identity_key != repo_identity(other).identity_key


def test_project_dir_and_delete_project(repo: Path, engine: Engine) -> None:
    handle, _identity = engine.resolve_repo(repo)
    engine.ingest_repo(handle.project_id, repo)
    directory = engine.project_dir(handle.project_id)
    assert directory.parent == engine.data_root / "projects"
    assert (directory / "index.db").is_file()
    assert (directory / "vectors").is_dir()
    assert (directory / "scan_manifest.json").is_file()

    engine.delete_project(handle.project_id)
    assert not directory.exists()

    with pytest.raises(EngineError, match="非法 project_id"):
        engine.project_dir("../../etc")


def test_engine_implements_context_engine_protocol(engine: Engine) -> None:
    assert isinstance(engine, ContextEngine)
    for method in ("resolve_project", "ingest", "sync_status", "search", "ask", "delete_project"):
        assert callable(getattr(engine, method))

    with pytest.raises(NotImplementedError, match="Phase 3"):
        engine.ask("0000000000000000", "token 过期后在哪里刷新")


def test_resolve_data_root_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)
    assert resolve_data_root() == DEFAULT_DATA_ROOT
    assert resolve_data_root(env={DATA_ROOT_ENV: str(tmp_path / "env")}) == tmp_path / "env"
    assert resolve_data_root(tmp_path / "explicit", env={DATA_ROOT_ENV: "/ignored"}) == (
        tmp_path / "explicit"
    )
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path / "from-env"))
    assert resolve_data_root() == tmp_path / "from-env"


def test_ingest_rejects_non_directory(repo: Path, engine: Engine) -> None:
    handle, _identity = engine.resolve_repo(repo)
    with pytest.raises(EngineError, match="不是目录"):
        engine.ingest_repo(handle.project_id, repo / "nope")


# ---------------------------------------------------------------------------
# TASK-111：分支参与身份计算
# ---------------------------------------------------------------------------


def _commit_all(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "init"],
        check=True,
        capture_output=True,
        env={
            "PATH": __import__("os").environ.get("PATH", ""),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def test_different_branches_of_same_remote_get_distinct_identity(tmp_path: Path) -> None:
    """TASK-111 的核心修复：同 remote 的两个分支不得碰撞成同一个 projectId。

    实测事故：main 与 feature/cvi-agent 共用一个 projectId，索引混合 353 文件，
    检索返回**当前 checkout 不存在**的文件，且 ``index: fresh`` 仍显示正常。
    """
    remote = "https://example.com/team/nova.git"
    repo = _git_repo(tmp_path / "nova", remote=remote)
    try:
        _commit_all(repo)
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-M", "main"], check=True, capture_output=True
        )
        main_identity = repo_identity(repo)
        assert main_identity.branch == "main"

        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-qb", "feature/x"],
            check=True,
            capture_output=True,
        )
        feature_identity = repo_identity(repo)
        assert feature_identity.branch == "feature/x"
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"环境不可用 git：{exc}")

    assert main_identity.identity_key != feature_identity.identity_key
    assert project_id_for(main_identity.identity_key) != project_id_for(
        feature_identity.identity_key
    )
    # 展示名带分支，UI 能一眼区分
    assert main_identity.display_name.endswith("@main")
    assert feature_identity.display_name.endswith("@feature/x")


def test_same_branch_in_two_checkouts_shares_identity(tmp_path: Path) -> None:
    """D-29 的核心价值必须保留：同分支的第二个 checkout 仍共享索引（不重复付费）。"""
    remote = "https://example.com/team/nova.git"
    left = _git_repo(tmp_path / "machine-a" / "nova", remote=remote)
    try:
        _commit_all(left)
        subprocess.run(
            ["git", "-C", str(left), "branch", "-M", "main"], check=True, capture_output=True
        )
        (tmp_path / "machine-b" / "ws").mkdir(parents=True, exist_ok=True)
        right = tmp_path / "machine-b" / "ws" / "nova"
        subprocess.run(
            ["git", "clone", "-q", str(left), str(right)], check=True, capture_output=True
        )
        # 把 clone 的 origin 改回「同一远程」，模拟两台机器各自 clone 同一 repo。
        subprocess.run(
            ["git", "-C", str(right), "remote", "set-url", "origin", remote],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"环境不可用 git：{exc}")

    assert repo_identity(left).identity_key == repo_identity(right).identity_key


def test_branch_identity_material_matches_documented_formula(tmp_path: Path) -> None:
    """身份材料的拼接口径必须可复算（``remote + repo路径 + \\x00 + branch``）。"""
    remote = "https://example.com/team/nova.git"
    repo = _git_repo(tmp_path / "nova", remote=remote)
    try:
        _commit_all(repo)
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-M", "main"], check=True, capture_output=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"环境不可用 git：{exc}")

    expected = hashlib.sha256(f"{remote}\x00main".encode()).hexdigest()
    assert repo_identity(repo).identity_key == expected


def test_branch_switch_surfaces_index_displacement_warning(
    tmp_path: Path, engine: Engine
) -> None:
    """TASK-114 / P2-7：换分支 = 换身份 = 从零全量重嵌 → 必须给出一条可操作告警。

    为什么需要它：D-29 + TASK-111 让分支进身份后，同一仓库的另一个分支会拿到全新的
    projectId，索引为空；下一次 ingest 会**静默**重嵌整仓（靶场实测 4.58M token）。
    本卡不做"接管旧索引"（冻结设计，换分支是否复用旧目录需编排者裁决），但至少不再沉默。
    """
    remote = "https://example.com/team/nova.git"
    root = _git_repo(tmp_path / "nova", remote=remote)
    try:
        _commit_all(root)
        subprocess.run(
            ["git", "-C", str(root), "branch", "-M", "main"], check=True, capture_output=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"环境不可用 git：{exc}")

    first, _ = engine.resolve_repo(root)
    first_report = engine.ingest_repo(first.project_id, root)
    assert first_report.warnings == (), "首个分支是正常的冷启动，不该告警"

    subprocess.run(
        ["git", "-C", str(root), "checkout", "-qb", "feature/displacement"],
        check=True,
        capture_output=True,
    )
    second, _ = engine.resolve_repo(root)
    assert second.project_id != first.project_id, "分支必须参与身份"

    report = engine.ingest_repo(second.project_id, root)

    assert any("从零重建" in warning for warning in report.warnings), report.warnings
    # 告警要说清"代价是本地时间、不是额度"（实测见 index-perf-task114-vps.md §10）。
    assert any(
        "不额外消耗 embedding 额度" in warning for warning in report.warnings
    ), report.warnings
    # 索引建好后再次 ingest 属于增量，不应再告警。
    assert engine.ingest_repo(second.project_id, root).warnings == ()
