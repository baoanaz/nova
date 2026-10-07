"""TASK-061 验收：租户双层（token → user → owns project；D-36 逻辑授权层）。

对应卡内 §C 的**逐端点**清单与 DoD 的可测项。本文件是 TASK-061 的**补做验收**：
该卡的 ``metadb.claim_project/owns_project/list_projects`` 由 TASK-060 产出、``resolve`` 处的
claim 由 TASK-085 补上，但 ``require_project_id`` 的**归属校验**与其余入口一直没接线 ——
也就是 TASK-051 A1 的越权面（任何登录用户可访问任何 projectId）。

覆盖矩阵（逐端点，**不许只测一个**）：

| §C 端点 | 越权（B 访问 A 的项目） | 用例 |
|---|---|---|
| ``GET /api/projects`` | 只看到自己的项目 | ``test_list_projects_only_shows_owned`` |
| ``GET /api/projects/{id}`` | 404 | ``test_cross_user_endpoints_are_404`` |
| ``DELETE /api/projects/{id}`` | 404（A 的项目仍在） | ``test_cross_user_delete_is_404`` |
| ``GET /api/sync/status/{projectId}`` | 404 | ``test_cross_user_endpoints_are_404`` |
| ``POST /api/sync/batch-upload`` | 404（不得隐式 claim） | ``test_batch_upload_never_claims`` |
| ``POST /api/sync/checkpoint`` | 404 | ``test_cross_user_endpoints_are_404`` |
| ``POST /api/sync/deletions`` | 404 | ``test_cross_user_endpoints_are_404`` |
| ``POST /api/query/search`` | 404 | ``test_cross_user_endpoints_are_404`` |
| ``POST /api/query/ask`` | 404 | ``test_cross_user_endpoints_are_404`` |
| ``GET /api/usage/projects/{id}`` | 404 | ``test_cross_user_endpoints_are_404`` |
| ``GET /api/projects/{id}/index-runs`` | 404 | ``test_cross_user_endpoints_are_404`` |
| ``GET /api/projects/{id}/index-stats`` | 404 | ``test_cross_user_endpoints_are_404`` |
| ``mcp.py::_project_id_for`` | **未接**（卡内自相矛盾） | 无（登记为未决问题） |

其余 DoD 项：归属的产生（``attach`` / ``resolve`` 的 claim、幂等）、``GET /api/projects`` 过滤、
本地模式回归（R34）、以及跨用户共用同一 identityKey 的既定简化。

纪律：``tmp_path`` 下的临时 data_root + 确定性假 provider，不联网、不加载真实模型。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from nova_core.engine import Engine
from nova_core.hashing import blob_hash
from nova_service.app import create_app
from nova_service.config import Settings
from nova_service.metadb import MetaDB
from nova_service.runtime import EngineManager

from tests.conftest import (
    SAMPLE_FILES,
    SAMPLE_MODULE_PATH,
    DeterministicBigramEmbedding,
    make_client,
    make_invite,
)

PASSWORD = "correct-horse-battery"


# --------------------------------------------------------------------------- 夹具


def _build(tmp_path: Path, *, local_mode: bool) -> SimpleNamespace:
    """构造云端/本地形态的应用（注入确定性假 provider 的 manager）。"""
    settings = Settings(
        data_root=tmp_path / "data",
        local_mode=local_mode,
        local_rescan_interval_s=0.0,
    )
    app = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    manager.attach_meta_db(app.state.meta_db)
    app.state.engine_manager = manager
    return SimpleNamespace(app=app, manager=manager, settings=settings)


def _token(client: TestClient, name: str) -> dict[str, str]:
    """为**当前会话身份**签一个 API Key，返回可复用的 Authorization 头。"""
    created = client.post("/api/auth/tokens", json={"name": name})
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['token']}"}


def _blobs(files: dict[str, str]) -> list[dict[str, str]]:
    return [
        {
            "path": path,
            "blobHash": blob_hash(path, content.encode("utf-8")),
            "contentB64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        for path, content in files.items()
    ]


@pytest.fixture
def two_users(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """云端形态、两个**真实**用户（A / B），各自 resolve 了一个项目。

    为什么用 API Key 而不是 session cookie：``TestClient`` 只有一个 cookie jar，注册 B 会覆盖
    A 的会话 cookie；用两个 Bearer 头才能在同一客户端上干净地切换身份。
    """
    ns = _build(tmp_path, local_mode=False)
    ns.client = _make_mcp_client(ns.app)
    with ns.client:
        bootstrap = ns.client.post(
            "/api/auth/bootstrap", json={"name": "alice", "password": PASSWORD}
        )
        assert bootstrap.status_code == 201, bootstrap.text
        ns.alice_id = bootstrap.json()["userId"]
        ns.alice = _token(ns.client, "alice-key")

        registered = ns.client.post(
            "/api/auth/register",
            json={
                "name": "bob",
                "password": PASSWORD,
                "inviteCode": make_invite(ns.app),
            },
        )
        assert registered.status_code == 201, registered.text
        ns.bob_id = registered.json()["userId"]
        ns.bob = _token(ns.client, "bob-key")

        ns.alice_project = ns.client.post(
            "/api/projects/resolve",
            json={"identityKey": "identity:alice-repo", "displayName": "alice-repo"},
            headers=ns.alice,
        ).json()["projectId"]
        ns.bob_project = ns.client.post(
            "/api/projects/resolve",
            json={"identityKey": "identity:bob-repo", "displayName": "bob-repo"},
            headers=ns.bob,
        ).json()["projectId"]
        ns.meta_db = ns.app.state.meta_db
        yield ns
    ns.manager.close()


# --------------------------------------------------------------------------- §C 逐端点：越权 → 404


#: §C 清单里全部消费 ``projectId`` 的端点（``rescan`` 仅本地模式 → 非越权面，不在此列）。
CROSS_USER_CALLS: list[tuple[str, str, dict[str, object]]] = [
    ("get", "/api/projects/{pid}", {}),
    ("get", "/api/sync/status/{pid}", {}),
    ("post", "/api/sync/batch-upload", {"blobs": []}),
    ("post", "/api/sync/checkpoint", {"blobHashes": []}),
    ("post", "/api/sync/deletions", {"paths": []}),
    ("post", "/api/query/search", {"query": "令牌过期后在哪里刷新"}),
    ("post", "/api/query/ask", {"question": "令牌过期后在哪里刷新"}),
    ("get", "/api/usage/projects/{pid}", {}),
    ("get", "/api/projects/{pid}/index-runs", {}),
    ("get", "/api/projects/{pid}/index-stats", {}),
]


@pytest.mark.parametrize(
    "method,path,body", CROSS_USER_CALLS, ids=[f"{m.upper()} {p}" for m, p, _ in CROSS_USER_CALLS]
)
def test_cross_user_endpoints_are_404(
    two_users: SimpleNamespace, method: str, path: str, body: dict[str, object]
) -> None:
    """B 访问 A 的 projectId → **404 project_not_found**（逐端点，**不是 403**）。

    为什么必须是 404 而不是 403：403 会泄露"这个 projectId 存在"，与 Module/06 §2.2 的
    "不给探测面"冲突（卡内 §C 已裁定）。因此越权与"真不存在"用**同一个 code 与状态码**。
    """
    url = path.format(pid=two_users.alice_project)
    payload = {"projectId": two_users.alice_project, **body} if method == "post" else None
    response = two_users.client.request(
        method, url, json=payload, headers=two_users.bob
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "project_not_found"


def test_cross_user_delete_is_404(two_users: SimpleNamespace) -> None:
    """B ``DELETE`` A 的项目 → 404，且 A 的项目**必须还在**（拒绝删除，不是静默成功）。"""
    url = f"/api/projects/{two_users.alice_project}"
    assert two_users.client.delete(url, headers=two_users.bob).status_code == 404
    assert two_users.client.get(url, headers=two_users.alice).status_code == 200


def test_owner_access_is_unaffected(two_users: SimpleNamespace) -> None:
    """同一批端点上，**归属者自己**访问照常 200（归属校验没有误伤）。"""
    for method, path in [
        ("get", "/api/projects/{pid}"),
        ("get", "/api/sync/status/{pid}"),
        ("get", "/api/usage/projects/{pid}"),
        ("get", "/api/projects/{pid}/index-runs"),
        ("get", "/api/projects/{pid}/index-stats"),
    ]:
        response = two_users.client.request(
            method, path.format(pid=two_users.alice_project), headers=two_users.alice
        )
        assert response.status_code == 200, f"{method} {path}: {response.text}"


def test_cross_user_rescan_is_local_mode_403_not_a_leak(two_users: SimpleNamespace) -> None:
    """``rescan`` 仅本地模式：云端对任何人都是 403 ``local_mode_required``（非越权面）。

    顺序纪律：能力检查（403）先于归属检查（404），因此它不会变成"存在性探针"。
    """
    response = two_users.client.post(
        f"/api/projects/{two_users.alice_project}/rescan", headers=two_users.bob
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "local_mode_required"


# --------------------------------------------------------------- §C：GET /api/projects 过滤


def test_list_projects_only_shows_owned(two_users: SimpleNamespace) -> None:
    """``GET /api/projects`` 只返回当前用户的项目（**不是** manager 的全量）。"""
    alice = two_users.client.get("/api/projects", headers=two_users.alice).json()
    bob = two_users.client.get("/api/projects", headers=two_users.bob).json()

    assert [item["projectId"] for item in alice] == [two_users.alice_project]
    assert [item["projectId"] for item in bob] == [two_users.bob_project]


# --------------------------------------------------------------------------- §B：归属的产生


def test_batch_upload_never_claims(two_users: SimpleNamespace) -> None:
    """``batch-upload`` 到**未归属**当前用户的 projectId → 404，且**不得**隐式 claim。

    为什么是 404 而不是卡内 §B 原文的 403：见 :func:`test_cross_user_endpoints_are_404` 的
    理由（§C 的"不给探测面"口径优先）。

    为什么绝不能隐式 claim：``projectId`` 是 ``sha256(identity)``（D-29），同仓库的他人**算得出**
    同一 id；一旦"知道 id 就能抢注"，归属就失去意义（卡内 §B 明令"不得"）。
    """
    payload = {"projectId": two_users.alice_project, "blobs": _blobs(SAMPLE_FILES)}
    response = two_users.client.post(
        "/api/sync/batch-upload", json=payload, headers=two_users.bob
    )
    assert response.status_code == 404
    assert two_users.meta_db.list_projects(two_users.bob_id) == [two_users.bob_project]


def test_upload_by_owner_indexes_and_is_searchable(two_users: SimpleNamespace) -> None:
    """归属者上传自己的项目 → 200；B 检索自己的项目正常（"B 自己的项目正常"）。"""
    uploaded = two_users.client.post(
        "/api/sync/batch-upload",
        json={"projectId": two_users.bob_project, "blobs": _blobs(SAMPLE_FILES)},
        headers=two_users.bob,
    )
    assert uploaded.status_code == 200, uploaded.text

    found = two_users.client.post(
        "/api/query/search",
        json={"projectId": two_users.bob_project, "query": "令牌过期后在哪里刷新"},
        headers=two_users.bob,
    )
    assert found.status_code == 200, found.text
    assert SAMPLE_MODULE_PATH in found.json()["markdown"]


def test_resolve_claims_and_is_idempotent(two_users: SimpleNamespace) -> None:
    """同一用户重复 resolve 同一 identityKey → 幂等（不产生第二行、不报错）。"""
    first = two_users.client.post(
        "/api/projects/resolve",
        json={"identityKey": "identity:alice-repo", "displayName": "alice-repo"},
        headers=two_users.alice,
    ).json()
    assert first == {"projectId": two_users.alice_project, "created": False}
    assert two_users.meta_db.list_projects(two_users.alice_id) == [two_users.alice_project]


def test_shared_repo_second_user_resolves_but_does_not_take_over(
    two_users: SimpleNamespace,
) -> None:
    """**共用仓库的既定简化**（卡内 §B 要求明确写出）：第二人 resolve 不报错、也不获得归属。

    本项目常见形态是多人共用同一仓库（同一 identityKey → 同一 projectId）。TASK-061 §A0 已裁定
    口径 A：``resolve`` **不**报 403 ``project_owned_by_other``（否则会把第二人直接卡死），
    越权一律由 §C 的 404 拦。代价是第二人 resolve 成功但项目不在其名下 —— 这正是"先到先得"的
    V1 简化：``org_id`` 列留待 V2 做共享（Module/06 §2.3）。
    """
    response = two_users.client.post(
        "/api/projects/resolve",
        json={"identityKey": "identity:alice-repo", "displayName": "alice-repo"},
        headers=two_users.bob,
    )
    assert response.status_code == 200, response.text
    assert response.json()["projectId"] == two_users.alice_project  # 同一 identity → 同一 id
    # 归属没有转移，也没有产生第二行。
    assert two_users.meta_db.list_projects(two_users.alice_id) == [two_users.alice_project]
    assert two_users.meta_db.list_projects(two_users.bob_id) == [two_users.bob_project]
    # 第二人随后访问该项目 → 404（§C 的归属校验生效）。
    assert (
        two_users.client.get(
            f"/api/projects/{two_users.alice_project}", headers=two_users.bob
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------- §B：attach 的 claim


def test_attach_claims_to_the_local_user(tmp_path: Path) -> None:
    """本地模式 + 已注入 MetaDB（``nova-service local`` 的真实路径）→ attach 认领给本地用户。

    §B 第二行要求 ``attach`` 认领给 ``is_local=1`` 的本地用户。本地模式默认没有账户体系
    （R34），因此这里在**元数据库已存在**时取/建一个 ``local`` 隐式账户再写归属。
    """
    from nova_service.auth import LOCAL_USER_NAME

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / SAMPLE_MODULE_PATH.replace("src/", "")).write_text(
        SAMPLE_FILES[SAMPLE_MODULE_PATH], encoding="utf-8"
    )
    settings = Settings(
        data_root=tmp_path / "data", local_mode=True, local_rescan_interval_s=0.0
    )
    app = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda r: Engine.open(r, provider=DeterministicBigramEmbedding()),
    )
    # 与 ``__main__._run_local`` 一致：本地模式由启动路径显式注入 MetaDB。
    db = MetaDB.open(settings.meta_db_path)
    manager.attach_meta_db(db)
    app.state.meta_db = db
    app.state.engine_manager = manager

    client = make_client(app)
    with client:
        attached = client.post(
            "/api/projects/attach", json={"root": str(repo), "displayName": "repo"}
        )
        assert attached.status_code == 200, attached.text
        project_id = attached.json()["projectId"]
    assert db.list_projects(db.ensure_local_user().id) == [project_id]
    assert db.get_user_by_name(LOCAL_USER_NAME) is not None
    manager.close()


def test_attach_does_not_create_meta_db_in_default_local_mode(tmp_path: Path) -> None:
    """默认本地模式（app 级无 MetaDB）→ attach **不建库、不写归属**（R34 回归保护）。

    与 :func:`test_attach_claims_to_the_local_user` 合起来钉住边界：认领只在"库已存在"时发生，
    默认本地单用户模式的行为与今天逐字一致（``nova-meta.db`` 不被顺手创建）。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    settings = Settings(
        data_root=tmp_path / "data", local_mode=True, local_rescan_interval_s=0.0
    )
    app = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda r: Engine.open(r, provider=DeterministicBigramEmbedding()),
    )
    app.state.engine_manager = manager
    assert app.state.meta_db is None
    with make_client(app) as client:
        assert client.post("/api/projects/attach", json={"root": str(repo)}).status_code == 200
    assert app.state.meta_db is None, "本地模式不该因为 attach 而创建 nova-meta.db"
    assert not settings.meta_db_path.exists(), "磁盘上也不该出现 nova-meta.db"
    manager.close()


