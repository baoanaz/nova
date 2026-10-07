"""FastAPI 依赖注入接缝（TASK-030 建；TASK-031 追加 engine_manager 与 projectId 解析）。

为什么走 ``app.state`` 而不是模块级单例：测试要能用临时 ``data_root`` 与假 embedding
provider 覆盖整个应用配置（``create_app(app_settings)`` 后写 ``app.state.engine_manager``），
而不用打补丁改全局状态。

``EngineManager`` 采用**懒构造**：首个真正访问 core 的请求才实例化（起服务不加载 embedding
模型，``/healthz`` 与占位路由保持毫秒级）。
"""

from __future__ import annotations

import threading

from fastapi import Request

from nova_service.config import Settings
from nova_service.errors import ApiError
from nova_service.metadb import MetaDB
from nova_service.runtime import EngineManager

__all__ = [
    "get_engine_manager",
    "get_settings",
    "project_not_found_message",
    "require_ownership_of",
    "require_project_id",
]

#: 懒构造 EngineManager 的互斥（多线程首个请求可能同时到达）。
_manager_lock = threading.Lock()


def get_settings(request: Request) -> Settings:
    """取当前应用的 ``Settings``（由 :func:`nova_service.app.create_app` 写入 ``app.state``）。"""
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):  # pragma: no cover - 只有手工拼装 app 才会走到
        raise ApiError(
            code="service_not_configured",
            message="应用未初始化 Settings（请通过 create_app() 创建应用）",
            status=500,
        )
    return settings


def get_engine_manager(request: Request) -> EngineManager:
    """取 ``EngineManager``（测试可预热注入；否则按 ``Settings.data_root`` 懒构造一次）。"""
    manager = getattr(request.app.state, "engine_manager", None)
    if isinstance(manager, EngineManager):
        return manager
    with _manager_lock:
        manager = getattr(request.app.state, "engine_manager", None)
        if not isinstance(manager, EngineManager):
            manager = EngineManager.open(get_settings(request).data_root)
            # TASK-085：懒构造也要接上元数据库，否则上传路径的索引 run 无处落库
            # （实测：npx nova-client 索引成功后 /api/index-stats 仍恒为 0）。
            # 直接读 ``app.state`` 而不调 ``auth.get_meta_db``：后者会**建库**，而本地模式
            # 按 R34 不该有账户库（``create_app`` 也只在校验形态下落 ``None``）。
            # ``local`` 子命令走的是 __main__ 里显式 attach 的那条路径，不受此处影响。
            db = getattr(request.app.state, "meta_db", None)
            manager.attach_meta_db(db if isinstance(db, MetaDB) else None)
            request.app.state.engine_manager = manager
    return manager


def require_project_id(request: Request, project_id: str | None) -> str:
    """解析请求里的 ``projectId``（CF-05 扩展 R37：本地模式可省略）。

    语义（TASK-061 §C 扩展）：**存在 + 归属当前用户**。

    - 显式给出：必须存在，否则 404 ``project_not_found``；已认证用户还必须是该项目的
      归属者，否则**同样** 404 ``project_not_found``（不报 403：403 会泄露"这个 projectId
      存在"，与 Module/06 §2.2"不给探测面"冲突）；
    - 省略：仅 ``nova-service local`` 模式允许，取**唯一**的本地项目；
      本地 0 个项目 → 404（附"先 resolve"提示）；多于 1 个 → 409 ``ambiguous_project``
      （不猜、不隐式选一个：静默挑错项目比报错更糟）。

    归属校验只在**有已认证用户**时生效（本地模式 ``nova_user`` 为 ``None``，无账户体系，
    R34：行为与今天逐字一致）。
    """
    manager = get_engine_manager(request)
    if project_id:
        if not manager.project_exists(project_id):
            raise _project_not_found(project_id)
        _require_ownership(request, project_id)
        return project_id

    settings = get_settings(request)
    if not settings.local_mode:
        raise ApiError(
            code="project_id_required",
            message="非本地模式必须显式提供 projectId（R37 的省略仅限 local 命令）",
            status=400,
        )
    projects = manager.list_projects()
    if not projects:
        raise ApiError(
            code="project_not_found",
            message="本地还没有任何项目：请先用 POST /api/projects/resolve 或先同步一次",
            status=404,
        )
    if len(projects) > 1:
        raise ApiError(
            code="ambiguous_project",
            message=(
                f"本地存在 {len(projects)} 个项目，省略 projectId 有歧义："
                "请显式传入 projectId（本地单项目模式才允许省略）"
            ),
            status=409,
        )
    return str(projects[0]["projectId"])


def _require_ownership(request: Request, project_id: str) -> None:
    """归属校验（TASK-061 §C）：未归属当前用户 → 404 ``project_not_found``。

    - 本地模式（无 ``nova_user``）跳过——R34 的第一验收项要求行为与今天逐字一致；
    - 云端形态元数据库缺失 → **fail closed**（按未归属处理），宁可拒绝也不放行越权；
    - 与"项目不存在"**用同一个 code 与文案**：调用方无法据此区分"不存在"与"别人的"。
    """
    user_id = getattr(getattr(request.state, "nova_user", None), "id", None)
    if user_id is None:
        return
    require_ownership_of(user_id, _meta_db(request), project_id)


def require_ownership_of(user_id: object, db: MetaDB | None, project_id: str) -> None:
    """归属校验的**形态无关核心**（TASK-089 提取；REST 与 MCP 共用一份）。

    :func:`_require_ownership` 从 ``Request`` 取 ``nova_user`` 与 ``MetaDB``；MCP 工具没有
    ``Request``（身份来自 ``mcp.py`` 的 contextvar 通道、元数据库来自 ``EngineManager.meta_db``），
    因此把两者都收成入参——业务判断只有这一份，两条路径不会漂移。

    纪律与 :func:`_require_ownership` 逐字一致：

    - ``user_id`` 为 ``None``（本地模式无账户，R34）或空串 → 直接返回（完全放行）；
    - ``db`` 缺失 → **fail closed**（按未归属处理，宁可拒绝也不放行越权）；
    - 未归属 → :func:`_project_not_found`（404 语义：与"真不存在"共用 code 与文案）。
    """
    if user_id is None or user_id == "":
        return
    if db is None or not db.owns_project(str(user_id), project_id):
        raise _project_not_found(project_id)


def _project_not_found(project_id: str) -> ApiError:
    """统一的"项目不存在"错误（越权与真不存在共用，不给探测面）。"""
    return ApiError(
        code="project_not_found",
        message=project_not_found_message(project_id),
        status=404,
    )


def project_not_found_message(project_id: str) -> str:
    """"项目不存在"的**唯一文案**（TASK-089 提取：MCP 面无 HTTP 404，但要给出逐字相同的话术）。

    REST 的 404 信封与 MCP 的 ``isError`` 文本同源，确保两条路径对外**不可区分**：
    无论走哪一面，越权用户看到的都是同一句"项目不存在：<id>"，因而都无法据此探测
    "这个 projectId 到底存不存在"（Module/06 §2.2 的不给探测面纪律）。
    """
    return f"项目不存在：{project_id}"


def _meta_db(request: Request) -> MetaDB | None:
    """app 级 ``MetaDB``（本地模式未建库 → ``None``；不在此处建库，避免破坏 R34）。"""
    db = getattr(request.app.state, "meta_db", None)
    return db if isinstance(db, MetaDB) else None
