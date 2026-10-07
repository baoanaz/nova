"""SourceProvider：流水线内部的源码读取契约（TASK-007 自定）。

边界：

- 流水线只依赖 ``read(path) -> bytes`` 与 ``list_files() -> Sequence[str]``；
  ``list_files`` 是 ``full_reparse``（遍历全部文件重跑增量）与 ``reembed``（枚举存量 chunk）
  的必需能力——任务卡只列了 ``read``，此处按卡内"遍历 SourceProvider 全部文件"的要求补齐，
  并在执行记录里作为 pipeline 内契约记录。
- CLI（TASK-013）提供 :class:`DirectorySource`（repo 目录）；Phase 2 service 提供 blobs 实现
  （按上传目录/DB 列举，本卡不实现，只保证协议够用）。
- **忽略规则（TASK-037）**：``.novaignore`` > 各层 ``.gitignore`` > 内置默认的三层语义由
  :mod:`nova_core.pipeline.ignore` 实现，``DirectorySource`` 默认启用；被忽略的**目录不被遍历**
  （R42 的性能要求——``cmake-build-release/`` 下有几万个文件）。
  其他 provider（service blobs、测试替身）可以照旧自行过滤。
- **白名单下钻（TASK-097）**：第 0 层"强制包含"白名单的**遍历侧**在这里落地——``.gitignore``
  排除了 ``.claude/`` 而白名单要救的 ``skills/`` 在其下时，必须进目录才能发现。下钻是
  **名字定向 + 有深度上限**的，且内置目录名/模式从不下钻（``node_modules/**/skills/a.py`` 进不来）。
- 大小 / 二进制阈值**不在这里**：它是"文件内容该不该读"的判定（``skipped_files`` 的原因），
  归 :class:`nova_core.pipeline.indexer.Indexer`（R43，见 ``ignore.IndexScope``）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Protocol, runtime_checkable

from nova_core.pipeline.ignore import (
    DEFAULT_SKIP_DIR_PATTERNS,
    DEFAULT_SKIP_DIRS,
    IgnoreRules,
)

__all__ = [
    "DEFAULT_SKIP_DIRS",
    "DirectorySource",
    "SourcePathError",
    "SourceProvider",
]


class SourcePathError(ValueError):
    """路径不是安全的仓库相对路径（绝对路径 / 越界 ``..`` / 反斜杠）。"""


@runtime_checkable
class SourceProvider(Protocol):
    """源码读取（pipeline 内部契约）。"""

    def read(self, path: str) -> bytes:
        """读取仓库相对路径 ``path`` 的原始字节；不存在时抛 ``FileNotFoundError``。"""
        ...

    def list_files(self) -> Sequence[str]:
        """列出全部可用文件（仓库相对路径，正斜杠，稳定排序）。"""
        ...


class DirectorySource:
    """``repo 目录`` 实现：``root`` 下的文件即源码，路径一律仓库相对、正斜杠。

    四层忽略（D-28 / R42 / TASK-097 第 0 层白名单）默认开启；``respect_ignore_files=False`` 时
    **只**按内置目录名剪枝——那是 TASK-037 之前的行为，保留给"想看仓库全貌"的调试与对照测量。

    白名单的**下钻**（进入被 ``.gitignore`` 排除的目录去看里面有没有白名单文件）在本类完成：
    ``IgnoreRules.is_ignored`` 只能回答"这个路径是包含还是排除"，而"要不要进这个目录"是遍历决策。
    两者必须同时正确，``.claude/skills/SKILL.md``（祖先 ``.claude`` 被 gitignore）才真能进来。
    """

    def __init__(
        self,
        root: str | Path,
        *,
        skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS,
        ignore: IgnoreRules | None = None,
        respect_ignore_files: bool = True,
    ) -> None:
        """``ignore`` 传入现成规则集；``respect_ignore_files=False`` 退回"只看内置目录名"。

        两个开关的关系：显式 ``ignore`` 一律优先；否则 ``respect_ignore_files`` 决定是否从
        ``root`` 构建四层规则（默认构建）。想拿"忽略规则引入前"的全量清单就用
        ``respect_ignore_files=False``（对照测量用）。
        """
        self._root = Path(root)
        self._skip_dirs = skip_dirs
        if ignore is not None:
            self._ignore: IgnoreRules | None = ignore
        elif respect_ignore_files:
            self._ignore = IgnoreRules.from_root(
                self._root,
                builtin_dirs=skip_dirs,
            )
        else:
            self._ignore = None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def ignore(self) -> IgnoreRules | None:
        """本 source 使用的忽略规则（``None`` = 只用内置目录名剪枝）。"""
        return self._ignore

    def read(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    def file_size(self, path: str) -> int | None:
        """文件的字节数（**不**读内容）；不存在/不可读返回 ``None``。

        这是对 :class:`SourceProvider` 协议的**可选**补充（不是协议要求）：索引器用
        ``getattr`` 探测它，以便在读到字节之前就拦掉超限文件（TASK-036 §C.1 实测：
        308 MB 的 ``lib/libcv.a`` 当前会被整份读入内存再解码）。
        """
        try:
            return self._resolve(path).stat().st_size
        except (OSError, SourcePathError):
            return None

    def list_files(self) -> tuple[str, ...]:
        root = self._root
        if not root.is_dir():
            return ()
        found: list[str] = []
        for relative in self._walk():
            found.append(relative)
        return tuple(sorted(found))

    def _walk(self) -> Iterator[str]:
        """自顶向下遍历：**遇到被忽略的目录就整体剪枝**（不进入、不 stat 其内容）。

        唯一的例外是**白名单下钻**（TASK-097）：``.gitignore`` 排除了 ``.claude/``，而白名单要救的
        ``skills/`` 就住在它下面时，不进去就永远发现不了。下钻是**名字定向 + 有深度上限**的：

        - 内置目录名（``node_modules`` / ``.git`` …）**从不下钻**（卡内 §A-4 的关键守护）；
        - 被忽略目录里只有直接子项命中白名单时才进（``gitignored`` 范围下改为"目录名自身命中"）；
        - 连续下钻层数受 ``DEFAULT_LOOKTHROUGH_DEPTH`` 封顶（否则 ``vendor/`` 这种被
          ``.gitignore`` 排除的大目录会被无界枚举，推翻 TASK-037 的性能约束）。

        ``budget`` 是本条栈帧的剩余下钻预算：``None`` = 本目录不在下钻链上。
        """
        root = self._root
        stack: list[tuple[Path, int | None]] = [(root, None)]
        while stack:
            current, budget = stack.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda entry: entry.name)
            except OSError:
                # 读不了某个目录不该中止整个仓库的列举（与 TASK-036 §B 的隔离口径一致）。
                continue
            for entry in entries:
                relative = Path(entry.path).relative_to(root).as_posix()
                try:
                    # 目录判定**不跟随**符号链接：与 ``Path.rglob`` 一致（否则会跟进环与仓库外）。
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:  # 断链符号链接等
                    continue
                if is_dir:
                    descend, child_budget = self._child_descend(relative, entry.path, budget)
                    if not descend:
                        continue
                    stack.append((Path(entry.path), child_budget))
                    continue
                try:
                    # 文件判定**跟随**符号链接：``rglob`` + ``is_file()`` 会列出指向文件的链接，
                    # 保持与忽略规则引入前一致（符号链接到目录仍不会被递归，见上）。
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                if self._is_skipped_file(relative):
                    continue
                yield relative

    def _child_descend(
        self, relative: str, path: str, budget: int | None
    ) -> tuple[bool, int | None]:
        """子目录 ``relative`` 是否进入，以及进入后的剩余下钻预算。

        判定顺序与 :meth:`IgnoreRules.is_ignored` 的短路顺序一致（卡内 §A-4）：

        1. **内置目录名与模式最先判**：``node_modules`` / ``.git`` / ``build-*`` 无论白名单
           怎么写都不进——这就是"白名单不救回 ``node_modules/pkg/skills/a.py``"的落地位置；
        2. 未被忽略的目录：照旧进入，预算重置为 ``None``（下钻链只统计**连续**被忽略的层数）；
        3. 被忽略的目录：只有"直接子项里有人命中白名单"时才进（``by-name`` 策略），
           否则整棵剪掉；预算耗尽同样不进（``DEFAULT_LOOKTHROUGH_DEPTH`` 封顶）。

        **为什么 ``skills/`` 之下不再需要特殊处理**：``IgnoreRules.is_ignored`` 的白名单判定
        会为命中目录的**全部后代**返回 False（:meth:`Allowlist.matched_entry` 检查所有祖先段），
        所以一旦 ``hacks/skills`` 被放行，它下面的文件与子目录自然都不再"被忽略"，
        无需额外的"子树已打开"状态。
        """
        if any(part in self._skip_dirs for part in relative.split("/")):
            return False, None
        if self._ignore is None:
            # respect_ignore_files=False：只看内置目录名与目录模式（TASK-037 之前的行为）。
            return (
                not any(
                    fnmatchcase(relative.rsplit("/", 1)[-1], pattern)
                    for pattern in DEFAULT_SKIP_DIR_PATTERNS
                ),
                None,
            )
        if self._ignore.reason_for(relative) is None:
            return True, None
        if self._ignore.reason_for(relative) == "builtin":
            # 内置目录名与模式（``.git`` / ``node_modules`` / ``build-*``）：不得救回。
            return False, None
        if self._ignore.allowlist_policy(relative) != "by-name":
            return False, None
        allowlist = self._ignore.allowlist
        remaining = allowlist.lookthrough_depth if budget is None else budget
        if remaining <= 0 or not self._has_allowlisted_child(path):
            return False, None
        return True, remaining - 1

    def _has_allowlisted_child(self, path: str) -> bool:
        """目录 ``path`` 的直接子项里是否有名字命中白名单（决定要不要下钻看一眼）。"""
        if self._ignore is None or self._ignore.allowlist is None:
            return False
        try:
            return any(
                self._ignore.allowlist.matches_name(entry.name)
                for entry in os.scandir(path)
            )
        except OSError:
            return False

    def _is_skipped_file(self, relative: str) -> bool:
        if self._ignore is not None:
            return self._ignore.is_ignored(relative, is_dir=False)
        parts = relative.split("/")
        return any(part in self._skip_dirs for part in parts[:-1])

    def _resolve(self, path: str) -> Path:
        if not path or path.startswith("/") or "\\" in path:
            raise SourcePathError(f"非法仓库相对路径：{path!r}")
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise SourcePathError(f"非法仓库相对路径：{path!r}")
        return self._root.joinpath(*parts)