# --------------------------------------------------------------- §D：本地模式回归（R34）


def test_local_mode_ignores_ownership_entirely(tmp_path: Path) -> None:
    """本地模式（无 ``nova_user``）：不带任何凭据即可访问全部端点，行为与今天一致（R34）。"""
    settings = Settings(
        data_root=tmp_path / "data", local_mode=True, local_rescan_interval_s=0.0
    )
    app = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda r: Engine.open(r, provider=DeterministicBigramEmbedding()),
    )
    app.state.engine_manager = manager
    with make_client(app) as client:
        resolved = client.post(
            "/api/projects/resolve", json={"identityKey": "identity:local-only"}
        ).json()
        project_id = resolved["projectId"]
        assert client.get("/api/projects").status_code == 200
        assert client.get(f"/api/projects/{project_id}").status_code == 200
        assert client.get(f"/api/sync/status/{project_id}").status_code == 200
        assert client.post(
            "/api/sync/batch-upload",
            json={"projectId": project_id, "blobs": _blobs(SAMPLE_FILES)},
        ).status_code == 200
    manager.close()


# --------------------------------------------------------------------------- MCP 面（TASK-089）

#: MCP 的请求头（缺 ``Accept`` 会被协议层拒）。base_url 必须带端口：SDK 的 DNS-rebinding
#: 白名单只有 ``127.0.0.1:*`` / ``localhost:*`` / ``[::1]:*``，``testserver`` 会被 421 拒。
_MCP_BASE_URL = "http://127.0.0.1:8787"
_MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _make_mcp_client(app) -> TestClient:
    """带端口的 ``TestClient``：MCP SDK 的 DNS-rebinding 白名单只有 ``127.0.0.1:*`` /
    ``localhost:*`` / ``[::1]:*``，默认的 ``http://testserver`` 会被 421 拒。REST 路径不受影响。

    不复用 :func:`tests.conftest.make_client`（它固定了默认 base_url）：本文件只有 MCP 必须带端口。
    """
    return TestClient(app, base_url=_MCP_BASE_URL, raise_server_exceptions=False)
