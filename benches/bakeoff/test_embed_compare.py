"""``benches/bakeoff/embed_compare.py`` 的单元测试（TASK-015A）。

纪律：**不联网、不加载真实模型**——只测脚本自己的纯逻辑（候选表、范围摘要、断点续跑判定、
指标合并、报告渲染）。真实模型的对比数字在 ``benches/results/phase2-bakeoff.md``。

运行（``benches/`` 不在根 ``pyproject.toml`` 的 ``testpaths`` 里，需显式给路径）：

```bash
uv run pytest benches/bakeoff/test_embed_compare.py
```
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT / "core", ROOT / "benches" / "bakeoff"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import embed_compare as ec  # noqa: E402

# ---------------------------------------------------------------------------
# 候选表与 spec 构造
# ---------------------------------------------------------------------------


def test_candidates_are_unique_and_complete() -> None:
    slugs = [candidate.slug for candidate in ec.CANDIDATES]
    assert len(slugs) == len(set(slugs))
    # 卡内点名的三个本地候选必须都在（可选项 bge-m3 也保留）
    assert {"multilingual-e5-small", "bge-small-zh-v1.5", "arctic-embed-xs"} <= set(slugs)


def test_registered_candidates_match_registry() -> None:
    """已登记候选的 dim / pooling / 前缀必须与 registry 一致（否则测的不是线上默认实现）。"""
    from nova_core.embedding import get_local_spec

    for candidate in ec.CANDIDATES:
        if not candidate.registered:
            continue
        spec = get_local_spec(candidate.slug)
        assert (spec.dim, spec.pooling) == (candidate.dim, candidate.pooling)
        assert spec.repo_id == candidate.repo_id
        assert (spec.query_prefix, spec.passage_prefix) == (
            candidate.query_prefix,
            candidate.passage_prefix,
        )


def test_candidate_for_unknown_slug_raises_systemexit() -> None:
    with pytest.raises(SystemExit):
        ec.candidate_for("no-such-model")


def test_spec_for_overrides_truncation() -> None:
    """截断 A/B 靠 ``max_input_tokens`` 覆盖，其余字段不动（唯一变量原则）。"""
    candidate = ec.candidate_for("multilingual-e5-small")
    base = ec.spec_for(candidate, 512)
    widened = ec.spec_for(candidate, 2048)
    assert base.max_input_tokens == 512
    assert widened.max_input_tokens == 2048
    assert base.dim == widened.dim == candidate.dim
    assert base.model_id == widened.model_id  # model_id 不含截断值：D-07 二级失效按模型+维度


def test_spec_for_unregistered_candidate_builds_spec() -> None:
    """未登记候选（bge-m3）就地构造 spec；不允许被静默当成已登记模型。"""
    candidate = ec.candidate_for("bge-m3-int8")
    assert candidate.registered is False
    spec = ec.spec_for(candidate, 512)
    assert spec.slug == "bge-m3-int8"
    assert spec.dim == 1024
    assert spec.max_input_tokens == 512


def test_run_key_encodes_truncation() -> None:
    assert ec.run_key("multilingual-e5-small", 512) == "multilingual-e5-small__t512"
    assert ec.run_key("multilingual-e5-small", 2048) != ec.run_key("multilingual-e5-small", 512)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def test_percentile_nearest_rank() -> None:
    values = [float(i) for i in range(1, 11)]  # 1..10
    assert ec._percentile(values, 0.50) == 5.0
    assert ec._percentile(values, 0.95) == 10.0
    assert ec._percentile(values, 0.10) == 1.0
    assert ec._percentile([], 0.5) == 0.0


def test_dir_bytes_counts_file_and_directory(tmp_path: Path) -> None:
    single = tmp_path / "a.bin"
    single.write_bytes(b"x" * 100)
    assert ec._dir_bytes(single) == 100
    nested = tmp_path / "dir" / "sub"
    nested.mkdir(parents=True)
    (nested / "b.bin").write_bytes(b"y" * 41)
    (tmp_path / "dir" / "c.bin").write_bytes(b"z" * 7)
    assert ec._dir_bytes(tmp_path / "dir") == 48
    assert ec._dir_bytes(tmp_path / "missing") == 0


def test_scope_digest_is_order_independent_and_content_sensitive(tmp_path: Path) -> None:
    manifest = tmp_path / "scan_manifest.json"
    payload = {"version": 1, "files": {"b.py": "hash-b", "a.md": "hash-a"}}
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    digest, count = ec._scope_digest(manifest)
    assert count == 2

    reordered = tmp_path / "reordered.json"
    reordered.write_text(
        json.dumps({"version": 1, "files": {"a.md": "hash-a", "b.py": "hash-b"}}),
        encoding="utf-8",
    )
    assert ec._scope_digest(reordered) == (digest, count)

    changed = tmp_path / "changed.json"
    changed.write_text(
        json.dumps({"version": 1, "files": {"a.md": "hash-a", "b.py": "hash-CHANGED"}}),
        encoding="utf-8",
    )
    assert ec._scope_digest(changed)[0] != digest


def test_scope_digest_missing_manifest_is_empty(tmp_path: Path) -> None:
    assert ec._scope_digest(tmp_path / "nope.json") == ("", 0)


def test_command_line_records_real_interpreter() -> None:
    line = ec._command_line()
    assert sys.executable in line
    assert "embed_compare.py" in line


# ---------------------------------------------------------------------------
# 断点续跑判定
# ---------------------------------------------------------------------------


def _index_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "key": "m__t512",
        "fingerprint": {"model_id": "local:m", "max_input_tokens": 512},
        "repo": {"commit": "abc123"},
    }
    payload.update(overrides)
    return payload


def test_resume_allows_identical_artifact() -> None:
    assert ec._resume_ok(_index_payload(), "local:m", 512, "abc123", force=False) is True


def test_resume_rejects_different_model_or_truncation_or_commit() -> None:
    assert ec._resume_ok(_index_payload(), "local:other", 512, "abc123", force=False) is False
    assert ec._resume_ok(_index_payload(), "local:m", 2048, "abc123", force=False) is False
    assert ec._resume_ok(_index_payload(), "local:m", 512, "deadbeef", force=False) is False
    assert ec._resume_ok(None, "local:m", 512, "abc123", force=False) is False


def test_resume_rejects_when_forced() -> None:
    assert ec._resume_ok(_index_payload(), "local:m", 512, "abc123", force=True) is False


# ---------------------------------------------------------------------------
# 指标合并与分组
# ---------------------------------------------------------------------------


def _eval_payload(key: str, cases: list[dict[str, object]]) -> dict[str, object]:
    return {"key": key, "cases": cases}


def test_merged_metrics_pools_repos_and_counts_negatives() -> None:
    payloads = [
        _eval_payload(
            "m__t512",
            [
                {"id": "a", "rank": 1, "is_negative": False, "answerable": False},
                {"id": "b", "rank": 7, "is_negative": False, "answerable": False},
                {"id": "n", "rank": None, "is_negative": True, "answerable": False},
            ],
        ),
        _eval_payload(
            "m__t512",
            [
                {"id": "c", "rank": None, "is_negative": False, "answerable": False},
                {"id": "m", "rank": None, "is_negative": True, "answerable": True},
            ],
        ),
        _eval_payload("other__t512", [{"id": "x", "rank": 1, "is_negative": False}]),
    ]
    merged = ec._merged_metrics(payloads, key="m__t512")
    assert merged["count"] == 3
    assert merged["negative_total"] == 2
    assert merged["negative_pass"] == 1
    assert merged["recall_at_5"] == pytest.approx(1 / 3, abs=1e-3)
    assert merged["recall_at_10"] == pytest.approx(2 / 3, abs=1e-3)


def test_merged_metrics_skips_errored_cases() -> None:
    payloads = [
        _eval_payload(
            "m__t512",
            [
                {"id": "a", "rank": 1, "is_negative": False, "error": None},
                {"id": "boom", "rank": None, "is_negative": False, "error": "RuntimeError: x"},
            ],
        )
    ]
    merged = ec._merged_metrics(payloads, key="m__t512")
    assert merged["count"] == 1
    assert merged["recall_at_5"] == 1.0


def test_by_group_groups_positive_cases_only() -> None:
    payloads = [
        _eval_payload(
            "m__t512",
            [
                {"id": "a", "lang": "zh", "rank": 2, "is_negative": False},
                {"id": "b", "lang": "zh", "rank": None, "is_negative": False},
                {"id": "c", "lang": "en", "rank": 1, "is_negative": False},
                {"id": "n", "lang": "zh", "rank": None, "is_negative": True},
            ],
        )
    ]
    groups = ec._by_group(payloads, "lang")
    assert set(groups) == {"en", "zh"}
    assert groups["en"]["mrr"] == 1.0
    assert groups["zh"]["count"] == 2
    assert groups["zh"]["recall_at_5"] == 0.5


# ---------------------------------------------------------------------------
# 模型缓存探测
# ---------------------------------------------------------------------------


def test_model_cache_report_detects_snapshot(tmp_path: Path) -> None:
    candidate = ec.candidate_for("arctic-embed-xs")
    snapshot = tmp_path / "models--Snowflake--snowflake-arctic-embed-xs" / "snapshots" / "rev1"
    snapshot.mkdir(parents=True)
    (snapshot / "tokenizer.json").write_bytes(b"t" * 10)
    (snapshot / candidate.onnx_file).parent.mkdir(parents=True, exist_ok=True)
    (snapshot / candidate.onnx_file).write_bytes(b"o" * 20)
    report = ec._model_cache_report(candidate, tmp_path)
    assert report["cached"] is True
    assert report["onnx"]["bytes"] == 20
    assert report["tokenizer"]["bytes"] == 10


def test_model_cache_report_missing(tmp_path: Path) -> None:
    report = ec._model_cache_report(ec.candidate_for("arctic-embed-xs"), tmp_path)
    assert report["cached"] is False
    assert report["onnx"] is None


# ---------------------------------------------------------------------------
# CLI 形态（DoD：--help 可跑）
# ---------------------------------------------------------------------------


def test_build_parser_subcommands() -> None:
    parser = ec.build_parser()
    for argv in (
        ["list"],
        ["fetch", "--model", "all"],
        ["run", "--model", "m", "--repo", ".", "--golden", "g"],
        ["aggregate", "--report", "r.md"],
        ["compare", "--left", "a", "--right", "b"],
        ["clean", "--key", "a__t512"],
    ):
        assert parser.parse_args(argv).command == argv[0]


def test_run_parser_rejects_missing_required() -> None:
    parser = ec.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--model", "m"])


def test_defaults_stay_outside_repo() -> None:
    """中间产物默认落在仓库外（benches/** 在 dogfood 索引范围内，写入会污染负例口径）。"""
    assert ROOT not in ec.DEFAULT_DATA_ROOT_BASE.parents
    assert ROOT not in ec.DEFAULT_RESULTS_DIR.parents
    assert "tmp" not in ec.DEFAULT_DATA_ROOT_BASE.parts
