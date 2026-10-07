"""TASK-034 验收：本地单用户模式（attach / 后台索引 / 进度 / 懒重扫 / 一键起）。

覆盖卡内四层：

- §A：``POST /api/projects/attach``（仅本地模式；``root`` 必须是存在的目录）、
  ``POST /api/projects/{id}/rescan``（202 / 409）、``GET /api/projects[/{id}]`` 的
  ``attachedRoot`` + ``indexProgress``；
- §B：后台索引 worker（不可重入、进度诚实、失败如实上报、不静默死线程、删项目不复活）；
- §C：懒重扫（间隔 0 = 禁用；检索命中新内容；**重扫失败不让检索失败**）；
- §D：``/healthz`` 的 ``projects`` 进度字段（不加载模型、索引中仍 200）。

纪律：不联网、不加载模型（确定性假 provider）；异步索引用**轮询 + 超时上限**验证，
不靠 ``sleep`` 猜时间；需要"索引中"这种时刻时用闸门（``threading.Event``）而不是 sleep。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nova_core.engine import Engine
from nova_service.app import create_app
from nova_service.config import Settings
from nova_service.runtime import EngineManager

from tests.conftest import (
    SAMPLE_DOC,
    SAMPLE_MODULE,
    SAMPLE_MODULE_PATH,
    TARGET_SYMBOL,
    DeterministicBigramEmbedding,
    make_client,
)

#: 轮询索引终态的超时上限（足够宽，正常路径毫秒级完成；失败时给出可读诊断）。
WAIT_TIMEOUT_S = 30.0
#: 轮询间隔（短轮询；不用固定 sleep 猜"索引大概好了"）。
POLL_INTERVAL_S = 0.02


# --------------------------------------------------------------------------- 夹具


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """临时小仓库（1 个 python 模块 + 1 份 markdown，非 git 目录）。"""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "token_service.py").write_text(SAMPLE_MODULE, encoding="utf-8")
    (root / "docs" / "token.md").write_text(SAMPLE_DOC, encoding="utf-8")
    return root


def _make(
    tmp_path: Path, *, fake_provider: bool = True, **overrides: object
) -> tuple[FastAPI, EngineManager, TestClient]:
    """构造应用 + EngineManager + TestClient（每个测试独立的 data_root）。

    ``fake_provider=False`` 时用 core 的真工厂（测试 provider 配置失败路径用；仍不触网）。
    """
    defaults: dict[str, object] = {
        "data_root": tmp_path / "data",
        "local_mode": True,
        "local_rescan_interval_s": 0.0,  # 默认关掉懒重扫：只有专门测它的用例才打开
    }
    settings = Settings(**{**defaults, **overrides})  # type: ignore[arg-type]
    factory = (
        (lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()))
        if fake_provider
        else None
    )
    manager = EngineManager.open(settings.data_root, engine_factory=factory)
    app = create_app(settings)
    app.state.engine_manager = manager
    return app, manager, make_client(app)


@pytest.fixture
def local(tmp_path: Path) -> SimpleNamespace:
    """默认（本地模式、懒重扫关闭）的应用与引擎管理器。"""
    app, manager, client = _make(tmp_path)
    with client:
        try:
            yield SimpleNamespace(app=app, manager=manager, client=client, tmp_path=tmp_path)
        finally:
            manager.close()


@pytest.fixture
def gated_ingest(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """把 ``Engine.ingest_repo`` 换成"进得去、出得来"的闸门版本。

    用途：确定性地制造"索引中"这一时刻（不靠 sleep 猜），并统计调用次数（懒重扫断言用）。
    """
    original = Engine.ingest_repo
    release = threading.Event()
    entered = threading.Event()
    calls: list[str] = []

    def gated(self: Engine, project_id: str, root: str | Path, *, full: bool = False) -> object:
        calls.append(project_id)
        entered.set()
        release.wait(WAIT_TIMEOUT_S)
        return original(self, project_id, root, full=full)

    monkeypatch.setattr(Engine, "ingest_repo", gated)
    return SimpleNamespace(release=release, entered=entered, calls=calls)


# --------------------------------------------------------------------------- 工具


def _attach(client: TestClient, root: Path, **extra: object) -> dict:
    response = client.post("/api/projects/attach", json={"root": str(root), **extra})
    assert response.status_code == 200, response.text
    return response.json()


def _progress(client: TestClient, project_id: str) -> dict:
    response = client.get(f"/api/projects/{project_id}")
    assert response.status_code == 200, response.text
    return response.json()["indexProgress"]


def _wait_for_state(
    client: TestClient, project_id: str, states: set[str], *, timeout: float = WAIT_TIMEOUT_S
) -> dict:
    """轮询直到进入 ``states``（带超时上限）：异步索引的确定性验证方式。"""
    deadline = time.monotonic() + timeout
    seen: dict = {}
    while time.monotonic() < deadline:
        seen = _progress(client, project_id)
        if seen["state"] in states:
            return seen
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"等待 {states} 超时，最后进度：{seen}")


def _index_threads(project_id: str) -> int:
    return sum(1 for t in threading.enumerate() if t.name == f"nova-index-{project_id}")


def _search(client: TestClient, project_id: str, query: str) -> dict:
    response = client.post("/api/query/search", json={"projectId": project_id, "query": query})
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- §A attach


def test_attach_returns_immediately_and_indexes_in_background(
    local: SimpleNamespace, repo: Path
) -> None:
    """attach：立即返回 + 后台索引；轮询到 ``done`` 后 search 能命中（DoD 第 1 条）。"""
    body = _attach(local.client, repo)

    assert set(body) >= {"projectId", "created", "root", "indexProgress", "rootKind", "note"}
    assert body["created"] is True
    assert body["root"] == str(repo.resolve())
    assert body["rootKind"] == "directory", "临时目录不是 git 仓库"
    assert body["note"] and "绝对路径 hash" in body["note"], "非 git 目录必须如实提示（R27）"

    done = _wait_for_state(local.client, body["projectId"], {"done"})
    assert done["processedFiles"] == 2
    assert done["totalFiles"] == 2
    assert done["error"] is None
    assert done["finishedAt"] and done["startedAt"]

    markdown = _search(local.client, body["projectId"], TARGET_SYMBOL)["markdown"]
    assert f"{SAMPLE_MODULE_PATH}:" in markdown, "索引完成后检索必须命中本地仓库的文件"


def test_attach_is_idempotent_for_the_same_root(local: SimpleNamespace, repo: Path) -> None:
    """同目录重复 attach → 同 projectId（D-29 身份幂等），第二次 ``created=false``。"""
    first = _attach(local.client, repo)
    _wait_for_state(local.client, first["projectId"], {"done"})
    second = _attach(local.client, repo)

    assert second["projectId"] == first["projectId"]
    assert second["created"] is False
    assert len(local.manager.list_projects()) == 1


def test_attach_requires_local_mode(tmp_path: Path, repo: Path) -> None:
    """非本地模式 → 403 ``local_mode_required``（DoD：远端模式没有共同文件系统）。

    TASK-060 补充：非本地模式下**先要过鉴权**（401），因此本用例带一个有效 Bearer token——
    "鉴权（401，你是谁）先于授权（403，这个能力在本地模式才开放）"是刻意的顺序：
    未认证调用者不该从 403 里推断出"这个部署支持本地模式"。
    """
    _app, manager, client = _make(tmp_path, local_mode=False)
    headers = _issue_token(_app)
    with client:
        unauthenticated = client.post("/api/projects/attach", json={"root": str(repo)})
        assert unauthenticated.status_code == 401

        response = client.post(
            "/api/projects/attach", json={"root": str(repo)}, headers=headers
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "local_mode_required"
        assert client.get("/healthz").json()["localMode"] is False
    manager.close()


def _issue_token(app: FastAPI) -> dict[str, str]:
    """在非本地模式的 app 上初始化首个账户并签一个 API Key（供鉴权用例复用）。"""
    from nova_service.metadb import MetaDB

    db = app.state.meta_db
    assert isinstance(db, MetaDB)
    user = db.create_user("tester", "x")  # 密码哈希在校验路径之外，这里只关心凭据载体
    from nova_service.auth import create_api_token

    raw, digest, prefix = create_api_token()
    db.create_token(user.id, token_hash=digest, prefix=prefix, name="test")
    return {"Authorization": f"Bearer {raw}"}


@pytest.mark.parametrize("bad_root", ["/nonexistent/path/for-sure", "   "])
def test_attach_invalid_root_is_400(
    local: SimpleNamespace, bad_root: str, tmp_path: Path
) -> None:
    """``root`` 不存在/空白/不是目录 → 400 ``invalid_root``（DoD 第 4 条第一半）。"""
    response = local.client.post("/api/projects/attach", json={"root": bad_root})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_root"

    file_path = tmp_path / "not-a-dir.txt"
    file_path.write_text("x", encoding="utf-8")
    as_file = local.client.post("/api/projects/attach", json={"root": str(file_path)})
    assert as_file.status_code == 400
    assert as_file.json()["error"]["code"] == "invalid_root"

    empty = local.client.post("/api/projects/attach", json={"root": ""})
    assert empty.status_code == 400, "空字符串由请求校验先拦下（仍是 400 信封）"


def test_project_list_and_detail_expose_attached_root_and_progress(
    local: SimpleNamespace, repo: Path
) -> None:
    """``GET /api/projects`` 与 ``/{id}`` 都带 ``attachedRoot`` + ``indexProgress``（冻结接口）。"""
    project_id = _attach(local.client, repo)["projectId"]
    _wait_for_state(local.client, project_id, {"done"})

    listed = local.client.get("/api/projects").json()
    assert len(listed) == 1
    assert listed[0]["attachedRoot"] == str(repo.resolve())
    assert listed[0]["indexProgress"]["state"] == "done"

    detail = local.client.get(f"/api/projects/{project_id}").json()
    assert detail["attachedRoot"] == str(repo.resolve())
    assert detail["indexProgress"]["state"] == "done"
    assert detail["sync"]["chunks"] > 0, "既有字段不动（TASK-031/032 的 sync/blob 仍在）"


def test_unattached_project_reports_idle_progress(local: SimpleNamespace) -> None:
    """未经 attach 的项目：``attachedRoot=null`` + ``state="idle"``（不假装有进度）。"""
    project_id = local.client.post(
        "/api/projects/resolve", json={"identityKey": "identity:remote"}
    ).json()["projectId"]

    detail = local.client.get(f"/api/projects/{project_id}").json()
    assert detail["attachedRoot"] is None
    assert detail["indexProgress"]["state"] == "idle"
    assert detail["indexProgress"]["processedFiles"] == 0

    rescan = local.client.post(f"/api/projects/{project_id}/rescan")
    assert rescan.status_code == 409
    assert rescan.json()["error"]["code"] == "local_root_unknown"


# --------------------------------------------------------------------------- §B 后台 worker


def test_no_second_worker_and_rescan_conflict(
    local: SimpleNamespace, repo: Path, gated_ingest: SimpleNamespace
) -> None:
    """索引中：重复 attach 不重入（幂等 200）、rescan → 409，且**只有一个 worker**。"""
    project_id = _attach(local.client, repo)["projectId"]
    assert gated_ingest.entered.wait(WAIT_TIMEOUT_S), "后台索引没有开始"
    running = _progress(local.client, project_id)
    assert running["state"] == "running"
    assert running["processedFiles"] == 0, "索引期间允许 0（不许伪造进度，D-30）"
    assert running["totalFiles"] == 2, "total 来自扫描阶段（目录列举）"

    again = _attach(local.client, repo)
    assert again["projectId"] == project_id
    assert again["indexProgress"]["state"] == "running"

    rescan = local.client.post(f"/api/projects/{project_id}/rescan")
    assert rescan.status_code == 409
    assert rescan.json()["error"]["code"] == "index_running"
    assert "已处理 0/2 个文件" in rescan.json()["error"]["message"]

    assert _index_threads(project_id) == 1, "不得起第二个 worker"
    assert _progress(local.client, project_id)["startedAt"] == running["startedAt"], (
        "进度不得被重置"
    )
    assert gated_ingest.calls == [project_id], "只有一次 ingest_repo 调用"

    gated_ingest.release.set()
    assert _wait_for_state(local.client, project_id, {"done"})["processedFiles"] == 2


def test_manual_rescan_reindexes_changed_file(local: SimpleNamespace, repo: Path) -> None:
    """rescan → 202 + 新的进度；改文件后重扫，检索命中新内容（增量）。"""
    project_id = _attach(local.client, repo)["projectId"]
    _wait_for_state(local.client, project_id, {"done"})

    (repo / "src" / "token_service.py").write_text(
        SAMPLE_MODULE.replace("续期令牌", "续期令牌（新增：增量刷新语义）"), encoding="utf-8"
    )
    response = local.client.post(f"/api/projects/{project_id}/rescan")
    assert response.status_code == 202, response.text
    assert set(response.json()) == {"indexProgress"}

    done = _wait_for_state(local.client, project_id, {"done"})
    assert done["processedFiles"] == 1, "只重解析变化的那一个文件"
    assert done["totalFiles"] == 2
    assert "增量刷新语义" in _search(local.client, project_id, TARGET_SYMBOL)["markdown"]


def test_index_failure_is_reported_and_service_stays_alive(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """索引失败 → ``state="failed"`` + ``error`` 非空，且服务仍 200 存活（DoD 第 4 条）。"""
    # 真实失败来源：provider 构造不出来（api 模式缺 model/base_url）→ ingest_repo 抛 EngineError
    monkeypatch.setenv("EMBED_MODE", "api")
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)
    _app, manager, client = _make(tmp_path, fake_provider=False)
    with client:
        project_id = _attach(client, repo)["projectId"]
        failed = _wait_for_state(client, project_id, {"failed"})

        assert failed["error"], "失败必须如实写进进度"
        assert "EmbeddingConfigError" in failed["error"]
        assert failed["finishedAt"]

        health = client.get("/healthz")  # 服务仍存活（不因索引失败而 500/进程退出）
        assert health.status_code == 200
        assert health.json()["projects"][0]["indexProgress"]["state"] == "failed"
    manager.close()


def test_delete_during_index_does_not_resurrect_project(
    local: SimpleNamespace, repo: Path, gated_ingest: SimpleNamespace
) -> None:
    """索引中删项目：等线程收尾后删除，**不得复活**目录（卡内 §B 纪律）。"""
    project_id = _attach(local.client, repo)["projectId"]
    assert gated_ingest.entered.wait(WAIT_TIMEOUT_S)
    project_dir = local.manager.project_dir(project_id)

    gated_ingest.release.set()  # 让索引能收尾（delete 会在锁上等它）
    deleted: list[int] = []

    def _delete() -> None:
        deleted.append(local.client.delete(f"/api/projects/{project_id}").status_code)

    deleter = threading.Thread(target=_delete, daemon=True)
    deleter.start()
    deleter.join(WAIT_TIMEOUT_S)

    assert not deleter.is_alive()
    assert deleted == [204]
    assert not project_dir.exists(), "删除后项目目录不得复活"
    assert local.client.get(f"/api/projects/{project_id}").status_code == 404


# --------------------------------------------------------------------------- §C 懒重扫


def test_lazy_rescan_disabled_when_interval_is_zero(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``local_rescan_interval_s=0`` → 检索前**不发扫描**（DoD：断言目录未被重读）。"""
    calls: list[str] = []
    original = Engine.ingest_repo

    def spy(self: Engine, project_id: str, root: str | Path, *, full: bool = False) -> object:
        calls.append(project_id)
        return original(self, project_id, root, full=full)

    monkeypatch.setattr(Engine, "ingest_repo", spy)
    _app, manager, client = _make(tmp_path, local_rescan_interval_s=0.0)
    with client:
        project_id = _attach(client, repo)["projectId"]
        _wait_for_state(client, project_id, {"done"})
        assert calls == [project_id], "attach 自身索引一次"

        (repo / "src" / "token_service.py").write_text(
            SAMPLE_MODULE.replace("续期令牌", "续期令牌（改了）"), encoding="utf-8"
        )
        body = _search(client, project_id, TARGET_SYMBOL)
        assert "rescanError" not in body["meta"]["freshness"]
        assert calls == [project_id], "间隔 0 = 禁用：检索不再触发扫描"
    manager.close()