_MCP_PROTOCOL = "2025-06-18"

#: 矩阵的两列：CF-06 冻结的两个工具 → （参数名，参数值）。
_MCP_TOOLS: dict[str, tuple[str, str]] = {
    "search_context": ("query", "令牌过期后在哪里刷新"),
    "ask_project": ("question", "令牌过期后在哪里刷新"),
}


def _mcp_call(
    client: TestClient,
    *,
    tool: str,
    project_root: str,
    headers: dict[str, str],
    msg_id: int = 3,
) -> tuple[int, dict[str, Any] | None]:
    """在**新会话**上跑一次 ``tools/call``，返回 ``(HTTP 状态, result | None)``。

    返回 ``None`` 的 result = 请求在协议/鉴权层就被拒（如 401），没有 JSON-RPC 结果对象——
    这正是把"未认证"与"已认证但越权"两行矩阵分开钉住的地方。
    """
    init = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _MCP_PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "nova-tests", "version": "0"},
            },
        },
        headers={**_MCP_HEADERS, **headers},
    )
    if init.status_code != 200:
        return init.status_code, None
    session_id = init.headers.get("mcp-session-id")
    assert session_id, "initialize 必须返回 mcp-session-id"
    session_headers = {**_MCP_HEADERS, **headers, "mcp-session-id": session_id}
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=session_headers,
    )
    field, value = _MCP_TOOLS[tool]
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": {field: value, "project_root": project_root},
            },
        },
        headers=session_headers,
    )
    if response.status_code != 200:
        return response.status_code, None
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return response.status_code, json.loads(line[len("data: ") :])["result"]
    raise AssertionError(f"响应里没有 data 帧：{response.text[:200]!r}")


