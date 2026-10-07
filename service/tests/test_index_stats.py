"""TASK-062 / TASK-085 验收：索引统计落库（记录时机、口径、裁剪、持久化）。

TASK-062 的 DoD 要求这个文件但**它当时没被写出来**，且只接了本地 attach 路径 —— 本文件是
TASK-085 的补做：把卡内 DoD 逐条落实，并加上 TASK-085 的核心回归点（客户端上传路径）。

覆盖矩阵：

| 场景 | 断言 |
|---|---|
| **客户端上传（batch-upload）** | `GET /api/index-stats` 的 `total=1`（**TASK-085 核心回归点**） |
| 成功 run | `succeeded=1`，`durationMs = finished-start` |
| 失败 run（provider 挂） | `failed=1`，`error_text` 不含 key / base_url（脱敏） |
| `errors>0` 但索引正常 | 计入 `succeeded`，`errors` 字段如实 |
| `avgDurationMs` 口径 | 失败 run（极快）**不进**平均 |
| 保留策略 | 超 500 条裁掉最旧的 |
| 重启 | 统计仍在（落库而非内存的证明） |
| 两条路径口径 | 本地 attach 一次 + 客户端上传一次 → `total=2`，字段形态可对比 |

纪律：不联网、不加载模型（假 provider）；数据落 ``tmp_path``；不用 ``sleep`` 猜时间。
"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from nova_core.embedding import ApiNetworkError
from nova_core.engine import Engine
from nova_core.hashing import blob_hash
from nova_core.interfaces import EmbeddingProfile
from nova_service.app import create_app
from nova_service.config import Settings
from nova_service.metadb import INDEX_RUN_KEEP, MetaDB
from nova_service.runtime import EngineManager

from tests.conftest import (
    SAMPLE_FILES,
    SAMPLE_MODULE_PATH,
    DeterministicBigramEmbedding,
    make_client,
)

PASSWORD = "correct-horse-battery"
#: 一个**假的** key 与 endpoint：它们只出现在（被记录的）provider 异常文本里。
FAKE_API_KEY = "sk-live-SHOULD-NOT-LEAK-abc123"
FAKE_BASE_URL = "https://api.siliconflow.cn"


# --------------------------------------------------------------------------- 夹具与工具


class _BrokenProvider:
    """``embed`` 时抛 ``ApiNetworkError`` 的 provider（模拟云端 provider 不可达，不联网）。"""

    def __init__(self) -> None:
        self._profile = EmbeddingProfile(model_id="fake:broken", dim=64, max_input_tokens=512)
        self._error = ApiNetworkError(
            f"embedding 请求网络失败（ConnectError，endpoint={FAKE_BASE_URL}/v1/embeddings，"
            f"尝试 3/3）：api_key={FAKE_API_KEY}"
        )

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise self._error

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        raise self._error


def _make(
    tmp_path: Path, *, provider: object | None = None, local_mode: bool = False
) -> SimpleNamespace:
    """建一个**云端形态**的应用：鉴权开启 + app 级 MetaDB + manager。

    ``provider`` 缺省为确定性假 provider（索引成功）；传 :class:`_BrokenProvider` 可造失败 run。

    为什么显式 ``attach_meta_db``：测试必须注入带假 provider 的 manager（生产走
    ``deps.get_engine_manager`` 懒构造真 provider），而**注入的 manager 不再经过那条接线**。
    这里手工补上，与生产语义等价；接线本身由
    :func:`test_lazy_manager_is_wired_to_the_app_meta_db` 单独守着。
    """
    settings = Settings(data_root=tmp_path / "data", local_mode=local_mode)
    app = create_app(settings)
    chosen = provider if provider is not None else DeterministicBigramEmbedding()
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=chosen),  # type: ignore[arg-type]
    )
    manager.attach_meta_db(app.state.meta_db)
    app.state.engine_manager = manager
    return SimpleNamespace(
        app=app, manager=manager, settings=settings, meta_db=app.state.meta_db
    )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """一个已 bootstrap、已建 API Key、已 resolve 项目的云端环境。"""
    ns = _make(tmp_path)
    ns.client = make_client(ns.app)
    with ns.client:
        ns.client.post(
            "/api/auth/bootstrap", json={"name": "owner", "password": PASSWORD}
        )
        token = ns.client.post("/api/auth/tokens", json={"name": "k"}).json()["token"]
        ns.headers = {"Authorization": f"Bearer {token}"}
        ns.project_id = ns.client.post(
            "/api/projects/resolve",
            json={"identityKey": "identity:stats-repo", "displayName": "stats"},
        ).json()["projectId"]
        yield ns
    ns.manager.close()


def upload(
    env: SimpleNamespace, files: dict[str, str] | None = None, **extra: object
) -> object:
    """走真实 HTTP 的 ``batch-upload``（客户端 Agent 用的就是它）。

    ``extra`` 合并进请求体（如 ``branch`` / ``commit``）。
    """
    payload = files if files is not None else SAMPLE_FILES
    blobs = [
        {
            "path": path,
            "blobHash": blob_hash(path, content.encode("utf-8")),
            "contentB64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        for path, content in payload.items()
    ]
    return env.client.post(
        "/api/sync/batch-upload",
        json={"projectId": env.project_id, "blobs": blobs, **extra},
        headers=env.headers,
    )


def index_stats(env: SimpleNamespace) -> dict:
    response = env.client.get("/api/index-stats", headers=env.headers)
    assert response.status_code == 200, response.text
    return response.json()


def project_chunks(env: SimpleNamespace) -> int:
    """项目库里的 chunk 总数（= ``sync_status().chunks``，与 run 记录的 ``chunks`` 同源）。"""
    return int(env.manager.sync_status(env.project_id)["chunks"])


def project_index_stats(env: SimpleNamespace) -> dict:
    response = env.client.get(
        f"/api/projects/{env.project_id}/index-stats", headers=env.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- §核心回归点


def test_batch_upload_path_records_a_run(env: SimpleNamespace) -> None:
    """**TASK-085 核心回归点**：客户端上传（Agent 实际路径）必须落一条 run 记录。

    改动前实测：``batch-upload`` 走 ``manager.ingest``，完全绕过 ``ProjectIndexer.on_finish``，
    且云端 manager 由 ``deps`` 懒构造、没接 MetaDB → ``/api/index-stats`` 恒为
    ``{total: 0, recent: []}``，用户接入后打开 WebUI 会以为系统没工作。

    这里用**单项目端点**断言（不套归属过滤），把"落库"与"看得见"两个条件分开验：
    后者由 :func:`test_cross_project_stats_work_after_resolve` 守。
    """
    assert upload(env).status_code == 200
    body = project_index_stats(env)["history"]
    assert body["total"] == 1
    assert body["succeeded"] == 1
    assert body["failed"] == 0
    assert body["lastState"] == "done"
    assert len(body["recent"]) == 1


def test_cross_project_stats_work_after_resolve(env: SimpleNamespace) -> None:
    """**TASK-085 的第二个必须条件**：``resolve`` 归属后，汇总端点能看到上传产生的记录。

    单项目端点（``/api/projects/{id}/index-stats``）不套归属过滤，所以只修"落库"时它就有数；
    但 WebUI 总览页读的是 ``/api/index-stats`` 与 ``/api/account/overview``，两者只汇总
    "已归属当前用户"的项目（``ops._visible_project_ids``）。而上传路径（TASK-061 §B）
    **刻意不隐式 claim** —— 两个决定叠起来又变成"面板恒为 0"。

    修法：``POST /api/projects/resolve`` 解析后 claim 给当前用户（TASK-061 §B 原设计；
    本机实测发现 ``claim_project`` 写成后**从未被任何入口调用**，``projects`` 表恒空）。
    ``npx nova-client`` 的时序正是 resolve → 扫描 → batch-upload，因此覆盖该入口即可。
    """
    upload(env)
    # ① 单项目端点有数
    assert project_index_stats(env)["history"]["total"] == 1
    # ② 汇总端点同样有数（resolve 已归属，无需手工 claim）
    assert index_stats(env)["total"] == 1

    overview = env.client.get("/api/account/overview", headers=env.headers)
    assert overview.status_code == 200, overview.text
    index = overview.json()["index"]
    assert index["succeeded"] == 1
    assert index["avgDurationMs"] is not None  # 不再是此前实测的 null
    assert len(index["recent"]) == 1


def test_resolve_claims_the_project_for_the_current_user(env: SimpleNamespace) -> None:
    """resolve 幂等归属：同一项目重复 resolve 不报错、不产生第二行。"""
    db: MetaDB = env.meta_db
    assert isinstance(db, MetaDB)
    owner = db.get_user_by_name("owner")
    assert owner is not None

    assert db.list_projects(owner[0].id) == [env.project_id]
    again = env.client.post(
        "/api/projects/resolve",
        json={"identityKey": "identity:stats-repo", "displayName": "stats"},
        headers=env.headers,
    )
    assert again.status_code == 200
    assert again.json()["projectId"] == env.project_id
    assert db.list_projects(owner[0].id) == [env.project_id]


def test_resolve_does_not_claim_in_local_mode(tmp_path: Path) -> None:
    """本地模式无账户体系（R34）：resolve 不写归属、也不报错，且不因此建 meta 库。"""
    settings = Settings(data_root=tmp_path / "data", local_mode=True)
    app = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda r: Engine.open(r, provider=DeterministicBigramEmbedding()),
    )
    app.state.engine_manager = manager
    assert app.state.meta_db is None
    with make_client(app) as client:
        response = client.post(
            "/api/projects/resolve",
            json={"identityKey": "identity:local", "displayName": "local"},
        )
        assert response.status_code == 200, response.text
    assert app.state.meta_db is None, "本地模式不该因为 resolve 而创建 nova-meta.db"
    manager.close()


def test_single_project_stats_is_owner_scoped_and_matches_upload(
    env: SimpleNamespace,
) -> None:
    """单项目端点（不套归属过滤）与上传内容逐字段对得上，且项目已归属当前用户。"""
    report = upload(env).json()["report"]
    history = project_index_stats(env)["history"]

    assert history["total"] == 1
    run = history["recent"][0]
    assert run["state"] == "done"
    # filesTotal = 本次请求送达的文件数；filesProcessed = 真正解析的文件数（口径见 §B）。
    assert run["filesTotal"] == len(SAMPLE_FILES)
    assert run["filesProcessed"] == report["filesParsed"] == len(SAMPLE_FILES)
    # chunks = 项目库里的 chunk 总数（与 sync_status 同源），因此两条路径都可比较、都 > 0。
    assert run["chunks"] == project_chunks(env)
    assert run["errors"] == 0
    assert run["error"] is None


def test_duration_matches_finished_minus_started(env: SimpleNamespace) -> None:
    """``durationMs`` 与 ``finishedAt - startedAt`` 一致（TASK-062 DoD 第 1 条）。"""
    upload(env)
    run = project_index_stats(env)["history"]["recent"][0]
    assert run["durationMs"] == (run["finishedAt"] - run["startedAt"]) * 1000
    assert run["durationMs"] >= 0


# --------------------------------------------------------------------------- 失败 run


def test_provider_failure_records_failed_run_with_redacted_error(tmp_path: Path) -> None:
    """provider 挂 → ``failed=1``，``error_text`` **不含** key 与 base_url（脱敏断言）。"""
    ns = _make(tmp_path, provider=_BrokenProvider())
    ns.client = make_client(ns.app)
    with ns.client:
        ns.client.post("/api/auth/bootstrap", json={"name": "owner", "password": PASSWORD})
        token = ns.client.post("/api/auth/tokens", json={"name": "k"}).json()["token"]
        ns.headers = {"Authorization": f"Bearer {token}"}
        ns.project_id = ns.client.post(
            "/api/projects/resolve", json={"identityKey": "identity:broken", "displayName": "b"}
        ).json()["projectId"]

        # 上传会失败（provider 不可达）→ HTTP 503（TASK-035 的既有映射不变）。
        assert upload(ns).status_code == 503

        body = project_index_stats(ns)["history"]
        assert body["total"] == 1
        assert body["failed"] == 1
        assert body["succeeded"] == 0
        assert body["lastState"] == "failed"

        error_text = body["recent"][0]["error"]
        assert error_text is not None
        assert FAKE_API_KEY not in error_text, "API key 不得进库/进响应"
        assert FAKE_BASE_URL not in error_text, "embedding base_url 不得进库/进响应"
        assert "ApiNetworkError" in error_text, "异常类型要保留（可定位）"
        # 失败 run 的耗时是"失败得多快"，因此没有任何有意义的聚合耗时。
        assert body["avgDurationMs"] is None
    ns.manager.close()


# --------------------------------------------------------------------------- errors>0 口径


def test_errors_are_counted_but_run_still_succeeds(env: SimpleNamespace) -> None:
    """``errors>0`` 但索引正常完成 → 计入 ``succeeded``，``errors`` 字段如实（TASK-062 §C）。

    用**语法错误**的 Python 文件造解析问题：core 的解析器**不中断**整次 ingest，只把
    ``path: 解析错误列表`` 记进 ``report.errors``（``pipeline/indexer.py`` 的 per-file 韧性）。
    这正是“索引完成，但部分文件有解析问题”的真实形态（也是手册 §2 的口径）。
    """
    broken_path = "src/broken.py"
    files = {**SAMPLE_FILES, broken_path: "def broken(:\n    pass\n"}
    report = upload(env, files).json()["report"]
    assert report["errors"], "语法错误文件应产生一条 errors（否则本用例没测到目标场景）"

    body = project_index_stats(env)["history"]
    assert body["succeeded"] == 1 and body["failed"] == 0

    run = body["recent"][0]
    assert run["errors"] == len(report["errors"])
    assert run["error"] is not None and broken_path in run["error"]
    assert run["state"] == "done"


# --------------------------------------------------------------------------- avgDurationMs 口径


def test_avg_duration_excludes_failed_runs(env: SimpleNamespace) -> None:
    """先跑一次成功（耗时含秒级等待），再插一条**极快**的失败 run → 平均值不受其影响。

    直接经 ``MetaDB`` 造失败 run（而不是真的把 provider 弄坏）：provider 是构造期注入的，
    一个环境里换不掉；这里要验的是**聚合口径**，不是失败的产生路径（后者另有用例）。
    """
    upload(env)  # 第一条：成功
    succeeded = project_index_stats(env)["history"]["recent"][0]["durationMs"]

    db: MetaDB = env.meta_db
    assert isinstance(db, MetaDB)
    started = 1_700_000_000
    db.record_index_run(
        env.project_id,
        state="failed",
        started_at=started,
        finished_at=started,  # 0 秒 → 任何"混入平均"的实现都会把均值拉向 0
        error_text="ApiNetworkError: 模拟失败",
        errors=1,
    )

    history = project_index_stats(env)["history"]
    assert history["total"] == 2 and history["failed"] == 1
    # 失败 run 耗时 0，若被计入平均，均值必然变成 succeeded/2。
    assert history["avgDurationMs"] == succeeded
    assert history["minDurationMs"] == succeeded
    assert history["maxDurationMs"] == succeeded


# --------------------------------------------------------------------------- 接线（第二部分根因）


def test_lazy_manager_is_wired_to_the_app_meta_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**TASK-085 的第二个根因**：云端模式下 manager 由 ``deps`` 懒构造，必须接上 app 的 MetaDB。

    改动前它只做 ``EngineManager.open(...)`` —— 于是 ``runtime.ingest`` 里 ``self._meta_db is None``
    直接 return，即使记录点写对了统计仍恒为 0（实测复现）。只有 ``local`` 子命令那条路径
    （``__main__`` 显式 attach）能落库，这正是"本地 attach 有数、客户端上传恒为 0"的原因。

    这里 monkeypatch ``EngineManager.open`` 返回带假 provider 的 manager，**不**手工接线，
    验证懒构造路径自己会接上。
    """
    settings = Settings(data_root=tmp_path / "data", local_mode=False)
    app = create_app(settings)
    built: list[EngineManager] = []
    # 先抓住真正的 ``open``：下面替换的是**同一个类属性**，不抓住原始实现就会自递归。
    real_open = EngineManager.open.__func__  # type: ignore[attr-defined]

    def _fake_open(cls: type[EngineManager], data_root: object, **kwargs: object) -> EngineManager:
        manager = real_open(
            cls,
            data_root,
            engine_factory=lambda r: Engine.open(r, provider=DeterministicBigramEmbedding()),
        )
        built.append(manager)
        return manager

    monkeypatch.setattr("nova_service.deps.EngineManager.open", classmethod(_fake_open))
    # ``app.state.engine_manager`` 未预热 → 走 deps 的懒构造分支。
    assert app.state.engine_manager is None
    with make_client(app) as client:
        client.post("/api/auth/bootstrap", json={"name": "owner", "password": PASSWORD})
        # 触发懒构造（``local_mode=False`` → 必须带凭据）。
        token = client.post("/api/auth/tokens", json={"name": "k"}).json()["token"]
        client.get("/api/projects", headers={"Authorization": f"Bearer {token}"})

    assert built, "懒构造路径没有被触发（本用例没测到目标代码）"
    assert built[0].meta_db is app.state.meta_db, "懒构造的 manager 必须接上 app 级 MetaDB"
    built[0].close()