def test_lazy_rescan_picks_up_new_content(tmp_path: Path, repo: Path) -> None:
    """间隔 > 0：改文件后 search 直接命中新内容（无需手动 rescan，D-27）。"""
    _app, manager, client = _make(tmp_path, local_rescan_interval_s=0.001)
    with client:
        project_id = _attach(client, repo)["projectId"]
        _wait_for_state(client, project_id, {"done"})

        marker = "懒重扫标记：缓存未命中时回落数据库并写入审计。"
        (repo / "docs" / "token.md").write_text(
            SAMPLE_DOC.replace("缓存未命中时回落数据库。", marker), encoding="utf-8"
        )
        markdown = _search(client, project_id, "令牌过期后在哪里刷新")["markdown"]
        assert marker in markdown, "懒重扫后检索必须看到磁盘上的新内容"
    manager.close()


def test_lazy_rescan_failure_does_not_break_retrieval(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重扫失败：记日志 + ``meta.freshness.rescanError`` 如实提示，**检索照常 200**（DoD）。"""
    original = Engine.ingest_repo
    state = SimpleNamespace(fail=False)

    def maybe_fail(
        self: Engine, project_id: str, root: str | Path, *, full: bool = False
    ) -> object:
        if state.fail:
            raise RuntimeError("磁盘炸了（模拟）")
        return original(self, project_id, root, full=full)

    monkeypatch.setattr(Engine, "ingest_repo", maybe_fail)
    _app, manager, client = _make(tmp_path, local_rescan_interval_s=0.001)
    with client:
        project_id = _attach(client, repo)["projectId"]
        _wait_for_state(client, project_id, {"done"})

        state.fail = True
        manager._last_rescan.pop(project_id, None)  # noqa: SLF001 - 让下一次检索必然触发重扫
        body = _search(client, project_id, TARGET_SYMBOL)

        assert body["meta"]["evidenceCount"] + body["meta"]["docsCount"] > 0, "照常返回结果"
        freshness = body["meta"]["freshness"]
        assert "rescanError" in freshness, "失败必须如实上报（不能静默）"
        assert "RuntimeError" in freshness["rescanError"]
        assert "磁盘炸了" in freshness["rescanError"]
        assert set(freshness) >= {"indexedAt", "staleFiles", "indexingFiles"}
        assert body["meta"]["degraded"] is False, "重扫失败不是检索通道降级（两者不混）"
    manager.close()


# --------------------------------------------------------------------------- §D healthz / CLI


def test_healthz_reports_attached_projects_and_stays_200_while_indexing(
    local: SimpleNamespace, repo: Path, gated_ingest: SimpleNamespace
) -> None:
    """``/healthz`` 带 ``projects``（纯内存）；索引中仍 200（服务是活的，卡内 §D）。"""
    project_id = _attach(local.client, repo)["projectId"]
    assert gated_ingest.entered.wait(WAIT_TIMEOUT_S)

    body = local.client.get("/healthz").json()
    assert body["status"] == "ok"
    assert len(body["projects"]) == 1
    entry = body["projects"][0]
    assert entry["projectId"] == project_id
    assert entry["attachedRoot"] == str(repo.resolve())
    assert entry["indexProgress"]["state"] == "running"
    assert entry["indexProgress"]["processedFiles"] == 0
    assert entry["indexProgress"]["totalFiles"] == 2

    gated_ingest.release.set()
    _wait_for_state(local.client, project_id, {"done"})


def test_healthz_without_touching_core_returns_empty_projects(tmp_path: Path) -> None:
    """从未有请求碰过 core 时，``/healthz`` 不为探活去构造 EngineManager（projects 为空）。"""
    settings = Settings(data_root=tmp_path / "data", local_mode=True)
    app = create_app(settings)  # 不注入 manager：走"懒构造"的真实初始状态
    with make_client(app) as client:
        body = client.get("/healthz").json()
        assert body["projects"] == []
        assert app.state.engine_manager is None, "探活不得顺手构造引擎（保持毫秒级）"


def test_local_cli_parses_without_starting_a_server(tmp_path: Path, repo: Path) -> None:
    """``nova-service local --repo`` 的参数面（真起进程的验收见执行记录；这里只测解析）。"""
    from nova_service.__main__ import build_parser

    args = build_parser().parse_args(
        ["local", "--repo", str(repo), "--data-root", str(tmp_path / "d"), "--port", "8792"]
    )
    assert args.command == "local"
    assert args.repo == repo
    assert args.port == 8792
    assert args.no_index is False

    # 旧形式（TASK-030/035 的调用方式）仍可用：无子命令 = serve
    legacy = build_parser().parse_args(["--port", "8797"])
    assert legacy.command is None and legacy.port == 8797
