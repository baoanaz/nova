"""TASK-031 §D：项目 API（resolve / list / get / delete 与错误形态）。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from nova_service.runtime import EngineManager

from tests.conftest import SAMPLE_FILES, make_client, upload_files


def test_resolve_project_is_idempotent_over_http(client: TestClient) -> None:
    first = client.post(
        "/api/projects/resolve", json={"identityKey": "identity:api", "displayName": "api"}
    )
    assert first.status_code == 200
    payload = first.json()
    assert payload["created"] is True
    assert payload["projectId"]

    second = client.post("/api/projects/resolve", json={"identityKey": "identity:api"})
    assert second.status_code == 200
    assert second.json() == {"projectId": payload["projectId"], "created": False}


def test_resolve_validates_identity_key(client: TestClient) -> None:
    missing = client.post("/api/projects/resolve", json={"displayName": "no key"})
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "invalid_request"

    blank = client.post("/api/projects/resolve", json={"identityKey": "   "})
    assert blank.status_code == 400
    assert blank.json()["error"]["code"] == "invalid_identity_key"


def test_list_and_get_project_detail(
    client: TestClient, engine_manager: EngineManager
) -> None:
    assert client.get("/api/projects").json() == []

    project_id = client.post(
        "/api/projects/resolve", json={"identityKey": "identity:detail", "displayName": "detail"}
    ).json()["projectId"]
    upload_files(engine_manager, project_id, SAMPLE_FILES)

    listed = client.get("/api/projects").json()
    assert [item["projectId"] for item in listed] == [project_id]
    assert listed[0]["displayName"] == "detail"
    assert listed[0]["createdAt"] > 0

    detail = client.get(f"/api/projects/{project_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["projectId"] == project_id
    assert body["displayName"] == "detail"
    assert body["sync"]["filesIndexed"] == len(SAMPLE_FILES)
    assert body["sync"]["chunks"] > 0
    assert body["blobs"] == body["sync"]["blobs"]
    assert body["blobs"]["count"] == 2


def test_project_not_found_shapes(client: TestClient) -> None:
    unknown = client.get("/api/projects/0123456789abcdef")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "project_not_found"

    malformed = client.get("/api/projects/not-a-project-id")
    assert malformed.status_code == 404, "非法 id 是「不存在」，不是 500"
    assert malformed.json()["error"]["code"] == "project_not_found"


def test_delete_project_cascades_and_then_404(
    client: TestClient, engine_manager: EngineManager
) -> None:
    project_id = client.post(
        "/api/projects/resolve", json={"identityKey": "identity:delete"}
    ).json()["projectId"]
    upload_files(engine_manager, project_id, SAMPLE_FILES)
    directory = engine_manager.project_dir(project_id)
    assert directory.is_dir()

    deleted = client.delete(f"/api/projects/{project_id}")
    assert deleted.status_code == 204
    assert not directory.exists()

    assert client.get(f"/api/projects/{project_id}").status_code == 404
    assert client.delete(f"/api/projects/{project_id}").status_code == 404


def test_engine_manager_is_lazily_created_when_not_injected(tmp_path) -> None:  # noqa: ANN001
    """未注入时按 ``Settings.data_root`` 懒构造（起服务不加载 embedding 模型）。"""
    from tests.conftest import make_app, test_settings

    application = make_app(tmp_path / "data")
    assert application.state.engine_manager is None, "create_app 不做任何 core 初始化"
    with make_client(application) as test_client:
        assert test_client.get("/api/projects").json() == []
    assert isinstance(application.state.engine_manager, EngineManager)
    assert application.state.engine_manager.data_root == test_settings(tmp_path / "data").data_root


def test_delete_project_removes_owner_row(tmp_path) -> None:  # noqa: ANN001
    """TASK-111 修正：用户侧删除必须同时清掉 ``projects`` 归属行（否则留幽灵项目）。

    原缺陷（实测复现于 live 库）：``DELETE /api/projects/{id}`` 删了目录但没删归属行，
    于是 ``GET /api/projects`` 仍列出该项目、点进去 404。清理函数
    （``MetaDB.delete_project_owner``）原先只接在管理员路径上。

    必须是**云端形态**（local_mode=False）才有关联的 meta_db 与真实用户；本地模式的
    ``client`` fixture 没有 ``meta_db``，测不到这个缺陷。
    """
    from nova_core.engine import Engine
    from nova_service.app import create_app
    from nova_service.config import Settings

    from tests.conftest import DeterministicBigramEmbedding, make_client

    settings = Settings(data_root=tmp_path / "data", local_mode=False, local_rescan_interval_s=0.0)
    app = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    manager.attach_meta_db(app.state.meta_db)
    app.state.engine_manager = manager
    try:
        with make_client(app) as test_client:
            boot = test_client.post("/api/auth/bootstrap", json={"name": "alice", "password": "pw"})
            assert boot.status_code == 201, boot.text
            owner_id = boot.json()["userId"]
            db = app.state.meta_db

            project_id = test_client.post(
                "/api/projects/resolve",
                json={"identityKey": "identity:ghost", "displayName": "ghost@main"},
            ).json()["projectId"]
            upload_files(manager, project_id, SAMPLE_FILES)

            assert project_id in db.list_projects(owner_id), "前置：归属行已写入"
            assert manager.project_dir(project_id).is_dir()

            deleted = test_client.delete(f"/api/projects/{project_id}")
            assert deleted.status_code == 204
            assert not manager.project_dir(project_id).exists()

            assert project_id not in db.list_projects(owner_id), "归属行必须与目录同生共死"
            assert test_client.get("/api/projects").json() == [], "不得留下幽灵项目"
    finally:
        manager.close()
