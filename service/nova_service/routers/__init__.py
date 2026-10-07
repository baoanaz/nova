"""HTTP 路由（每张卡拥有自己的文件，互不越界）。

分工（TASK-030 建骨架，后续卡就地替换实现）：

- ``ops.py``：``GET /healthz``（本卡实现）+ ``GET /api/usage/projects/{id}``（占位）；
- ``auth.py``：``/api/auth/*`` 全部占位 501——鉴权与用户体系归 M2c（TASK-060/061）；
- ``projects.py``：TASK-031 替换为真实实现；
- ``query.py``：TASK-032 替换为真实实现；
- ``sync.py``：TASK-033 替换为真实实现。

路径集合在本卡一次性冻结为与 CF-05（``docs/contracts/openapi.yaml``）完全一致，
``service/tests/test_skeleton.py`` 有一致性测试（多/少/改名都会失败）。
"""

from __future__ import annotations

from fastapi import APIRouter

from nova_service.routers import admin, auth, ops, projects, query, sync

__all__ = ["all_routers"]


def all_routers() -> tuple[APIRouter, ...]:
    """返回全部路由（``create_app`` 的唯一挂载点）。"""
    return (ops.router, auth.router, admin.router, projects.router, sync.router, query.router)
