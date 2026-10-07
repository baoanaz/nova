"""TASK-094 验收：存储配额（§A 占用可见 / §B tool 告警 / §C trace id，含 migration）。

DoD 逐条对应（本文件覆盖后端面；前端在 ``web/src/pages/*.test.tsx``）：

| DoD 条目 | 用例 |
|---|---|
| ``limit=0``（不限）→ 恒 ``ok`` + 无告警节 | ``test_zero_limits_are_always_ok_and_silent`` |
| ≥ 80% → ``warning`` + tool 返回出现告警节 | ``test_warning_appears_in_mcp_tool_content`` |
| ≥ 100% → ``exceeded``，文案不同且仍放行 | ``test_exceeded_still_serves_...`` |
| 配额判定抛异常时检索仍成功（旁路纪律） | ``test_quota_failure_never_breaks_*`` |
| 多用户隔离：A 的用量不计入 B 的额度 | ``test_quota_is_per_user`` |
| ``request_id`` 落库 == 响应头 ``X-Request-Id`` | ``test_audit_request_id_matches_header`` |
| migration：旧库启动新代码 → 升级 + 旧数据不丢 | ``test_migration_upgrades_legacy_db`` |
| §A ``GET /api/projects`` 每项目带 ``diskBytes`` | ``test_project_list_reports_disk_bytes`` |

纪律：临时 ``tmp_path`` + 确定性假 provider（``tests.conftest``），不联网、不加载模型；
**不 sleep 猜时间**；用量由真实写入的文件撑起来（不 mock 一个假数字）。
"""

from __future__ import annotations

import base64
import dataclasses
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nova_core.engine import Engine
from nova_core.hashing import blob_hash
from nova_service import quota as quota_module
from nova_service.app import create_app
from nova_service.config import Settings
from nova_service.metadb import MetaDB
from nova_service.quota import (
    STATUS_EXCEEDED,
    STATUS_OK,
    STATUS_WARNING,
    STORAGE_WARNING_HEADING,
    check_quota,
    format_bytes,
    status_from_sizes,
    warning_for,
)
from nova_service.runtime import EngineManager

from tests.conftest import (
    SAMPLE_FILES,
    SAMPLE_MODULE_PATH,
    TARGET_SYMBOL,
    DeterministicBigramEmbedding,
    upload_files,
)
from tests.test_mcp_endpoint import (
    ASK_TOOL,
    BASE_URL,
    SEARCH_TOOL,
    _call_tool,
    _connected,
    _text,
)

#: 一兆字节（做额度时的单位换算，避免散落的魔数）。
MB = 1024 * 1024


# --------------------------------------------------------------------------- 夹具


def _settings_for(data_root: Path, **overrides: object) -> Settings:
    """夹具配置：本地模式、关掉懒重扫、**默认不限配额**（用例按需覆盖）。"""
    defaults: dict[str, object] = {
        "data_root": data_root,
        "local_mode": True,
        "local_rescan_interval_s": 0.0,
        "storage_limit_per_project_bytes": 0,
        "storage_limit_per_user_bytes": 0,
    }
    return Settings(**{**defaults, **overrides})  # type: ignore[arg-type]


def _build_env_raw(
    tmp_path: Path,
) -> tuple[FastAPI, EngineManager, TestClient]:
    """最小的"应用 + manager + client"三件套（不要项目、不要 attach；给隔离类用例自建数据）。"""
    settings = _settings_for(tmp_path / "data")
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app = create_app(settings)
    app.state.meta_db = MetaDB.open(settings.meta_db_path)
    app.state.engine_manager = manager
    client = TestClient(app, base_url=BASE_URL, raise_server_exceptions=False)
    return app, manager, client