def _mcp_text(result: dict[str, Any] | None) -> str:
    assert result is not None, "没有 JSON-RPC 结果对象（协议/鉴权层已拒）"
    return "\n".join(block["text"] for block in result["content"] if block["type"] == "text")


def _mcp_error_reason(result: dict[str, Any] | None) -> str:
    """工具错误的**业务文本**（剥掉 SDK 的 ``Error executing tool <name>: `` 前缀）。

    为什么只比业务部分：前缀随工具名变化（``... search_context: ...`` vs ``ask_project`` 那条），
    它不是 nova 的语义。
    """
    text = _mcp_text(result)
    _, separator, reason = text.partition(": ")
    return reason if separator else text


#: "项目不存在"拒绝的形态（projectId 是 16 位十六进制）。
_DENIAL_RE = re.compile(r"^项目不存在：[0-9a-f]{16}$")


def _assert_indistinguishable_denial(result: dict[str, Any] | None) -> None:
    """断言拒绝是**同一种形态**且**不含任何可操作线索**。

    为什么不必比 projectId 字面值：它由调用方自己提交的 ``project_root`` 决定，B 本来就能算出来，
    写进文案不构成泄露。真正的泄露面是"两种结果形态不同"——例如"越权"给简短拒绝、"不存在"给
    ``未知项目：… 请用 nova-service local --repo …`` 这种可操作提示，B 据此即可区分存在性。
    """
    reason = _mcp_error_reason(result)
    assert _DENIAL_RE.match(reason), reason
    assert "未知项目" not in reason, f"不得给可区分的存在性提示：{reason}"
    assert "nova-service local" not in reason, f"不得给可区分的存在性提示：{reason}"


