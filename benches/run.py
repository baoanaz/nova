#!/usr/bin/env python3
"""benchmark 统一入口：靶场名解析 + ``nova-core eval`` 参数透传。

两种用法：

```bash
# ① 只给靶场名（推荐）：golden / projectId 从 benches/targets.json 解析
uv run python benches/run.py --target langchain-v1 --data ~/.nova/bench \
  --report benches/results/<name>.md --vector-cache <侧车> --replay
uv run python benches/run.py --list-targets

# ② 直接给全套参数（等价于 nova-core eval；历史行为不变）
uv run python benches/run.py --golden benches/golden/nova --repo . \
  --data ~/.nova/bench --report benches/results/<name>.md
```

命中判定与指标口径**全部**在 ``nova_core.cli.eval``（TASK-013）；本文件只做两件事：
（a）参数透传，（b）把靶场名展开成 ``--golden`` / ``--project-id``。
**不要**在这里实现第二套判定——那会让跨主机口径分叉（``benches/README.md`` 的"同一用例集
在不同机器上复现同一口径"）。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: 仓库根（本文件的上一级）。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "benches") not in sys.path:
    sys.path.insert(0, str(ROOT / "benches"))

from targets import (  # noqa: E402  （依赖上面的 sys.path，顺序不能动）
    TargetError,
    build_eval_args,
    describe_targets,
    load_targets,
    provenance,
    resolve_target,
    split_target_arg,
)

#: 靶场展开需要接管的参数（值型 / 开关型）——其余原样透传。
_VALUE_FLAGS = {
    "--data": "data",
    "--report": "report",
    "--vector-cache": "vector_cache",
    "--repo": "repo",
}
_FLAG_FLAGS = {"--replay": "replay"}


def _usage() -> str:
    return (
        "用法：\n"
        "  benches/run.py --list-targets [--targets-file <外部清单>]\n"
        "  benches/run.py --target <靶场名> --data <索引根> --report <报告路径> [其余 eval 参数]\n"
        "不给 --target 时按 nova-core eval 的原样参数透传（此时要自己给 --golden）。"
    )


def _partition(args: Sequence[str]) -> tuple[dict[str, Any], list[str]]:
    """把参数分成「靶场展开接管的」与「原样透传的」两类。"""
    known: dict[str, Any] = {}
    extra: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in _VALUE_FLAGS and index + 1 < len(args):
            known[_VALUE_FLAGS[arg]] = args[index + 1]
            index += 2
            continue
        if arg in _FLAG_FLAGS:
            known[_FLAG_FLAGS[arg]] = True
            index += 1
            continue
        extra.append(arg)
        index += 1
    return known, extra


def _split_targets_file(args: Sequence[str]) -> tuple[Path | None, list[str]]:
    remaining: list[str] = []
    path: Path | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--targets-file" or arg.startswith("--targets-file="):
            if path is not None:
                raise TargetError("--targets-file 给了多次")
            if arg == "--targets-file":
                index += 1
                if index >= len(args) or args[index].startswith("--"):
                    raise TargetError("--targets-file 后面缺少清单路径")
                value = args[index]
            else:
                value = arg.split("=", 1)[1]
            if not value:
                raise TargetError("--targets-file 后面缺少清单路径")
            path = Path(value).expanduser()
        else:
            remaining.append(arg)
        index += 1
    return path, remaining


def main(argv: Sequence[str] | None = None) -> int:
    """解析靶场名后把参数原样转给 ``nova-core eval``。"""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        targets_file, args = _split_targets_file(args)
    except TargetError as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 2
    if "--list-targets" in args:
        try:
            print(describe_targets(load_targets(targets_file)))
        except TargetError as exc:
            print(f"bench: {exc}", file=sys.stderr)
            return 2
        return 0

    try:
        name, rest = split_target_arg(args)
        if name is not None:
            target = resolve_target(name, path=targets_file)
            if "--golden" in rest or "--project-id" in rest:
                raise TargetError("--target 不能与 --golden/--project-id 同时给（清单里已经写了）")
            known, extra = _partition(rest)
            missing = [flag for flag in ("data", "report") if not known.get(flag)]
            if missing:
                raise TargetError("--target 模式必须给：" + "、".join(f"--{f}" for f in missing))
            print(provenance(target), file=sys.stderr)
            rest = build_eval_args(
                target,
                data=known["data"],
                report=known["report"],
                vector_cache=known.get("vector_cache"),
                replay=bool(known.get("replay")),
                repo=known.get("repo"),
                extra=extra,
            )
    except TargetError as exc:
        print(f"bench: {exc}\n{_usage()}", file=sys.stderr)
        return 2

    core = ROOT / "core"
    if core.is_dir() and str(core) not in sys.path:
        # 未安装 nova-core 的裸 venv 里也能直接跑（安装环境下这行是空操作）。
        sys.path.insert(0, str(core))
    from nova_core.cli.app import main as cli_main

    return cli_main(["eval", *rest])


if __name__ == "__main__":
    raise SystemExit(main())