class Env:
    """一套可运行环境：应用 + manager + 项目 + 当前生效的配额。

    配额变更只有一个入口：:meth:`relaunch`。

    为什么封装成类：配额必须在**建 app 时**给定（``create_app`` → ``build_mcp`` 会把当时的
    ``Settings`` 包进工具闭包，见 :meth:`relaunch`），因此"改配额"= 重建一套 app 与 client。
    把这件事收敛到一个方法里，测试就不会各写一种改法、也不会在只改了 ``app.state.settings``
    的错觉下单测 MCP 面。
    """

    def __init__(self, app: FastAPI, manager: EngineManager, tmp_path: Path,
                 project_id: str, project_root: str, settings: Settings) -> None:
        self.app = app
        self.manager = manager
        self.tmp_path = tmp_path
        self.project_id = project_id
        self.project_root = project_root
        self.settings = settings
        self.client = TestClient(app, base_url=BASE_URL, raise_server_exceptions=False)
        self._clients: list[TestClient] = []

    def __enter__(self) -> Env:
        self.client.__enter__()
        self._clients.append(self.client)
        return self

    def __exit__(self, *exc: object) -> None:
        for client in reversed(self._clients):
            client.__exit__(None, None, None)
        self._clients.clear()
        self.manager.close()

    def relaunch(self, **overrides: object) -> None:
        """换配额并**重建应用**（= "改配置重启服务"），保留项目/索引/审计/manager。

        为什么不能只改 ``app.state.settings``：REST 面每次请求现读 ``app.state.settings``，
        但 **MCP 面**吃的是 ``build_mcp`` 构造时包进闭包的那一份快照（TASK-088 刻意如此：
        "MCP 会话可能活很久"）。只改 state 会让两面用不同的配额，测试就会在错觉下验证错东西。
        """
        self.settings = dataclasses.replace(self.settings, **overrides)
        self.app = create_app(self.settings)
        self.app.state.engine_manager = self.manager
        self.app.state.meta_db = self._meta_db()  # 同一张库（不重开，避免多份 WAL 句柄）
        self.client = TestClient(self.app, base_url=BASE_URL, raise_server_exceptions=False)
        self.client.__enter__()
        self._clients.append(self.client)

    def _meta_db(self) -> MetaDB:
        """当前已知的元数据库（优先取最近一个 app 上的那份）。"""
        existing = getattr(self.app.state, "meta_db", None)
        if isinstance(existing, MetaDB):
            return existing
        return MetaDB.open(self.settings.meta_db_path)

    def usage_bytes(self, project_id: str | None = None) -> int:
        """项目索引数据的真实占用（与被测代码同一来源：目录求和）。"""
        return quota_module.project_usage_bytes(self.manager, project_id or self.project_id)

    def limit_for_ratio(self, ratio: float, *, project_id: str | None = None) -> int:
        """造一个"该用量恰好占上限 ``ratio``"的上限（``ratio>=0.8`` 落在 ``warning``）。"""
        return max(1, int(self.usage_bytes(project_id) / ratio))


def _build_env(tmp_path: Path, *, tag: str = "env", **overrides: object) -> Env:
    """建一套环境：**attach 过真实目录** + 已索引 2 个文件的项目。

    为什么要 attach：MCP 工具的参数是 ``project_root``（绝对路径），要按 D-29 身份反解回
    projectId，因此必须有真实存在、可反解的仓库目录。
    """
    settings = _settings_for(tmp_path / f"{tag}-data", **overrides)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app = create_app(settings)
    # 本地模式默认不建库（R34），但审计/历史/配额归属都要它：与 ``nova-service local`` 的实际
    # 启动路径一致（__main__ 里同样显式注入）。
    app.state.meta_db = MetaDB.open(settings.meta_db_path)
    app.state.engine_manager = manager
    repo = tmp_path / f"{tag}-repo"
    repo.mkdir(parents=True, exist_ok=True)
    attached = manager.attach_local(repo, index=False)
    upload_files(manager, attached.project_id, SAMPLE_FILES)
    return Env(
        app=app,
        manager=manager,
        tmp_path=tmp_path,
        project_id=attached.project_id,
        project_root=str(attached.root),
        settings=settings,
    )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    """默认环境：**配额不限**（两个上限为 0）+ 一个已索引项目。"""
    with _build_env(tmp_path) as built:
        yield built


def _usage_bytes(ns: Env, project_id: str | None = None) -> int:
    """项目索引数据的真实占用（与被测代码同一来源：目录求和）。"""
    return ns.usage_bytes(project_id)


def _search_via_mcp(ns: Env, query: str = TARGET_SYMBOL) -> str:
    """经 MCP 面跑一次 ``search_context``，返回工具文本。"""
    session_id = _connected(ns.client)
    result = _call_tool(
        ns.client,
        session_id,
        SEARCH_TOOL,
        {"query": query, "project_root": ns.project_root},
    )
    assert result.get("isError") is not True, _text(result)
    return _text(result)


def _ask_via_mcp(ns: Env, question: str = TARGET_SYMBOL) -> str:
    session_id = _connected(ns.client)
    result = _call_tool(
        ns.client,
        session_id,
        ASK_TOOL,
        {"question": question, "project_root": ns.project_root},
    )
    assert result.get("isError") is not True, _text(result)
    return _text(result)


# --------------------------------------------------------------------------- 纯判定


def test_zero_limits_are_always_ok_and_silent(env: Env) -> None:
    """DoD：两个上限都为 ``0`` → 恒 ``ok``，且**不出现**告警（不打扰本地开发）。

    同时钉住"不限时 ``ratio`` 是 ``None`` 而不是 0"（"没有上限"与"用了 0%"是两件事）。
    """
    status = check_quota(env.manager, env.settings, project_id=env.project_id)

    assert status.status == STATUS_OK
    assert status.should_warn is False
    assert status.warning_markdown() is None
    assert status.user.unlimited and status.project.unlimited
    assert status.user.ratio is None and status.project.ratio is None
    assert warning_for(env.manager, env.settings, project_id=env.project_id) is None