def _materialize_repo(tmp_path: Path, name: str) -> Path:
    """在 ``tmp_path`` 下写一份样例仓库并返回其根（D-29 非 git → 身份 = 绝对路径 hash）。"""
    repo = tmp_path / name
    for path, content in SAMPLE_FILES.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return repo


def _prepare_project(
    ns: SimpleNamespace, repo: Path, headers: dict[str, str]
) -> tuple[str, str]:
    """把真实目录 ``repo`` 变成一个**已提交给当前身份且已索引**的项目。

    为什么要走 resolve + batch-upload 而不是 attach：MCP 工具用 ``repo_identity`` 从
    ``project_root`` 反解 projectId，而 attach 只在本地模式可用（云端 403）。
    ``POST /api/projects/resolve`` 要**调用方自己算 identityKey**（D-29）：

    - 有 git remote → ``sha256(remote_url)``；
    - 无 git → ``sha256(canonical 绝对路径)``。

    这里用后者（临时目录不是 git 仓库），之后 resolve 会认领它、上传会灌入样例文件。
    返回 ``(identityKey, project_root)``。

    为何不用 ``manager.attach_local``：那条路径会给 manager 绑上 ``attached_root``，使 MCP 的
    懒重扫（``rescan_if_due``）去扫本地目录，而不是走已上传的 blobs——本卡要验的是归属校验，
    不是重扫，故保持"云端上传"这一条形态。
    """
    resolved = repo.resolve()
    identity_key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
    resolved_project = ns.client.post(
        "/api/projects/resolve",
        json={"identityKey": identity_key, "displayName": repo.name},
        headers=headers,
    )
    assert resolved_project.status_code == 200, resolved_project.text
    project_id = resolved_project.json()["projectId"]
    _upload_via_api(ns, project_id, headers)
    return project_id, str(resolved)


