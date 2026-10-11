"""Offline delay injection is independent of fixture values / paid providers."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import numpy as np
import pytest

_PATH = Path(__file__).parent / "embed-bench" / "replay.py"
_SPEC = importlib.util.spec_from_file_location("_nova_replay_delay_test", _PATH)
replay = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(replay)


@pytest.fixture
def fixture(tmp_path):
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.executemany("INSERT INTO metadata VALUES (?,?)", [
            ("schema", "1"), ("model_id", "api:test-replay"),
            ("dim", "4"), ("max_input_tokens", "32000"),
        ])
        db.execute("CREATE TABLE embeddings(key TEXT PRIMARY KEY, vector BLOB NOT NULL)")
        db.execute("INSERT INTO embeddings VALUES (?,?)", (
            replay.text_key("x"), np.array([1, 0, 0, 0], dtype="<f4").tobytes(),
        ))
    return path


def test_zero_delay_and_delayed_fixture_values_match(fixture, monkeypatch) -> None:
    sleeps = []
    monkeypatch.setattr(replay.time, "sleep", sleeps.append)
    plain = replay.ReplayEmbedding(fixture)
    delayed = replay.ReplayEmbedding(fixture, batch_delay_s=1.5, query_delay_s=.35)
    assert np.array_equal(plain.embed_array(["x"]), delayed.embed_array(["x"]))
    assert sleeps == [1.5]
    assert plain.embed_query(["x"]) == delayed.embed_query(["x"])
    assert sleeps == [1.5, .35]  # query does not also pay passage delay
    delayed.embed_array(["x"] * 2001)
    assert sleeps[-1] == 3.0  # five batches require two four-batch waves


@pytest.mark.parametrize("delay", [-1.0, float("nan"), float("inf")])
def test_invalid_delays_fail_locally(fixture, delay) -> None:
    with pytest.raises(ValueError):
        replay.ReplayEmbedding(fixture, batch_delay_s=delay)
    with pytest.raises(ValueError):
        replay.ReplayEmbedding(fixture, query_delay_s=delay)


def test_empty_input_and_fixture_miss_never_fallback(fixture, monkeypatch) -> None:
    sleeps = []
    monkeypatch.setattr(replay.time, "sleep", sleeps.append)
    provider = replay.ReplayEmbedding(fixture, batch_delay_s=2, query_delay_s=.2)
    assert provider.embed_array([]).shape == (0, 4)
    assert provider.embed_query([]) == []
    assert not sleeps
    with pytest.raises(RuntimeError, match="network fallback is forbidden"):
        provider.embed_array(["missing"])
