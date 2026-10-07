"""``EngineManager``：core 引擎的进程内持有者（TASK-031 §C；D-34 薄壳）。

服务侧只做四件事：**项目登记（物理目录）**、**blob/账本访问**、**串行化**、**转调 core**；
检索、组装、索引的逻辑全部在 core（本文件不出现任何检索/组装代码）。

并发口径（§C）：core 是"单写者"假设（TASK-001/007/009），而 FastAPI 会把同步 handler 丢进
线程池 → **每 project 一把 ``threading.Lock``，同一 project 的 ingest / delete 串行**，
不同 project 可并行。锁表懒创建；``delete_project`` 也持锁。

不做 per-project 引擎实例缓存：``Store`` / ``VectorStore`` 都是 per-call 打开，
``Engine`` 本身无状态句柄（卡内明确"不要提前优化"）。

已知契约缺口（TASK-035 §C 已收敛）：CF-07 的 ``ingest`` 只返回 ``job_id``，而同步 API 需要
``IngestReport``（added/skipped/errors 等）。现改调 core 的**公开** ``Engine.apply_changes``
（TASK-035 §C 方案 1），service 侧不再出现 ``engine._ingest``。

provider 健康（TASK-035 §A/§B）：:meth:`EngineManager.provider_health` 供 sync/query 的
错误分支使用；它**不加载模型、不做推理**，只用两个信号：①最近一次 provider 故障记忆
（每次 ingest/search 后更新）②``Engine.provider`` 的构造（配置合法性 + 本地模型文件定位）。

本地单用户模式（TASK-034）：:meth:`EngineManager.attach_local` 绑一个本地仓库 + 起
:class:`~nova_service.indexer.ProjectIndexer` 后台索引（不阻塞启动），:meth:`rescan_if_due`
在检索前做增量懒重扫。**绑定的 root 只存内存**：本地模式由 ``nova-service local --repo``
每次重新绑定（``resolve_repo`` 幂等，D-29 身份不变），因此不新增落盘状态文件
（R35 的同步账本只服务上传路径；M2c 引入 ``nova-meta.db`` 时再统一）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from nova_core.engine import PROJECT_META_FILENAME, Engine, EngineError, RepoIdentity, SearchTrace
from nova_core.pipeline import IngestReport
from nova_core.storage.db import DB_FILENAME
from nova_core.types import BlobInput, ChangeSet, ProjectHandle

from nova_service.blobstore import BlobSource, BlobStore
from nova_service.errors import ApiError, embedding_failure_reason
from nova_service.indexer import (
    STATE_DONE,
    STATE_FAILED,
    IndexProgress,
    ProjectIndexer,
    validate_local_root,
)
from nova_service.logging import current_request_id, redact_text
from nova_service.metadb import MetaDB
from nova_service.stats import dir_size_bytes
from nova_service.sync_state import SyncState

__all__ = ["AttachResult", "EngineManager"]

logger = logging.getLogger("nova_service.runtime")

#: 持久化 ``error_text`` 时要抹掉的 endpoint 形态（``https://host/path``）。
#: 为什么只在**落库**时抹：HTTP 错误面（``errors.py``）刻意保留 endpoint 便于定位，
#: 而 ``index_runs`` 是**长期保留 + 在 WebUI 里展示**的记录（公司内网部署时 base_url
#: 就是内网主机名），脱敏代价（少一截定位信息）明显小于收益。
#: 代价与口径差异记在 TASK-062 的「补做记录（TASK-085）」节。
_ENDPOINT_RE = re.compile(r"https?://[^\s,;，；、）)]+")

#: 引擎工厂（测试注入假 embedding provider 的接缝）。
EngineFactory = Callable[[Path], Engine]

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class AttachResult:
    """``attach_local`` 的结果（TASK-034 的冻结接口；TASK-040 依赖）。"""

    project_id: str
    created: bool
    root: str
    identity: RepoIdentity
    index_progress: IndexProgress

    @property
    def identity_key(self) -> str:
        return self.identity.identity_key

    @property
    def is_git(self) -> bool:
        """是否有 git remote 身份（False = D-29 退化为绝对路径 hash，需在响应里如实提示）。"""
        return self.identity.remote_url is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "projectId": self.project_id,
            "created": self.created,
            "root": self.root,
            "identityKey": self.identity_key,
            "rootKind": "git" if self.is_git else "directory",
            "note": None
            if self.is_git
            else (
                "该目录不是带 remote 的 git 仓库：项目身份按 D-29 退化为绝对路径 hash，"
                "换路径/换机器会得到不同 projectId"
            ),
            "indexProgress": self.index_progress.to_json(),
        }


class EngineManager:
    """一个进程一个实例：持有 ``Engine`` + per-project 锁表。"""

    def __init__(self, data_root: str | Path, engine: Engine) -> None:
        self._data_root = Path(data_root).expanduser()
        self._engine = engine
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._deleting: set[str] = set()
        #: 最近一次 provider 故障摘要（脱敏）；成功后清空。见 provider_health。
        self._provider_error: str | None = None
        #: 本地模式（TASK-034）：projectId → 已绑定的仓库根 / 后台索引任务 / 上次重扫时刻。
        self._local_roots: dict[str, Path] = {}
        self._indexers: dict[str, ProjectIndexer] = {}
        self._last_rescan: dict[str, float] = {}
        #: 元数据库（TASK-062/064：索引历史与查询审计）；未注入时历史/统计退化为空而不报错。
        self._meta_db: MetaDB | None = None

    @classmethod
    def open(
        cls, data_root: str | Path, *, engine_factory: EngineFactory | None = None
    ) -> EngineManager:
        """打开管理器（provider 懒构造：起服务不加载模型；测试可注入假 provider）。"""
        root = Path(data_root).expanduser()
        factory: EngineFactory = engine_factory or Engine.open
        return cls(root, factory(root))

    # ------------------------------------------------------------------ 项目

    @property
    def data_root(self) -> Path:
        return self._data_root

    @property
    def engine(self) -> Engine:
        """底层引擎（TASK-032/033 需要 ``search_with_trace`` 等 core 公开方法时用；R33）。"""
        return self._engine

    def resolve_project(self, identity_key: str, display_name: str = "") -> ProjectHandle:
        """幂等解析/创建项目（D-29：identity_key 由调用方给出；物理层隔离在 core）。"""
        return self._engine.resolve_project(identity_key, display_name)

    def project_dir(self, project_id: str) -> Path:
        return self._engine.project_dir(project_id)

    def project_meta(self, project_id: str) -> dict[str, Any] | None:
        """读 ``project.json``；不存在或 id 非法 → ``None``（HTTP 层据此 404）。"""
        try:
            directory = self._engine.project_dir(project_id)
        except EngineError:
            return None
        path = directory / PROJECT_META_FILENAME
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("项目元数据不可读：%s", path)
            return None
        if not isinstance(raw, Mapping):
            return None
        created_at = raw.get("created_at")
        display_name = str(raw.get("display_name") or "")
        return {
            "projectId": project_id,
            "displayName": display_name,
            "createdAt": created_at if isinstance(created_at, int) else 0,
            # TASK-111：分支是身份的一部分（同一仓库不同分支 = 不同项目）。``display_name``
            # 由 CLI/MCP 路径写成 ``<name>@<branch>``；这里拆成结构化字段，UI 可直接分组，
            # 不必让前端去 split 一个展示字符串。取不到分支时如实给 ``null``。
            "branch": _branch_from_display_name(display_name),
        }

    def project_exists(self, project_id: str) -> bool:
        return self.project_meta(project_id) is not None

    def list_projects(self) -> list[dict[str, Any]]:
        """列出 ``{data_root}/projects/*/project.json`` 的摘要。

        本地模式（M2a）返回全部；M2c 才按 user 过滤。
        """
        projects_root = self._data_root / "projects"
        if not projects_root.is_dir():
            return []
        found: list[dict[str, Any]] = []
        for directory in sorted(projects_root.iterdir()):
            if not directory.is_dir():
                continue
            meta = self.project_meta(directory.name)
            if meta is not None:
                found.append(self.describe_project(directory.name, meta))
        return sorted(found, key=lambda item: (-item["createdAt"], item["projectId"]))

    def describe_project(
        self, project_id: str, meta: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """项目摘要 + 本地模式字段（``attachedRoot`` / ``indexProgress``；TASK-034 冻结）。

        TASK-094 §A 追加 ``diskBytes``：该项目索引数据的磁盘占用（``{data_root}/projects/{id}``
        递归求和，含 ``index.db`` / ``vectors/`` / ``blobs/`` / 同步账本），**不含源码仓库**。

        性能口径（实测，本卡实施日）：真机 ``cockpit-agents`` 项目（18 个文件、28 MiB、3416
        chunks）一次求和 **0.8 ms**；合成 25,000 文件（≈500 MB 量级）**约 0.3 s**。因此**每次列表
        都现算**，不做缓存——缓存要引入失效点，而当前成本远低于该复杂度。
        （项目列表页是用户主动打开的页面，毫秒级成本可接受；大仓数量级变差时再加缓存
        并如实标注，见任务卡"未决问题"。）

        **口径诚实（TASK-083 纪律）**：目录不存在 → ``0``（确实没有数据）；目录存在但求和失败
        （权限/并发删除）由 :func:`dir_size_bytes` 自行跳过单个文件，因此不会把"未测量"伪装成 0
        ——前端对 ``null`` 显示 ``—``，对本字段的 0 则如实显示 ``0 B``。
        """
        base = dict(meta) if meta is not None else self.project_meta(project_id)
        if base is None:  # pragma: no cover - 调用方先判存在性
            base = {"projectId": project_id, "displayName": "", "createdAt": 0}
        root = self._local_roots.get(project_id)
        return {
            **base,
            "attachedRoot": str(root) if root is not None else None,
            "indexProgress": self.index_progress(project_id).to_json(),
            "diskBytes": dir_size_bytes(self._engine.project_dir(project_id)),
        }

    def delete_project(self, project_id: str) -> bool:
        """级联删除（core 目录 rm -rf，含 ``blobs/`` 与 ``sync-state.json``）；不存在返回 False。

        TASK-034 §B：先取消并等后台索引线程收尾，再拿 per-project 锁删除——否则索引线程
        可能在 ``rm -rf`` 之后重建项目目录（"删除后不复活目录"）。
        """
        with self._locks_guard:
            if project_id in self._deleting:
                return False
            self._deleting.add(project_id)
        try:
            self._stop_indexer(project_id)
            with self._lock_for(project_id):
                if not self.project_exists(project_id):
                    return False
                self._engine.delete_project(project_id)
                # Keep the lock identity: requests already waiting on it must not race
                # a newly resolved project using a different lock for the same id.
            return True
        finally:
            with self._locks_guard:
                self._deleting.discard(project_id)

    # ------------------------------------------------------------------ blob / 账本

    def blob_store(self, project_id: str) -> BlobStore:
        return BlobStore.open(self._engine.project_dir(project_id))

    def sync_state(self, project_id: str) -> SyncState:
        """从磁盘读同步账本（每次读盘：多进程/多次请求间不持有陈旧快照）。"""
        return SyncState.load(self._engine.project_dir(project_id))

    def blob_source(self, project_id: str) -> BlobSource:
        """构造给 core 的 ``SourceProvider``（账本快照 + blob 镜像）。"""
        return BlobSource(self.blob_store(project_id), self.sync_state(project_id))

    def project_paths(self, project_id: str) -> tuple[BlobStore, SyncState]:
        """``(blob 镜像, 同步账本)``——写路径共用同一份打开结果，避免重复读盘。"""
        directory = self._engine.project_dir(project_id)
        return BlobStore.open(directory), SyncState.load(directory)

    # ------------------------------------------------------------------ 数据面

    def project_lock(self, project_id: str) -> threading.RLock:
        """Serialize blob/ledger/index mutations with the same project lock."""
        return self._lock_for(project_id)

    def flush_sync(self, project_id: str) -> IngestReport:
        """Drain durable work in bounded ingests; acknowledge after persistence only."""
        from dataclasses import fields

        total = IngestReport()
        with self._lock_for(project_id):
            state = self.sync_state(project_id)
            while state.pending:
                source = self.blob_source(project_id)
                files = state.files
                added: list[BlobInput] = []
                modified: list[BlobInput] = []
                selected: list[str] = []
                byte_count = 0
                for path, kind in sorted(state.pending_files.items()):
                    size = files[path].size
                    if selected and (len(selected) >= 64 or byte_count + size > 1024 * 1024):
                        break
                    item = BlobInput(path=path, content=source.read(path),
                                     blob_hash=files[path].blob_hash)
                    (added if kind == "added" else modified).append(item)
                    selected.append(path)
                    byte_count += size
                deleted = state.pending_deleted[:64]
                report = self.ingest(project_id, ChangeSet(
                    added=tuple(added), modified=tuple(modified), deleted=deleted,
                    branch=state.branch, commit_id=state.commit,
                ))
                # Preserve the public aggregate report while keeping source bytes
                # and core parse/embed work bounded to one ingest at a time.
                combined = {}
                for field in fields(report):
                    left, right = getattr(total, field.name), getattr(report, field.name)
                    if field.name in ("invalidation", "languages"):
                        combined[field.name] = right
                    else:
                        combined[field.name] = left + right
                total = IngestReport(**combined)
                if report.errors:
                    return total
                state.complete_batch(selected, deleted, report.skipped_files)
                state.save()
            with self._locks_guard:
                previous = self._indexers.get(project_id)
                if previous is not None and not previous.running and previous.progress().error:
                    self._indexers.pop(project_id, None)
            return total

    def start_sync(self, project_id: str) -> IndexProgress:
        """Start or join a single background flush; no in-memory queue to lose on restart."""
        self._initialize_project(project_id)
        lock = self._lock_for(project_id)
        with self._locks_guard:
            if project_id in self._deleting or not self.project_exists(project_id):
                raise ApiError("project_not_found", "项目不存在或正在删除", 404)
            current = self._indexers.get(project_id)
            if current is not None and current.running:
                return current.progress()
            if not self.sync_state(project_id).pending:
                return current.progress() if current is not None else IndexProgress(state="done")
            indexer = ProjectIndexer(
                project_id, self._engine.project_dir(project_id),
                ingest=lambda: self.flush_sync(project_id), lock=lock,
                count_files=lambda: len(self.sync_state(project_id).pending_files),
            )
            self._indexers[project_id] = indexer
            indexer.start()
            return indexer.progress()

    def ingest(self, project_id: str, changes: ChangeSet) -> IngestReport:
        """索引一次变更集（同 project 串行；source 用账本快照，见卡内 §A/§B）。

        **记录点（TASK-085 §A）**：本方法是所有索引路径的汇聚点（客户端 ``batch-upload`` /
        ``deletions`` 走这里；本地模式走 ``ProjectIndexer`` 的 ``ingest_repo``），因此 run 记录
        挂在这里才能覆盖 Agent 接入形态。**成功与失败都落**（只记成功会让"失败次数"恒为 0）。

        ``files_total`` 的口径（TASK-085 §B）：``IngestReport`` **没有**"仓库共多少文件"这个字段，
        因此记**本次请求送达的文件数**（added + modified）。它可能与 ``files_processed``
        （真正重解析的文件数）相等——**相等不代表没干活**：重扫时 chunk 走 ``chunks_reused``
        复用、向量不重算。故意不从 ``sync_state`` 取账本总数：那会与 ``files_processed``
        分属两种量纲，让 web 的"解析/总数"两列失去可解释性。
        """
        started_at = int(time.time())
        started_perf = time.perf_counter()
        with self._lock_for(project_id):
            source = self.blob_source(project_id)
            # TASK-035 §C：调 core 的公开 ``apply_changes``（不再跨包调 ``Engine._ingest``）。
            try:
                report = self._observe_provider(
                    lambda: self._engine.apply_changes(project_id, changes, source=source)
                )
            except Exception as exc:
                # 失败也要留痕（否则"失败次数"恒为 0），但**不能让记帐改变索引结果**：
                # 原异常照旧向上抛（HTTP 层映射 503/500 的逻辑不变）。
                self._record_failed_ingest(
                    project_id,
                    changes,
                    started_at=started_at,
                    elapsed_ms=(time.perf_counter() - started_perf) * 1000,
                    exc=exc,
                )
                raise
            self._record_ingest_run(
                project_id,
                changes,
                started_at=started_at,
                elapsed_ms=(time.perf_counter() - started_perf) * 1000,
                report=report,
            )
            return report

    def sync_status(self, project_id: str, *, progress_only: bool = False) -> dict[str, Any]:
        """默认返回完整统计；就绪度轮询只读 worker 快照，避免反复开库/遍历 blob。"""
        if progress_only:
            progress = self.index_progress(project_id)
            running = progress.state == "running"
            state = None if running else self.sync_state(project_id)
            return {
                "projectId": project_id,
                "pendingJobs": int(running or (state is not None and (
                    state.pending or (state.session_id is not None and not state.input_closed)
                ))),
                "inputClosed": state.input_closed if state is not None else False,
                "indexProgress": progress.to_json(),
                "skippedFiles": [] if state is None else list(state.skipped_files),
            }
        status = self._engine.sync_status(project_id)
        state = self.sync_state(project_id)
        blob_count, blob_bytes = self.blob_store(project_id).usage()
        progress = self.index_progress(project_id)
        return {
            "projectId": status.project_id,
            "filesIndexed": status.files_indexed,
            "chunks": status.chunks,
            "symbols": status.symbols,
            "edges": status.edges,
            "pendingJobs": max(
                status.pending_jobs,
                int(state.pending or progress.state == "running"
                    or (state.session_id is not None and not state.input_closed)),
            ),
            "inputClosed": state.input_closed,
            "indexingFiles": list(status.indexing_files),
            "indexProgress": progress.to_json(),
            "skippedFiles": list(state.skipped_files),
            "lastIndexedAt": status.last_indexed_at,
            "branch": state.branch,
            "commit": state.commit,
            "blobs": {"count": blob_count, "bytes": blob_bytes},
            "checkpoints": len(state.checkpoints),
        }

    def search(
        self, project_id: str, query: str, max_tokens: int = 10_000, *, deep: bool = False
    ) -> SearchTrace:
        """检索（core ``search_with_trace``：通道健康度/候选计数给 meta 用，R33）。

        ``deep``（TASK-109）：Deep 模式（``ask_project``）用更大的 **Gap 补检配额**。
        两种模式走 core 里**同一条管线**（D-10），这里只是把模式标记透传下去——
        service 不参与任何补检判定。
        """
        return self._observe_provider(
            lambda: self._engine.search_with_trace(project_id, query, max_tokens, deep=deep),
            # 降级（如向量通道失败）不是"provider 恢复了"：保留故障记忆，不谎报健康。
            recovered=lambda trace: not trace.degraded,
        )
    # ------------------------------------------------------------------ provider 健康（§A/§B）

    def provider_health(self) -> tuple[bool, str | None]:
        """provider 健康快照 ``(ok, reason)``（TASK-035 §A/§B）。

        **不加载模型、不做真实推理**，只用两个信号：

        1. 故障记忆：最近一次 ingest/search 是否因 provider 挂掉失败（成功即清空）——
           连接类故障（如 API 地址不可达）只有在真实调用时才暴露，光看配置看不出来；
        2. 配置合法性 + 已加载状态：``Engine.provider`` 的构造（``EMBED_*`` 合法？本地模型文件
           可定位？）。已构造过（``provider`` 属性已缓存）→ 直接 ok。

        调用方（query/sync 的错误分支）据此把根因放在首位：provider 坏了就要报 503，
        而不是让客户端看到"请先同步"去无限重试（TASK-035 §B）。
        """
        if self._provider_error is not None:
            return False, self._provider_error
        try:
            _ = self._engine.provider  # 构造（不推理）：配置非法/模型文件不可用在此暴露
        except Exception as exc:  # EngineError（包装 EmbeddingError）/ EmbeddingError
            reason = embedding_failure_reason(exc) or redact_text(
                f"{type(exc).__name__}: {exc}"
            )
            return False, reason
        return True, None

    def _observe_provider(
        self, call: Callable[[], _T], *, recovered: Callable[[_T], bool] | None = None
    ) -> _T:
        """执行一次可能触碰 provider 的 engine 调用，记录/清除 provider 故障记忆。

        ``recovered`` 判定"这次调用算不算 provider 正常"（缺省：没抛异常就算）；
        ``search`` 传 ``not trace.degraded``——向量通道降级时 provider 其实没恢复。
        """
        try:
            result = call()
        except Exception as exc:
            reason = embedding_failure_reason(exc)
            if reason is not None:
                self._provider_error = reason
                logger.warning("embedding provider 故障：%s", reason)
            raise
        if recovered is None or recovered(result):
            self._provider_error = None
        return result

    # ------------------------------------------------------------------ 本地单用户模式（TASK-034）

    def attach_local(
        self, root: str | Path, *, display_name: str = "", index: bool = True
    ) -> AttachResult:
        """绑定一个本地目录并（可选）启动后台索引（TASK-034 §A 的冻结接口）。

        - ``resolve_repo`` 幂等：同目录重复 attach → 同 projectId（D-29 身份）；
        - ``index=True`` 时**立即返回**，索引在后台线程跑（不阻塞启动；§B）；
          **已在索引中时不重复启动**（返回当前进度，HTTP 层据此 409）；
        - root 不存在/不是目录 → :class:`~nova_service.indexer.LocalRootError`（HTTP 400）。
        """
        path = validate_local_root(root)
        handle, identity = self._engine.resolve_repo(path)
        project_id = handle.project_id
        self._local_roots[project_id] = path
        self._initialize_project(project_id)

        indexer = self._indexer_for(project_id, path)
        if index and not indexer.running:
            indexer.start()
        return AttachResult(
            project_id=project_id,
            created=handle.created,
            root=str(path),
            identity=identity,
            index_progress=indexer.progress(),
        )

    def attached_root(self, project_id: str) -> str | None:
        """该实例已绑定的本地仓库根（未绑定/重启后未再 attach → ``None``）。"""
        root = self._local_roots.get(project_id)
        return str(root) if root is not None else None

    def index_progress(self, project_id: str) -> IndexProgress:
        """后台索引进度（TASK-034 的冻结接口；无任务 → ``state="idle"``）。"""
        indexer = self._indexers.get(project_id)
        return indexer.progress() if indexer is not None else IndexProgress()

    def start_index(self, project_id: str, *, full: bool = False) -> bool:
        """启动后台增量重扫（手动 ``/rescan``）；已在跑/未绑定 root → ``False``。"""
        indexer = self._indexers.get(project_id)
        if indexer is None or indexer.running:
            return False
        if full:  # 全量重建留给 TASK-062（本卡只做增量：卡内明确不做 full）
            logger.warning("full=True 未实现，按增量处理：%s", project_id)
        return indexer.start()

    def rescan_if_due(self, project_id: str, *, min_interval_s: float) -> bool:
        """本地模式懒重扫（TASK-034 §C 的冻结接口）：距上次扫描 ≥ 间隔则同步增量扫一次。

        - ``min_interval_s <= 0`` → **禁用**（不发扫描，直接 False）；
        - 未绑定本地 root（远端上传路径）→ False（远端靠客户端上传，不适用）；
        - 已有后台索引在跑 → False（不叠加、不阻塞）；
        - **同步执行**增量 ``ingest_repo``（不是全量）；失败**向上抛**，由调用方（检索路径）
          记日志 + 如实上报，**不得**让检索失败（卡内 §C 纪律）。
        """
        if min_interval_s <= 0:
            return False
        root = self._local_roots.get(project_id)
        if root is None:
            return False
        indexer = self._indexers.get(project_id)
        if indexer is not None and indexer.running:
            return False
        now = time.monotonic()
        last = self._last_rescan.get(project_id)
        if last is not None and now - last < min_interval_s:
            return False
        self._last_rescan[project_id] = now
        with self._lock_for(project_id):
            self._engine.ingest_repo(project_id, root)
        return True

    def close(self) -> None:
        for indexer in list(self._indexers.values()):
            indexer.cancel()
            indexer.join()
        self._indexers.clear()
        self._engine.close()

    # ------------------------------------------------------------------ 内部（本地模式）

    def _initialize_project(self, project_id: str) -> None:
        """首次建库（SQLite schema 初始化），已在建/已建好则**立即返回**（不取锁）。

        **为什么必须做（TASK-034 实测发现的真实竞争）**：``Store.open`` 首次会跑
        ``executescript(schema)``。后台索引线程与 HTTP 读请求（``GET /api/projects/{id}``
        → ``sync_status``）同时首次打开同一项目时，两边都看到"表不存在"、都去建表，
        后到的一方拿到 ``sqlite3.OperationalError: table index_config already exists``，
        索引直接失败。本方法在**启动后台索引之前**、持项目锁把库建好。

        快路径（文件已存在即返回，``不取锁``）是必须的：索引线程会长时间持锁
        （实测 aibox 全量 ~17 分钟），attach 若每次都去抢锁就会被卡住。
        """
        directory = self._engine.project_dir(project_id)
        if (directory / DB_FILENAME).exists():
            return
        with self._lock_for(project_id):
            if project_id in self._deleting or not self.project_exists(project_id):
                raise ApiError("project_not_found", "项目不存在或正在删除", 404)
            if not (directory / DB_FILENAME).exists():
                self._engine.sync_status(project_id)  # 建库/建表（空库代价极小）

    def _indexer_for(self, project_id: str, root: Path) -> ProjectIndexer:
        lock = self._lock_for(project_id)  # 先取项目锁（它自己要拿 _locks_guard，不可嵌套）
        with self._locks_guard:
            indexer = self._indexers.get(project_id)
            if indexer is None or indexer.root != root:
                indexer = ProjectIndexer(
                    project_id,
                    root,
                    ingest=lambda: self._engine.ingest_repo(project_id, root),
                    lock=lock,
                    on_finish=(
                        lambda progress, pid=project_id: self._record_index_run(pid, progress)
                    ),
                )
                self._indexers[project_id] = indexer
            return indexer

    # ------------------------------------------------------------------ 历史与统计（TASK-062/064）

    def attach_meta_db(self, db: MetaDB | None) -> None:
        """注入元数据库（``create_app`` 或测试调用；未注入时历史/统计不可用但不报错）。

        ``None`` 是合法输入（幂等置空）：app 级 ``meta_db`` 为 ``None`` 时调用方
        （``deps`` / ``mcp`` 的懒构造）直接传它，不必自己判空。
        """
        self._meta_db = db

    def _project_chunk_count(self, project_id: str) -> int:
        """run 记录里的 ``chunks``：索引结束后项目库里的 chunk **总数**。

        与 ``sync_status().chunks`` 同源（同一份 ``store.counts()``），因此：
        - 历史表里的 chunks 列与项目页显示的 chunks 始终是同一个数；
        - **两条索引路径**（本地 attach / 客户端上传）得到完全相同的口径 ——
          TASK-062 §A 的响应样例（``filesTotal: 1436`` 配 ``chunks: 9389``）也表明
          ``chunks`` 指**项目总量**，不是"本次新增"（后者在增量重扫/整批重传时会显示 0，
          看起来像没工作 —— 而那正是本卡要消灭的观察）。

        取不到（库未建/打不开）时记 0 并告警：**记帐不得拖垮索引**（TASK-062 §C 纪律）。
        """
        try:
            return int(self._engine.sync_status(project_id).chunks)
        except Exception:  # noqa: BLE001 - 见 docstring：旁路统计不阻断主路径
            logger.warning("读取 chunk 总数失败（run 记录的 chunks 记 0）：%s", project_id)
            return 0

    def _record_ingest_run(
        self,
        project_id: str,
        changes: ChangeSet,
        *,
        started_at: int,
        elapsed_ms: float,
        report: IngestReport,
    ) -> None:
        """把一次**成功**的上传索引写进 ``index_runs``（口径与 ``_record_index_run`` 一致）。

        - ``state="done"``（本方法只在没抛异常时被调）；
        - ``errors`` 非空仍算 succeeded（只是部分文件有解析问题，TASK-062 §C）；
        - ``error_text`` = 逗号分隔的解析问题摘要（与 ``_count_errors`` 的计数器同源），
          因此 web 的"errors"列与"有解析问题"标记在两条路径上含义相同；
        - ``finished_at`` 用真实墙钟；**真实耗时**（``perf_counter`` 差）走日志，
          库里落的是它的整秒表示——原因与代价见下方注释与执行记录。
        """
        db = self._meta_db
        if db is None:
            return
        logger.info(
            "上传索引完成：%s（added=%d，modified=%d，parsed=%d，errors=%d，实测耗时 %.1f ms）",
            project_id,
            report.added,
            report.modified,
            report.files_parsed,
            len(report.errors),
            elapsed_ms,
        )
        errors = ", ".join(report.errors)
        try:
            db.record_index_run(
                project_id,
                state=STATE_DONE,
                started_at=started_at,
                # ``record_index_run`` 是 TASK-062 的冻结签名（只收秒级 started_at/finished_at，
                # 由它自己算 duration_ms），因此**不能**在这里传毫秒。代价：亚秒级的上传
                # （典型客户端小仓库）``durationMs`` 会呈现为 0——与本地 attach 路径的整秒
                # 粒度一致（TASK-085 §B 要求两条路径不得两套解释），真实值看上面的日志。
                finished_at=int(time.time()),
                files_total=len(changes.added) + len(changes.modified),
                files_processed=report.files_parsed,
                chunks=self._project_chunk_count(project_id),
                errors=len(report.errors),
                error_text=errors or None,
                # TASK-099 §B：把**当前请求**的 trace id 当作 callId 落库。客户端在同一次
                # Tool 调用里给所有请求（含 N 次 batch-upload）发同一个 ``X-Request-Id``，
                # 因此这几条 run 的 call_id 相同，与同一次调用的 ``query_audit.request_id``
                # 也相同——分组靠这一个值，不需要额外的映射表（卡内 §B-3）。
                call_id=current_request_id(),
            )
        except Exception:  # 记不上账不能把已经成功的索引变成失败（TASK-062 §C 的既有纪律）
            logger.exception("索引记录落库失败（不影响索引结果）：%s", project_id)

    def _record_failed_ingest(
        self,
        project_id: str,
        changes: ChangeSet,
        *,
        started_at: int,
        elapsed_ms: float,
        exc: BaseException,
    ) -> None:
        """把一次**失败**的索引写进 ``index_runs``（``state="failed"``）。

        ``error_text`` 走 :func:`redact_text`（key/token 不出库）；``files_processed`` 记 0——
        ``IngestReport`` 在异常路径上根本不存在，"失败前处理了几个文件"无从得知。
        **不谎报**：0 在这里的含义是"未知/未产出报告"，不是"一个文件都没处理"。
        """
        db = self._meta_db
        if db is None:
            return
        logger.error(
            "上传索引失败：%s（实测耗时 %.1f ms）", project_id, elapsed_ms, exc_info=exc
        )
        try:
            db.record_index_run(
                project_id,
                state=STATE_FAILED,
                started_at=started_at,
                finished_at=int(time.time()),
                files_total=len(changes.added) + len(changes.modified),
                errors=1,
                error_text=_persist_error_text(exc),
                # TASK-099 §B：失败的上传同样属于这次调用（否则时间线上会缺一块，
                # 而"哪一步慢了/挂了"恰恰是用户最想看的）。
                call_id=current_request_id(),
            )
        except Exception:  # 同上：记帐失败不改变索引结果（原异常仍在向上传播）
            logger.exception("索引失败记录落库失败（不影响错误上报）：%s", project_id)

    @property
    def meta_db(self) -> MetaDB | None:
        return self._meta_db

    def _record_index_run(self, project_id: str, progress: IndexProgress) -> None:
        """把一次索引的结束态写进 ``index_runs``（``running`` 不落库：服务被杀不留幽灵行）。

        ``call_id``（TASK-099 §B）取 :func:`nova_service.logging.current_request_id`：本回调在
        **触发它的那次请求的线程内**执行（懒重扫路径）或后台线程内执行（``attach_local`` 的
        启动索引），后者拿到 ``None``——即"不是某次 HTTP 调用的一部分"。该区分是**故意的**：
        启动索引不该被挂在碰巧同刻发生的那次检索的 callId 下（那是两件事）。

        口径（TASK-062 §C/§D）：
        - **成功与失败都落**（只记成功会让"失败次数"恒为 0）；
        - ``state="done"`` 且 ``error`` 非空仍算 **succeeded**（那只是部分文件有解析问题），
          ``errors`` 字段存条数、``error_text`` 存摘要；
        - 回调异常在 ``ProjectIndexer._notify_finish`` 已吞掉，不让记帐拖垮索引。

        与上传路径（:meth:`ingest`）的关系：``duration_ms`` 沿用本路径（整秒精度，
        ``finished_at - started_at``），``chunks`` 用同一个 :meth:`_project_chunk_count`。
        两条路径的**共同语义**是 ``state`` / ``errors`` / ``error_text`` / ``chunks`` /
        ``call_id``：
        web 的"成功/失败"、"有解析问题"、"chunks"与"属于哪次调用"四列因此一致；
        耗时绝对值的精度差异见 TASK-062 的「补做记录（TASK-085）」节。
        """
        db = self._meta_db
        if db is None or progress.state not in (STATE_DONE, STATE_FAILED):
            return
        started = progress.started_at or progress.finished_at or int(time.time())
        finished = progress.finished_at or int(time.time())
        db.record_index_run(
            project_id,
            state=progress.state,
            started_at=int(started),
            finished_at=int(finished),
            files_total=progress.total_files,
            files_processed=progress.processed_files,
            chunks=self._project_chunk_count(project_id),
            errors=_count_errors(progress.error),
            error_text=progress.error,
            call_id=current_request_id(),
        )

    def index_stats(
        self, project_id: str, *, limit: int = 20, days: int | None = None
    ) -> dict[str, Any]:
        """单项目索引统计（内存态当前进度 + 落库历史；无 MetaDB 时只剩当前进度）。"""
        current = self.index_progress(project_id).to_json()
        history = (
            self._meta_db.index_stats(project_id, limit=limit).to_json()
            if self._meta_db is not None
            else {"total": 0, "succeeded": 0, "failed": 0, "recent": []}
        )
        return {
            "projectId": project_id,
            "current": current,
            "history": history,
            "diskBytes": dir_size_bytes(self._engine.project_dir(project_id)),
        }

    def _stop_indexer(self, project_id: str) -> None:
        """取消并等后台索引收尾（删除项目前调用；见 :meth:`delete_project`）。"""
        with self._locks_guard:
            indexer = self._indexers.pop(project_id, None)
            self._local_roots.pop(project_id, None)
            self._last_rescan.pop(project_id, None)
        if indexer is not None:
            indexer.cancel()
            indexer.join()

    # ------------------------------------------------------------------ 内部

    def _lock_for(self, project_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(project_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[project_id] = lock
            return lock


def _branch_from_display_name(display_name: str) -> str | None:
    """``<name>@<branch>`` → ``branch``；无后缀时返回 ``None``（TASK-111）。

    为什么从展示名解析而不是新增一个落盘字段：``project.json`` 是**已存在**的元数据文件，
    TASK-111 不改它的 schema（避免旧项目需要迁移）。分支已经在 ``display_name`` 里
    （:func:`nova_core.engine.repo_identity` 生成），``rpartition`` 拆开即可。

    边界：仓库名本身含 ``@``（罕见）时，取**最后一个** ``@`` 之后的部分——
    与生成侧的拼接方式对称（后缀总是最后追加的）。
    """
    if "@" not in display_name:
        return None
    _base, _sep, branch = display_name.rpartition("@")
    return branch or None


def _persist_error_text(exc: BaseException) -> str:
    """失败 run 的 ``error_text``：沿用 :func:`redact_text`（key/token/Bearer），再抹掉 endpoint。

    两步都是**保留诊断价值**的收窄，而不是编辑事实：异常类型与原因照旧，只有
    "key 本体"与"embedding 服务地址"不出库。
    """
    return _ENDPOINT_RE.sub("***", redact_text(f"{type(exc).__name__}: {exc}"))


def _count_errors(error_text: str | None) -> int:
    """从 ``IndexProgress.error`` 摘要里数出条目数（逗号分隔的解析问题列表）。

    口径：``state="done"`` 时它非空表示"索引完成，但这些文件有解析问题"（不是失败），
    因此条数单独记在 ``errors`` 字段里，与 ``state`` 的含义互不覆盖。
    """
    if not error_text:
        return 0
    return len([item for item in (part.strip() for part in error_text.split(",")) if item])
