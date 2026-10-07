"""TASK-013 §B/DoD：search 端到端（Markdown / CF-03 JSON / 跨模块 E2E / 降级）。

卡内强制项（contracts.md §3.3 R13）：`ingest → search` 必须有一条跨模块断言——
中文自然语言查询（≥5 token）在**至少两个通道**命中目标符号且 `ContextPack.answerable is True`。
本目录的测试用真实 Store + VectorStore + Indexer + retrieval + contextpack，embedding 用确定性假
provider（离线、可复现）；仅单层测试无法暴露 R11/R12 一类集成缺陷。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from nova_core.cli.app import main
from nova_core.engine import Engine
from nova_core.interfaces import EmbeddingProfile
from nova_core.text import segment

from .conftest import E2E_QUERY, E2E_TARGET_PATH, E2E_TARGET_SYMBOL, TEST_PROFILE

RunCli = Callable[..., tuple[int, str, str]]

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "contextpack.schema.json"


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def test_e2e_markdown_reports_numbered_evidence(
    repo: Path, data_root: Path, run_cli: RunCli
) -> None:
    code, _out, err = run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    assert code == 0, err

    code, out, err = run_cli(
        "search", E2E_QUERY, "--repo", str(repo), "--data", str(data_root)
    )
    assert code == 0, err
    assert out.startswith("## Relevant Context")
    assert "[E1]" in out
    assert E2E_TARGET_PATH in out
    assert "refresh_token" in out
    assert "confidence:" in out


def test_e2e_json_validates_against_cf03_schema(
    repo: Path, data_root: Path, run_cli: RunCli, validator: Draft202012Validator
) -> None:
    run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    code, out, err = run_cli(
        "search", E2E_QUERY, "--json", "--repo", str(repo), "--data", str(data_root)
    )
    assert code == 0, err
    payload = json.loads(out)
    validator.validate(payload)
    assert payload["mode"] == "fast"
    assert payload["query"] == E2E_QUERY
    assert payload["evidence"], "E2E 查询必须产出代码证据"


def test_cross_module_e2e_target_hit_by_two_channels_and_answerable(
    repo: Path, data_root: Path, run_cli: RunCli, provider
) -> None:
    """R13：至少两个通道命中目标符号 + answerable（本卡发现的 R11/R12 类缺陷的唯一暴露面）。"""
    code, _out, err = run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    assert code == 0, err

    # "≥5 token" 的口径与索引侧一致：走 jieba 预分词（R11 的零命中正是长中文查询暴露的）。
    assert len(segment(E2E_QUERY).split()) >= 5, segment(E2E_QUERY)

    engine = Engine(data_root, provider=provider)
    handle, _identity = engine.resolve_repo(repo)
    trace = engine.search_with_trace(handle.project_id, E2E_QUERY)

    assert trace.degraded is False, trace.degraded_reason
    target = trace.candidate_for(E2E_TARGET_SYMBOL)
    assert target is not None, f"目标符号未进候选池：{[c.symbol_fqn for c in trace.candidates]}"
    assert len(target.channel_ranks) >= 2, target.channel_ranks
    assert "inferred" in target.channel_ranks and "vector" in target.channel_ranks
    assert trace.pack.answerable is True, trace.pack.missing_evidence

    packed = [*trace.pack.evidence, *trace.pack.docs]
    assert any(item.path == E2E_TARGET_PATH for item in packed), "目标路径未进入渲染结果"


def test_search_max_tokens_is_forwarded_to_budget(
    repo: Path, data_root: Path, run_cli: RunCli
) -> None:
    run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    code, out, err = run_cli(
        "search",
        E2E_QUERY,
        "--json",
        "--max-tokens",
        "4000",
        "--repo",
        str(repo),
        "--data",
        str(data_root),
    )
    assert code == 0, err
    assert json.loads(out)["budget"]["hardCap"] == 4000


def test_search_on_unindexed_repo_is_honest(repo: Path, data_root: Path, run_cli: RunCli) -> None:
    """没有索引就不许"看起来有答案"：无证据 + answerable False + no_context_match（D-30/A5）。"""
    code, out, err = run_cli(
        "search", E2E_QUERY, "--json", "--repo", str(repo), "--data", str(data_root)
    )
    assert code == 0, err
    payload = json.loads(out)
    assert payload["evidence"] == [] and payload["docs"] == []
    assert payload["answerable"] is False
    assert [item["code"] for item in payload["missingEvidence"]] == ["no_context_match"]


def test_search_degrades_when_vector_channel_fails(
    repo: Path, data_root: Path, run_cli: RunCli, capsys: pytest.CaptureFixture[str]
) -> None:
    """Module/02 §5：向量通道故障 → 降级不抛错、stderr 说明原因、结果来自其余通道。"""

    class BrokenQueryEmbedding:
        profile: EmbeddingProfile = TEST_PROFILE

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            raise AssertionError("索引侧不应在降级测试中被调用")

        def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
            raise RuntimeError("模型不可用")

    code, _out, err = run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    assert code == 0, err

    code = main(
        ["search", E2E_QUERY, "--repo", str(repo), "--data", str(data_root)],
        engine_factory=lambda data: Engine(data, provider=BrokenQueryEmbedding()),
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "warning:" in captured.err and "vector" in captured.err
    assert captured.out.startswith("## Relevant Context")


def test_argument_errors_exit_with_code_2() -> None:
    with pytest.raises(SystemExit) as missing_subcommand:
        main([])
    assert missing_subcommand.value.code == 2
    with pytest.raises(SystemExit) as missing_repo:
        main(["ingest"])
    assert missing_repo.value.code == 2
    with pytest.raises(SystemExit) as bad_max_tokens:
        main(["search", "q", "--repo", ".", "--max-tokens", "0"])
    assert bad_max_tokens.value.code == 2


def test_runtime_error_exits_with_code_1(repo: Path, data_root: Path) -> None:
    code = main(["search", E2E_QUERY, "--repo", str(repo), "--data", str(data_root)])
    assert code == 0  # 空索引不是错误

    code = main(["search", "   ", "--repo", str(repo), "--data", str(data_root)])
    assert code == 1

    code = main(["ingest", "--repo", str(repo / "missing"), "--data", str(data_root)])
    assert code == 1


# --------------------------------------------------------------------------- 工具