def _upload_via_api(ns: SimpleNamespace, project_id: str, headers: dict[str, str]) -> None:
    """走 REST 上传（注入的 manager 无 attached_root，不能用 MCP 的懒重扫路径）。"""
    uploaded = ns.client.post(
        "/api/sync/batch-upload",
        json={"projectId": project_id, "blobs": _blobs(SAMPLE_FILES)},
        headers=headers,
    )
    assert uploaded.status_code == 200, uploaded.text


@pytest.mark.parametrize("tool", sorted(_MCP_TOOLS))
def test_mcp_cross_user_call_is_indistinguishable_from_missing(
    two_users: SimpleNamespace, tool: str
) -> None:
    """**TASK-089 主验收**：B 在 MCP 面调 A 的项目 → ``isError``，且与"项目真不存在"逐字同文案。

    两条断言合起来才完整：

    1. ``isError=true`` 且文本是 REST 面的 404 文案 —— 越权被拒；
    2. 与 **B 自己拿着同样格式、但从未存在的 projectId** 所得的文本**逐字相同** —— 没有探测面。
       若两者不同，B 就能据此判断"这个 projectId 存在"。
    """
    alice_project, alice_root = _prepare_project(
        two_users,
        _materialize_repo(two_users.settings.data_root.parent, "alice-repo"),
        two_users.alice,
    )
    ghost_root = _materialize_repo(two_users.settings.data_root.parent, "ghost-repo")

    status, denied = _mcp_call(
        two_users.client, tool=tool, project_root=alice_root, headers=two_users.bob
    )
    assert status == 200, "MCP 的工具错误走正常响应 + isError（Module/05 §2.2），不是 HTTP 4xx"
    assert denied is not None and denied.get("isError") is True, _mcp_text(denied)
    assert _mcp_error_reason(denied) == f"项目不存在：{alice_project}", _mcp_text(denied)

    _, missing = _mcp_call(
        two_users.client, tool=tool, project_root=str(ghost_root), headers=two_users.bob
    )
    # 越权与不存在走**同一种**拒绝形态（均无 "未知项目" / "请用 nova-service local" 这类线索）。
    _assert_indistinguishable_denial(missing)
    _assert_indistinguishable_denial(denied)


@pytest.mark.parametrize("tool", sorted(_MCP_TOOLS))
def test_mcp_owner_call_succeeds(two_users: SimpleNamespace, tool: str) -> None:
    """**归属者自己在 MCP 面正常**（不误伤）：同一 project_root，A 调 → 非 ``isError``。"""
    _, alice_root = _prepare_project(
        two_users,
        _materialize_repo(two_users.settings.data_root.parent, "alice-repo"),
        two_users.alice,
    )
    status, result = _mcp_call(
        two_users.client, tool=tool, project_root=alice_root, headers=two_users.alice
    )
    assert status == 200, status
    assert result is not None and result.get("isError") is not True, _mcp_text(result)
    assert SAMPLE_MODULE_PATH in _mcp_text(result), "归属者应拿到真实检索结果"