def test_one_sided_limit_keeps_other_side_unlimited(env: Env) -> None:
    """只设一侧上限 → 另一侧如实保持"不限"（不把「没设上限」伪装成用了 0%）。"""
    env.relaunch(storage_limit_per_project_bytes=MB)
    status = check_quota(env.manager, env.settings, project_id=env.project_id)

    assert status.project.unlimited is False
    assert status.user.unlimited is True
    assert status.user.ratio is None
    assert status.status == STATUS_OK, "单项目未到 80% 就不该告警"


@pytest.mark.parametrize(
    ("used", "limit", "expected"),
    [
        (0, 100, STATUS_OK),
        (79, 100, STATUS_OK),
        (80, 100, STATUS_WARNING),  # 恰好 80% = 阈值（>= 而非 >）
        (99, 100, STATUS_WARNING),
        (100, 100, STATUS_EXCEEDED),  # 恰好撞线即 exceeded
        (101, 100, STATUS_EXCEEDED),
        (1000, 0, STATUS_OK),  # 不限
    ],
)
def test_three_state_boundaries(used: int, limit: int, expected: str) -> None:
    """三态边界（默认 ``warn_ratio=0.8``）：``ok`` / ``warning``(≥80%) / ``exceeded``(≥100%)。"""
    assert quota_module._judge(used, limit, 0.8).status == expected


