"""单项目后台索引 worker（TASK-034 §B）。

为什么需要它：TASK-033 实测 aibox 规模全量索引 ~17 分钟。同步阻塞会让服务在启动时不可用，
且用户看不到任何进展。本卡做的是 Module/06 §2.4 索引 job 的**本地单用户简化版**：
**per project 一个 ``threading.Thread``，不做 job 表 / worker 池 / 多项目排队**（那些归 TASK-062）。

口径（本卡冻结）：

- **每 project 最多一个在跑的索引任务**；重复触发返回 ``False``（HTTP 层转 409
  ``index_running``），**不排队堆积**；
- **进度诚实（D-30）**：``ingest_repo`` 是全同步、无回调的，因此索引期间 ``state="running"``
  而 ``processed_files`` 允许为 0——**不伪造百分比**；``total_files`` 来自一次轻量目录列举
  （只 walk，不读文件内容），终值取 ``IngestReport.files_parsed``；
- **失败如实上报**：``state="failed"`` + 脱敏后的 ``error`` 写进进度；异常**不吞、不静默死线程**，
  同时写日志（全量堆栈）；
- **可中断**：:meth:`ProjectIndexer.cancel` + :meth:`ProjectIndexer.join`，配合 per-project 锁，
  保证 ``delete_project`` 之后索引线程不会把项目目录"复活"；
- worker 持 per-project 锁跑 ``ingest_repo``，因此与 ``EngineManager.ingest``（上传路径）、
  ``delete_project`` 严格串行（不同 project 可并行）。

``state`` 取值（冻结给 TASK-040）：``idle`` / ``running`` / ``done`` / ``failed``。
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from zace_core.pipeline import DirectorySource, IngestReport

from zace_service.logging import redact_text

__all__ = [
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_IDLE",
    "STATE_RUNNING",
    "IndexProgress",
    "LocalRootError",
    "ProjectIndexer",
]

logger = logging.getLogger("zace_service.indexer")

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"

#: ``cancel()`` 后等待索引线程收尾的上限（秒）。索引本身不可中断，超时只影响日志噪音。
JOIN_TIMEOUT_S = 30.0


class LocalRootError(ValueError):
    """本地仓库根不合法（不存在 / 不是目录）。HTTP 层转 400 ``invalid_root``。"""


@dataclass(frozen=True, slots=True)
class IndexProgress:
    """一个项目的后台索引进度（TASK-034 的冻结接口；TASK-040 的 MCP 错误文案要用）。"""

    state: str = STATE_IDLE
    started_at: int | None = None
    finished_at: int | None = None
    processed_files: int = 0
    total_files: int = 0
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        """CF-05 风格的 camelCase 视图（``indexProgress`` 字段的值）。"""
        return {
            "state": self.state,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "processedFiles": self.processed_files,
            "totalFiles": self.total_files,
            "error": self.error,
        }


def validate_local_root(root: str | Path) -> Path:
    """校验并规范化本地仓库根：必须是**存在的目录**，否则抛 :class:`LocalRootError`。

    **不做** git 仓库校验（卡内 §A）：本地模式允许指向任意目录，身份按 core 的 D-29 规则解析
    （非 git 目录退化为绝对路径 hash，调用方在响应里如实提示）。
    """
    raw = str(root).strip()
    if not raw:
        raise LocalRootError("root 不能为空")
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise LocalRootError(f"root 不是存在的目录：{raw}")
    return path.resolve()


class ProjectIndexer:
    """单个项目的后台索引任务（不可重入；每次 ``start`` 一个线程）。"""

    def __init__(
        self,
        project_id: str,
        root: Path,
        *,
        ingest: Callable[[], IngestReport],
        lock: threading.Lock,
        on_finish: Callable[[IndexProgress], None] | None = None,
        count_files: Callable[[], int] | None = None,
    ) -> None:
        self._project_id = project_id
        self._root = root
        self._ingest_repo = ingest
        self._lock = lock
        #: 结束回调（TASK-062）：把这一次的 run 落进 ``index_runs``。**成功与失败两条路径都调**
        #: （只记成功会让"失败次数"恒为 0）。回调自身的异常不得影响索引结果。
        self._on_finish = on_finish
        self._count_source_files = count_files
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancelled = threading.Event()
        self._progress = IndexProgress()

    # ------------------------------------------------------------------ 控制面

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def root(self) -> Path:
        return self._root

    @property
    def running(self) -> bool:
        with self._guard:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """启动后台索引；已有任务在跑 → ``False``（不排队、不重入）。"""
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancelled.clear()
            # 立刻把快照置为 running（started_at 为现在）：``attach`` 的返回值与 CLI 就绪信息
            # 读的都是这个快照。留 idle 会让调用方以为"索引还没开始"（TASK-040 的空索引
            # 错误文案也读它）——D-30：状态必须反映真实情况。``total_files`` 由工作线程
            # 目录列举后补齐（那之前为 0，属"还没数出来"而不是"没有文件"）。
            self._progress = IndexProgress(
                state=STATE_RUNNING, started_at=int(time.time())
            )
            context = contextvars.copy_context()
            self._thread = threading.Thread(
                target=lambda: context.run(self._run),
                name=f"zace-index-{self._project_id}",
                daemon=True,  # 进程退出不等索引（服务是长期进程；测试也不该被线程拖住）
            )
            self._thread.start()
            return True

    def cancel(self) -> None:
        """请求取消：线程若尚未开始（或还没拿到锁）就不会写任何索引状态。"""
        self._cancelled.set()

    def join(self, timeout: float | None = JOIN_TIMEOUT_S) -> None:
        with self._guard:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def progress(self) -> IndexProgress:
        with self._guard:
            return self._progress

    # ------------------------------------------------------------------ 工作线程

    def _run(self) -> None:
        if self._cancelled.is_set():
            logger.info("索引任务已取消（未开始）：%s", self._project_id)
            self._reset_to_idle()
            return
        try:
            total = self._count_files()
        except Exception as exc:  # 目录列举失败：如实上报，线程不静默死
            self._fail(exc)
            return

        with self._guard:
            self._progress = IndexProgress(
                state=STATE_RUNNING,
                # 沿用 start() 记下的时刻：attach/CLI 读到的是同一个 startedAt。
                started_at=self._progress.started_at or int(time.time()),
                total_files=total,
            )
        logger.info("开始索引：%s（root=%s，files=%d）", self._project_id, self._root, total)

        with self._lock:  # 与上传 ingest / delete_project 串行
            if self._cancelled.is_set():
                logger.info("索引任务在拿锁后取消：%s", self._project_id)
                self._reset_to_idle()
                return
            try:
                report = self._ingest_repo()
            except Exception as exc:
                self._fail(exc)
                return
        self._finish(report, total)

    def _count_files(self) -> int:
        """``total_files``：一次目录列举（只 walk，不读内容，因此不拖慢索引）。"""
        if self._count_source_files is not None:
            return self._count_source_files()
        return len(DirectorySource(self._root).list_files())

    def _finish(self, report: IngestReport, total: int) -> None:
        if self._cancelled.is_set():
            logger.info("索引任务已完成但项目已被删除，丢弃状态：%s", self._project_id)
            self._reset_to_idle()
            return
        with self._guard:
            self._progress = IndexProgress(
                state=STATE_DONE,
                started_at=self._progress.started_at,
                finished_at=int(time.time()),
                # 终值取 IngestReport.files_parsed（卡内口径）；增量重扫时它只数本次真正解析的
                # 文件，"仓库共多少"看 total_files——两者含义不同，故不合成百分比（D-30）。
                processed_files=report.files_parsed,
                total_files=total,
                error=", ".join(report.errors) if report.errors else None,
            )
        logger.info(
            "索引完成：%s（parsed=%d/%d，added=%d，modified=%d，deleted=%d，errors=%d）",
            self._project_id,
            report.files_parsed,
            total,
            report.added,
            report.modified,
            report.deleted,
            len(report.errors),
        )
        self._notify_finish()

    def _fail(self, exc: BaseException) -> None:
        message = redact_text(f"{type(exc).__name__}: {exc}")
        with self._guard:
            self._progress = replace(
                self._progress,
                state=STATE_FAILED,
                finished_at=int(time.time()),
                error=message,
            )
        logger.error("索引失败：%s（root=%s）", self._project_id, self._root, exc_info=exc)
        self._notify_finish()

    def _notify_finish(self) -> None:
        """把结束态交给回调（TASK-062 落库）。**回调异常只记日志**，不影响索引结果与进度。"""
        if self._on_finish is None:
            return
        try:
            self._on_finish(self.progress())
        except Exception:  # 记不上账不能把已经成功的索引变成失败
            logger.exception("索引记录落库失败（不影响索引结果）：%s", self._project_id)

    def _reset_to_idle(self) -> None:
        with self._guard:
            self._progress = IndexProgress()
