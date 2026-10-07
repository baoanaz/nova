"""nova-core 冻结接口（CF-07/CF-09）。

来源：Module/04 §2（AnswerProvider）、Module/06 §1（ContextEngine）、
Module/01 §2.4 + D-44（EmbeddingProvider）、Module/01 §2.2（Parser）。
维护者：编排者；变更前须在任务卡执行记录里提契约变更申请、等裁决。

实现位置约定（各卡交付物）：
  ContextEngine      → core/nova_core/engine.py        （TASK-007/013；TASK-031 接入 service）
  EmbeddingProvider  → core/nova_core/embedding/       （TASK-008）
  AnswerProvider     → core/nova_core/llm/             （Phase 3，Module/04）
  Parser             → core/nova_core/parsing/         （TASK-002..005）
  SourceProvider     → core/nova_core/pipeline/source.py（服务端 blob 实现归 TASK-031）
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from nova_core.types import (
    AskResult,
    ChangeSet,
    ContextPack,
    ParsedFile,
    ProjectHandle,
    SyncStatus,
)

if TYPE_CHECKING:  # 仅类型检查期导入：运行时导入会造成 interfaces ↔ pipeline 循环
    from nova_core.pipeline.source import SourceProvider

# ---------------------------------------------------------------------------
# ContextEngine：core 对 service 暴露的唯一接口面（Module/06 §1）
# ---------------------------------------------------------------------------


@runtime_checkable
class ContextEngine(Protocol):
    """进程内对象，per service 进程；零 HTTP / 零鉴权 / 零用户概念（D-34）。"""

    @classmethod
    def open(cls, data_root: Path) -> ContextEngine:
        """打开/初始化引擎（数据根如 ~/.nova；core 只管 projects/）。"""
        ...

    def resolve_project(self, identity_key: str, display_name: str = "") -> ProjectHandle:
        """project identity（D-29）幂等解析/创建；identity_key 由调用方按 D-29 规则计算。"""
        ...

    def ingest(
        self,
        project_id: str,
        changes: ChangeSet,
        *,
        source: SourceProvider | None = None,
    ) -> str:
        """写入变更集并完成索引（V1 同步语义，异步 job 归 TASK-062）；返回 job_id。

        ``source``（2026-09-10 L2 契约扩展，实施卡 TASK-031）：服务端上传模式下由 service
        提供的源码读取实现——``list_files()`` 必须是**项目已知全部文件**，``read(path)`` 返回
        该 path 当前内容的原始字节（正规定义：``nova_core.pipeline.source.SourceProvider``）。
        为 ``None`` 时引擎按已绑定的本地目录读取（TASK-013 CLI 路径）。

        为什么服务端必须传 ``source``：配置指纹二级/一级失效（D-07）会走 ``reembed`` /
        ``full_reparse``，该路径**遍历 ``source.list_files()`` 重建**；上传模式下若不传
        （内部退化为空 source），会**静默清空索引且不重建任何文件**（TASK-031 卡内有最小复现）。

        幂等语义见 Module/06 §2.1。
        """
        ...

    def sync_status(self, project_id: str) -> SyncStatus: ...

    def search(self, project_id: str, query: str, max_tokens: int = 10_000) -> ContextPack:
        """Fast 模式：检索 + 组装（Module/02+03），不调 LLM。"""
        ...

    def ask(self, project_id: str, question: str) -> AskResult:
        """Deep 模式：检索 + 组装 + AnswerProvider（Module/02/03/04）。"""
        ...

    def delete_project(self, project_id: str) -> None:
        """级联删除（D-03：rm -rf 项目目录 + 元数据行）。"""
        ...


# ---------------------------------------------------------------------------
# EmbeddingProvider：TASK-008 双实现（D-44）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    """embedding 指纹（写入 index_config；变更触发 D-07 二级失效）。

    ``model_id`` 命名（TASK-008 口径）：``local:<model_slug>`` / ``api:<model_name>``。
    该串会落库，改名等于换模型（触发全量重嵌），需谨慎。
    """

    model_id: str                 # 如 "local:multilingual-e5-small" / "api:bge-m3"
    dim: int
    max_input_tokens: int         # 超长输入由 provider 内部截断（Module/01 §2.4：默认 ≤2048）


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def profile(self) -> EmbeddingProfile: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """索引侧（passage）批量嵌入。

        输入为完整文本，截断策略由 provider 按 ``profile.max_input_tokens`` 执行；
        返回单位向量（已 L2 归一化）。消费者：TASK-007（索引期分片嵌入）。
        """
        ...

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        """检索侧（query）批量嵌入（2026-09-10 升格为契约，原 TASK-008 卡内实现约定）。

        存在的理由：e5 等模型对 query / passage 要求不同前缀，缺前缀会显著掉质量；
        无前缀约定的模型（bge / arctic）本方法等价于 ``embed``。
        消费者：TASK-010（检索向量通道）。索引侧一律用 ``embed``，不得混用。
        """
        ...


# ---------------------------------------------------------------------------
# AnswerProvider：Module/04 §2（Phase 3）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    system: str
    user: str
    max_tokens: int = 3072
    temperature: float = 0.2


@runtime_checkable
class AnswerProvider(Protocol):
    def complete(self, req: CompletionRequest) -> str: ...


# ---------------------------------------------------------------------------
# Parser：TASK-002..005（纯函数，无 I/O，可并行开发）
# ---------------------------------------------------------------------------


@runtime_checkable
class Parser(Protocol):
    language: str                 # python / c / cpp / markdown

    def parse(self, path: str, content: str) -> ParsedFile:
        """解析单文件；不抛异常（失败 → ParsedFile(fallback=True, parse_errors=[...])）。"""
        ...