def test_warning_and_exceeded_markdown_differ(env: Env) -> None:
    """DoD：``warning`` 与 ``exceeded`` 的**文案不同**（exceeded 必须说清上传会被拒）。

    TASK-110 改口径（用户 2026-09-15 拍板）：超限时**检索照常、上传被拒**，因此
    exceeded 分支必须写明"已有项目的使用不受影响"与"新的索引上传会被拒绝"——
    只说前半句会让 Agent 转述成"一切正常"，而用户下一批上传会直接拿 413。
    """
    used = _usage_bytes(env)
    assert used > 0, "夹具项目应当有真实的索引占用（否则下面的额度没有意义）"

    env.relaunch(storage_limit_per_project_bytes=env.limit_for_ratio(0.8))
    warning = warning_for(env.manager, env.settings, project_id=env.project_id)
    assert warning is not None and warning.startswith(STORAGE_WARNING_HEADING)
    assert "上传会被拒绝" not in warning
    assert "请提醒用户" in warning

    env.relaunch(storage_limit_per_project_bytes=max(1, used // 2))
    exceeded = warning_for(env.manager, env.settings, project_id=env.project_id)
    assert exceeded is not None and exceeded.startswith(STORAGE_WARNING_HEADING)
    assert "已超出上限" in exceeded
    assert "上传会被拒绝" in exceeded, "超限必须写明上传会被拒（TASK-110 硬拒新索引）"
    assert "检索" in exceeded and "不受影响" in exceeded
    assert "删除" in exceeded and "源码文件不受影响" in exceeded
    assert exceeded != warning


def test_warning_markdown_states_real_numbers(env: Env) -> None:
    """文案里的数字是**真实测量值**（用量与上限都能对回目录求和），不是编的。"""
    used = _usage_bytes(env)
    limit = env.limit_for_ratio(0.8)
    env.relaunch(storage_limit_per_project_bytes=limit)

    markdown = warning_for(env.manager, env.settings, project_id=env.project_id)
    assert markdown is not None
    assert format_bytes(used) in markdown
    assert format_bytes(limit) in markdown
    assert "80%" in markdown
    assert markdown.count("80%") == 1, "百分比只出现一次（不要重复一遍）"


def test_user_limit_counts_all_owned_projects(env: Env) -> None:
    """用户额度 = **其全部项目**之和（不是只算当前项目）。"""
    second = env.manager.resolve_project("identity:quota-2", "quota-repo-2").project_id
    upload_files(env.manager, second, SAMPLE_FILES)
    total = _usage_bytes(env) + _usage_bytes(env, second)

    # 上限略低于"两个项目之和"但高于"单个项目" → 只有在正确求和时才会 exceeded。
    env.relaunch(storage_limit_per_user_bytes=max(1, total - 1))
    status = check_quota(
        env.manager,
        env.settings,
        project_id=env.project_id,
        project_ids=[env.project_id, second],
    )

    assert status.user.used_bytes == total
    assert status.user.status == STATUS_EXCEEDED
    assert status.project.status == STATUS_OK, "单项目维度不该被用户总额拖下水"


def test_status_from_sizes_matches_directory_walk(env: Env) -> None:
    """``status_from_sizes``（概览页用，避免重复遍历）与直接求和**同一结果**。"""
    sizes = {env.project_id: _usage_bytes(env)}
    env.relaunch(storage_limit_per_project_bytes=env.limit_for_ratio(0.8))

    from_sizes = status_from_sizes(sizes, env.settings, project_id=env.project_id)
    from_disk = check_quota(env.manager, env.settings, project_id=env.project_id)

    assert from_sizes.to_json() == from_disk.to_json()


def test_quota_is_per_user(tmp_path: Path) -> None:
    """DoD：多用户隔离——A 的用量**不计入** B 的额度。

    自建一个带元数据库的运行环境：两个账户各自 claim 一个项目，A 的项目撑起真实占用。
    只按"当前用户的项目"求和，因此 A 超限时 B 仍为 ``ok``（B 没有项目 → 用量 0）。
    """
    app, manager, client = _build_env_raw(tmp_path)
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    with client:
        db = app.state.meta_db
        alice = db.create_user("alice", "hash")
        bob = db.create_user("bob", "hash")
        project_a = manager.resolve_project("identity:per-user-a", "a").project_id
        upload_files(manager, project_a, SAMPLE_FILES)
        db.claim_project(alice.id, project_a, "a")

        used_a = quota_module.project_usage_bytes(manager, project_a)
        assert used_a > 0

        settings = dataclasses.replace(
            app.state.settings, storage_limit_per_user_bytes=max(1, used_a // 2)
        )

        alice_ids = quota_module.visible_project_ids(manager, db, alice.id)
        bob_ids = quota_module.visible_project_ids(manager, db, bob.id)
        assert alice_ids == [project_a]
        assert bob_ids == [], "bob 不该看到 alice 的项目"

        alice_status = check_quota(
            manager, settings, project_id=project_a, project_ids=alice_ids
        )
        bob_status = check_quota(
            manager, settings, project_id=project_a, project_ids=bob_ids
        )

        assert alice_status.user.used_bytes == used_a
        assert alice_status.status == STATUS_EXCEEDED
        assert bob_status.user.used_bytes == 0, "bob 的额度里不得出现 alice 的用量"
        assert bob_status.status == STATUS_OK
    manager.close()


# --------------------------------------------------------------------------- §A 占用可见


def test_project_list_reports_disk_bytes(env: Env) -> None:
    """§A：``GET /api/projects`` 的每个项目带 ``diskBytes``，且等于真实目录求和。"""
    listed = env.client.get("/api/projects").json()
    assert len(listed) == 1
    item = listed[0]
    assert item["projectId"] == env.project_id
    assert item["diskBytes"] == _usage_bytes(env)
    assert item["diskBytes"] > 0

    # 既有字段不动（TASK-034 冻结的 attachedRoot / indexProgress 仍在）。
    assert item["attachedRoot"] == env.project_root
    assert "indexProgress" in item

    detail = env.client.get(f"/api/projects/{env.project_id}").json()
    assert detail["diskBytes"] == item["diskBytes"], "列表与详情必须是同一个口径"


def test_overview_reports_storage_status(env: Env) -> None:
    """§B4：``/api/account/overview`` 给出用户维度的用量/状态 + 每项目 ``diskBytes``。"""
    used = _usage_bytes(env)
    env.relaunch(storage_limit_per_user_bytes=used * 10)

    payload = env.client.get("/api/account/overview").json()
    storage = payload["storage"]

    assert storage["status"] == STATUS_OK
    assert storage["user"]["usedBytes"] == used
    assert storage["user"]["limitBytes"] == used * 10
    # 概览不重复遍历目录：项目项里的 diskBytes 就是判定用的那份数据。
    assert payload["projects"][0]["diskBytes"] == used

    env.relaunch(storage_limit_per_user_bytes=max(1, used // 2))
    warned = env.client.get("/api/account/overview").json()["storage"]
    assert warned["status"] == STATUS_EXCEEDED
    assert warned["user"]["ratio"] > 1


# --------------------------------------------------------------------------- §B MCP 面


def test_no_warning_section_when_quota_disabled(env: Env) -> None:
    """DoD：不限时 MCP 工具返回里**没有** ``### Storage Warning``（不打扰）。"""
    assert STORAGE_WARNING_HEADING not in _search_via_mcp(env)


def test_warning_appears_in_mcp_tool_content(env: Env) -> None:
    """DoD：≥80% → **tool 返回内容**里出现告警节（用户明确要求，不只是日志）。"""
    used = _usage_bytes(env)
    env.relaunch(storage_limit_per_user_bytes=env.limit_for_ratio(0.8))

    text = _search_via_mcp(env)

    assert STORAGE_WARNING_HEADING in text
    assert "请提醒用户" in text, "文案要能被 Agent 直接转述给用户"
    assert format_bytes(used) in text
    # 告警是**追加**在原有内容之后：正文仍逐字来自 render_markdown（D-21 不动）。
    assert text.startswith("[nova] answerable=")
    assert "## Relevant Context" in text
    assert text.index("### Meta") < text.index(STORAGE_WARNING_HEADING)


def test_exceeded_still_serves_and_warns_about_uploads(env: Env) -> None:
    """DoD：≥100% → ``exceeded``（文案不同）且**检索仍放行**；告警明确说上传会被拒。

    TASK-110 改口径：**检索**不阻断（它不会让占用变大，拒绝它毫无意义）——这是本条的可观察
    证据；而**上传**硬拒由 :func:`test_exceeded_rejects_upload` 守。
    """
    used = _usage_bytes(env)
    env.relaunch(storage_limit_per_user_bytes=max(1, used // 2))

    text = _search_via_mcp(env)

    assert STORAGE_WARNING_HEADING in text
    assert "已超出上限" in text
    assert "上传会被拒绝" in text
    # 检索结果**照常**返回（这是"检索不受影响"的可观察证据）。
    assert f"{SAMPLE_MODULE_PATH}:" in text
    assert "## Relevant Context" in text


def test_exceeded_rejects_upload(env: Env) -> None:
    """DoD（TASK-110 用户拍板）：超限时**拒绝**新的索引上传（413 ``quota_exceeded``）。

    这是与 TASK-094 的"告警不阻断"的**唯一行为差异**：写路径硬拒，读路径不变。
    """
    env.relaunch(storage_limit_per_user_bytes=1, storage_limit_per_project_bytes=1)

    content = b"x = 1\n"
    response = env.client.post(
        "/api/sync/batch-upload",
        json={
            "projectId": env.project_id,
            "blobs": [
                {
                    "path": "src/extra.py",
                    "blobHash": blob_hash("src/extra.py", content),
                    "contentB64": base64.b64encode(content).decode(),
                }
            ],
        },
    )
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "quota_exceeded"
    # 拒绝必须发生在**写入之前**：账本里不该出现这个文件（否则会把索引搞成半成品）。
    _blobs, state = env.manager.project_paths(env.project_id)
    assert "src/extra.py" not in state.files


def test_ask_project_tool_also_carries_warning(env: Env) -> None:
    """``ask_project`` 的返回同样带告警（两个工具都能提醒，不只 search）。"""
    _usage_bytes(env)
    env.relaunch(storage_limit_per_user_bytes=env.limit_for_ratio(0.8))

    assert STORAGE_WARNING_HEADING in _ask_via_mcp(env)


# --------------------------------------------------------------------------- §B REST 面


def test_rest_search_markdown_carries_warning(env: Env) -> None:
    """REST 面（``POST /api/query/search``）的 ``markdown`` 同样带告警（两面一致）。"""
    _usage_bytes(env)
    env.relaunch(storage_limit_per_project_bytes=env.limit_for_ratio(0.8))

    body = env.client.post(
        "/api/query/search", json={"projectId": env.project_id, "query": TARGET_SYMBOL}
    ).json()

    assert STORAGE_WARNING_HEADING in body["markdown"]
    assert body["markdown"].startswith("## Relevant Context"), "正文仍逐字来自 render_markdown"


def test_rest_ask_carries_warning_in_degraded_branch(env: Env) -> None:
    """``ask`` 的**降级分支**也必须带告警——恰好走降级时不该看不到提醒。"""
    _usage_bytes(env)
    env.relaunch(storage_limit_per_project_bytes=env.limit_for_ratio(0.8))

    body = env.client.post(
        "/api/query/ask", json={"projectId": env.project_id, "question": TARGET_SYMBOL}
    ).json()

    assert body["status"] == "degraded"  # 夹具未配置 ANSWER_*
    assert STORAGE_WARNING_HEADING in body["answer"]


def test_rest_search_without_quota_has_no_warning(env: Env) -> None:
    """不限时 REST 也不出现告警节（不打扰；两面口径一致）。"""
    body = env.client.post(
        "/api/query/search", json={"projectId": env.project_id, "query": TARGET_SYMBOL}
    ).json()
    assert STORAGE_WARNING_HEADING not in body["markdown"]


# --------------------------------------------------------------------------- 旁路纪律


@pytest.mark.parametrize("failure", [OSError("目录读不到（模拟）"), RuntimeError("模拟崩溃")])
def test_quota_failure_never_breaks_mcp_search(
    env: Env, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """DoD：**配额判定抛异常时检索仍成功**（MCP 面；与 TASK-084 审计同一纪律）。"""
    _usage_bytes(env)
    env.relaunch(storage_limit_per_project_bytes=env.limit_for_ratio(0.8))

    def boom(*_args: object, **_kwargs: object) -> int:
        raise failure

    monkeypatch.setattr(quota_module, "project_usage_bytes", boom)

    text = _search_via_mcp(env)

    assert f"{SAMPLE_MODULE_PATH}:" in text, "检索结果照常返回"
    assert "## Relevant Context" in text
    assert STORAGE_WARNING_HEADING not in text, "判定失败时不告警（不编造状态）"


def test_quota_failure_never_breaks_rest_search(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同上，REST 面：判定炸了也照常 200 + 完整 markdown。"""
    _usage_bytes(env)
    env.relaunch(storage_limit_per_project_bytes=env.limit_for_ratio(0.8))

    def boom(*_args: object, **_kwargs: object) -> int:
        raise OSError("模拟")

    monkeypatch.setattr(quota_module, "project_usage_bytes", boom)

    response = env.client.post(
        "/api/query/search", json={"projectId": env.project_id, "query": TARGET_SYMBOL}
    )
    assert response.status_code == 200, response.text
    assert response.json()["markdown"].startswith("## Relevant Context")


def test_quota_failure_is_logged(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """判定失败要留痕（WARN），否则"告警为什么没出现"会变成谜。"""
    _usage_bytes(env)
    env.relaunch(storage_limit_per_project_bytes=env.limit_for_ratio(0.8))

    def boom(*_args: object, **_kwargs: object) -> int:
        raise OSError("模拟")

    monkeypatch.setattr(quota_module, "project_usage_bytes", boom)
    with caplog.at_level("WARNING", logger="nova_service.quota"):
        warning_for(env.manager, env.settings, project_id=env.project_id)

    assert any("配额判定失败" in record.message for record in caplog.records)


# --------------------------------------------------------------------------- §C trace id


def _audit_rows(db: MetaDB) -> list[sqlite3.Row]:
    return list(db._connect().execute("SELECT * FROM query_audit ORDER BY id"))


def test_audit_request_id_matches_response_header(env: Env) -> None:
    """DoD（本卡与 TASK-090 的接缝）：``query_audit.request_id`` == 响应头 ``X-Request-Id``。

    必须**实测**：客户端自带 ``X-Request-Id`` → 响应回显同一个 → 库里那条审计也带它。
    这样用户在历史页看到的 id 才能在服务端日志里查到完整链路。
    """
    trace = "trace-quota-094-abc123"
    response = env.client.post(
        "/api/query/search",
        json={"projectId": env.project_id, "query": TARGET_SYMBOL},
        headers={"X-Request-Id": trace},
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-Id"] == trace

    rows = _audit_rows(env.app.state.meta_db)
    assert len(rows) == 1
    assert rows[0]["request_id"] == trace
    assert rows[0]["mode"] == "fast"


def test_audit_request_id_generated_when_absent(env: Env) -> None:
    """不带 ``X-Request-Id`` 时，服务端生成的 id 同样落库（且与响应头一致）。"""
    response = env.client.post(
        "/api/query/search", json={"projectId": env.project_id, "query": TARGET_SYMBOL}
    )
    generated = response.headers["X-Request-Id"]
    assert generated

    rows = _audit_rows(env.app.state.meta_db)
    assert rows[-1]["request_id"] == generated


def test_request_id_returned_by_usage_endpoints(env: Env) -> None:
    """DoD：``/api/usage/*`` 的 ``recent[]`` 带 ``requestId``，且能拿它查到日志（TASK-090）。"""
    trace = "trace-usage-094"
    env.client.post(
        "/api/query/search",
        json={"projectId": env.project_id, "query": TARGET_SYMBOL},
        headers={"X-Request-Id": trace},
    )

    summary = env.client.get("/api/usage/summary").json()
    per_project = env.client.get(f"/api/usage/projects/{env.project_id}").json()
    overview = env.client.get("/api/account/overview").json()

    assert summary["recent"][0]["requestId"] == trace
    assert per_project["recent"][0]["requestId"] == trace
    assert overview["usage"]["recent"][0]["requestId"] == trace

    # 历史页要能靠它去查服务端日志（TASK-090 的接缝：同一 id 真的查得到）。
    lookup = env.client.get(f"/api/request-log/{trace}")
    assert lookup.status_code == 200, lookup.text
    assert lookup.json()["requestId"] == trace


def test_failed_query_also_records_request_id(env: Env) -> None:
    """失败路径（业务失败）同样落 ``request_id``：报错时的 id 才是最需要能查的。

    用"项目不存在"制造业务失败（走审计的 ``record_query_error`` 分支）。
    """
    trace = "trace-failed-094"
    response = env.client.post(
        "/api/query/search",
        json={"projectId": "no-such-project-id", "query": "x"},
        headers={"X-Request-Id": trace},
    )
    assert response.status_code == 404, response.text

    rows = _audit_rows(env.app.state.meta_db)
    assert len(rows) == 1
    assert rows[0]["request_id"] == trace
    assert rows[0]["degraded"] == 1


# --------------------------------------------------------------------------- §C migration


def _legacy_db(path: Path) -> None:
    """写一个**旧版本**的库：``query_audit`` 缺 ``request_id`` 等三列。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users (
          id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
          created_at INTEGER NOT NULL, is_local INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE projects (
          project_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          display_name TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
        );
        CREATE TABLE query_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, user_id TEXT,
          mode TEXT NOT NULL, query TEXT NOT NULL, answerable INTEGER, confidence TEXT,
          degraded INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL,
          evidence_count INTEGER NOT NULL DEFAULT 0, docs_count INTEGER NOT NULL DEFAULT 0,
          used_tokens INTEGER NOT NULL DEFAULT 0, citation_coverage REAL,
          evidence_json TEXT NOT NULL DEFAULT '[]', created_at INTEGER NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO query_audit (project_id, mode, query, answerable, latency_ms,"
        " evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy-project", "deep", "旧版本写下的查询", 1, 321, '[]', 1_700_000_000),
    )
    connection.commit()
    connection.close()


def test_migration_upgrades_legacy_db_without_losing_rows(tmp_path: Path) -> None:
    """DoD：**用已有数据的旧库启动新代码** → 表结构升级成功、旧数据不丢。

    这是"项目里没有 ALTER TABLE 先例，要自建安全路径"的直接验证：

    1. 旧库（无 ``request_id``）能打开——把 ``CREATE INDEX ... request_id`` 写在 DDL 段会直接炸
       （本卡实测踩到过：``no such column: request_id``），因此索引必须在 ALTER 之后建；
    2. 三列都被补上，且**可重复打开**（幂等，不报 duplicate column）；
    3. 旧行仍在，新列为 ``NULL``（"旧版本/没测过"就是 ``NULL``，不编造）；
    4. 新旧数据能共存，且按新列索引真的查得动。
    """
    db_path = tmp_path / "data" / "nova-meta.db"
    _legacy_db(db_path)

    before = sqlite3.connect(db_path)
    before_columns = {row[1] for row in before.execute("PRAGMA table_info(query_audit)")}
    before_count = before.execute("SELECT COUNT(*) FROM query_audit").fetchone()[0]
    before.close()
    assert "request_id" not in before_columns, "前置条件：旧库确实没有这三列"
    assert before_count == 1

    # ---- 用新代码打开（迁移发生在这里）----
    db = MetaDB.open(db_path)
    try:
        after_columns = {row[1] for row in db._connect().execute("PRAGMA table_info(query_audit)")}
        assert {"request_id", "llm_latency_ms", "answer_tokens"} <= after_columns

        summary = db.usage_summary(["legacy-project"], days=10_000)
        assert summary.total == 1, "旧数据必须还在（迁移不丢行）"
        record = summary.recent[0]
        assert record.query == "旧版本写下的查询"
        assert record.latency_ms == 321
        assert record.request_id is None, "旧行没有 request_id → NULL（不是编造的 id）"
        assert record.llm_latency_ms is None
        assert record.to_json()["requestId"] is None

        # 新旧共存：写一条带 request_id 的，旧行原样还在。
        db.record_query(
            project_id="legacy-project",
            mode="fast",
            query="新版本写下的查询",
            latency_ms=10,
            request_id="trace-after-migration",
        )
        rows = list(
            db._connect().execute("SELECT query, request_id FROM query_audit ORDER BY id")
        )
        assert [row[0] for row in rows] == ["旧版本写下的查询", "新版本写下的查询"]
        assert rows[0][1] is None
        assert rows[1][1] == "trace-after-migration"

        # 迁移引入的索引真的建起来了（按 request_id 查得动）。
        found = list(
            db._connect().execute(
                "SELECT id FROM query_audit WHERE request_id = ?", ("trace-after-migration",)
            )
        )
        assert len(found) == 1
    finally:
        db.close()

    # ---- 幂等：再打开一次不该报 duplicate column ----
    again = MetaDB.open(db_path)
    try:
        assert {row[1] for row in again._connect().execute("PRAGMA table_info(query_audit)")} >= {
            "request_id",
            "llm_latency_ms",
            "answer_tokens",
        }
        assert again._connect().execute("SELECT COUNT(*) FROM query_audit").fetchone()[0] == 2
    finally:
        again.close()


def test_legacy_db_app_serves_queries_and_records_trace(tmp_path: Path) -> None:
    """端到端：旧库 + 新代码起服务 → 查询照常，且**新审计带 trace id**（旧行原样保留）。"""
    data_root = tmp_path / "data"
    _legacy_db(data_root / "nova-meta.db")

    settings = _settings_for(data_root)
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app = create_app(settings)
    app.state.meta_db = MetaDB.open(settings.meta_db_path)
    app.state.engine_manager = manager
    try:
        with TestClient(app, base_url=BASE_URL, raise_server_exceptions=False) as client:
            project_id = manager.resolve_project("identity:legacy", "legacy").project_id
            upload_files(manager, project_id, SAMPLE_FILES)

            trace = "trace-legacy-upgrade"
            response = client.post(
                "/api/query/search",
                json={"projectId": project_id, "query": TARGET_SYMBOL},
                headers={"X-Request-Id": trace},
            )
            assert response.status_code == 200, response.text

            recent = client.get("/api/usage/summary").json()["recent"]
            assert recent[0]["requestId"] == trace

            rows = list(
                app.state.meta_db._connect().execute(
                    "SELECT query, request_id FROM query_audit ORDER BY id"
                )
            )
            assert rows[0][0] == "旧版本写下的查询", "旧行仍在"
            assert rows[0][1] is None
    finally:
        manager.close()


# --------------------------------------------------------------------------- 配置与序列化


def test_storage_config_is_exposed_on_meta(env: Env) -> None:
    """§B1/B4：``/api/meta`` 暴露配额（设置页只读展示），且不含任何 secret。"""
    used = _usage_bytes(env)
    env.relaunch(
        storage_limit_per_project_bytes=used * 4,
        storage_limit_per_user_bytes=used * 40,
    )

    payload = env.client.get("/api/meta").json()
    storage = payload["config"]["storage"]

    assert storage["perProjectBytes"] == used * 4
    assert storage["perUserBytes"] == used * 40
    assert storage["warnRatio"] == 0.8
    assert storage["enabled"] is True
    assert json.dumps(storage, sort_keys=True)  # 可序列化（无 exotic 类型）


def test_storage_config_reports_disabled_when_unlimited(env: Env) -> None:
    """两个上限都为 0 → ``enabled=false``（前端据此只显示已用、不画进度条）。"""
    storage = env.client.get("/api/meta").json()["config"]["storage"]
    assert storage["enabled"] is False
    assert storage["perProjectBytes"] == 0
    assert storage["perUserBytes"] == 0


def test_format_bytes_units() -> None:
    """文案里的单位换算（与 web 的 ``formatBytes`` 同一套；0 显示 ``0 B`` 而不是 ``—``）。"""
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(2048) == "2.0 KiB"
    assert format_bytes(5 * MB) == "5.0 MiB"
    assert format_bytes(3 * 1024 * MB) == "3.00 GiB"


def test_quota_status_json_shape(env: Env) -> None:
    """``to_json`` 的字段集（web 消费的形态；只增不改）。"""
    payload = check_quota(env.manager, env.settings, project_id=env.project_id).to_json()

    assert set(payload) == {"status", "warnRatio", "projectId", "user", "project"}
    for key in ("user", "project"):
        assert set(payload[key]) == {"usedBytes", "limitBytes", "ratio", "status", "unlimited"}


def test_config_parses_quota_env_and_rejects_nonsense() -> None:
    """§B1 环境变量：0 = 不限；非法值显式报错（不静默取默认——配置写错会静默误导）。"""
    settings = Settings.from_env(
        {
            "NOVA_STORAGE_LIMIT_PER_PROJECT_BYTES": "1048576",
            "NOVA_STORAGE_LIMIT_PER_USER_BYTES": "0",
            "NOVA_STORAGE_WARN_RATIO": "0.5",
        }
    )
    assert settings.storage_limit_per_project_bytes == MB
    assert settings.storage_limit_per_user_bytes == 0
    assert settings.storage_warn_ratio == 0.5
    assert settings.storage_quota_enabled is True

    # 两侧都为 0 → 不限（enabled=False，判定恒 ok）。
    unlimited = Settings.from_env(
        {
            "NOVA_STORAGE_LIMIT_PER_PROJECT_BYTES": "0",
            "NOVA_STORAGE_LIMIT_PER_USER_BYTES": "0",
        }
    )
    assert unlimited.storage_quota_enabled is False

    for bad in (
        {"NOVA_STORAGE_WARN_RATIO": "0"},  # 0 会被读成"一超就告警"，是反向开关
        {"NOVA_STORAGE_WARN_RATIO": "1.5"},
        {"NOVA_STORAGE_LIMIT_PER_USER_BYTES": "-1"},
        {"NOVA_STORAGE_LIMIT_PER_PROJECT_BYTES": "abc"},
    ):
        with pytest.raises(ValueError):
            Settings.from_env(bad)


def test_defaults_are_the_agreed_500mb_and_2gb() -> None:
    """B1 的默认值就是用户拍板的 500 MiB / 2 GiB（**不为了让数字好看而调它**）。"""
    settings = Settings.from_env({})
    assert settings.storage_limit_per_project_bytes == 500 * MB
    assert settings.storage_limit_per_user_bytes == 2 * 1024 * MB
    assert settings.storage_warn_ratio == 0.8