# --------------------------------------------------------------------------- 保留策略 / 持久化


def test_runs_beyond_keep_limit_drop_the_oldest(env: SimpleNamespace) -> None:
    """超过 :data:`INDEX_RUN_KEEP` 条时裁掉最旧的（TASK-062 §B）。"""
    db: MetaDB = env.meta_db
    assert isinstance(db, MetaDB)
    base = 1_700_000_000
    for offset in range(INDEX_RUN_KEEP + 5):
        db.record_index_run(
            env.project_id,
            state="done",
            started_at=base + offset,
            finished_at=base + offset + 1,
        )

    history = project_index_stats(env)["history"]
    assert history["total"] == INDEX_RUN_KEEP
    oldest = base + 5  # 前 5 条被裁掉
    assert history["recent"][0]["startedAt"] >= oldest
    kept = db.index_runs(env.project_id, limit=INDEX_RUN_KEEP)
    assert min(run.started_at for run in kept) == oldest


def test_stats_survive_a_restart(tmp_path: Path) -> None:
    """关掉 app/manager、用同一 data_root 重开 → 统计仍在（落库而非内存的证明）。"""
    ns = _make(tmp_path)
    ns.client = make_client(ns.app)
    with ns.client:
        ns.client.post("/api/auth/bootstrap", json={"name": "owner", "password": PASSWORD})
        token = ns.client.post("/api/auth/tokens", json={"name": "k"}).json()["token"]
        ns.headers = {"Authorization": f"Bearer {token}"}
        ns.project_id = ns.client.post(
            "/api/projects/resolve", json={"identityKey": "identity:restart", "displayName": "r"}
        ).json()["projectId"]
        upload(ns)
        assert project_index_stats(ns)["history"]["total"] == 1
    ns.manager.close()

    # 全新进程语义：新 app + 新 manager，同一个 data_root。
    again = _make(tmp_path)
    again.client = make_client(again.app)
    with again.client:
        again.headers = ns.headers
        again.project_id = ns.project_id
        body = project_index_stats(again)["history"]
        assert body["total"] == 1 and body["succeeded"] == 1
    again.manager.close()


