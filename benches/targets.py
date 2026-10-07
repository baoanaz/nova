"""靶场清单（``benches/targets.json``）：把「用例集 + 预建索引」绑成一个可复用的靶场名。

为什么需要它：一次跑分要同时凑齐四样东西——``--golden``（用例）、``--data``（索引根）、
``--project-id``（跳过 D-29 身份计算，挂预建索引）、``--vector-cache``（查询向量侧车）。
手抄这套组合每次都可能错，而且错法很难察觉：抄错 projectId 会报"索引缺失"（容易发现），
漏带侧车则会走"向量通道降级"、指标口径与出题机不同（很难发现）。清单把组合固化成数据，
命令行只剩「靶场名 + 数据根」。

三条纪律：

1. **只放元信息，不放索引本体**：``index.db`` 里的 ``chunks.content`` 就是切片正文，bundle 因此
   等同靶场源码副本；清单进公开仓库，索引只走内网（见 ``benches/README.md`` 的合规一节）；
2. ``project_id`` 是 benchmark 放行口（跳过身份核验与 ``project.json`` 校验），
   **不得**用在生产服务路径上；
3. 索引/侧车/配置三者指纹不符时由 ``zace-core`` 如实报错，本模块**不做**任何自动重建、
   也不把"没找到索引"翻译成"降级跑一遍"。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "SCHEMA",
    "ROOT",
    "TARGETS_PATH",
    "Target",
    "TargetError",
    "build_eval_args",
    "describe_targets",
    "load_targets",
    "provenance",
    "resolve_target",
    "split_target_arg",
]

#: 清单格式版本（不兼容变更时递增，旧清单会被拒绝而不是半解析）。
SCHEMA = 1

#: 仓库根（本文件的上一级）。
ROOT = Path(__file__).resolve().parents[1]

#: 默认清单路径。
TARGETS_PATH = Path(__file__).resolve().parent / "targets.json"

#: 清单里每个靶场必须给的字段。
_REQUIRED = ("golden", "repo_hint", "commit")

#: 角色：primary=主靶场（公共仓库）/ dogfood=本仓自检 / internal=内部靶场（不进默认流程）。
_ROLES = ("primary", "dogfood", "internal")


class TargetError(RuntimeError):
    """使用者可修正的错误（清单缺失/格式错/靶场名未知/参数组合矛盾）。"""


@dataclass(frozen=True)
class Target:
    """一个靶场：用例集 + 它对应的预建索引身份。"""

    name: str
    golden: Path
    repo_hint: str
    commit: str
    role: str = "internal"
    project_id: str | None = None
    index_how: str = "unknown"
    index_hint: str = ""
    embedding: Mapping[str, Any] | None = field(default=None)

    @property
    def has_project_id(self) -> bool:
        """是否绑定了预建索引（``--project-id`` 放行口）。"""
        return bool(self.project_id)


def _target_from_spec(name: str, spec: Any) -> Target:
    if not isinstance(spec, dict):
        raise TargetError(f"靶场 {name} 的配置不是对象：{spec!r}")
    missing = [key for key in _REQUIRED if not spec.get(key)]
    if missing:
        raise TargetError(f"靶场 {name} 缺字段：{missing}")
    role = str(spec.get("role", "internal"))
    if role not in _ROLES:
        raise TargetError(f"靶场 {name} 的 role 非法：{role!r}（可选：{'、'.join(_ROLES)}）")
    index = spec.get("index") or {}
    if not isinstance(index, dict):
        raise TargetError(f"靶场 {name} 的 index 不是对象：{index!r}")
    return Target(
        name=name,
        golden=(ROOT / str(spec["golden"])).resolve(),
        repo_hint=str(spec["repo_hint"]),
        commit=str(spec["commit"]),
        role=role,
        project_id=(str(spec["project_id"]) if spec.get("project_id") else None),
        index_how=str(index.get("how", "unknown")),
        index_hint=str(index.get("hint", "")),
        embedding=spec.get("embedding"),
    )


def load_targets(path: Path | None = None) -> dict[str, Target]:
    """读靶场清单；返回 ``{靶场名: Target}``（按清单里的顺序）。"""
    target_path = Path(path) if path is not None else TARGETS_PATH
    try:
        raw = json.loads(target_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TargetError(f"找不到靶场清单：{target_path}") from exc
    except OSError as exc:
        raise TargetError(f"无法读取靶场清单：{target_path}（{exc.strerror}）") from exc
    except json.JSONDecodeError as exc:
        raise TargetError(f"靶场清单不是合法 JSON：{target_path}（{exc}）") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        actual = raw.get("schema") if isinstance(raw, dict) else raw
        raise TargetError(f"靶场清单 schema 不支持：期望 {SCHEMA}，实际 {actual!r}")
    specs = raw.get("targets")
    if not isinstance(specs, dict) or not specs:
        raise TargetError(f"靶场清单里没有任何靶场：{target_path}")
    return {name: _target_from_spec(name, spec) for name, spec in specs.items()}


def resolve_target(name: str, path: Path | None = None) -> Target:
    """按名字取靶场；未知名字时报出可选清单（比 KeyError 好排查）。"""
    targets = load_targets(path)
    if name not in targets:
        available = "、".join(targets)
        raise TargetError(f"未知靶场：{name}（可选：{available}）")
    return targets[name]


def describe_targets(targets: Mapping[str, Target]) -> str:
    """``--list-targets`` 的表格输出。"""
    lines = [
        "靶场清单（benches/targets.json）",
        "  默认流程 = 公共仓库（role=primary）：本地建索引一次，落持久目录，之后只跑 eval",
        "",
        f"{'名字':<20} {'角色':<10} {'commit':<14} {'索引':<16} golden",
        f"{'-' * 20} {'-' * 10} {'-' * 14} {'-' * 16} {'-' * 32}",
    ]
    for target in targets.values():
        commit = target.commit if target.commit == "self" else target.commit[:12]
        row = f"{target.name:<20} {target.role:<10} {commit:<14} {target.index_how:<16}"
        lines.append(f"{row} {target.golden}")
        if target.has_project_id:
            lines.append(f"{'':<20} {'':<10} project_id  : {target.project_id}")
        if target.embedding:
            lines.append(f"{'':<20} {'':<10} embedding   : {target.embedding}")
        if target.index_hint:
            lines.append(f"{'':<20} {'':<10} 获取方式    : {target.index_hint}")
    return "\n".join(lines)


def provenance(target: Target) -> str:
    """一行出处（跑分报告要能回答"这份索引来自哪个 commit/工作区"）。"""
    index = target.project_id or "（按 D-29 身份计算）"
    return (
        f"[bench] 靶场={target.name}（{target.role}）golden={target.golden} "
        f"commit={target.commit} index={target.index_how} project={index}"
    )


def split_target_arg(args: Sequence[str]) -> tuple[str | None, list[str]]:
    """从参数表里摘出 ``--target NAME`` / ``--target=NAME``，其余原样返回。"""
    remaining: list[str] = []
    name: str | None = None
    pending = False
    for arg in args:
        if pending:
            name, pending = arg, False
            continue
        if arg == "--target":
            pending = True
            continue
        if arg.startswith("--target="):
            if name is not None:
                raise TargetError("--target 给了多次")
            name = arg.split("=", 1)[1]
            continue
        remaining.append(arg)
    if pending:
        raise TargetError("--target 后面缺少靶场名")
    return name, remaining


def build_eval_args(
    target: Target,
    *,
    data: str | Path,
    report: str | Path,
    vector_cache: str | Path | None = None,
    replay: bool = False,
    repo: str | Path | None = None,
    extra: Sequence[str] = (),
) -> list[str]:
    """把靶场 + 运行参数拼成 ``zace-core eval`` 的参数表（不含 ``eval`` 子命令名）。"""
    if not target.golden.is_dir() and not target.golden.is_file():
        raise TargetError(f"靶场 {target.name} 的 golden 不存在：{target.golden}")
    if not target.has_project_id and repo is None:
        raise TargetError(
            f"靶场 {target.name} 没有绑定预建索引（清单里 project_id 为空），"
            "必须用 --repo 指向 checkout，让 core 按身份找索引"
        )
    if replay and vector_cache is None:
        raise TargetError("--replay 必须同时给 --vector-cache（离线回放只从侧车取查询向量）")
    argv = ["--golden", str(target.golden)]
    if target.project_id:
        argv += ["--project-id", target.project_id]
    argv += ["--data", str(data), "--report", str(report)]
    if repo is not None:
        argv += ["--repo", str(repo)]
    if vector_cache is not None:
        argv += ["--vector-cache", str(vector_cache)]
    if replay:
        argv.append("--replay")
    argv += list(extra)
    return argv
