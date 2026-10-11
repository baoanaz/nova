"""Optional client routing preserves legacy D-29 and never trusts a client-supplied user."""
from nova_core.engine import project_id_for
from nova_service.app import create_app
from nova_service.config import Settings

from tests.conftest import make_client, make_manager, register_with_invite, upload_files


def payload(device="a", workspace="b"):
    return {
        "identityKey": "absolute-path-identity",
        "displayName": "workspace-documents",
        "clientScope": {"deviceKey": device * 64, "workspaceKey": workspace * 64},
    }


def test_legacy_identity_and_response_are_unchanged(client):
    response = client.post("/api/projects/resolve", json={"identityKey": "legacy-branch"})
    assert response.status_code == 200
    assert response.json() == {"projectId": project_id_for("legacy-branch"), "created": True}


def test_scoped_route_is_idempotent_and_isolates_devices_and_workspaces(client):
    first = client.post("/api/projects/resolve", json=payload())
    assert first.status_code == 200, first.text
    assert first.json()["routingVersion"] == 1
    same = client.post("/api/projects/resolve", json=payload())
    assert same.json()["projectId"] == first.json()["projectId"]
    assert same.json()["created"] is False
    other_device = client.post("/api/projects/resolve", json=payload(device="c")).json()
    other_workspace = client.post("/api/projects/resolve", json=payload(workspace="d")).json()
    projects = {first.json()["projectId"], other_device["projectId"], other_workspace["projectId"]}
    assert len(projects) == 3
    invalid = payload()
    invalid["clientScope"]["deviceKey"] = "../../other"
    assert client.post("/api/projects/resolve", json=invalid).status_code == 400


def test_cloud_scope_uses_authenticated_account_and_enforces_ownership(tmp_path):
    settings = Settings(data_root=tmp_path / "data", local_mode=False, local_rescan_interval_s=0.0)
    app = create_app(settings)
    manager = make_manager(settings.data_root)
    manager.attach_meta_db(app.state.meta_db)
    app.state.engine_manager = manager
    try:
        with make_client(app) as alice:
            # The MCP lifespan is single-use; a second HTTP client must not run it again.
            bob = make_client(app)
            boot = alice.post("/api/auth/bootstrap", json={"name": "alice", "password": "pw"})
            assert boot.status_code == 201, boot.text
            register_with_invite(bob, app, "bob", "pw")
            own = alice.post("/api/projects/resolve", json=payload()).json()["projectId"]
            forged = payload()
            forged["userId"] = boot.json()["userId"]
            other = bob.post("/api/projects/resolve", json=forged)
            assert other.status_code == 200, other.text
            assert other.json()["projectId"] != own
            assert bob.get(f"/api/projects/{own}").status_code == 404
            assert alice.get(f"/api/projects/{other.json()['projectId']}").status_code == 404
    finally:
        manager.close()


def test_existing_cross_project_embedding_cache_reuses_identical_code(
    client, engine_manager, monkeypatch,
):
    monkeypatch.delenv("NOVA_EMBED_CACHE", raising=False)
    provider = engine_manager.engine.provider
    original = provider.embed
    calls = []

    def counted(texts):
        calls.extend(texts)
        return original(texts)

    monkeypatch.setattr(provider, "embed", counted)
    files = {"shared.py": "def shared_operation(value):\n    return value + 1\n"}
    for branch in ("main", "agent-task"):
        project = client.post("/api/projects/resolve", json={
            "identityKey": f"repo-branch:{branch}",
        }).json()["projectId"]
        upload_files(engine_manager, project, files)
        if branch == "main":
            first = len(calls)
            assert first > 0
        else:
            assert len(calls) == first, "unchanged code must hit the existing embedding cache"