@pytest.mark.parametrize("tool", sorted(_MCP_TOOLS))
def test_mcp_without_credentials_is_401(two_users: SimpleNamespace, tool: str) -> None:
    """**无凭据** → HTTP **401**（与 REST 面一致）。

    这一行由 ``app.py`` 的鉴权中间件给出（TASK-060），早于任何工具执行——
    因此没有 JSON-RPC 结果对象，也拿不到 ``project_root``（不给越权者任何项目信息）。

    必须先清 cookie：``bootstrap``/``register`` 会在 ``TestClient`` 的 cookie jar 里留下一个
    session（见夹具注释），不清掉就不是真的『无凭据』。
    """
    _, alice_root = _prepare_project(
        two_users,
        _materialize_repo(two_users.settings.data_root.parent, "alice-repo"),
        two_users.alice,
    )
    two_users.client.cookies.clear()
    status, result = _mcp_call(two_users.client, tool=tool, project_root=alice_root, headers={})
    assert status == 401, status
    assert result is None


def test_mcp_identity_is_per_request_not_sticky(two_users: SimpleNamespace) -> None:
    """**身份按请求而非按会话**：同一 session 上换 Bearer 头 → 看到的是新身份。

    这条是 §A ``contextvars`` 方案的正确性钉子：若身份被粘在 session 上（方案 B 的天然风险），
    B 只要在 A 建好的 session 上发一个请求就能读到 A 的项目。
    """
    alice_project, alice_root = _prepare_project(
        two_users,
        _materialize_repo(two_users.settings.data_root.parent, "alice-repo"),
        two_users.alice,
    )
    init = two_users.client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _MCP_PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "nova-tests", "version": "0"},
            },
        },
        headers={**_MCP_HEADERS, **two_users.alice},
    )
    session_id = init.headers["mcp-session-id"]
    two_users.client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**_MCP_HEADERS, **two_users.alice, "mcp-session-id": session_id},
    )
    field, value = _MCP_TOOLS["search_context"]

    def call(headers: dict[str, str], msg_id: int) -> dict[str, Any]:
        response = two_users.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": "tools/call",
                "params": {
                    "name": "search_context",
                    "arguments": {field: value, "project_root": alice_root},
                },
            },
            headers={**_MCP_HEADERS, **headers, "mcp-session-id": session_id},
        )
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[len("data: ") :])["result"]
        raise AssertionError(response.text[:200])

    # A 建会话 + 调用 → 正常；同 session 换 B 的凭据 → 拒绝（不泄露）
    assert call(two_users.alice, 2).get("isError") is not True
    denied = call(two_users.bob, 3)
    assert denied.get("isError") is True
    assert _mcp_error_reason(denied) == f"项目不存在：{alice_project}"


def test_mcp_local_mode_still_fully_open(tmp_path: Path) -> None:
    """**本地模式（R34）完全放行**：无凭据、无账户，两个工具都照常工作（不误伤）。"""
    ns = _build(tmp_path, local_mode=True)
    ns.client = _make_mcp_client(ns.app)
    with ns.client:
        repo = _materialize_repo(tmp_path, "local-repo")
        identity_key = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()
        project_id = ns.client.post(
            "/api/projects/resolve", json={"identityKey": identity_key}
        ).json()["projectId"]
        _upload_via_api(ns, project_id, {})
        for tool in sorted(_MCP_TOOLS):
            status, result = _mcp_call(ns.client, tool=tool, project_root=str(repo), headers={})
            assert status == 200, status
            assert result is not None and result.get("isError") is not True, _mcp_text(result)
    ns.manager.close()


def test_mcp_local_mode_unknown_project_keeps_actionable_hint(tmp_path: Path) -> None:
    """**本地模式 R34 回归**：未知目录仍给 TASK-040 的可操作文案，逐字未变（不误伤）。"""
    ns = _build(tmp_path, local_mode=True)
    ns.client = _make_mcp_client(ns.app)
    with ns.client:
        ghost = _materialize_repo(tmp_path, "ghost-repo")
        status, result = _mcp_call(
            ns.client, tool="search_context", project_root=str(ghost), headers={}
        )
        assert status == 200, status
        assert result is not None and result.get("isError") is True
        text = _mcp_text(result)
        assert "未知项目" in text and "nova-service local --repo" in text, text
        assert "项目不存在" not in text, "本地模式不该被云端的『不给探测面』文案顶替"
    ns.manager.close()
