"""``ContextEngine`` 装配（TASK-013 / CF-07 / D-29）。

把 TASK-007（索引流水线）、TASK-010/011（检索与图扩展/rerank）、TASK-012（组装）装配成
进程内引擎；Phase 2 的 service 与 Phase 3 的 ask 复用同一装配，不另写一份检索链。

设计依据：``docs/design/Module/06-服务化与部署.md`` §1（core 纯库边界：CLI 是调试形态）、
``docs/design/Module/05-MCP与同步.md`` §3.4（D-29 project identity）；
契约：``core/nova_core/interfaces.py`` 的 ``ContextEngine``（CF-07）与
``docs/contracts/contextpack.schema.json``（CF-03）。

卡内实现口径（TASK-013，"同步调用"）：

- ``ingest`` / ``resolve_project`` / ``sync_status`` / ``search`` / ``delete_project`` 为
  同步调用；异步 job 与进度上报是 Phase 2 service 的事，本卡 ``ingest`` 直接跑完流水线并返回
  一个 job id 字符串（形态与 CF-07 一致，语义是"已完成"）。
- ``ask`` 抛 ``NotImplementedError``（Phase 3 / Module/04），不静默返回空包。
- **本卡扩展（不在 CF-07 内）**：``Engine.open`` 之外的本地目录辅助入口（``resolve_repo`` /
  ``ingest_repo`` / ``search_with_trace`` / ``project_dir``）供 CLI 与测试使用；
  它们只做"仓库 → ChangeSet"的本地适配，检索链与契约方法完全共用。

D-29 project identity（本卡实现基础版）：

```text
有 git remote → identity_key = sha256(remote_url + repo 在 git 根内的相对路径)
无 git        → identity_key = sha256(canonical 绝对路径)
project_id    = sha256(identity_key) 前 16 位十六进制
数据目录      = {data_root}/projects/{project_id}/
```

冻结接口 ``resolve_project(identity_key, display_name)`` 由调用方给出 identity_key（CF-07 原样）；
D-29 的计算放在模块级 :func:`repo_identity`，CLI 走 :meth:`Engine.resolve_repo` 组合两者。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from nova_core.contextpack import (
    MODE_DEEP,
    MODE_FAST,
    BudgetConfig,
    assemble,
    budget_for,
    collect_index_signals,
)
from nova_core.embedding import EmbeddingConfig, create_provider
from nova_core.hashing import blob_hash, file_content_hash
from nova_core.interfaces import ContextEngine, EmbeddingProvider
from nova_core.pipeline import DirectorySource, Indexer, IngestReport
from nova_core.pipeline.index_state import (
    IndexState,
    IndexStatus,
    building_state,
    failed_state,
    read_index_state,
    ready_state,
    write_index_state,
)
from nova_core.pipeline.source import SourceProvider
from nova_core.retrieval import RecallLimits, recall
from nova_core.retrieval.exact import extract_inferred
from nova_core.retrieval.expand import REASON_REEXPORT, ExpansionLimits, expand
from nova_core.retrieval.gap import (
    GAP_REASON_CALLEE,
    GAP_REASON_CONTAINER,
    GAP_REASON_REEXPORT,
    GapLimits,
    GapPlan,
    SymbolMember,
    plan_gaps,
)
from nova_core.retrieval.rerank import collect_signals, rerank
from nova_core.retrieval.vector import QueryEmbeddingCache
from nova_core.storage import Store
from nova_core.types import (
    AskResult,
    BlobInput,
    Candidate,
    ChangeSet,
    ContextPack,
    MissingEvidence,
    ProjectHandle,
    SyncStatus,
)
from nova_core.vectors import VectorStore
from nova_core.vectors.cache import EmbeddingCache

logger = logging.getLogger(__name__)

__all__ = [
    "DATA_ROOT_ENV",
    "DEFAULT_DATA_ROOT",
    "PROJECTS_DIRNAME",
    "PROJECT_META_FILENAME",
    "SCAN_MANIFEST_FILENAME",
    "Engine",
    "EngineError",
    "DEEP_GAP_LIMITS",
    "RepoIdentity",
    "SearchTrace",
    "git_remote_url",
    "plan_scan",
    "project_dir_for",
    "project_id_for",
    "repo_identity",
    "resolve_data_root",
]

#: 默认数据根（Module/06 §1：core 只管 ``projects/``）。
DEFAULT_DATA_ROOT = Path.home() / ".nova"
#: 覆盖数据根的环境变量（CLI ``--data`` 优先）。
DATA_ROOT_ENV = "NOVA_DATA_ROOT"

#: 关闭跨项目 embedding 缓存的开关（TASK-111）：``off`` / ``0`` / ``false`` / ``no``。
#: 用于回归对比（验证"开了缓存省了多少"）与受限环境。
EMBED_CACHE_ENV = "NOVA_EMBED_CACHE"
#: 项目目录所在子目录（D-03）。
PROJECTS_DIRNAME = "projects"
#: 项目元数据文件名（本卡扩展：identity_key/display_name 的落盘记录，也是"创建"标记）。
PROJECT_META_FILENAME = "project.json"
#: 本地扫描状态文件名（本卡扩展：CLI 侧增量对账；Phase 2 由 client 的 scan 状态取代）。
SCAN_MANIFEST_FILENAME = "scan_manifest.json"

#: Deep 模式的 Gap 补检配额（TASK-109）。比 Fast 宽，但仍是**有界**的：Deep 的定位是
#: "综合判断"，需要更完整的调用链；Fast 是"毫秒级定位器"，配额收敛到能补上断层即止。
#: 两边走的是**同一份代码**（D-10），这里只是参数差异。
DEEP_GAP_LIMITS = GapLimits(
    max_containers=3,
    members_per_container=32,
    max_spec_anchors=3,
    refs_per_spec=12,
    max_total=32,
)

_PROJECT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_GIT_TIMEOUT_S = 5.0


class EngineError(RuntimeError):
    """引擎的使用/配置错误（CLI 映射为退出码 1）。"""


# ---------------------------------------------------------------------------
# D-29 project identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoIdentity:
    """一个本地仓库目录的 D-29 身份（``repo_path`` 是它在 git 根内的相对路径）。

    ``branch``（TASK-111）：D-29 原文要求"同一机器不同 checkout 应隔离"，但原实现只 hash
    ``remote + 相对路径``，**不含分支**——同一 remote 的两个分支/两个 worktree 会碰撞成同一个
    projectId，索引互相污染（实测：main 与 feature 分支混合索引 353 文件，检索返回对方分支的
    文件且 ``index: fresh`` 仍显示正常）。本字段记录参与身份计算的分支名
    （detached HEAD 时为短 commit，取不到则为 ``None`` = 退回不含分支的旧口径）。
    """

    identity_key: str
    display_name: str
    remote_url: str | None = None
    git_root: str | None = None
    repo_path: str = ""
    branch: str | None = None


def project_id_for(identity_key: str) -> str:
    """``project_id = sha256(identity_key)`` 前 16 位十六进制。"""
    if not identity_key:
        raise EngineError("identity_key 不能为空")
    return hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:16]


def project_dir_for(data_root: str | Path, project_id: str) -> Path:
    """``{data_root}/projects/{project_id}/``；顺带拦掉路径穿越的非法 id。"""
    if not _PROJECT_ID_RE.match(project_id):
        raise EngineError(f"非法 project_id：{project_id!r}（期望 16 位小写十六进制）")
    return Path(data_root).expanduser() / PROJECTS_DIRNAME / project_id


def resolve_data_root(data: str | Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """数据根解析顺序：显式参数 → ``$NOVA_DATA_ROOT`` → ``~/.nova``。"""
    if data is not None:
        return Path(data).expanduser()
    source = os.environ if env is None else env
    raw = source.get(DATA_ROOT_ENV)
    return Path(raw).expanduser() if raw else DEFAULT_DATA_ROOT


def _git(args: list[str], cwd: Path) -> str | None:
    """跑一条 git 命令；git 缺失/超时/非仓库一律返回 ``None``（D-29 的"无 git"分支）。"""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def git_branch(root: str | Path) -> str | None:
    """当前分支名（TASK-111 身份维度）：``rev-parse --abbrev-ref HEAD``。

    退化顺序：分支名 → detached HEAD 的短 commit → ``None``（取不到时**不引入分支维度**，
    与旧口径一致，保证无 git / 异常环境下的行为不回归）。
    """
    path = Path(root).expanduser()
    name = _git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    if name and name != "HEAD":
        return name
    head = _git(["rev-parse", "--short", "HEAD"], path)
    return f"detached@{head}" if head else None


def git_remote_url(root: str | Path) -> str | None:
    """仓库的 git remote URL：优先 ``origin``，否则取第一个 remote；无 git/无 remote → ``None``。"""
    path = Path(root).expanduser()
    origin = _git(["config", "--get", "remote.origin.url"], path)
    if origin:
        return origin
    names = _git(["remote"], path)
    if not names:
        return None
    first = names.splitlines()[0].strip()
    if not first:
        return None
    return _git(["config", "--get", f"remote.{first}.url"], path)


def repo_identity(root: str | Path) -> RepoIdentity:
    """按 D-29 计算仓库身份（有 remote 用 remote+相对路径，无则用绝对路径 hash）。"""
    path = Path(root).expanduser().resolve()
    git_root = _git(["rev-parse", "--show-toplevel"], path)
    remote = git_remote_url(path) if git_root else None
    if remote and git_root:
        resolved_git_root = Path(git_root).expanduser().resolve()
        try:
            relative = path.relative_to(resolved_git_root).as_posix()
        except ValueError:  # show-toplevel 不是 path 的祖先（符号链接等）：退回仓库自身
            relative = ""
        repo_path = "" if relative == "." else relative
        branch = git_branch(path)
        # TASK-111：分支参与身份计算（D-29"不同 checkout 应隔离"的字面落地）。
        # 分隔符 \x00 防止 (remote+路径) 与分支名的拼接歧义。
        material = remote + repo_path + ("\x00" + branch if branch else "")
        display_name = f"{path.name}@{branch}" if branch else path.name
        return RepoIdentity(
            identity_key=hashlib.sha256(material.encode("utf-8")).hexdigest(),
            display_name=display_name,
            remote_url=remote,
            git_root=str(resolved_git_root),
            repo_path=repo_path,
            branch=branch,
        )
    return RepoIdentity(
        identity_key=hashlib.sha256(str(path).encode("utf-8")).hexdigest(),
        display_name=path.name,
        remote_url=None,
        git_root=str(Path(git_root).resolve()) if git_root else None,
        branch=git_branch(path) if git_root else None,
    )


# ---------------------------------------------------------------------------
# 检索 trace（本卡扩展：CF-03 无 degraded 字段，降级信息不进 ContextPack）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchTrace:
    """一次 ``search`` 的完整结果（ContextPack + 通道健康度 + 候选池）。

    ``candidates`` = 送进组装的最终候选序（rerank 后），带 ``channel_ranks``——组装会做
    同符号聚合/相邻区间合并，通道命中信息在 ContextPack 里不再完整可读，E2E 断言需要它。

    ``gap_kinds`` / ``backfilled``（TASK-109）：二轮补检的可观测面——命中了哪些 Gap 规则、
    补入了多少候选。测试与基准报告靠它区分"首轮就命中"与"靠补检命中"。
    """

    pack: ContextPack
    channels_used: tuple[str, ...] = ()
    degraded: bool = False
    degraded_reason: str | None = None
    candidates: tuple[Candidate, ...] = ()
    gap_kinds: tuple[str, ...] = ()
    backfilled: int = 0

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    def candidate_for(self, symbol_fqn: str) -> Candidate | None:
        """按符号 fqn 取候选（E2E 断言/Debug 用；同符号只返回候选池里的首次出现）。"""
        for candidate in self.candidates:
            if candidate.symbol_fqn == symbol_fqn:
                return candidate
        return None


class _EmptySource:
    """无本地目录绑定的占位 source（service 形态：内容全在 ChangeSet 里）。

    ``full_reparse`` 需要枚举文件；ChangeSet-only 调用下没有可枚举的本地文件，
    因此返回空清单（Phase 2 service 会传入自己的 blobs source）。
    """

    def read(self, path: str) -> bytes:
        raise FileNotFoundError(path)

    def list_files(self) -> tuple[str, ...]:
        return ()


def _chunks_vectors(fields: dict[str, object]) -> dict[str, int]:
    """字段桶 → ``(chunks, vectors)`` 计数（缺省 0 = 不参与对账）。"""
    return {
        "chunks": int(fields.get("expected_chunks") or 0),  # type: ignore[arg-type]
        "vectors": int(fields.get("expected_vectors") or 0),  # type: ignore[arg-type]
    }


def _vector_index_gap(store: Store, vectors: VectorStore) -> str | None:
    """R41 附注 / TASK-036 §D：``chunks > 0`` 但向量表为空 → 返回可读的降级原因。

    这是 TASK-031 实测过的"静默清空"形态：``Engine.ingest`` 不传 ``source`` 时配置指纹失效
    触发 ``full_reparse``，遍历空清单重建出空向量表，**既不报错也不留痕**。检索侧照常返回
    BM25 通道结果，调用方无从判断向量索引其实已经不在了。本函数让该状态在
    :class:`SearchTrace` 上可见（只复用既有 ``degraded`` / ``degraded_reason`` 字段，
    CF-03/CF-04 不动）。

    反向不成立：``chunks == 0``（真空库）不算降级——那是"还没索引"，由 ``answerable``
    与 SyncStatus 表达，不该伪装成"通道坏了"。
    """
    if vectors.count() != 0:
        return None
    chunks = store.counts()["chunks"]
    if chunks == 0:
        return None
    return f"向量索引为空（可能未重建）：chunks={chunks}，vectors=0"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _split_identifier(name: str) -> set[str]:
    """标识符 → 小写词元集合（``_make_tools_to_model_edge`` → {make,tools,to,model,edge}）。

    用途：G3 的“callee 名字是否含查询词”判定。拆法是确定性的两种约定：
    下划线分词 + 驼峰分词（``getBoundModel`` → {get,bound,model}），不做词干化——
    保守一点只是少给几个候选加分，不会引入错误证据。
    """
    tokens: list[str] = []
    for chunk in str(name).replace("::", ".").split("."):
        for piece in chunk.split("_"):
            if not piece:
                continue
            current = ""
            for char in piece:
                if char.isupper() and current:
                    tokens.append(current)
                    current = char
                else:
                    current += char
            if current:
                tokens.append(current)
    return {token.lower() for token in tokens if token}


def _query_words(query: str) -> frozenset[str]:
    """查询里的“词面”词元（供 G3 判断 callee 名是否名中问题）。

    两个来源合并：

    1. **标识符形态**的词（复用 ``extract_inferred``：驼峰/蛇形/SCREAMING）再向外拆，
       这样 ``wrap_model_call`` / ``auto_strategy`` 里的 ``model`` / ``strategy`` 都能取出；
    2. **普通小写英文词**（长度 ≥ 3）——这一步实测必需：``model`` / ``tools`` 在自然语言
       问句里是普通词，不会匹配标识符正则，若只听 ``extract_inferred`` 则这两个最关键
       的词会全部丢失，排序退化成字母序。

    为什么要这一步：实测（langchain，问 model↔tools 边如何路由、何时退出循环）中，
    ``create_agent`` 的 6 个可用 callee 里只有 ``_make_tools_to_model_edge`` 的名字含
    ``model`` / ``tools`` —— 单靠“私有函数优先”（``_chain_*`` / ``_add_*`` / ``_dedupe_*``
    也是私有函数）无法区分，必须用词面证据。
    """
    words: set[str] = set()
    for token in extract_inferred(query):
        for piece in _split_identifier(token):
            if len(piece) >= 3:
                words.add(piece)
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9]{2,}", query):
        words.add(match.group(0).lower())
    return frozenset(words)


class Engine:
    """``ContextEngine`` 的过程化实现（per 进程；进程内单写者假设，同 TASK-001/009）。"""

    def __init__(
        self,
        data_root: str | Path,
        *,
        provider: EmbeddingProvider | None = None,
        embedding_config: EmbeddingConfig | None = None,
        limits: RecallLimits | None = None,
        expansion_limits: ExpansionLimits | None = None,
        query_cache: object | None = None,
    ) -> None:
        self._data_root = Path(data_root).expanduser()
        self._provider = provider
        self._embedding_config = embedding_config
        self._limits = limits or RecallLimits()
        self._expansion_limits = expansion_limits or ExpansionLimits()
        #: 查询向量缓存（TASK-101 §F）：传入时**复用它**（离线回放/预热），并在进程存活期间共享
        #: 同一份，使预热与回放走同一条链。取值只需满足 get/put（``retrieval.vector`` 的鸭子类型）。
        #: 未传入时由 :attr:`query_cache` **惰性建一份默认的**并跨查询持有。
        #: TASK-REVIEW-RUNTIME P2-1：旧行为是 ``recall()`` 每次调用新建、调用结束即丢，
        #: 注释里写的 60s 跨查询复用压根不存在。
        self._query_cache = query_cache
        self._repo_roots: dict[str, Path] = {}

    # ------------------------------------------------------------------ 生命周期

    @classmethod
    def open(
        cls,
        data_root: str | Path,
        *,
        provider: EmbeddingProvider | None = None,
        embedding_config: EmbeddingConfig | None = None,
        query_cache: object | None = None,
    ) -> Engine:
        """打开引擎（``data_root`` 下的 ``projects/`` 按需创建；provider 懒构造）。"""
        return cls(
            data_root,
            provider=provider,
            embedding_config=embedding_config
            if embedding_config is not None
            else EmbeddingConfig.from_env(),
            query_cache=query_cache,
        )

    def close(self) -> None:
        """释放引擎持有的资源（provider 句柄；Store/VectorStore 是 per-call 打开的）。"""
        self._repo_roots.clear()

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def data_root(self) -> Path:
        return self._data_root

    @property
    def provider(self) -> EmbeddingProvider:
        """embedding provider（懒构造：默认本地 ONNX，D-44）。"""
        if self._provider is None:
            try:
                self._provider = create_provider(self._embedding_config)
            except Exception as exc:  # EmbeddingConfigError 等配置类错误显式暴露
                raise EngineError(
                    f"embedding provider 不可用：{type(exc).__name__}: {exc}"
                ) from exc
        return self._provider

    # ------------------------------------------------------------------ 项目（CF-07）

    def project_dir(self, project_id: str) -> Path:
        """项目数据目录 ``{data_root}/projects/{project_id}/``（本卡扩展，供 CLI/测试）。"""
        return project_dir_for(self._data_root, project_id)

    def resolve_project(self, identity_key: str, display_name: str = "") -> ProjectHandle:
        """幂等解析/创建项目（D-29：identity_key 由调用方给出）。

        注意：**不在此处追加分支**。TASK-111 把分支并进 ``identity_key``（客户端算），
        因此同一仓库的不同分支自然得到不同 projectId；``display_name`` 由调用方给出
        （CLI/MCP 路径已含 ``@分支`` 后缀，见 :meth:`repo_identity`）。
        """
        project_id = project_id_for(identity_key)
        directory = self.project_dir(project_id)
        directory.mkdir(parents=True, exist_ok=True)
        meta_path = directory / PROJECT_META_FILENAME
        created = not meta_path.exists()
        if created:
            meta = {
                "project_id": project_id,
                "identity_key": identity_key,
                "display_name": display_name,
                "created_at": int(time.time()),
            }
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        return ProjectHandle(project_id=project_id, created=created)

    def resolve_repo(self, root: str | Path) -> tuple[ProjectHandle, RepoIdentity]:
        """本地仓库目录 → (项目句柄, D-29 身份)，并绑定该目录供后续 ``ingest`` 使用。"""
        identity = repo_identity(root)
        handle = self.resolve_project(identity.identity_key, identity.display_name)
        self._repo_roots[handle.project_id] = Path(root).expanduser().resolve()
        self._record_repo_identity(handle.project_id, identity)
        return handle, identity

    def set_query_cache(self, cache: object | None) -> None:
        """替换查询向量缓存（TASK-101 §F：CLI 在 engine 建好后注入侧车缓存）。"""
        self._query_cache = cache

    @property
    def query_cache(self) -> object:
        """进程内查询向量缓存（未注入时惰性建默认的）。

        TASK-REVIEW-RUNTIME P2-1：缓存必须归 ``Engine`` 生命周期持有，否则 60s TTL 复用形同虚设。
        调用方（CLI / service）不必知道具体类型，只需 it 支持 ``get/put``。
        """
        if self._query_cache is None:
            self._query_cache = QueryEmbeddingCache(ttl_s=self._limits.query_cache_ttl_s)
        return self._query_cache

    def _bind_query_cache_identity(self, provider: EmbeddingProvider) -> None:
        """把当前 provider 的模型身份绑到查询缓存（P2-1）。

        为什么每次开项目都绑：``set_provider()`` 能在生命周期内换模型（``--replay`` 切离线
        provider、D-47 换用户模型）。缓存 key 不带模型就会取到另一个模型的向量——
        维度相同吋静默给出错误相似度，比报错更难察觉。身份变了缓存会自清。

        用 getattr 探测而非硬依赖：调用方可以注入任意满足 get/put 的鸭子类型对象。
        ``PersistentQueryVectorCache``（侧车）的绑定接口是 ``(model, dim)`` 关键字形式，
        且其一致性由 CLI 的 ``_sync_cache_identity`` 指纹校验负责，这里**不重复处理**。

        取 ``self.query_cache``（属性）而非 ``self._query_cache``（字段）：缓存是惰性建的，
        开项目时字段可能还是 ``None``——用字段会静默跳过绑定（实测踩过）。
        """
        cache = self.query_cache
        bind = getattr(cache, "bind_identity", None)
        if not callable(bind):
            return
        if "dim" in inspect.signature(bind).parameters:
            return
        bind(provider.profile.model_id)

    def set_provider(self, provider: EmbeddingProvider) -> None:
        """替换 embedding provider（TASK-101 §F：``--replay`` 用它切到离线 provider）。

        只影响**查询侧**调用（provider 每次 ``_open_project`` 重取，见其实现）；
        索引侧语义不变——离线 provider 的 ``embed()`` 会直接抛错（见 ``CachedOnlyProvider``）。
        """
        self._provider = provider

    def bind_repo(self, project_id: str, root: str | Path) -> None:
        """把本地仓库目录绑到**已存在的** project_id（本卡扩展：benchmark 放行口，TASK-101 §E）。

        与 :meth:`resolve_repo` 的区别：不计算 D-29 身份、不创建 ``project.json``，因此可以把
        一个**预建好的索引目录**（内含 embedding）挂到任意 checkout 路径上复现跑分，避免每次
        改检索代码都重新索引（真实成本：287 文件仓库一次索引分钟级 + embedding 花费）。

        纪律：绑定不检查索引是否可用——索引缺失/维度不符时，检索链会**如实抛错**
        （``VectorStore.open`` 的 ``DimensionMismatchError``），不静默重建、不伪装成空结果。
        """
        self._repo_roots[project_id] = Path(root).expanduser().resolve()

    def ingest(
        self, project_id: str, changes: ChangeSet, *, source: SourceProvider | None = None
    ) -> str:
        """写入变更集并完成索引（本卡同步语义），返回 job id（已完成的 job）。

        ``source``（CF-07 的 ``source`` 参数，TASK-031 §A）：服务端上传模式下由 service 传入
        源码读取实现（``list_files()`` 必须是项目已知全部文件）。**必须传**：配置指纹一级/二级失效
        （D-07）会走 ``full_reparse`` / ``reembed``，该路径遍历 ``source.list_files()`` 重建；
        不传则内部退化为 ``_EmptySource``，结果是在库里已有文件的情况下重建出空集合，
        **向量索引被静静清空且不报错**（回归测试：``core/tests/integration/test_ingest_source.py``）。
        """
        self._ingest(project_id, changes, full=False, source=source)
        return f"job-sync-{uuid.uuid4().hex[:12]}"

    def apply_changes(
        self,
        project_id: str,
        changes: ChangeSet,
        *,
        source: SourceProvider | None = None,
        full: bool = False,
    ) -> IngestReport:
        """应用变更集并返回 **IngestReport**（TASK-035 §C 的公开面）。

        服务端同步语义入口：与 CF-07 的 :meth:`ingest`（异步 job 版，只回 job id）区别在于
        本方法**同步返回报告**，且属 core 的公开 API 但**不在 CF-07 面内**（R33 允许 service
        使用 core 公开类与方法）。``_ingest`` 仍是内部实现，未被删除或改签名
        （``ingest`` 在用）。
        """
        return self._ingest(project_id, changes, full=full, source=source)

    def ingest_repo(self, project_id: str, root: str | Path, *, full: bool = False) -> IngestReport:
        """本地目录摄入（CLI 入口）：扫描 → 与上次扫描对账 → 索引增量 → 落扫描状态。"""
        repo = Path(root).expanduser().resolve()
        if not repo.is_dir():
            raise EngineError(f"仓库路径不是目录：{repo}")
        self._repo_roots[project_id] = repo
        scan = plan_scan(repo, self.read_manifest(project_id))
        # 位移诊断要在 ingest 之前算：ingest 一跑，项目就不再是"空索引"了（P2-7）。
        warning = self._displacement_warning(project_id, repo)
        report = self._ingest(project_id, scan.changes, full=full)
        if scan.errors:
            report = replace(report, errors=report.errors + scan.errors)
        if warning:
            report = replace(report, warnings=report.warnings + (warning,))
        self.write_manifest(project_id, repo, scan.hashes)
        return report

    def sync_status(self, project_id: str) -> SyncStatus:
        """索引现状（Phase 1 全同步：``pending_jobs`` 恒 0、``indexing_files`` 恒空）。"""
        with Store.open(self.project_dir(project_id)) as store:
            counts = store.counts()
            freshness = store.freshness()
        return SyncStatus(
            project_id=project_id,
            files_indexed=counts["files"],
            chunks=counts["chunks"],
            symbols=counts["symbols"],
            edges=counts["edges"],
            pending_jobs=0,
            indexing_files=(),
            last_indexed_at=freshness.indexed_at,
        )

    def search(self, project_id: str, query: str, max_tokens: int = 10_000) -> ContextPack:
        """Fast 模式：检索 + 组装（TASK-010 → 011 → 012），不调 LLM。"""
        return self.search_with_trace(project_id, query, max_tokens).pack

    def ask(self, project_id: str, question: str) -> AskResult:
        """Deep 模式：Phase 3（Module/04 AnswerProvider）实现，本卡不提供。"""
        raise NotImplementedError(
            "ask（Deep 模式）属 Phase 3（Module/04 AnswerProvider）；"
            "本版本请用 search 取 ContextPack 后自行交给调用方 LLM。"
        )

    def delete_project(self, project_id: str) -> None:
        """级联删除（D-03：整个项目目录 rm -rf，含 index.db / vectors / 扫描状态）。"""
        directory = self.project_dir(project_id)
        self._repo_roots.pop(project_id, None)
        if directory.exists():
            shutil.rmtree(directory)

    # ------------------------------------------------------------------ 检索链（本卡扩展）

    def search_with_trace(
        self,
        project_id: str,
        query: str,
        max_tokens: int = 10_000,
        *,
        deep: bool = False,
    ) -> SearchTrace:
        """``search`` 的带 trace 版本（通道健康度/候选计数；CLI 与测试用）。

        两轮管线（TASK-109，D-19；Module/02 §4.7/§4.8）：

        ```text
        第一轮：recall → expand → rerank → assemble
                    ↓
                gap 检查（确定性，纯函数，<5ms）
                    ↓ 命中 G1/G2
        第二轮：定向补检（只补缺口，不全量重跑）
                → 并入同一 rerank → 重新 assemble（独立小预算）
        ```

        ``deep``（TASK-109 §“与 ask 的关系”）：Deep 模式（``ask_project``）用**更大**的
        补检配额。Fast/Deep **共用同一条管线与同一份代码**（D-10）——差异只在配额，
        不在分支；分叉点在**组装之后**（Fast 拿到包即停，Deep 交给 LLM）。
        两边都触发补检：实测两个失败用例（``cockpit-0033``/``0035``）都是 search 题，
        只在 Deep 触发就修不了它们。

        ``deep`` 同时选定**装填预算档位**（TASK-MCP-BUDGET 修复）：此前两次 ``assemble``
        都硬编码 ``mode=MODE_FAST``（该参数与 ``config`` 二选一，传了 ``config`` 后 ``mode``
        就是死参），因此 ``DEEP_BUDGET`` 从未生效、``ask_project`` 实际按 Fast 预算装填。
        现在 ``mode`` 随 ``deep`` 走：Fast 14K / Deep 16K。
        """
        if not query.strip():
            raise EngineError("query 不能为空")
        if max_tokens <= 0:
            raise EngineError(f"max_tokens 必须为正整数，收到 {max_tokens}")
        limits = DEEP_GAP_LIMITS if deep else GapLimits()
        mode = MODE_DEEP if deep else MODE_FAST
        with self._open_project(project_id) as (store, vectors, provider):
            recalled = recall(
                store,
                query,
                provider=provider,
                vector_store=vectors,
                limits=self._limits,
                cache=self.query_cache,
            )
            vector_gap = _vector_index_gap(store, vectors)
            # P1-1 / P1-5：索引状态（building/failed/对账不一致）同样要体现在降级上。
            # 与 _vector_index_gap 合并为同一个 degraded_reason；两者独立，都可能单独出现。
            state_gap = self._index_state_gap(project_id, store, vectors)
            expansion = expand(store, recalled.candidates, limits=self._expansion_limits)
            pool = [*recalled.candidates, *expansion.candidates]
            ranked = rerank(pool, collect_signals(store, query, pool))
            pack = assemble(
                store,
                query,
                ranked,
                flows=expansion.flows,
                freshness=store.freshness(),
                mode=mode,
                config=self._budget(max_tokens, mode),
                signals=collect_index_signals(store, ranked),
            )
            # ---- 第二轮：Evidence-Gap 定向补检（≤ 1 次，确定性） ----
            gaps, backfill, ranked = self._backfill_gaps(
                store, query, ranked, pack, limits=limits
            )
            if gaps.triggered:
                pack = assemble(
                    store,
                    query,
                    ranked,
                    flows=expansion.flows,
                    freshness=store.freshness(),
                    mode=mode,
                    config=self._budget(max_tokens, mode),
                    signals=collect_index_signals(store, ranked),
                    backfill=backfill,
                    # 本轮的主贪心循环会把预算再填满一次（candidates 含全部候选），
                    # 若不为补检留出余量，补检循环会在第一行 ``break``——
                    # 实测 LC-21：首轮留的余量被本轮贪心吃掉，19 个补检候选全部落空。
                    reserve_backfill=True,
                )
        degraded_reason = recalled.degraded_reason
        for gap in (vector_gap, state_gap):
            if gap is not None:
                degraded_reason = f"{degraded_reason}；{gap}" if degraded_reason else gap
        return SearchTrace(
            pack=pack,
            channels_used=recalled.channels_used,
            degraded=recalled.degraded or vector_gap is not None or state_gap is not None,
            degraded_reason=degraded_reason,
            candidates=tuple(ranked),
            gap_kinds=gaps.kinds,
            backfilled=len(backfill),
        )

    # ------------------------------------------------------------------ Gap 二轮（TASK-109）

    def _backfill_gaps(
        self,
        store: Store,
        query: str,
        ranked: list[Candidate],
        pack: ContextPack,
        *,
        limits: GapLimits,
    ) -> tuple[GapPlan, list[tuple[Candidate, str]], list[Candidate]]:
        """首轮包 → gap 计划 → 补检候选（并入同一 rerank，**不插队**）。

        三条纪律（TASK-109 §“二轮纪律”，不得放宽）：

        1. 最多 1 次迭代（本函数只调一次 ``plan_gaps``，不做循环）；
        2. 二轮只补缺口对应通道（G1 走符号容器、G2 走 spec 引用），不全量重跑；
        3. 二轮结果**进同一 rerank** 重排，不直接插队（否则破坏 D-16 的排序唯一性）。
        """
        # 包内证据 → chunk_id：组装会做同符号聚合与相邻区间合并，因此一条 ``EvidenceItem``
        # 的行区间可能覆盖**多个**候选（实测 ``intent_router.py:(13,24)`` 合并了
        # ``IntentRouter:13`` 与 ``FixedIntentRouter:19`` 两个候选）。
        # 故用**行区间重叠**判定候选是否已在包内，而不是 ``(path, 行区间)`` 全等——
        # 后者会把已被合并装填的候选误判为"未进包"，于是补检把它们再装一遍。
        packed_ranges: dict[str, list[tuple[int, int]]] = {}
        for item in (*pack.evidence, *pack.docs):
            if item.lines is None:
                continue
            packed_ranges.setdefault(item.path, []).append(
                (item.lines[0], item.lines[1])
            )

        def in_pack(candidate: Candidate) -> bool:
            """候选的行区间是否与包内任一同路径证据重叠（重叠 = 内容已在包内）。"""
            if candidate.path is None or candidate.start_line is None or candidate.end_line is None:
                return False
            for start, end in packed_ranges.get(candidate.path, ()):
                if not (candidate.end_line < start or candidate.start_line > end):
                    return True
            return False

        packed_candidates = [candidate for candidate in ranked if in_pack(candidate)]
        packed_cids = {candidate.chunk_id for candidate in packed_candidates}
        packed_symbols = [
            item.symbol for item in pack.evidence if item.symbol
        ]
        # 首轮池序（chunk_id → 下标）：G2 的引用目标按**池序**取，而不是按
        # ``spec_references`` 的字典序——图扩展把 spec 引用全部拉进了池，
        # 池序才含“哪个引用的符号更可能是答案”的相关度判定。
        pool_order = {candidate.chunk_id: index for index, candidate in enumerate(ranked)}
        # G2 的输入：包内 spec 块 → 它 spec_references 指向的 chunk id（按池序）。
        packed_spec_refs: dict[str, tuple[str, ...]] = {}
        for candidate in packed_candidates:
            if candidate.kind != "spec":
                continue
            refs = tuple(
                sorted(
                    (ref.symbol_id for ref in store.spec_refs_for_spec(candidate.chunk_id)),
                    key=lambda chunk_id: pool_order.get(chunk_id, 1 << 30),
                )
            )
            if refs:
                packed_spec_refs[candidate.chunk_id] = refs

        def members_of(container: str) -> list[SymbolMember]:
            return [
                SymbolMember(fqn=row.fqn, chunk_id=row.chunk_id)
                for row in store.symbols_in_container(container)
            ]

        # G4：包内代码证据的 ``re-export site`` 候选 → 它导出的符号名。
        # 只在**包内已存在该符号**时才计，与 G1/G3 的“锚点已进包”同一纪律：
        # 否则“随便一个被 import 的文件”都会触发。
        packed_fqn_set = {item.symbol for item in pack.evidence if item.symbol}
        reexport_marks: dict[str, str] = {}
        for candidate in ranked:
            if not any(REASON_REEXPORT == r for r in candidate.reasons):
                continue
            for fqn in packed_fqn_set:
                name = fqn.replace("::", ".").rsplit(".", 1)[-1]
                if name and store.reexport_sources(name) and candidate.path in (
                    store.reexport_sources(name)
                ):
                    reexport_marks.setdefault(candidate.chunk_id, name)
                    break

        def callees_of(container: str) -> list[SymbolMember]:
            """G3：容器（含嵌套符号）的出边目标 → 可落地的池内候选（按“与问题的契合度”排）。

            五处必须同时做对（各自实测踩过）：

            1. **包含嵌套符号**：``create_agent`` 调用 ``_make_tools_to_model_edge`` 的边
               在符号表里是一条独立记录，而嵌套回调（``create_agent.model_node``）自身
               也可能再往外调；只取裸名会漏掉后者。
            2. **只取出边**：``Store.edges_for`` 是**双向**的（source 侧 + target 侧，
               ``ORDER BY kind, source, target``），不过滤就会把 caller 也当成“它调用的东西”。
            3. **按契合度排序后再截断**：不排就按字母序截断——实测 ``_add_middleware_edge``
               / ``_chain_*`` / ``_dedupe_transformers`` 占满名额，而目标
               ``_make_tools_to_model_edge`` 恰好排在它们之后，被上限切掉，等于 G3 白做。
            4. **不按池序重排**（在 :func:`gap._callee_closure` 里保证）：G3 候选几乎全是
               图扩展进来的 tier3、``score`` 恒为 0.5，池序没有区分度。
            5. **“名字含查询词”优先于“私有函数”**：两者在本题恰好冲突——``_chain_*
               / ``_add_*`` 也都是私有函数，单靠“私有优先”仍然切不到目标；而
               ``_make_tools_to_model_edge`` 的名字里恰好含查询词 ``model`` 与 ``tools``。
               这是**词面证据**（与 BM25 同源），不是猜测：名字里出现了问题里的词，
               说明它很可能就是被问的那个实现。
            """
            prefixes = (container, f"{container}.")
            members: list[SymbolMember] = []
            seen: set[str] = set()
            for prefix in prefixes:
                for edge in store.edges_for(prefix, kinds=["calls"]):
                    if edge.source != prefix or edge.target in seen:
                        continue
                    seen.add(edge.target)
                    rows = store.exact_symbols(edge.target, limit=None)
                    chunk_id = next((r.chunk_id for r in rows if r.chunk_id is not None), None)
                    members.append(SymbolMember(fqn=edge.target, chunk_id=chunk_id))

            query_words = _query_words(query)

            def rank(member: SymbolMember) -> tuple[int, int, int, str]:
                name = member.fqn.replace("::", ".").rsplit(".", 1)[-1]
                own = _split_identifier(name)
                overlap = len([t for t in own if t in query_words and len(t) >= 3])
                private = name.startswith("_") and not (
                    name.startswith("__") and name.endswith("__")
                )
                local = 0 if member.chunk_id else 2
                return (
                    -overlap,
                    0 if private else 1 if local == 0 else 2,
                    local,
                    member.fqn,
                )

            members.sort(key=rank)
            return members

        plan = plan_gaps(
            query,
            packed_symbols=packed_symbols,
            packed_chunk_ids=packed_cids,
            pool_chunk_ids=[candidate.chunk_id for candidate in ranked],
            packed_spec_refs=packed_spec_refs,
            members_of=members_of,
            callees_of=callees_of,
            reexport_marks=reexport_marks,
            limits=limits,
        )
        if not plan.triggered:
            return plan, [], ranked

        by_id = {candidate.chunk_id: candidate for candidate in ranked}
        # 补检优先级（TASK-109，实测驱动）：**先补已进包成员的被调用方**，再按相关度。
        #
        # 为什么需要这层排序：G1 的补检目标里有相当一部分在池内是 ``score=0.000``——
        # 它们只靠图扩展（``._invoke`` 的 callee）入池，没有直接通道命中。
        # 纯按分数排时，这些 0 分组会与一堆无关成员混在一起按片段大小争配额，
        # 真正被问的方法（``_should_retry`` / ``_normalize``）拿不到位置。
        #
        # “已进包成员的 callee”是**确定性的调用链闭合依据**（Module/02 §4.4-a 的 calls 边），
        # 也是“调用链断裂”这个缺口的字面含义：``_invoke`` 已在包里，它调用的方法却不在，
        # 补齐它们就是补断链；其余同容器成员排在后面。
        packed_fqns = {
            candidate.symbol_fqn for candidate in packed_candidates if candidate.symbol_fqn
        }
        callees_of_packed: set[str] = set()
        for fqn in packed_fqns:
            for edge in store.edges_for(fqn, kinds=["calls"]):
                if edge.source == fqn:
                    callees_of_packed.add(edge.target)

        def chain_priority(chunk_id: str) -> int:
            candidate = by_id[chunk_id]
            fqn = candidate.symbol_fqn
            if not fqn:
                return 2
            return 0 if fqn in callees_of_packed else 1

        def source_priority(chunk_id: str) -> int:
            """补检来源的确定性分档（0 最优先）——决定配额先给谁。

            为什么需要（实测 langchain LC-21，2026-09-17）：补检总额（``backfill_ratio``
            × hard_cap = 3500）在第 7 条就被吃完，而真正的答案
            ``_chain_model_call_handlers``（90 行 / 1262 token）排在第 9 位。
            吃掉配额的前 7 条里，**3 条是同一个 ``create_agent`` 切片**（G2 文档符号
            引用对着同一符号的三条 spec_references）——它们内容完全重复，却各占一笔预算。

            分档依据是“该来源对回答的不可替代性”：

            - 0：G3 容器成员的被调用方——调用链题的唯一答案（``_chain_*`` / ``_make_*``）；
            - 1：G4 公开导出点——“在哪导出”类题的唯一答案；
            - 2：G1 同容器成员；
            - 3：G2 文档符号引用——它常与其它规则指向**同一个** chunk（本例的 3 条
              ``create_agent``），重复计份；排在最后，让真正的实现函数先拿配额。

            这不改变任何闸门，只改**同一次补检内部的先后**（``plan.chunk_ids`` 原本按
            G1→G2→G3→G4 固定顺序，与本档位恰好相反）。
            """
            reason = plan.reason_for(chunk_id) or ""
            if reason.startswith(GAP_REASON_CALLEE):
                return 0
            if reason.startswith(GAP_REASON_REEXPORT):
                return 1
            if reason.startswith(GAP_REASON_CONTAINER):
                return 2
            return 3

        #: 查询词元（与 ``callees_of`` 内的 ``rank`` 同源：同一个 ``_query_words``）。
        #: 提到函数级只算一次，避免在 ``order_key`` 里每个候选重算。
        query_words = _query_words(query)

        def word_overlap(chunk_id: str) -> int:
            """候选符号名与查询词面的重合度（复用 ``callees_of.rank`` 的同一条规则）。

            为什么需要（实测 langchain LC-21，2026-09-17）：补检目标
            ``_chain_model_call_handlers`` 与同容器的 4 个兄弟同为 G3 来源、同为 tier3、
            score 均为 0.0，原先只能按 span 升序 tie-break，于是 4 个 span 更小的兄弟
            （``_make_model_to_model_edge`` 25 行 … ``_chain_async_tool_call_wrappers`` 62 行）
            排在目标（90 行）之前，累计 2406 token 先把 ``backfill_ratio`` 配额耗掉，
            目标（第 6 位、累计 4927）直接出局。

            而名字里的词面证据能把它们区分开：目标名含查询里的 ``model`` 与 ``call``
            （overlap=2），四个兄弟只含一个（overlap=1）。

            **这不是新启发式**：``callees_of`` 内部的 ``rank``（本文件 :818-838）早在用
            完全相同的 ``_query_words`` + ``_split_identifier`` overlap 判定，当时的注释
            写明理由——``_make_tools_to_model_edge`` 与 ``_chain_*`` 都是私有函数，
            单靠“私有优先”排不出来，必须用词面证据（与 BM25 同源）。本处只是把**同一个
            已存在的原则**补到 ``order_key``（先前那里只有 span 升序，与 callees_of 不一致）。
            """
            fqn = by_id[chunk_id].symbol_fqn
            if not fqn:
                return 0
            name = fqn.replace("::", ".").rsplit(".", 1)[-1]
            return len([t for t in _split_identifier(name) if t in query_words and len(t) >= 3])

        def order_key(chunk_id: str) -> tuple[int, int, int, float, int, str]:
            candidate = by_id[chunk_id]
            span = (
                (candidate.end_line - candidate.start_line + 1)
                if candidate.start_line is not None and candidate.end_line is not None
                else 1 << 30
            )
            # 分层：来源分档 → 调用链闭合 → 词面重合 → 分数 → span → chunk_id。
            # 后三项仍是原有 tie-break（保证与改动前同分候选的相对次序不变）。
            return (
                source_priority(chunk_id),
                chain_priority(chunk_id),
                -word_overlap(chunk_id),
                -candidate.score,
                span,
                chunk_id,
            )

        missing = [
            by_id[chunk_id]
            for chunk_id in sorted(plan.chunk_ids, key=order_key)
            if chunk_id in by_id
        ]
        if not missing:
            return plan, [], ranked
        # 并入同一 rerank（重排全部候选，不插队）：补检候选与首轮候选走**同一个** rerank。
        #
        # 必须**重新收集信号**，不能拿首轮那份复用：``collect_signals`` 的 ``top1_seed_chunk_id``
        # 取自“当前候选的最高分”（``ordered[0]``），而首轮 ``rerank`` 已把 ``score`` 从
        # ``rrf_score`` 改写为最终分。复用旧信号会把 rerank 的图连通特征
        # （``FEATURE_GRAPH_1HOP``，依赖 top-1 seed）锚到一个**旧 top-1**上，
        # 实测使 4 个用例的装填顺序发生无关漂移（hits@5 29 vs 30）。
        merged = rerank([*ranked, *missing], collect_signals(store, query, [*ranked, *missing]))
        backfill = [
            (candidate, plan.reason_for(candidate.chunk_id) or "")
            for candidate in missing
        ]
        return plan, backfill, merged

    # ------------------------------------------------------------------ 扫描状态（本卡扩展）

    def read_manifest(self, project_id: str) -> dict[str, str]:
        """读上次本地扫描的对账状态（缺失/损坏 → 空表 = 全量摄入）。"""
        path = self.project_dir(project_id) / SCAN_MANIFEST_FILENAME
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, Mapping):
            return {}
        return {str(key): str(value) for key, value in files.items()}

    def write_manifest(
        self, project_id: str, root: Path, hashes: Mapping[str, str]
    ) -> None:
        """原子写扫描状态（先写临时文件再 replace，避免半截 json 被当成有效状态）。"""
        directory = self.project_dir(project_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / SCAN_MANIFEST_FILENAME
        payload = {
            "version": 1,
            "root": str(root),
            "scanned_at": int(time.time()),
            "files": dict(sorted(hashes.items())),
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)

    # ------------------------------------------------------------------ 内部

    def _budget(self, max_tokens: int, mode: str = MODE_FAST) -> BudgetConfig:
        """装填预算：调用方给的 ``max_tokens`` 优先，否则用该 mode 的默认档位。

        TASK-MCP-BUDGET：``mode`` 参与选择基准档位（Fast 14K / Deep 16K）。
        调用方显式传的 ``max_tokens`` 仍是硬上限（CLI ``--max-tokens`` 与基准脚本靠它做对照）。
        """
        base = budget_for(mode)
        return base if max_tokens == base.hard_cap else replace(base, hard_cap=max_tokens)

    def _source_for(self, project_id: str):
        root = self._repo_roots.get(project_id)
        return DirectorySource(root) if root is not None else _EmptySource()

    def _ingest(
        self,
        project_id: str,
        changes: ChangeSet,
        *,
        full: bool,
        source: SourceProvider | None = None,
    ) -> IngestReport:
        with self._open_project(project_id) as (store, vectors, provider):
            indexer = Indexer(
                store,
                provider,
                vectors,
                source or self._source_for(project_id),
                embedding_cache=self._embedding_cache(),
            )
            # P1-1：先标 building（让并发查询能看出“索引未就绪”），
            # 成功后再标 ready 并记下期望计数（下次查询据此对账中间态）。
            self._mark_index_state(project_id, "building")
            try:
                report = indexer.full_reparse(changes) if full else indexer.ingest(changes)
            except BaseException as exc:
                self._mark_index_state(
                    project_id,
                    "failed",
                    stage="reparse" if full else "ingest",
                    reason=f"{type(exc).__name__}: {exc}",
                )
                raise
            counts = store.counts()
            self._mark_index_state(
                project_id,
                "ready",
                expected_chunks=counts["chunks"],
                expected_vectors=vectors.count(),
            )
            return report

    def _index_state(self, project_id: str) -> IndexState | None:
        """读本项目的索引状态标记（P1-1/P1-5）；无标记 → ``None``。"""
        return read_index_state(self.project_dir(project_id))

    def _mark_index_state(self, project_id: str, status: IndexStatus, **fields: object) -> None:
        """写索引状态标记（不强求成功：观测不得阻断索引）。"""
        directory = self.project_dir(project_id)
        if status == "building":
            state = building_state(**_chunks_vectors(fields))
        elif status == "ready":
            state = ready_state(**_chunks_vectors(fields))
        else:
            state = failed_state(
                stage=str(fields.get("stage") or "indexing"),
                reason=str(fields.get("reason") or "未知原因"),
            )
        write_index_state(directory, state)

    def _index_state_gap(
        self, project_id: str, store: Store, vectors: VectorStore
    ) -> str | None:
        """索引状态是否要求降级（P1-1 / P1-5）；不需降级 → ``None``。

        三种情况都在这里变成**可见的**降级原因（而不是静默给出可能错的结果）：

        - ``building``：另一进程正在索引，查询可能读到 SQLite 新 / 向量旧的中间态；
        - ``failed``：上次索引中途失败，库可能停在半成品状态；
        - ``ready`` 但对账不一致：期望数与实际数不符，或 ``chunks>0 而 vectors=0``。

        ``ready`` 且对账通过 → ``None``（正常路径不受影响，零额外 SQL 以外开销）。
        """
        state = self._index_state(project_id)
        if state is None:
            return None
        if state.status == "building":
            return (
                "索引正在构建中（index-state=building）：当前结果可能基于未完成的索引，"
                f"阶段={state.stage or 'indexing'}"
            )
        if state.status == "failed":
            return (
                f"上次索引未完成（index-state=failed）：阶段={state.stage or '未知'}，"
                f"原因={state.reason or '未知'}"
            )
        counts = store.counts()
        return state.mismatch_reason(chunks=counts["chunks"], vectors=vectors.count())

    def _embedding_cache(self) -> EmbeddingCache | None:
        """data_root 级的跨项目 embedding 缓存（TASK-111）。

        为什么放在这里：TASK-111 让分支进身份后，同一仓库的每个分支都是独立项目；
        没有共享缓存时"换分支 = 全量重嵌"（实测 lane-c 对 main 零复用，各嵌 5842/5913 个 chunk）。
        缓存按 ``(model_id, content_hash)`` 索引，因此跨分支/跨项目命中同一向量。

        失败一律降级为 ``None``（不建缓存、不阻断索引）——它是优化而非正确性来源。

        ``NOVA_EMBED_CACHE=off`` 可关闭（回归对比 / 受限环境）。
        """
        if os.environ.get(EMBED_CACHE_ENV, "").strip().lower() in {"off", "0", "false", "no"}:
            return None
        try:
            provider = self.provider
            return EmbeddingCache.open(
                self._data_root, provider.profile.model_id, provider.profile.dim
            )
        except Exception as exc:  # noqa: BLE001 - 缓存不可用不得阻断索引
            logger.warning("embedding 缓存不可用，退化为项目内复用：%s", exc)
            return None

    # ------------------------------------------------------------------ 索引位移诊断（P2-7）

    def _project_meta(self, project_id: str) -> dict[str, object] | None:
        """读项目 ``project.json``；不存在/损坏返回 ``None``（诊断信息不得阻断索引）。"""
        path = self.project_dir(project_id) / PROJECT_META_FILENAME
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _record_repo_identity(self, project_id: str, identity: RepoIdentity) -> None:
        """把 D-29 身份的输入（remote / 相对路径 / 分支）记进 ``project.json``。

        为什么需要：分支进身份后，"换分支/换目录 = 换 projectId = 从零重嵌"（TASK-111 的
        已知代价）。记下这三个输入，:meth:`_displacement_warning` 才能在"新项目为空"时
        找回同一仓库的其它分支索引，把一次静默的 4.58M token 全量重嵌变成一条可操作告警。

        纯诊断字段：写失败只记日志，不影响任何索引语义。
        """
        meta = self._project_meta(project_id)
        if meta is None:
            return
        fields = {
            "remote_url": identity.remote_url,
            "repo_path": identity.repo_path,
            "branch": identity.branch,
        }
        if all(meta.get(key) == value for key, value in fields.items()):
            return
        meta.update(fields)
        try:
            (self.project_dir(project_id) / PROJECT_META_FILENAME).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("project.json 写入失败（不影响索引）：%s", exc)

    def _displacement_warning(self, project_id: str, root: str | Path) -> str | None:
        """索引位移告警（P2-7 的"只告警"版本）：当前项目为空、同一仓库的其它分支已有索引。

        不实现"接管旧索引目录"——那会改变 D-29/TASK-111 的冻结口径，属编排者裁决范围。
        这里只保证"不会再静默烧 token"：告警写进 ``IngestReport.warnings``（CLI 会打印）。
        """
        identity = repo_identity(root)
        self._record_repo_identity(project_id, identity)
        if identity.remote_url is None:
            return None
        with Store.open(self.project_dir(project_id)) as store:
            if store.counts()["files"] > 0:
                return None  # 已有索引：本次是增量，不存在全量重嵌
        projects_root = self._data_root / PROJECTS_DIRNAME
        if not projects_root.is_dir():
            return None
        siblings: list[str] = []
        for meta_path in sorted(projects_root.glob(f"*/{PROJECT_META_FILENAME}")):
            if meta_path.parent.name == project_id:
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(meta, dict) or meta.get("remote_url") != identity.remote_url:
                continue
            if (meta.get("repo_path") or "") != identity.repo_path:
                continue
            siblings.append(str(meta.get("display_name") or meta_path.parent.name))
        if not siblings:
            return None
        return (
            "检测到同一仓库的其它分支/目录已有索引（"
            + "、".join(siblings[:5])
            + "）：当前分支身份不同（D-29 分支隔离），本次将从零重建该分支的索引。"
            "向量通常能由内容寻址缓存命中，不额外消耗 embedding 额度"
            "（实测 2729 chunk → 0 请求）；"
            "只有换数据根 / 换模型或维度 / 清掉缓存时才会重新付费，代价主要是本地重解析时间。"
        )

    @contextmanager
    def _open_project(
        self, project_id: str
    ) -> Iterator[tuple[Store, VectorStore, EmbeddingProvider]]:
        directory = self.project_dir(project_id)
        directory.mkdir(parents=True, exist_ok=True)
        store = Store.open(directory)
        try:
            provider = self.provider
            vectors = VectorStore.open(directory, provider.profile.dim)
        except BaseException:
            store.close()
            raise
        # P2-1：缓存 key 必须带模型身份；provider 可在生命周期内被替换，因此每次开项目都重绑。
        self._bind_query_cache_identity(provider)
        try:
            yield store, vectors, provider
        finally:
            vectors.close()
            store.close()


# ---------------------------------------------------------------------------
# 本地扫描（ChangeSet 构造）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ScanResult:
    changes: ChangeSet
    hashes: dict[str, str] = field(default_factory=dict)
    errors: tuple[str, ...] = ()


def plan_scan(root: str | Path, previous: Mapping[str, str]) -> _ScanResult:
    """仓库目录 + 上次扫描状态 → 增量 ``ChangeSet``（Phase 2 client 的同步逻辑本地版）。

    - ``added`` = 上次状态里没有的路径；``modified`` = content_hash 变化的路径；
    - ``deleted`` = 上次有、本次磁盘上没有的路径；
    - **未变化的文件不进 ChangeSet**（不重解析、不重嵌入）；
    - 读取失败的文件计入 ``errors`` 并从状态中剔除（下次会重试）。
    """
    source = DirectorySource(root)
    hashes: dict[str, str] = {}
    added: list[BlobInput] = []
    modified: list[BlobInput] = []
    errors: list[str] = []
    for path in source.list_files():
        try:
            data = source.read(path)
        except Exception as exc:  # noqa: BLE001 - 单文件读失败隔离（同 Indexer._safe_read）
            # TASK-036 §B：此前只捕 OSError，而 DirectorySource 对含反斜杠的文件名抛
            # SourcePathError（ValueError）——一个这样的文件会让整次 ingest 中止。
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        digest = file_content_hash(data)
        hashes[path] = digest
        if previous.get(path) == digest:
            continue
        blob = BlobInput(path=path, content=data, blob_hash=blob_hash(path, data))
        (added if path not in previous else modified).append(blob)
    deleted = tuple(sorted(set(previous) - set(hashes)))
    return _ScanResult(
        changes=ChangeSet(added=tuple(added), modified=tuple(modified), deleted=deleted),
        hashes=hashes,
        errors=tuple(errors),
    )


#: 编译期自证：``Engine`` 的方法面覆盖 CF-07（``interfaces.ContextEngine``）。
_ENGINE_PROTOCOL: type[ContextEngine] = Engine


# ---------------------------------------------------------------------------
# 查询覆盖率（TASK-101 §D：答了但答偏，要如实报出来）
# ---------------------------------------------------------------------------

#: 覆盖率闸门：低于它即认为"包内证据没接住查询的具体关键词"。
QUERY_COVERAGE_FLOOR = 0.34
#: 参与统计的最少内容词数（短查询统计噪声大，不报缺口）。
QUERY_COVERAGE_MIN_TOKENS = 4
#: message 里列出的缺失词上限。
QUERY_COVERAGE_LISTED = 6
#: 缺口 code（CF-03 的 ``MissingEvidence.code`` 是自由字符串，无需改契约）。
QUERY_COVERAGE_CODE = "query_partially_matched"

#: 问句骨架/虚词（不算"内容词"）。
_COVERAGE_STOPWORDS = frozenset(
    {
        "这个", "那个", "这些", "那些", "什么", "怎么", "如何", "哪里", "哪个", "哪些",
        "是否", "可以", "需要", "请问", "一下", "以及", "分别", "具体", "多少",
        "一次", "一共", "几个", "还有", "通过", "用于", "它们", "其中",
        "the", "and", "for", "what", "which", "where", "how", "does", "are", "is", "was",
    }
)


def _content_tokens(query: str) -> list[str]:
    """查询的"内容词"（CJK 分词 + 去停用词/单字/纯标点）。

    只做**诊断信号**（TASK-101 §D），不参与检索与排序，因此可以比检索侧更激进地过滤：
    问句骨架（"哪个/在哪里/多少"）在证据里必然缺失，算进去只会把覆盖率压成人人偏低。
    """
    from nova_core.text import segment

    tokens: list[str] = []
    for raw in segment(query).split():
        token = raw.strip()
        if len(token) < 2 or not any(char.isalnum() for char in token):
            continue
        if token.lower() in _COVERAGE_STOPWORDS:
            continue
        tokens.append(token.lower())
    return list(dict.fromkeys(tokens))


def _with_query_coverage(query: str, pack: ContextPack) -> ContextPack:
    """包内证据对查询内容词的覆盖率不足 → 追加 ``query_partially_matched`` 缺口。

    为什么需要（真实客户反馈）：``confidence`` / ``answerable`` 奖励的是**多通道共识**，
    因此"主题词被召回、但查询里的具体指纹（符号名/字面量/文件名）一个都没进包"时，
    NOVA 依然报 ``answerable=true / medium``，Agent 无从得知自己拿到的是背景材料。
    真实代价：一个外部 AI 客户据此写下"这个仓库没有 main() 入口"（实际它在 ``pyproject.toml`` 里）。

    只增 ``missing_evidence``（与已有 ``next_queries`` 并列），不改 ``answerable``/``confidence``：
    那两个字段的判定参数属 R22 冻结口径，不在本卡职权内。
    """
    tokens = _content_tokens(query)
    if len(tokens) < QUERY_COVERAGE_MIN_TOKENS or not pack.answerable:
        return pack
    body = "\n".join(item.content for item in [*pack.evidence, *pack.docs]).lower()
    missing = [token for token in tokens if token not in body]
    if not missing or (len(tokens) - len(missing)) / len(tokens) >= QUERY_COVERAGE_FLOOR:
        return pack
    listed = ", ".join(missing[:QUERY_COVERAGE_LISTED])
    more = " 等" if len(missing) > QUERY_COVERAGE_LISTED else ""
    pack.missing_evidence.append(
        MissingEvidence(
            code=QUERY_COVERAGE_CODE,
            message=(
                f"查询里有 {len(missing)}/{len(tokens)} 个关键词在返回的上下文里找不到"
                f"（{listed}{more}）：包内证据只覆盖了查询的主题，"
                "不代表这些关键词在仓库里不存在——请换更具体的符号名/文件路径/配置键再查一次，"
                "或先用 grep 核对再下结论。"
            ),
            symbol=missing[0],
        )
    )
    if not pack.next_queries:
        # ``_next_queries`` 只在 answerable=false 时生成；本缺口是"答了但答偏"，
        # 补一条确定性的自愈查询（不动已有列表）。
        pack.next_queries = [f"{missing[0]} 的实现位置"]
    return pack
