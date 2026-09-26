"""zace-core CLI（TASK-013 §B）：M1 验收的唯一人工入口。

设计依据：``docs/design/Module/06-服务化与部署.md`` §1（core 是纯库；CLI 是**调试形态**，
不是产品面；Phase 2 的 service 复用同一 :class:`~zace_core.engine.Engine`）。

四个子命令（card 冻结形态）：

```text
zace-core ingest --repo <PATH> [--data <ROOT>] [--full]     # 全量/增量索引（--full 忽略增量）
zace-core search "<query>" --repo <PATH> [--data <ROOT>] [--max-tokens N] [--json]
zace-core status --repo <PATH> [--data <ROOT>] [--json]
zace-core eval --golden <DIR|FILE> --repo <PATH> --report <FILE>
```

退出码：``0`` 成功 / ``1`` 运行错误 / ``2`` 参数错误（argparse 原生）。错误信息只含路径与
异常摘要，不打印任何 token / 凭据。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from zace_core.cli.eval import GoldenError, load_cases, render_report, run_golden, write_report
from zace_core.contextpack import render_markdown, to_json
from zace_core.engine import Engine, EngineError, resolve_data_root
from zace_core.retrieval.qcache import CachedOnlyProvider, PersistentQueryVectorCache
from zace_core.storage import Store
from zace_core.types import ProjectHandle

__all__ = ["EXIT_ERROR", "EXIT_OK", "EXIT_USAGE", "build_parser", "main"]

PROG = "zace-core"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

#: 默认检索预算（token；``--max-tokens`` 透传 ContextPack 组装预算 D-23）。
DEFAULT_MAX_TOKENS = 10_000

#: 引擎工厂（测试注入 fake embedding provider 的接缝）。
EngineFactory = Callable[[Path], Engine]


def main(argv: Sequence[str] | None = None, *, engine_factory: EngineFactory | None = None) -> int:
    """CLI 入口（``zace-core`` console script 指向本函数）。"""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    factory: EngineFactory = engine_factory or Engine.open
    try:
        return args.handler(args, factory)
    except (EngineError, GoldenError) as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (OSError, RuntimeError, ValueError) as exc:  # 运行期故障（含 embedding/lancedb）
        print(f"{PROG}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        print(f"{PROG}: 已中断", file=sys.stderr)
        return EXIT_ERROR


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="zace 核心引擎调试入口（索引 / 检索 / 状态 / golden 评估）",
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, metavar="{ingest,search,status,eval}"
    )

    ingest = subparsers.add_parser("ingest", help="全量/增量索引本地仓库")
    _add_common(ingest)
    ingest.add_argument("--full", action="store_true", help="忽略增量：全量重解析 + 重建向量表")
    ingest.set_defaults(handler=_cmd_ingest)

    search = subparsers.add_parser("search", help="检索并输出 ContextPack")
    search.add_argument("query", help="自然语言查询（可含反引号包裹的符号/路径）")
    _add_common(search, repo_required=False)
    search.add_argument(
        "--max-tokens",
        type=_positive_int,
        default=DEFAULT_MAX_TOKENS,
        help=f"ContextPack 预算上限（默认 {DEFAULT_MAX_TOKENS}）",
    )
    search.add_argument("--json", action="store_true", help="输出 CF-03 JSON（默认 Markdown）")
    _add_vector_cache_args(search)
    search.set_defaults(handler=_cmd_search)

    status = subparsers.add_parser(
        "status", help="输出索引现状（files/chunks/symbols/edges/freshness）"
    )
    _add_common(status, repo_required=False)
    status.add_argument("--json", action="store_true", help="输出 JSON（字段同 SyncStatus）")
    status.set_defaults(handler=_cmd_status)

    evaluate = subparsers.add_parser("eval", help="跑 golden set 并写指标报告")
    _add_common(evaluate, repo_required=False)
    evaluate.add_argument("--golden", type=Path, required=True, help="golden 文件或目录（*.jsonl）")
    evaluate.add_argument("--report", type=Path, required=True, help="Markdown 报告输出路径")
    # TASK-MCP-BUDGET：允许跑分时指定装填预算——默认值调档（10K→14K）后，
    # 必须能拿旧档位做**同一索引、同一题目**的对照跑分，否则无法区分
    # “预算变化的效果”与“本次改动的效果”。默认仍是 CLI 的 DEFAULT_MAX_TOKENS。
    evaluate.add_argument(
        "--max-tokens",
        type=_positive_int,
        default=None,
        help=f"ContextPack 装填预算上限（默认 {DEFAULT_MAX_TOKENS}）",
    )
    _add_vector_cache_args(evaluate)
    evaluate.set_defaults(handler=_cmd_eval)
    return parser


def _add_common(parser: argparse.ArgumentParser, *, repo_required: bool = True) -> None:
    parser.add_argument(
        "--repo",
        type=Path,
        required=repo_required,
        help=(
            "本地仓库路径。**给了 --project-id 时可以省略**（TASK-101 §G）——eval/search 只读"
            "索引与向量库，不需要靶场源码：没有靶场代码的机器也能跑分。"
        ),
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help=(
            "直接指定 projectId，跳过 D-29 身份计算与 project.json 核验"
            "（benchmark 放行口，TASK-101 §E）：把一个**预建好的索引**（含 embedding）"
            "挂到任意 checkout 路径上复现跑分，不必重新索引。"
            "索引缺失/维度不符时**如实报错**，不静默重建。"
        ),
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help="数据根（默认 $ZACE_DATA_ROOT，否则 ~/.zace）",
    )


def _add_vector_cache_args(parser: argparse.ArgumentParser) -> None:
    """``--vector-cache`` / ``--replay``（TASK-101 §F）：eval 与 search **共用**。

    为什么要抽出来共用：上一版只给 ``eval`` 加了 ``--replay``，于是无 key 的机器连
    "随便问一句"（``search``）都做不到——实测直接抛维度不匹配（期望 384 / 实际 1024），
    因为"以索引指纹为准"只发生在 replay 路径里。共用后两个命令的离线语义完全一致。
    """
    parser.add_argument(
        "--vector-cache",
        type=Path,
        default=None,
        help=(
            "查询向量侧车文件：存在则**复用**其中的 query 向量，跑完后把新增的写回。"
            "用于跨主机/离线——有 key 的机器预热一次，其他机器带上即可复现同一口径。"
            "**注意**：它是缓存、不是嵌入后端，只覆盖预热过的 query。"
        ),
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        help=(
            "离线回放：只用 --vector-cache 里的向量，不调用任何 embedding 后端，"
            "并以**索引里的指纹**为准（因此不需要 key、不需要本地模型）。"
            "未预热的 query 会如实走'向量通道降级'，不会静默降级。需同时给 --vector-cache。"
        ),
    )


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"必须是整数：{raw!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"必须为正整数：{raw!r}")
    return value


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def _engine(
    args: argparse.Namespace,
    factory: EngineFactory,
    *,
    query_cache: PersistentQueryVectorCache | None = None,
) -> Engine:
    engine = factory(resolve_data_root(args.data))
    if query_cache is not None:
        engine.set_query_cache(query_cache)
    return engine


def _query_cache(args: argparse.Namespace) -> PersistentQueryVectorCache | None:
    """``--vector-cache`` → 侧车缓存（未指定则 ``None`` = 使用进程内 60s 缓存）。

    这里只负责"能不能读"：模型/维度自证字段在 :meth:`EngineManager` 侧比对，
    因为 profile 要等 engine 的 provider 就绪后才拿得到。
    """
    path = getattr(args, "vector_cache", None)
    if path is None:
        return None
    cache = PersistentQueryVectorCache(path)
    if cache.load() == 0 and not path.is_file():
        print(f"{PROG}: 提示：向量侧车文件尚不存在，本次将新建：{path}", file=sys.stderr)
    return cache


def _index_embedding_fingerprint(engine: Engine, project_id: str) -> tuple[str, int]:
    """从**索引自身**读 embedding 指纹（``index_config``），返回 ``(model_id, dim)``。

    为什么以索引为准而不是环境变量：``--replay`` 要在**没有 key、没有本地模型**的主机上跑，
    此时 ``EmbeddingConfig.from_env()`` 会回落到默认本地模型（实测 dim=384）并与索引
    （dim=1024）冲突，整轮跑分直接失败。索引里的指纹才是"这批向量是谁算的"的权威来源
    （它由 D-07 指纹机制维护，换模型会触发重嵌）。
    """
    with Store.open(engine.project_dir(project_id)) as store:
        model = store.get_config("embedding_model")
        raw_dim = store.get_config("embedding_dim")
    if not model or not raw_dim:
        raise EngineError(
            f"索引缺少 embedding 指纹（project={project_id}）：该索引不是本版本建立的，"
            "无法离线回放。请在有 key 的机器上重新 ingest 后分发。"
        )
    return model, int(raw_dim)


def _sync_cache_identity(
    engine: Engine, cache: PersistentQueryVectorCache, *, project_id: str, replay: bool = False
) -> None:
    """校验侧车文件与索引的 embedding 指纹，并决定本次用**真实 provider** 还是**离线 provider**。

    判定顺序（每步都实测验过）：

    1. 指数以**索引**为准：``(model, dim)`` 从 ``index_config`` 读，不读环境变量
       （目标机 env 会回落到默认本地模型：实测 384 维 vs 索引 1024 维）；
    2. 侧车自证字段必须与索引一致，否则**拒绝**（拿别的模型的向量算 cosine，指标会莫名变差）；
    3. 选 provider：
       - ``--replay`` → 强制离线（不需要 key、不需要本地模型）；
       - 否则尝试真实 provider：**配置维度与索引一致**→ 用它（正常路径）；
       - 若真实 provider 取不到（无 key、模型缺失）或维度与索引不符 → **自动切离线**并打印提示。
         为什么自动切而不是报错：无 key 的机器上"随便问一句"（``search``）本该可用——
         上一版只让 ``--replay`` 走指纹路径，结果无 key 的 ``search`` 直接抛
         ``DimensionMismatchError``（真实缺陷，已由本次修正）。
    """
    model, dim = _index_embedding_fingerprint(engine, project_id)
    mismatched = cache.identity_mismatch(model=model, dim=dim)
    if mismatched:
        raise EngineError(
            f"向量侧车文件与索引的 embedding 不一致（{mismatched}）："
            f"索引为 {model} / {dim} 维。请用同一配置预热，不要混用不同模型的向量。"
        )
    cache.bind_identity(model=model, dim=dim)

    def _offline(reason: str) -> None:
        engine.set_provider(CachedOnlyProvider(cache, model=model, dim=dim))
        print(
            f"{PROG}: {reason}；已切到离线回放（只用侧车向量，未预热的查询会降级）",
            file=sys.stderr,
        )

    if replay:
        _offline("--replay")
        return
    try:
        real_dim = engine.provider.profile.dim
    except EngineError as exc:  # 无 key / 本地模型缺失
        _offline(f"embedding provider 不可用（{exc}）")
        return
    if real_dim != dim:
        _offline(f"当前配置 {real_dim} 维、索引 {dim} 维（{model}）")


def _resolved_repo(args: argparse.Namespace, *, required: bool = True) -> Path | None:
    """``--repo`` → 绝对路径；``--project-id`` 模式下允许省略（返回 ``None``）。

    为什么可以省略（TASK-101 §G）：``eval`` / ``search`` 只读索引与向量库
    （``index.db`` + ``vectors/``），**不需要靶场源码**——于是"没有靶场代码的机器"也能跑分，
    这正是跨主机复现要的形态（靶场代码本身是另一套 147M 的检出）。
    """
    raw = getattr(args, "repo", None)
    if raw is None:
        if required:
            raise EngineError("缺少 --repo（本项目未绑定本地仓库）")
        return None
    repo = Path(raw).expanduser()
    if not repo.is_dir():
        raise EngineError(f"--repo 不是目录：{repo}")
    return repo.resolve()


def _resolve_target(
    engine: Engine, args: argparse.Namespace, *, repo_required: bool = True
) -> tuple[ProjectHandle, object]:
    """``--repo``(+``--project-id``) → 项目句柄（TASK-101 §E/§G）。

    三种组合：

    | 给了 | 行为 |
    |---|---|
    | 只有 ``--repo`` | 走 :meth:`Engine.resolve_repo`（D-29 身份），生产语义不变 |
    | ``--project-id`` + ``--repo`` | 直连该 id 并把仓库绑到它（拿旧 embedding 跑新代码） |
    | 只有 ``--project-id`` | 直连该 id、**不绑定仓库**（没有靶场代码的机器跑分） |
    """
    project_id = getattr(args, "project_id", None)
    if project_id:
        repo = _resolved_repo(args, required=False)
        if repo is not None:
            engine.bind_repo(str(project_id), repo)
        return ProjectHandle(project_id=str(project_id), created=False), None
    return engine.resolve_repo(_resolved_repo(args, required=repo_required))


def _cmd_ingest(args: argparse.Namespace, factory: EngineFactory) -> int:
    engine = _engine(args, factory)
    repo = _resolved_repo(args)
    handle, identity = engine.resolve_repo(repo)
    started = time.perf_counter()
    report = engine.ingest_repo(handle.project_id, repo, full=args.full)
    elapsed = time.perf_counter() - started

    print(f"project: {handle.project_id}{' (created)' if handle.created else ''}")
    print(f"repo: {repo}")
    if identity.remote_url:
        print(f"identity: git remote {identity.remote_url} (path {identity.repo_path or '.'})")
    else:
        print("identity: 无 git remote（按绝对路径 hash）")
    mode = "full_reparse" if args.full else "incremental"
    print(f"mode: {mode} (invalidation={report.invalidation.value})")
    print(
        f"files: added={report.added} modified={report.modified} "
        f"deleted={report.deleted} parsed={report.files_parsed}"
    )
    print(
        f"chunks: new={report.chunks_new} reused={report.chunks_reused} "
        f"removed={report.chunks_removed} deduped={report.chunks_deduped}"
    )
    print(f"vectors: upserted={report.vectors_upserted} deleted={report.vectors_deleted}")
    print(
        f"graph: edges_retargeted={report.edges_retargeted} "
        f"unresolved_resolved={report.unresolved_resolved} spec_refs={report.spec_refs} "
        f"ambiguous={report.ambiguous_refs}"
    )
    if report.skipped_files:
        print(f"skipped: {len(report.skipped_files)} 个二进制/不可解码文件")
    if report.orphan_files:
        print(
            f"warning: {len(report.orphan_files)} 个删除文件的向量无法枚举"
            "（跨进程删除留孤儿，下一次全量重建清理）",
            file=sys.stderr,
        )
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for warning in report.errors[:5]:
        print(f"warning: {warning}", file=sys.stderr)
    if len(report.errors) > 5:
        print(f"warning: 另有 {len(report.errors) - 5} 条解析/读取告警", file=sys.stderr)
    print(f"elapsed: {elapsed:.1f}s")
    return EXIT_OK


def _cmd_search(args: argparse.Namespace, factory: EngineFactory) -> int:
    cache = _query_cache(args)
    engine = _engine(args, factory, query_cache=cache)
    handle, _identity = _resolve_target(engine, args, repo_required=False)
    if cache is not None:
        _sync_cache_identity(
            engine,
            cache,
            project_id=handle.project_id,
            replay=bool(getattr(args, "replay", False)),
        )
    trace = engine.search_with_trace(handle.project_id, args.query, args.max_tokens)
    if trace.degraded:
        print(f"warning: {trace.degraded_reason}", file=sys.stderr)
    if args.json:
        print(json.dumps(to_json(trace.pack), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(trace.pack))
    return EXIT_OK


def _cmd_status(args: argparse.Namespace, factory: EngineFactory) -> int:
    engine = _engine(args, factory)
    repo = _resolved_repo(args)
    handle, _identity = _resolve_target(engine, args, repo_required=False)
    status = engine.sync_status(handle.project_id)
    if args.json:
        print(json.dumps(dataclasses.asdict(status), ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"project: {status.project_id}")
    print(f"repo: {repo if repo is not None else '(未绑定：--project-id 模式)'}")
    print(f"files: {status.files_indexed}")
    print(f"chunks: {status.chunks}")
    print(f"symbols: {status.symbols}")
    print(f"edges: {status.edges}")
    print(f"pending_jobs: {status.pending_jobs}")
    print(f"freshness: {_freshness_label(status.last_indexed_at)}")
    return EXIT_OK


def _cmd_eval(args: argparse.Namespace, factory: EngineFactory) -> int:
    cache = _query_cache(args)
    engine = _engine(args, factory, query_cache=cache)
    repo = _resolved_repo(args, required=False)
    handle, _identity = _resolve_target(engine, args, repo_required=False)
    if cache is not None:
        _sync_cache_identity(
            engine,
            cache,
            project_id=handle.project_id,
            replay=bool(getattr(args, "replay", False)),
        )
    status = engine.sync_status(handle.project_id)
    if status.files_indexed == 0:
        print(
            f"{PROG}: 项目尚未索引（{handle.project_id}）："
            f"先运行 `{PROG} ingest --repo <靶场路径>`（或用 --data 指向已解包的索引）。",
            file=sys.stderr,
        )
        return EXIT_ERROR
    cases = load_cases(args.golden)
    if not cases:
        raise GoldenError(f"golden 集为空：{args.golden}")
    max_tokens = DEFAULT_MAX_TOKENS if args.max_tokens is None else args.max_tokens
    report = run_golden(
        cases,
        lambda query: engine.search_with_trace(handle.project_id, query, max_tokens),
        golden=str(args.golden),
        repo=str(repo) if repo is not None else "(未绑定仓库：--project-id 模式)",
        project_id=handle.project_id,
        max_tokens=max_tokens,
    )
    if cache is not None:
        # 预热：把本次取到的 query 向量写回侧车文件（供其他主机/离线回放复用）。
        total = cache.put_all()
        print(
            f"vector-cache: {cache.path}（共 {total} 条；本次命中 {cache.hits} / "
            f"未命中 {cache.misses}）",
            file=sys.stderr,
        )
    written = write_report(report, args.report)
    print(render_report(report), end="")
    print(f"report: {written}")
    return EXIT_OK


def _freshness_label(indexed_at: int | None) -> str:
    if indexed_at is None:
        return "unknown（尚无索引）"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(indexed_at))
    age = max(int(time.time()) - indexed_at, 0)
    return f"{stamp}（{age}s ago）"


if __name__ == "__main__":  # pragma: no cover - 直接运行（python -m zace_core.cli.app）
    sys.exit(main())
