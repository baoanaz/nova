"""``nova-service`` 命令行入口（TASK-030 建；TASK-034 §D 追加 ``local``）。

风格与 core CLI（``nova_core/cli/app.py``）一致：argparse + ``main() -> int``。

```text
nova-service [serve] [--host H] [--port N] [--data-root PATH] [--log-level LEVEL] [--reload]
nova-service local --repo PATH [--data-root PATH] [--host H] [--port N]
```

``local``（TASK-034 §D）一条命令完成"本机自己用"：强制本地模式 → ``resolve_repo`` 拿 projectId
→ 起后台索引（**不等索引完成**，立刻开始监听）→ 打印可读的就绪信息 + 下一步。
``serve`` 是不带仓库的纯服务模式（客户端上传路径）。**不带子命令的旧形式**
（``nova-service --port 8787``）仍按 ``serve`` 解析（公共参数在根解析器上同样可用）——
它已在 TASK-030/035 的验收与文档里出现，属向下兼容而不是新语义。

- 参数缺省值来自 :class:`~nova_service.config.Settings`（即 ``NOVA_DATA_ROOT`` /
  ``NOVA_LOCAL_RESCAN_INTERVAL`` 等环境变量），命令行显式给出时优先；
- ``--reload`` 是开发便利开关：uvicorn 的 reload 需要 import string，此时子进程从
  **环境变量**重建配置（``NOVA_DATA_ROOT`` 由本函数写入），因此 ``--log-level`` 只作用于
  uvicorn 自身日志，服务自身 JSON 日志沿用默认 ``info``；
- uvicorn 自带 access log 关闭（``access_log=False``）：访问日志由 middleware 以 JSON 输出，
  避免 stderr 上出现两套格式、也避免 uvicorn 记录带 query string 的 URL。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import uvicorn
from nova_core.preload import preload_runtime

from nova_service.app import create_app
from nova_service.cli_hint import format_snippets, mcp_url
from nova_service.config import (
    DATA_ROOT_ENV,
    LOCAL_RESCAN_INTERVAL_ENV,
    Settings,
)
from nova_service.metadb import MetaDB
from nova_service.runtime import AttachResult, EngineManager

__all__ = ["build_parser", "main"]

PROG = "nova-service"
LOG_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")
#: 子命令（不写子命令时默认 serve：向下兼容 TASK-030/035 的调用形式）。
#: ``mcp-config``（TASK-040）只输出编辑器配置片段，不起服务。
SUBCOMMANDS = ("serve", "local", "mcp-config")
#: 关闭启动预加载的开关（``0`` / ``false`` / ``no`` / ``off``）；默认开启。
PRELOAD_ENV = "NOVA_PRELOAD"


def _preload() -> None:
    """长驻服务启动时预加载 lancedb / jieba / tree-sitter（见 ``nova_core.preload``）。

    放在监听之前同步执行：启动多花几秒，换来第一次索引不再承担这些一次性成本。
    """
    if os.environ.get(PRELOAD_ENV, "").strip().lower() in ("0", "false", "no", "off"):
        return
    preload_runtime()


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI（根参数 + ``serve`` / ``local`` 子命令）。

    根解析器也带一份公共参数，且子解析器用 ``default=SUPPRESS``：这样两种写法都合法——
    旧形式 ``nova-service --port 8790``（TASK-030/035 的验收与文档在用）与
    ``nova-service local --repo X --port 8790``；子解析器不会用自己的默认值覆盖根层的值。
    """
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="nova 服务化外壳（M2a 本地单用户模式：无鉴权、绑 127.0.0.1）",
    )
    _add_common_arguments(parser)
    subparsers = parser.add_subparsers(
        dest="command", metavar="{" + ",".join(SUBCOMMANDS) + "}"
    )
    for name, help_text in (
        ("serve", "纯服务模式：不绑本地仓库（客户端上传路径）"),
        ("local", "本地单用户：绑定一个仓库 + 后台索引 + 起服务（一条命令）"),
        ("mcp-config", "只输出编辑器（Cursor 等）的 MCP 配置片段，不起服务"),
    ):
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        _add_common_arguments(sub, suppress=True)
        if name == "local":
            sub.add_argument(
                "--repo",
                type=Path,
                required=True,
                help="要绑定的仓库/目录绝对路径（不存在或不是目录时启动失败）",
            )
            sub.add_argument("--name", default="", help="项目显示名（缺省用目录名）")
            sub.add_argument(
                "--no-index",
                action="store_true",
                help="只绑定不起后台索引（调试用；之后可 POST /api/projects/{id}/rescan）",
            )
    return parser


