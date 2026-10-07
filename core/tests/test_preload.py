"""启动预加载（``nova_core.preload``）：各项可用、失败只告警不抛。"""

from __future__ import annotations

import logging

import pytest
from nova_core import preload


def test_preload_runtime_loads_all_steps() -> None:
    timings = preload.preload_runtime()
    assert set(timings) == {"lancedb", "jieba", "parsers"}


def test_failed_step_is_logged_and_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []

    def boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        preload, "_STEPS", (("bad", boom), ("good", lambda: calls.append("good")))
    )
    with caplog.at_level(logging.WARNING, logger=preload.__name__):
        timings = preload.preload_runtime()
    assert list(timings) == ["good"] and calls == ["good"]
    assert "bad" in caplog.text
