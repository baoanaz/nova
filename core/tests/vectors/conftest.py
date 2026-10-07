"""TASK-009 测试配置：注册 ``slow`` 标记，并让规模冒烟默认不进基线/CI。"""

from __future__ import annotations

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "slow: 规模冒烟测试（默认跳过；NOVA_RUN_SLOW=1 时运行）"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("NOVA_RUN_SLOW") == "1":
        return
    skip = pytest.mark.skip(reason="规模冒烟默认跳过（NOVA_RUN_SLOW=1 启用）")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
