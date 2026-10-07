"""``benches/targets.py`` 的单元测试（靶场清单解析与命令拼装）。

纪律：**不联网、不建索引、不读真实数据根**——只测纯逻辑（清单校验、参数拼装、错误信息）。
真实数字来自 ``benches/results/*.md``。

运行（``benches/`` 不在根 ``pyproject.toml`` 的 ``testpaths`` 里，需显式给路径）：

```bash
uv run pytest -o addopts="" -q benches/test_targets.py
```
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "benches") not in sys.path:
    sys.path.insert(0, str(ROOT / "benches"))

import targets as tt  # noqa: E402


def _manifest(tmp_path: Path, targets: dict) -> Path:
    path = tmp_path / "targets.json"
    path.write_text(json.dumps({"schema": tt.SCHEMA, "targets": targets}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 清单读取与校验
# ---------------------------------------------------------------------------


def test_repo_manifest_lists_all_kept_targets() -> None:
    targets = tt.load_targets()
    assert list(targets) == [
        "hello-agents",
        "zace",
        "leveldb-v1",
        "helloagents-v1",
        "langchain-v1",
    ]
    for target in targets.values():
        assert target.golden.is_dir(), f"{target.name} 的 golden 目录不存在"
        assert list(target.golden.glob("*.jsonl")), f"{target.name} 的 golden 没有用例"


def test_primary_target_is_a_public_local_build() -> None:
    """主靶场必须是公共仓库，可本地建索引或复用本机已登记的持久索引。"""
    targets = tt.load_targets()
    primaries = [t for t in targets.values() if t.role == "primary"]
    assert [t.name for t in primaries] == [
        "hello-agents",
        "leveldb-v1",
        "helloagents-v1",
        "langchain-v1",
    ]
    for target in targets.values():
        if target.role in ("primary", "dogfood"):
            assert target.index_how in {"local-build", "vps-persistent"}
    assert all(target.role != "internal" for target in targets.values())


def test_illegal_role_is_rejected(tmp_path: Path) -> None:
    spec = {"golden": "benches/golden/zace", "repo_hint": "x", "commit": "self", "role": "boss"}
    path = _manifest(tmp_path, {"x": spec})
    with pytest.raises(tt.TargetError) as excinfo:
        tt.load_targets(path)
    assert "role" in str(excinfo.value)


def test_recorded_indexes_carry_a_project_id_and_fingerprint() -> None:
    targets = tt.load_targets()
    assert targets["hello-agents"].project_id == "e9ee9dd1d41a7d2c"
    assert targets["zace"].project_id == "adfdd1a626db62b7"
    assert targets["leveldb-v1"].project_id == "3ed886ce58bc0e47"
    assert targets["helloagents-v1"].project_id == "06078cc80c7ce7d7"
    assert targets["langchain-v1"].project_id == "ca2050db0db5b1e2"
    # 记了 projectId 的靶场必须同时记指纹：否则拿别的模型的向量算 cosine 无从察觉
    for target in targets.values():
        assert target.embedding == {"model": "api:voyage-4-lite", "dim": 1024}


def _unbound_target(tmp_path: Path) -> tt.Target:
    """合成一个"还没建索引"的靶场：用来测需要 --repo 的那条路径。"""
    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "x.jsonl").write_text('{"id": "x-0001"}\n', encoding="utf-8")
    return tt.Target(
        name="synthetic",
        golden=golden,
        repo_hint="synthetic",
        commit="self",
        role="primary",
    )


def test_unknown_target_lists_available_names(tmp_path: Path) -> None:
    with pytest.raises(tt.TargetError) as excinfo:
        tt.resolve_target("missing-private-target", path=tt.TARGETS_PATH)
    message = str(excinfo.value)
    assert "未知靶场：missing-private-target" in message
    assert "hello-agents" in message


def test_bad_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "targets.json"
    path.write_text(json.dumps({"schema": 999, "targets": {}}), encoding="utf-8")
    with pytest.raises(tt.TargetError) as excinfo:
        tt.load_targets(path)
    assert "schema" in str(excinfo.value)


def test_missing_required_field_is_rejected(tmp_path: Path) -> None:
    path = _manifest(tmp_path, {"zace": {"golden": "benches/golden/zace", "commit": "self"}})
    with pytest.raises(tt.TargetError) as excinfo:
        tt.load_targets(path)
    assert "repo_hint" in str(excinfo.value)


def test_missing_manifest_is_reported(tmp_path: Path) -> None:
    with pytest.raises(tt.TargetError) as excinfo:
        tt.load_targets(tmp_path / "nope.json")
    assert "找不到靶场清单" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 命令拼装
# ---------------------------------------------------------------------------


def test_build_eval_args_binds_project_id_and_data() -> None:
    target = tt.resolve_target("helloagents-v1")
    argv = tt.build_eval_args(
        target,
        data="/tmp/bench",
        report="/tmp/r.md",
        vector_cache="/tmp/qvec.json",
        replay=True,
    )
    assert argv == [
        "--golden", str(target.golden),
        "--project-id", "06078cc80c7ce7d7",
        "--data", "/tmp/bench",
        "--report", "/tmp/r.md",
        "--vector-cache", "/tmp/qvec.json",
        "--replay",
    ]


def test_build_eval_args_keeps_extra_flags_passthrough(tmp_path: Path) -> None:
    argv = tt.build_eval_args(
        _unbound_target(tmp_path), data="/tmp/bench", report="/tmp/r.md",
        repo="/path/to/repo", extra=["--max-tokens", "8000"],
    )
    assert argv[-2:] == ["--max-tokens", "8000"]
    assert "--project-id" not in argv  # 没绑索引 → 由 D-29 身份计算


def test_recorded_project_id_lets_any_checkout_reuse_the_index() -> None:
    """记了 projectId 的靶场不需要 --repo：换 checkout/换 worktree 都能挂同一份索引。"""
    target = tt.resolve_target("zace")
    argv = tt.build_eval_args(target, data="/tmp/bench", report="/tmp/r.md")
    assert "--repo" not in argv
    assert argv[argv.index("--project-id") + 1] == "adfdd1a626db62b7"


def test_target_without_index_binding_needs_repo(tmp_path: Path) -> None:
    with pytest.raises(tt.TargetError) as excinfo:
        tt.build_eval_args(_unbound_target(tmp_path), data="/tmp/bench", report="/tmp/r.md")
    assert "--repo" in str(excinfo.value)


def test_replay_without_sidecar_is_rejected() -> None:
    target = tt.resolve_target("helloagents-v1")
    with pytest.raises(tt.TargetError) as excinfo:
        tt.build_eval_args(target, data="/tmp/bench", report="/tmp/r.md", replay=True)
    assert "--vector-cache" in str(excinfo.value)


def test_target_arg_is_extracted_in_both_forms() -> None:
    assert tt.split_target_arg(["--target", "zace", "--data", "x"]) == ("zace", ["--data", "x"])
    assert tt.split_target_arg(["--target=zace", "--replay"]) == ("zace", ["--replay"])
    assert tt.split_target_arg(["--golden", "g"]) == (None, ["--golden", "g"])
    with pytest.raises(tt.TargetError):
        tt.split_target_arg(["--target"])


def test_describe_targets_mentions_every_target_and_role() -> None:
    text = tt.describe_targets(tt.load_targets())
    for name in (
        "zace",
        "hello-agents",
        "leveldb-v1",
        "helloagents-v1",
        "langchain-v1",
    ):
        assert name in text
    for role in ("primary", "dogfood"):
        assert role in text
    assert "internal" not in text
    assert "06078cc80c7ce7d7" in text
