"""Public defaults exclude private data; private manifests require explicit CLI selection."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BENCHES = Path(__file__).resolve().parent
if str(BENCHES) not in sys.path:
    sys.path.insert(0, str(BENCHES))

import run as runner  # noqa: E402


def test_public_manifest_contains_no_internal_targets(capsys) -> None:
    assert runner.main(["--list-targets"]) == 0
    output = capsys.readouterr().out
    assert "langchain-v1" in output
    assert "internal" not in output


def test_external_manifest_is_used_only_when_selected(tmp_path, capsys) -> None:
    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "case.jsonl").write_text('{"id":"private-example"}\n')
    manifest = tmp_path / "targets.json"
    manifest.write_text(json.dumps({
        "schema": 1, "targets": {"private-example": {
            "golden": str(golden), "repo_hint": "example", "commit": "self", "role": "internal",
        }},
    }))
    assert runner.main(["--targets-file", str(manifest), "--list-targets"]) == 0
    output = capsys.readouterr().out
    assert "private-example" in output
    assert "langchain-v1" not in output
    assert runner.main(["--list-targets"]) == 0
    assert "private-example" not in capsys.readouterr().out


@pytest.mark.parametrize("args", [
    ["--targets-file"],
    ["--targets-file="],
    ["--targets-file", "--list-targets"],
    ["--targets-file=a.json", "--targets-file=b.json"],
])
def test_invalid_manifest_argument_is_reported(args, capsys) -> None:
    assert runner.main(args) == 2
    assert "--targets-file" in capsys.readouterr().err


def test_directory_manifest_reports_usage_error(tmp_path, capsys) -> None:
    assert runner.main(["--targets-file", str(tmp_path), "--list-targets"]) == 2
    assert "无法读取靶场清单" in capsys.readouterr().err