def _add_common_arguments(
    parser: argparse.ArgumentParser, *, suppress: bool = False
) -> None:
    """公共参数（``suppress=True`` 时不写默认值：让根层的值不被子命令覆盖）。"""
    absent = argparse.SUPPRESS if suppress else None
    parser.add_argument("--host", default=absent, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=absent, help="监听端口（默认 8787）")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=absent,
        help=f"数据根目录（默认 ${DATA_ROOT_ENV} 或 ~/.nova）",
    )
    parser.add_argument(
        "--log-level",
        default=absent,
        choices=LOG_LEVELS,
        help="日志级别（默认 info）",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="代码变更自动重启（开发用）",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """启动 uvicorn（前台阻塞）。"""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    base = Settings.from_env()
    settings = replace(
        base,
        host=args.host or base.host,
        port=args.port or base.port,
        data_root=args.data_root or base.data_root,
        log_level=args.log_level or base.log_level,
    )
    if args.command == "mcp-config":  # TASK-040：只打配置片段，不起服务、不碰 core
        print(format_snippets(settings.port, host=settings.host), flush=True)
        return 0
    if args.command == "local":
        return _run_local(args, base, settings)
    if args.reload:
        os.environ[DATA_ROOT_ENV] = str(settings.data_root)
        print(format_snippets(settings.port, host=settings.host), flush=True)
        uvicorn.run(
            "nova_service.app:create_app",
            factory=True,
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level,
            access_log=False,
            reload=True,
        )
        return 0
    print(format_snippets(settings.port, host=settings.host), flush=True)
    _preload()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        access_log=False,
    )
    return 0


def _run_local(args: argparse.Namespace, _base: Settings, settings: Settings) -> int:
    """``nova-service local --repo``：绑定仓库 + 后台索引 + 立刻监听（TASK-034 §D）。"""
    from nova_service.indexer import LocalRootError

    settings = replace(settings, local_mode=True)
    app = create_app(settings)
    if args.reload:  # reload 需 import string：绑仓库/索引无法在 reload 子进程里保留
        print(
            "[警告] --reload 下后台索引不会自动启动（子进程重新导入应用）："
            "去掉 --reload，或显式用 --no-index + POST /api/projects/{id}/rescan",
            file=sys.stderr,
        )
    _preload()  # attach_local 会立即开始后台索引，须在其之前
    manager = EngineManager.open(settings.data_root)
    # 本地模式也记索引历史（TASK-062）：用户看得到每次索引的耗时与成功/失败，
    # 而账户/API Key 仍不可用（无账户体系，R34）。
    manager.attach_meta_db(MetaDB.open(settings.meta_db_path))
    app.state.meta_db = manager.meta_db
    app.state.engine_manager = manager
    try:
        attached = manager.attach_local(
            args.repo, display_name=args.name, index=not args.no_index
        )
    except LocalRootError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    _print_ready(settings, attached, indexed=not args.no_index)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        access_log=False,
    )
    return 0


def _print_ready(settings: Settings, attached: AttachResult, *, indexed: bool) -> None:
    """打印人类可读的就绪信息（stdout；不含任何 secret）。

    ``flush=True`` 是必须的：``nova-service local`` 随后会阻塞在 ``uvicorn.run``，
    若走 stdout 缓冲（nohup/重定向到文件时的默认），用户会先看到服务日志、
    要等缓冲区凑满才看到就绪信息。
    """
    project_id = attached.project_id
    progress = attached.index_progress
    lines = [
        f"nova-service local 已启动（{settings.host}:{settings.port}）",
        f"  projectId : {project_id}",
        f"  dataRoot  : {settings.data_root}",
        f"  repo      : {attached.root}",
    ]
    if attached.is_git:
        lines.append("  身份      : git remote（D-29）")
    else:
        lines.append(
            "  身份      : 非 git 仓库 → 绝对路径 hash（D-29）；换路径/换机器 projectId 会变"
        )
    if indexed:
        lines.append(
            "  索引      : 后台进行中（state="
            f"{progress.state}，已处理 {progress.processed_files}/{progress.total_files} 个文件）"
        )
        lines.append(
            f"             进度：GET http://{settings.host}:{settings.port}"
            f"/api/projects/{project_id} ｜ 服务现在已可响应，不必等索引完成"
        )
    else:
        lines.append("  索引      : 未启动（--no-index）；需要时 POST /api/projects/{id}/rescan")
    lines.append(f"  检索接口  : POST http://{settings.host}:{settings.port}/api/query/search")
    lines.append(f"  MCP       : {mcp_url(settings.port, host=settings.host)}（Streamable HTTP）")
    lines.append(
        f"  懒重扫    : 每 {settings.local_rescan_interval_s:g}s 一次"
        f"（0=禁用，{LOCAL_RESCAN_INTERVAL_ENV} 可改）"
    )
    print("\n".join(lines), flush=True)
    # 编辑器配置片段单独一段（TASK-040）：用户复制粘贴即可，不用去猜路径形态。
    print("\n" + format_snippets(settings.port, host=settings.host), flush=True)


if __name__ == "__main__":  # pragma: no cover - 手工入口
    raise SystemExit(main())