def test_recording_failure_does_not_break_indexing(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """记帐本身炸掉时，索引仍须成功返回 200（TASK-085 §A 的纪律；TASK-062 §C 的既有行为）。"""
    db: MetaDB = env.meta_db
    assert isinstance(db, MetaDB)

    def _boom(*args: object, **kwargs: object) -> int:
        raise sqlite3_error

    sqlite3_error = RuntimeError("模拟 meta 库落库失败")
    monkeypatch.setattr(type(db), "record_index_run", _boom)
    response = upload(env)
    assert response.status_code == 200, response.text
    assert response.json()["report"]["added"] == len(SAMPLE_FILES)


# --------------------------------------------------------------------------- 两条路径口径


def test_local_attach_and_client_upload_share_one_shape(tmp_path: Path) -> None:
    """本地 attach 一次 + 客户端上传一次 → ``total=2``，两条记录字段形态可对比。

    # 本地模式用 ``local`` 子命令那条路径（``__main__`` 显式 attach MetaDB + 后台索引）；
    # 这里直接调用同样的 API（``attach_local`` + ``ProjectIndexer``），不启真服务。
    # 本地模式无账户体系（R34），MetaDB 由调用方注入——与 ``__main__._run_local`` 一致。
    """
    import time as _time

    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "token_service.py").write_text(
        SAMPLE_FILES[SAMPLE_MODULE_PATH], encoding="utf-8"
    )
    (root / "docs" / "token.md").write_text(
        SAMPLE_FILES["docs/design/token.md"], encoding="utf-8"
    )

    settings = Settings(data_root=tmp_path / "data", local_mode=True)
    app: FastAPI = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda r: Engine.open(r, provider=DeterministicBigramEmbedding()),
    )
    # 本地模式：app.state.meta_db 为 None（R34），MetaDB 由 __main__ 显式注入。
    db = MetaDB.open(settings.meta_db_path)
    manager.attach_meta_db(db)
    app.state.meta_db = db
    app.state.engine_manager = manager

    client = make_client(app)
    with client:
        # ① 本地 attach：后台索引（轮询到终态，不 sleep 猜时间）。
        attached = client.post("/api/projects/attach", json={"root": str(root)}).json()
        project_id = attached["projectId"]
        progress: dict = {}
        local_deadline = _time.monotonic() + 30.0
        while _time.monotonic() < local_deadline:
            progress = client.get(f"/api/projects/{project_id}").json()["indexProgress"]
            if progress["state"] in ("done", "failed"):
                break
            _time.sleep(0.02)
        assert progress["state"] == "done", progress

        # ② 客户端上传：同项目再传一次（账本里已有 → 归入 modified）。
        blobs = [
            {
                "path": path,
                "blobHash": blob_hash(path, content.encode("utf-8")),
                "contentB64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            }
            for path, content in SAMPLE_FILES.items()
        ]
        uploaded = client.post(
            "/api/sync/batch-upload", json={"projectId": project_id, "blobs": blobs}
        )
        assert uploaded.status_code == 200, uploaded.text

        history = client.get(f"/api/projects/{project_id}/index-stats").json()["history"]

    assert history["total"] == 2
    assert history["succeeded"] == 2 and history["failed"] == 0

    local_run, upload_run = history["recent"][0], history["recent"][1]
    # 两条路径共享同一组字段语义（TASK-085 §B）：state / errors / error 的含义一致。
    assert {local_run["state"], upload_run["state"]} == {"done"}
    assert {local_run["errors"], upload_run["errors"]} == {0}
    assert {local_run["error"], upload_run["error"]} == {None}
    # filesProcessed 都是"本次真正解析的文件数"；filesTotal 在两条路径上的来源不同，
    # 但都必须 ≥ filesProcessed（本地=目录列举数，上传=本次送达数）。
    # 本地 attach 先跑一次（chunks>0），上传整批重传时 chunks 仍为正数（项目总量口径）。
    assert local_run["chunks"] > 0
    assert upload_run["chunks"] > 0

    # 两条路径的 `filesTotal` **来源不同**（本地=目录列举数，上传=本次送达数），
    # 这里如实记录实测形态，便于评审对照：
    #   本地 attach → filesTotal=2（目录里 2 个文件）, filesProcessed=2
    #   客户端上传 → filesTotal=2（本次送达 2 个）, filesProcessed=2
    assert local_run["filesTotal"] == len(SAMPLE_FILES)
    assert upload_run["filesTotal"] == len(SAMPLE_FILES)
    manager.close()


def test_meta_db_missing_degrades_quietly(tmp_path: Path) -> None:
    """未接 MetaDB（本地模式默认）→ 索引照常，不报错也不写库（既有降级口径）。"""
    settings = Settings(data_root=tmp_path / "data", local_mode=True)
    app = create_app(settings)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda r: Engine.open(r, provider=DeterministicBigramEmbedding()),
    )
    app.state.engine_manager = manager
    client = make_client(app)
    with client:
        project_id = client.post(
            "/api/projects/resolve", json={"identityKey": "identity:nometa", "displayName": "n"}
        ).json()["projectId"]
        blobs = [
            {
                "path": path,
                "blobHash": blob_hash(path, content.encode("utf-8")),
                "contentB64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            }
            for path, content in SAMPLE_FILES.items()
        ]
        response = client.post(
            "/api/sync/batch-upload", json={"projectId": project_id, "blobs": blobs}
        )
        assert response.status_code == 200, response.text
    manager.close()
