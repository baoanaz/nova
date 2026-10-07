"""索引范围策略：三层忽略规则引擎 + 大小/二进制阈值（TASK-037）。

设计依据：

- ``docs/design/Module/05-MCP与同步.md`` §3.1（D-28 三层忽略；``>128KB`` / ``>10%`` 不可打印判
  二进制）；
- ``docs/contracts/PROCESS.md`` §3.9 **R42**（本地模式落 Python 实现；契约是"忽略语义"不是库）
  与 **R43**（阈值下推 core，跳过必须如实进 ``skipped_files`` 并带原因）；
- 边界说明见 ``docs/tasks/TASK-037-索引范围策略.md`` §A/§B。

为什么在 core 落地（R42 的依据）：D-28 把 ``.gitignore`` 真实解析放在 client（Rust ``ignore``
crate），
但 M2a 本地模式没有 client（R38），而本地模式的索引就在 core 里发生。**本模块定义的是一份可对照的
"忽略语义清单"**——将来 client 用 ``ignore`` crate 时，两侧行为必须一致（见
``core/tests/pipeline/test_ignore.py``）。

层优先级（高 → 低；同层内后写的规则覆盖先写的）：

```text
0. {repo}/.novainclude + 内置白名单 + $NOVA_INDEX_ALLOWLIST   强制**包含**（TASK-097）
1. {repo}/.novaignore                 项目自定义排除（语法同 gitignore）
2. {repo}/**/.gitignore               真实解析：注释 / ! 否定 / 目录尾 / / ** / 前导 / / 字符类
3. 内置默认（DEFAULT_SKIP_DIRS + 常见产物模式）
```

第 0 层与其余三层**方向相反**（"包含"而非"排除"），因此它不是一条规则而是求值顺序上的一个
短路分支：命中白名单的文件在第 1/2 层**无条件放行**；但**第 3 层的目录剪枝仍然先判**——
`.git/objects/**` 与 `node_modules/*/skills/a.py` 救不回来（TASK-097 §A-4 的硬约束，
理由见 :class:`Allowlist`）。

V1 明确不读（与卡内 §A 一致，简化理由写在执行记录）：``{repo}/.git/info/exclude`` 与全局
``core.excludesFile``。

本模块只做**语义实现**，不引入第三方依赖（``pathspec`` 等）：内置匹配器覆盖 gitignore 的
主要形态，行为由测试逐条锁定；偏离 ``git`` 的边角（见 ``_Pattern`` 的注释）在报告里标注。
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ALLOWLIST_DEFAULT_SCOPE",
    "ALLOWLIST_ENV_VAR",
    "ALLOWLIST_SCOPE_DEEP",
    "ALLOWLIST_SCOPE_ENV_VAR",
    "ALLOWLIST_SCOPE_GITIGNORED",
    "ALLOWLIST_SCOPE_SHALLOW",
    "ALLOWLIST_SCOPES",
    "Allowlist",
    "BINARY_RATIO",
    "DEFAULT_ALLOWLIST",
    "DEFAULT_ALLOWLIST_DIRS",
    "DEFAULT_ALLOWLIST_FILENAMES",
    "DEFAULT_LOOKTHROUGH_DEPTH",
    "DEFAULT_SKIP_DIRS",
    "DEFAULT_SKIP_DIR_PATTERNS",
    "DEFAULT_MAX_FILE_BYTES",
    "IGNORE_FILENAME",
    "INCLUDE_FILENAME",
    "GITIGNORE_FILENAME",
    "IgnoreRules",
    "IndexScope",
    "SKIP_REASON_BINARY",
    "SKIP_REASON_OVERSIZE",
    "binary_reason",
    "is_allowlist_scope",
    "oversize_reason",
    "reason_from_entry",
]

#: gitignore 语法所用的文件名（D-28 第 2 层）。
GITIGNORE_FILENAME = ".gitignore"
#: 项目自定义忽略文件名（D-28 第 1 层，优先级最高）。
IGNORE_FILENAME = ".novaignore"
#: 项目自定义**白名单**文件名（TASK-097 第 0 层；只读仓库根这一份）。
INCLUDE_FILENAME = ".novainclude"
#: 白名单的全局环境变量（逗号分隔；服务端 / CI 场景）。
ALLOWLIST_ENV_VAR = "NOVA_INDEX_ALLOWLIST"
#: 白名单**下钻范围**的环境变量，取 ALLOWLIST_SCOPES 之一。
ALLOWLIST_SCOPE_ENV_VAR = "NOVA_ALLOWLIST_SCOPE"

#: 默认大小阈值：``> 128 KB`` 跳过（Module/05 §3.1 / R43，与 client 同口径）。
DEFAULT_MAX_FILE_BYTES = 128 * 1024
#: 二进制判定的不可打印字符比例上限（Module/05 §3.1：``> 10%`` 判二进制）。
BINARY_RATIO = 0.10
#: 二进制探测只读前 8 KB（避免为一个 308 MB 的文件读全量；R43 / TASK-036 §C.1）。
BINARY_PROBE_BYTES = 8 * 1024

#: ``skipped_files`` 条目里的原因分隔符（``"{path}:{reason}"``）。
_REASON_SEPARATOR = ":"

#: 原因标签：超限（含字节数，让运维一眼看出多大）。
SKIP_REASON_OVERSIZE = "oversize"
#: 原因标签：二进制 / 不可解码。
SKIP_REASON_BINARY = "binary"

#: ``oversize`` 原因的形状（``oversize:<字节数>``），供 :func:`reason_from_entry` 识别。
_OVERSIZE_TAIL_RE = re.compile(rf"{SKIP_REASON_OVERSIZE}{_REASON_SEPARATOR}\d+$")


def oversize_reason(size: int) -> str:
    """``"oversize:134217728"``——带真实字节数（R43 示例形态）。"""
    return f"{SKIP_REASON_OVERSIZE}{_REASON_SEPARATOR}{size}"


def binary_reason() -> str:
    """``"binary"``——不可解码 / 高比例不可打印字符。"""
    return SKIP_REASON_BINARY


def reason_from_entry(entry: str) -> str:
    """从 ``skipped_files`` 条目取原因标签（``"a/b.png:oversize:2048"`` → ``"oversize"``）。

    兼容裸路径（无原因）：返回 ``SKIP_REASON_BINARY``——旧报告里的 ``skipped_files`` 全部是
    二进制/不可解码跳过（TASK-007 的既有语义），沿用该默认值可让按原因分组的消费方不必区分新旧。

    路径本身可能含 ``:``（Linux 合法文件名），因此靠**原因标签的形状**匹配，而不是靠 ``split``。
    """
    if entry.endswith(f"{_REASON_SEPARATOR}{SKIP_REASON_BINARY}"):
        return SKIP_REASON_BINARY
    if _OVERSIZE_TAIL_RE.search(entry):
        return SKIP_REASON_OVERSIZE
    return SKIP_REASON_BINARY


# ---------------------------------------------------------------------------
# 内置默认（第 3 层）
# ---------------------------------------------------------------------------

#: 目录扫描默认跳过的目录名（工程噪声；D-28 第 3 层的现有部分）。
DEFAULT_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
        "dist",
        "build",
        "target",
        ".idea",
        ".vscode",
        ".nova",
    }
)

#: 目录名**模式**（第 3 层补齐 TASK-036 §C.3 的实测缺口）。
#:
#: ``build`` 这类固定名靠 :data:`DEFAULT_SKIP_DIRS` 挡住，但真实仓库里的构建产物是**变体**
#: （``cmake-build-release`` / ``build-debug`` / ``out`` / ``.cache`` …）。**用模式而不是穷举**
#: 是 TASK-037 §A 的明文要求。``bin`` / ``obj`` / ``lib`` 刻意**不在**列表里：它们是合法的
#: 源码目录名（Python 包里的 ``lib/``、C 项目的 ``bin/`` 头文件），误伤代价高于收益。
#:
#: **为什么不设内置的“产物文件名模式”（``*.a`` / ``*.min.js`` 之类）**：卡内 §A 把内置层限定为
#: “常见产物**目录**”，而 R43 要求跳过必须**如实进 ``skipped_files`` 并带原因**。若在目录列举阶段
#: 就按文件名模式剪掉，``lib/libcv.a`` 这类文件会**静默消失**、不产生任何原因——比不剪更差。
#: 因此二进制 / 超大文件的裁决统一走 §B 阈值（大小 + 内容），理由可观测。
DEFAULT_SKIP_DIR_PATTERNS: tuple[str, ...] = (
    "cmake-build-*",   # CLion/CMake 默认产物目录（TASK-036 §C.3 的直接输入）
    "build-*",         # build-debug / build-release …
    "out",             # 通用产物目录
    "htmlcov",         # coverage html（.gitignore 常见条目）
    ".cache",          # 工具缓存
    ".gradle",         # JVM 构建缓存
    ".next",           # Next.js 构建产物
    ".nuxt",           # Nuxt 构建产物
    ".turbo",          # Turborepo 缓存
    ".parcel-cache",   # Parcel 缓存
    "__pypackages__",  # PDM 本地包目录
)


# ---------------------------------------------------------------------------
# 第 0 层：索引白名单（TASK-097，强制包含）
# ---------------------------------------------------------------------------

#: 默认放行的**文件名**（按 basename 匹配，大小写不敏感）。
#:
#: 只收"AI 指令文档"这一类高信息密度、且常被项目写进 ``.gitignore`` 的文件：
#: ``AGENTS.md`` / ``CLAUDE.md`` 是主流编辑器的项目指令文件，``.agent.md`` 是
#: agent 专用变体，``.cursorrules`` 是 Cursor 的规则文件，``HANDOFF.md`` 是项目交接文档。
#: **刻意不内置** ``README.md`` / ``SKILL.md``：前者本来就被索引，后者靠 ``skills/`` 目录规则覆盖，
#: 单独放行反而会在 ``node_modules`` 之外的任意目录里把同名文件拽进来。
DEFAULT_ALLOWLIST_FILENAMES: tuple[str, ...] = (
    "agents.md",
    "claude.md",
    ".agent.md",
    ".cursorrules",
    "handoff.md",
)

#: 默认放行的**目录名**（路径中任意一段命中即放行该文件）。
#:
#: ``skills`` 命中任意层级：``skills/`` / ``.claude/skills/`` / ``tools/mine/skills/`` 都算。
#: 用户原话是"skills/ 文件夹里面的都上传吧？代替 SKILL.md"——因此**不限文件名**，
#: ``SKILL.md`` 与同目录的参考文件、脚本、模板一起进来。
#: 风险（已写进 ``docs/handbook/operations/索引白名单.md``）：这是个通用目录名，
#: 可能与其他项目的同名目录撞车
#: （如依赖包的 ``node_modules/*/skills/``）；后者由内置目录剪枝拦住（不参与白名单）。
DEFAULT_ALLOWLIST_DIRS: tuple[str, ...] = ("skills",)

#: 内置默认白名单（文件名 + 目录）的对外只读视图；**基线内容受测试锁定**。
#: 用户既可以在 ``.novainclude`` 里追加，也可以用 ``!`` 前缀取消（第 0 层内部的否定）。
DEFAULT_ALLOWLIST: tuple[str, ...] = DEFAULT_ALLOWLIST_DIRS + DEFAULT_ALLOWLIST_FILENAMES

#: 白名单**下钻**范围（见 :data:`ALLOWLIST_SCOPES`）——控制"要不要进被忽略的目录"。
#: ``deep``（默认）：**按名字下钻**——被忽略的目录里只要有直接子项命中白名单，就进去看；
#: ``gitignored``：只进"自己就被白名单命中"的被忽略目录（更保守）；
#: ``shallow``：完全不进（白名单只能救回被 ``.gitignore`` 匹配的文件，救不回被排除目录里的）。
ALLOWLIST_SCOPE_DEEP = "deep"
ALLOWLIST_SCOPE_GITIGNORED = "gitignored"
ALLOWLIST_SCOPE_SHALLOW = "shallow"
#: 三个取值的全名单（顺序无关，仅供校验与文档）。
ALLOWLIST_SCOPES: frozenset[str] = frozenset(
    {ALLOWLIST_SCOPE_DEEP, ALLOWLIST_SCOPE_GITIGNORED, ALLOWLIST_SCOPE_SHALLOW}
)
#: 默认范围：``deep``（名字下钻）。
ALLOWLIST_DEFAULT_SCOPE = ALLOWLIST_SCOPE_DEEP

#: 下钻链的**深度上限**（仅 :data:`ALLOWLIST_SCOPE_DEEP` 的"名字下钻"计数；
#: 单位：连续被忽略的层数）。
#:
#: 为什么必须有界：``.gitignore`` 里的 ``vendor/`` / ``logs/`` 可能有几十万文件；
#: 无界下钻会推翻
#: TASK-037 的性能约束（“遇到被忽略的目录就整体剪枝”）。有界 + **名字定向**两重限制后，
#: 额外代价仅为"每个被忽略目录多一次 ``scandir`` 看子项名字"：
#: ``hacks/skills/**`` 这种只要 2 层，而 ``vendor/{a,b,c…}`` 第一层就不命中、立即剪掉。
DEFAULT_LOOKTHROUGH_DEPTH = 4


def is_allowlist_scope(value: str) -> bool:
    """:data:`ALLOWLIST_SCOPES` 的取值校验（配置从环境变量来，非法值应回落默认）。"""
    return value in ALLOWLIST_SCOPES


def _lookthrough_depth(value: object, *, default: int = DEFAULT_LOOKTHROUGH_DEPTH) -> int:
    """下钻深度解析：非法值回落默认（一个打错的环境变量不该让索引起不来）。"""
    if isinstance(value, bool):  # bool 是 int 的子类，先排掉
        return default
    if not isinstance(value, int):
        return default
    return value if value >= 0 else default


@dataclass(frozen=True, slots=True)
class _AllowEntry:
    """一条已编译的白名单条目。

    ``negated`` = 行首 ``!``：第 0 层内部的否定（把内置白名单里的某项取消）。这里的方向与
    gitignore **相反**——白名单的本意是"包含"，所以无前缀是包含、``!`` 是排除；但"后写覆盖先写"
    的求值语义完全一致。
    """

    raw: str
    negated: bool
    #: 目录型（``skills`` / 显式 ``skills/``）——命中任意路径段，含其下全部文件。
    directory: bool
    #: 文件名型——按 basename 匹配，大小写不敏感。
    filename: str | None
    #: 目录型时用于匹配的目录名（原始大小写）。
    dirname: str


class Allowlist:
    """第 0 层"强制包含"白名单（TASK-097）。

    来源取并集：``.novainclude`` ∪ 内置默认 ∪ ``$NOVA_INDEX_ALLOWLIST``。

    **语义（三条，缺一会让本卡最容易写错的地方侥幸通过）**：

    1. **只压第 1/2 层**：命中白名单的文件/目录在 ``.novaignore`` / ``.gitignore`` 面前无条件放行；
    2. **绝不压第 3 层**：内置目录剪枝（``.git`` / ``node_modules`` / ``build-*`` …）
       **先于**白名单生效，
       且白名单不得把剪枝过的目录重新拉进来。这是卡内 §A-4 的硬约束，
       由 :meth:`IgnoreRules.is_ignored` 的短路顺序与 :meth:`DirectorySource._descend` 共同保证
       （内置命中的目录连白名单都看不到）。
    3. **允许下钻但必须有界**：``.gitignore`` 排除了 ``.claude/``，而白名单要救的 ``skills`` 就住在
       它下面（``.claude/skills/SKILL.md``）——不进目录就永远发现不了。是否下钻由**遍历侧**
       （:meth:`DirectorySource._descend`）按 :data:`ALLOWLIST_SCOPES` + :meth:`matches_name` 裁决：
       "被忽略的目录里有没有直接子项命中白名单"，有才进；连续下钻层数受
       :data:`DEFAULT_LOOKTHROUGH_DEPTH` 封顶（否则一个被忽略的 ``vendor/`` 会无界枚举）。
       本类不负责遍历，只回答"这个名字/路径算不算白名单"。

    ``scope`` 决定下钻的激进程度（取值见 :data:`ALLOWLIST_SCOPES`）：

    - ``deep``（默认）：被忽略的目录里只要有**直接子项**命中白名单就进去（名字定向，有深度上限）；
    - ``gitignored``：只进"目录名自身命中白名单"的被忽略目录（如 ``.claude/skills``）；
    - ``shallow``：完全不进——等同于"白名单只压 ``.gitignore`` 的文件级规则"。
    """

    __slots__ = ("_builtin", "_config", "_env", "_entries", "_scope", "_depth")

    def __init__(
        self,
        *,
        include: Sequence[str] = (),
        env: Sequence[str] = (),
        builtin: Sequence[str] = DEFAULT_ALLOWLIST,
        scope: str = ALLOWLIST_DEFAULT_SCOPE,
        lookthrough_depth: int = DEFAULT_LOOKTHROUGH_DEPTH,
    ) -> None:
        self._scope = scope if is_allowlist_scope(scope) else ALLOWLIST_DEFAULT_SCOPE
        self._depth = _lookthrough_depth(lookthrough_depth)
        self._builtin = tuple(builtin)
        self._config = tuple(include)
        self._env = tuple(env)
        entries: list[_AllowEntry] = []
        # 顺序即优先级（后写覆盖先写）：内置默认 < 环境变量 < ``.novainclude``。
        # “越靠近项目越优先”与第 1/2 层（.novaignore > .gitignore）方向一致，且让 ``!skills``
        # 这样的取消条目真能生效——默认清单因此是"可删的"（手册 §默认清单 明写）。
        for raw in (*self._builtin, *self._env, *self._config):
            entry = _parse_allow_entry(raw)
            if entry is not None:
                entries.append(entry)
        self._entries = tuple(entries)

    # ---------------------------------------------------------------- 构建

    @classmethod
    def from_root(
        cls,
        root: str | Path,
        *,
        builtin: Sequence[str] = DEFAULT_ALLOWLIST,
        env: Mapping[str, str] | None = None,
        scope: str | None = None,
    ) -> Allowlist:
        """读 ``{root}/.novainclude`` + 环境变量，与 ``builtin`` 取并集。

        与 :meth:`IgnoreRules.from_root` 的差异：**只读仓库根这一份** ``.novainclude``。
        忽略规则需要按目录作用域（``sub/.gitignore`` 只管 ``sub/``），而白名单是
        "用户声明的高价值文件"，嵌套声明会让作用域判定变复杂而收益为零。

        ``.novainclude`` 不存在 / 不可读 → 该来源为空（与忽略文件同口径，不报错）。
        """
        import os

        source = os.environ if env is None else env
        lines: list[str] = []
        candidate = Path(root) / INCLUDE_FILENAME
        try:
            if candidate.is_file():
                lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        raw_env = source.get(ALLOWLIST_ENV_VAR, "")
        env_lines = [part for part in raw_env.split(",") if part.strip()]
        raw_scope = scope if scope is not None else source.get(ALLOWLIST_SCOPE_ENV_VAR, "")
        return cls(
            include=lines,
            env=env_lines,
            builtin=builtin,
            scope=raw_scope or ALLOWLIST_DEFAULT_SCOPE,
            lookthrough_depth=source.get(
                "NOVA_ALLOWLIST_DEPTH", DEFAULT_LOOKTHROUGH_DEPTH
            ),
        )

    # ---------------------------------------------------------------- 查询

    def __bool__(self) -> bool:
        return bool(self._entries)

    @property
    def scope(self) -> str:
        return self._scope

    @property
    def lookthrough_depth(self) -> int:
        """下钻链的深度上限（仅 :data:`ALLOWLIST_SCOPE_DEEP` 的"名字下钻"计数）。

        单位是"连续被忽略的层数"：每为一个被忽略的目录进入下一层就 -1，跨过未被忽略的目录则重置。
        """
        return self._depth

    def names(self) -> tuple[str, ...]:
        """生效条目的规范名（供文档 / ``__repr__`` / 配置指纹使用）。"""
        seen: list[str] = []
        for entry in self._entries:
            name = entry.filename or entry.dirname
            value = f"!{name}" if entry.negated else name
            if value not in seen:
                seen.append(value)
        return tuple(seen)

    def matched_entry(self, path: str, *, is_dir: bool) -> _AllowEntry | None:
        """返回**最终**命中的条目（``None`` = 未命中；命中的是 ``!`` 条目也算"最终排除"）。

        顺序扫描、后写覆盖先写：``.novainclude`` 里的 ``!skills`` 能取消内置的 ``skills``。
        """
        normalized = _normalize(path)
        if not normalized:
            return None
        parts = normalized.split("/")
        name = parts[-1]
        directories = parts if is_dir else parts[:-1]
        winner: _AllowEntry | None = None
        for entry in self._entries:
            if entry.directory:
                hit = any(_same_name(part, entry.dirname) for part in directories)
            else:
                hit = _same_name(name, entry.filename or "")
            if hit:
                winner = entry
        return winner

    def allows(self, path: str, *, is_dir: bool) -> bool:
        """``path`` 是否被白名单**最终**放行（``!`` 条目命中时为 False）。"""
        entry = self.matched_entry(path, is_dir=is_dir)
        return entry is not None and not entry.negated

    def dir_matches(self, path: str) -> bool:
        """``path``（目录）是否被**目录型**条目放行。

        与 :meth:`allows` 的差异：只看目录名条目、不看文件名条目。用于
        :data:`ALLOWLIST_SCOPE_GITIGNORED` 的保守下钻（"这个被忽略的目录本身就是白名单目录"）。
        """
        entry = self.matched_entry(path, is_dir=True)
        return entry is not None and entry.directory and not entry.negated

    def matches_name(self, name: str) -> bool:
        """``name``（单个路径段）是否命中任一白名单条目（含 ``!`` 条目）。

        遍历侧的"名字下钻"探针：一个被忽略的目录里只要有直接子项命中这里，就值得进去看。
        与 :meth:`allows` 一样遵循后写覆盖先写。
        """
        if not name:
            return False
        winner: _AllowEntry | None = None
        for entry in self._entries:
            if entry.directory:
                hit = _same_name(name, entry.dirname)
            else:
                hit = _same_name(name, entry.filename or "")
            if hit:
                winner = entry
        return winner is not None and not winner.negated

    def descent_policy(self, path: str) -> str:
        """``path``（被忽略的目录）的下钻策略：``"none"`` / ``"by-name"``。

        - ``shallow`` 范围：一律 ``"none"``——白名单只压 ``.gitignore`` 的**文件级**规则；
        - ``gitignored`` 范围：命中目录名 → ``"always"``（调用方直接开子树）；否则 ``"none"``；
        - ``deep``（默认）→ ``"by-name"``：遍历侧看直接子项名字决定要不要进。

        这个策略只影响**遍历是否进入一个已被忽略的目录**；进来的文件算不算索引仍由
        :meth:`allows` 决定（两个问题分开答，才不会出现"进去了但文件还是被忽略"的静默失败）。
        """
        if self._scope == ALLOWLIST_SCOPE_SHALLOW:
            return "none"
        if self._scope == ALLOWLIST_SCOPE_GITIGNORED:
            return "always" if self.dir_matches(path) else "none"
        return "by-name"


def _parse_allow_entry(raw: str) -> _AllowEntry | None:
    """一行白名单文本 → 条目；空行 / 注释返回 ``None``。

    语法刻意收窄（不是完整 gitignore 语法，避免语义分裂）：

    - ``#`` 开头是注释（``\\#`` 转义为字面量）；
    - ``!`` 前缀是"取消该白名单项"（第 0 层内部否定）；
    - **目录型**：含 ``/``（``skills/``、``docs/skills``）或**不含点**的裸名字（``skills``）
      ——取末段作目录名，命中任意深度的同名目录；
    - **文件名型**：不含 ``/`` 且**含点**的裸名字（``AGENTS.md``、``.cursorrules``、``.agent.md``）
      ——按 basename 匹配，大小写不敏感；
    - 不支持 ``*`` / ``**`` / ``?`` 字符类：白名单是"我必须拿到这些高价值文件"的显式清单，
      通配会把它变成第二个 ``.gitignore``（双份语义，R42 的对照测试会立刻失效）。

    "含点 = 文件、不含点 = 目录"是启发式：它对实际会写的两种形态都正确；带 ``/`` 的写法可完全
    消除歧义（``skills/`` 明确是目录）。无扩展名的文件名不在支持范围内（手册已注明）。
    """
    text = raw.rstrip("\n")
    if text.endswith("\r"):
        text = text[:-1]
    text = text.rstrip()
    if not text or text.lstrip().startswith("#"):
        return None
    negated = text.startswith("!")
    if negated:
        text = text[1:].strip()
    if not text:
        return None
    if text.startswith("\\") and len(text) > 1 and text[1] in "#!":
        text = text[1:]
    if any(char in text for char in "*?["):
        return None  # 不支持通配（见 docstring）
    if "/" in text:
        text = text.strip("/")
        if not text:
            return None
        return _AllowEntry(
            raw=raw,
            negated=negated,
            directory=True,
            filename=None,
            dirname=text.rsplit("/", 1)[-1],
        )
    if "." not in text:  # 不含点的裸名字 → 目录型（``skills`` / ``my-notes``）
        return _AllowEntry(
            raw=raw,
            negated=negated,
            directory=True,
            filename=None,
            dirname=text,
        )
    return _AllowEntry(
        raw=raw,
        negated=negated,
        directory=False,
        filename=text.lower(),
        dirname="",
    )


def _same_name(left: str, right: str) -> bool:
    """文件名 / 目录名比较：大小写不敏感（卡内 DoD 要求 ``agents.md`` 等于 ``AGENTS.md``）。"""
    return left.casefold() == right.casefold()


# ---------------------------------------------------------------------------
# gitignore 语法
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Pattern:
    """一条（已编译的）忽略规则。

    ``negated`` = ``!pattern``：git 语义里**后面的规则覆盖前面的**，因此求值必须顺序扫描、
    命中即刷新结论。``directory_only`` = 尾 ``/``；``anchor`` = 前导 ``/``（或模式内部含 ``/``）。
    ``base`` = 该规则所在目录（仓库相对，根为 ``""``）——``sub/.gitignore`` 里的规则只作用于
    ``sub/`` 之下，这是 git 的按目录作用域语义。
    """

    pattern: str
    negated: bool
    directory_only: bool
    anchor: bool
    has_slash: bool
    base: str = ""


def _compile(line: str, *, base: str = "") -> _Pattern | None:
    """一行 ``.gitignore`` 文本 → 规则；空行/注释/纯 ``!`` 返回 ``None``（跳过）。"""
    text = line.rstrip("\n")
    if text.endswith("\r"):  # 容错 CRLF
        text = text[:-1]
    if not text.strip():
        return None
    if text.lstrip().startswith("#"):  # 注释（首字符 #；git 允许前导空白）
        return None
    # 行尾未转义的空白被忽略（git 语义）；``\ `` 可保留尾部空格，V1 不处理转义空格。
    text = text.rstrip()
    if not text:
        return None
    negated = text.startswith("!")
    if negated:
        text = text[1:]
    if not text:
        return None
    if text.startswith("\\") and len(text) > 1 and text[1] in "#!":  # ``\#`` / ``\!`` 字面量
        text = text[1:]
    directory_only = text.endswith("/")
    if directory_only:
        text = text[:-1]
    if not text:
        return None
    has_slash = "/" in text
    anchor = text.startswith("/")
    if anchor:
        text = text[1:]
    if not text:
        return None
    return _Pattern(
        pattern=text,
        negated=negated,
        directory_only=directory_only,
        anchor=anchor,
        has_slash=has_slash,
        base=base,
    )


def _glob_to_regex(pattern: str) -> str:
    """gitignore 的 glob → 正则片段。

    按 ``/`` 切段处理，因为 ``**`` 的语义是**跨段**的（段内的 ``*`` 不跨 ``/``）：

    - ``**/`` 在开头 → 零个或多个前导目录（``**/gen/x.py`` 命中 ``a/b/gen/x.py``）；
    - ``/**`` 在结尾 → 其下所有内容（``logs/**`` 命中 ``logs/a/b``，不包括 ``logs`` 自身）；
    - ``/**/`` 在中间 → 零个或多个中间目录（``a/**/b`` 命 ``a/b`` 与 ``a/x/y/b``）；
    - 其余段内的 ``*`` 不跨 ``/``，``?`` 匹配单个非 ``/`` 字符，``[]``/``[!]`` 是字符类。
    """
    segments = pattern.split("/")
    pieces: list[str | None] = []
    for segment in segments:
        # ``**`` 单独成段才有跨段语义；段内出现的 ``**``（如 ``a**b``）按普通 ``*`` 处理。
        pieces.append(None if segment == "**" else _segment_to_regex(segment))

    out: list[str] = []
    last = len(pieces) - 1
    for index, piece in enumerate(pieces):
        if piece is None:
            if index == 0 and last == 0:
                out.append(".*")
            elif index == 0:
                out.append("(?:.*/)?")  # 自带分隔符，后续段不必再加
            elif index == last:
                out.append("/.*")
            else:
                out.append("/(?:.*/)?")
            continue
        if index == 0:
            out.append(piece)
        elif pieces[index - 1] is None:
            out.append(piece)  # globstar 已处理过分隔符
        else:
            out.append("/" + piece)
    return "".join(out)


def _segment_to_regex(segment: str) -> str:
    """单个路径段内的 glob → 正则（``*`` 不跨 ``/``；字符类与转义照旧）。"""
    out: list[str] = []
    index = 0
    length = len(segment)
    while index < length:
        char = segment[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = _find_class_end(segment, index)
            if end is None:
                out.append(re.escape(char))
            else:
                out.append(_class_body_to_regex(segment[index + 1 : end]))
                index = end
        elif char == "\\" and index + 1 < length:
            out.append(re.escape(segment[index + 1]))
            index += 1
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def _find_class_end(pattern: str, start: int) -> int | None:
    """``[`` 的配对 ``]`` 下标；未闭合返回 ``None``（按字面量处理）。"""
    index = start + 1
    if index < len(pattern) and pattern[index] == "!":
        index += 1
    if index < len(pattern) and pattern[index] == "]":  # ``[]]``：首个 ] 是字符
        index += 1
    while index < len(pattern):
        if pattern[index] == "]":
            return index
        index += 1
    return None


def _class_body_to_regex(body: str) -> str:
    """字符类体 → 正则字符类（``!`` 前缀表示取反）。"""
    if not body:
        return r"\[\]"
    negate = body.startswith("!")
    if negate:
        body = body[1:]
    escaped = "".join(
        re.escape(char) if char in "\\^]" else char for char in body
    )
    return f"[^{escaped}]" if negate else f"[{escaped}]"


def _compile_matcher(pattern: _Pattern) -> _Compiled:
    """规则 → 两个正则：匹配**自身**与匹配**其后代**（git 语义：忽略目录即忽略其内容）。

    含 ``/`` 或前导 ``/`` → 锚定到规则所在目录；无 ``/`` → 匹配该目录下**任意层级**的同名条目
    （git 语义：``*.log`` 匹配 ``sub/`` 下任何深度的 ``.log``）。
    """
    body = _glob_to_regex(pattern.pattern)
    prefix = re.escape(pattern.base) + "/" if pattern.base else ""
    if pattern.directory_only:
        # 尾 ``/`` 规则：命中该目录本身及其下任何内容；**不**匹配同名普通文件（git 语义）。
        # 无 ``/``（且非前导 ``/``）时匹配任意深度的同名目录（``memory/`` 命中 ``a/b/memory/``）。
        if not (pattern.has_slash or pattern.anchor):
            head = f"{prefix}(?:.*/)?{body}"
        else:
            head = f"{prefix}{body}"
        return _Compiled(
            pattern=pattern,
            itself=re.compile(rf"^{head}$"),
            subtree=re.compile(rf"^{head}/.*$"),
        )
    if pattern.has_slash or pattern.anchor:
        return _Compiled(
            pattern=pattern,
            itself=None,
            subtree=re.compile(rf"^{prefix}{body}(?:/.*)?$"),
        )
    return _Compiled(
        pattern=pattern,
        itself=None,
        subtree=re.compile(rf"^{prefix}(?:.*/)?{body}(?:/.*)?$"),
    )


@dataclass(frozen=True, slots=True)
class _Compiled:
    """一条规则的已编译匹配器。"""

    pattern: _Pattern
    #: 尾 ``/`` 规则的"目录本身"匹配（仅当 ``is_dir=True`` 时使用）；其余规则为 ``None``。
    itself: re.Pattern[str] | None
    #: 匹配该规则命中的路径及其所有后代（对非目录规则等价于自身匹配）。
    subtree: re.Pattern[str]


# ---------------------------------------------------------------------------
# 规则集
# ---------------------------------------------------------------------------


class IgnoreRules:
    """一个仓库的三层忽略规则（``from_root`` 构建一次，之后只读）。"""

    __slots__ = (
        "_allowlist",
        "_builtin_dirs",
        "_builtin_dir_patterns",
        "_custom",
        "_gitignore",
        "_root",
    )

    def __init__(
        self,
        root: str | Path,
        *,
        custom: Sequence[str | tuple[str, str]] = (),
        gitignore: Sequence[str | tuple[str, str]] = (),
        allowlist: Allowlist | None = None,
        builtin_dirs: frozenset[str] = DEFAULT_SKIP_DIRS,
        builtin_dir_patterns: Sequence[str] = DEFAULT_SKIP_DIR_PATTERNS,
    ) -> None:
        self._root = Path(root)
        self._builtin_dirs = builtin_dirs
        self._builtin_dir_patterns = tuple(builtin_dir_patterns)
        #: 第 1 层（.novaignore）+ 第 2 层（各层 .gitignore），按优先级从高到低排列。
        #: 求值时先扫高优先层；同层内后写的规则覆盖先写的（git 语义）。
        self._custom = _Layer(_as_pairs(custom))
        self._gitignore = _Layer(_as_pairs(gitignore))
        #: 第 0 层（TASK-097）：强制包含。``None`` = 关闭白名单（旧行为，由测试的对照组使用）。
        self._allowlist = allowlist

    # ---------------------------------------------------------------- 构建

    @classmethod
    def from_root(
        cls,
        root: str | Path,
        *,
        allowlist: Allowlist | None = None,
        builtin_dirs: frozenset[str] = DEFAULT_SKIP_DIRS,
        builtin_dir_patterns: Sequence[str] = DEFAULT_SKIP_DIR_PATTERNS,
    ) -> IgnoreRules:
        """扫描 ``root``，解析 ``.novaignore`` / ``.novainclude`` 与**所有层级**的 ``.gitignore``。

        ``allowlist`` 默认从 ``root`` 构建（读 ``.novainclude`` + 环境变量）；传入现成对象可覆盖，
        传 ``Allowlist(builtin=())`` 则关闭白名单（对照测量旧行为）。

        忽略规则文件（``.gitignore`` / ``.novaignore``）不存在时**不报错**（与"忽略规则未引入前"
        的行为一致），只是该层为空。读取失败的忽略文件同样按"该层为空"处理——忽略规则文件坏了
        不该让索引整体失败。
        """
        root_path = Path(root)
        custom: list[tuple[str, str]] = []
        gitignore: list[tuple[str, str]] = []
        for directory in _walk_dirs(root_path):
            relative = directory.relative_to(root_path).as_posix() if directory != root_path else ""
            for name, sink in (
                (IGNORE_FILENAME, custom),        # 第 1 层：优先级最高
                (GITIGNORE_FILENAME, gitignore),  # 第 2 层
            ):
                candidate = directory / name
                if not candidate.is_file():
                    continue
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for raw in text.splitlines():
                    sink.append((raw, relative))
        return cls(
            root_path,
            custom=custom,
            gitignore=gitignore,
            allowlist=Allowlist.from_root(root_path) if allowlist is None else allowlist,
            builtin_dirs=builtin_dirs,
            builtin_dir_patterns=builtin_dir_patterns,
        )

    # ---------------------------------------------------------------- 求值

    @property
    def root(self) -> Path:
        return self._root

    @property
    def allowlist(self) -> Allowlist | None:
        """第 0 层白名单（``None`` = 本规则集未启用白名单）。"""
        return self._allowlist

    def allowlist_policy(self, relative_dir: str) -> str:
        """遍历侧的下钻策略：``"none"`` / ``"by-name"`` / ``"always"``。

        目录名命中的子树由 :meth:`allowlist_dir_match` 判（调用方直接开子树）。

        白名单未启用时一律 ``"none"``——目录剪枝行为与 TASK-037 逐字一致。
        """
        if self._allowlist is None:
            return "none"
        return self._allowlist.descent_policy(relative_dir)

    def allowlist_dir_match(self, relative_dir: str) -> bool:
        """目录名是否命中白名单（命中则整棵子树强制包含，见 ``DirectorySource._walk``）。"""
        return self._allowlist is not None and self._allowlist.dir_matches(relative_dir)

    def allowlist_names(self) -> tuple[str, ...]:
        """生效白名单条目的规范名（服务/CLI 可据以自述“我打算强制索引什么”）。"""
        return () if self._allowlist is None else self._allowlist.names()

    def is_ignored(self, path: str, *, is_dir: bool) -> bool:
        """``path``（仓库相对、正斜杠）是否被忽略。``is_dir`` 决定目录专属规则的适用性。

        顺序（TASK-097 后为四层，但第 0 层方向相反、只压第 1/2 层）：

        1. **第 3 层内置目录剪枝先判，命中即返回**——白名单不得救回 ``.git/objects`` /
           ``node_modules/*/skills/*``（卡内 §A-4）；
        2. 第 0 层白名单命中 → 强制包含（越过第 1/2 层）；
        3. 否则走原三层（第 2 层 → 第 1 层）。

        **注意**：这是独立 API 的语义，覆盖"文件自身或祖先命中白名单"两种情形；
        ``DirectorySource._walk`` 的**下钻**另有一层带预算的裁决（见该方法的 docstring），
        以免一个被 ``.gitignore`` 排除的大目录被无界枚举。
        """
        normalized = _normalize(path)
        if not normalized:
            return False

        # 第 3 层（最低）、也是唯一的硬剪枝：内置目录名 + 目录模式。
        # 注：内置层只判目录（卡内 §A 限定为“常见产物**目录**”）；文件级裁决走 §B 阈值，
        # 这样被跳过的文件才会带得上原因（否则会静默消失）。
        # 但“在被内置忽略的目录**里面**”的文件也算被忽略（否则 ``is_ignored`` 作为
        # 独立 API 会与遍历结果不一致：``DirectorySource`` 早把该目录剪掉了）。
        # **此处必须短路返回**：白名单不突破内置剪枝（TASK-097 §A-4）。
        if self._builtin_dir_hit(normalized, is_dir=is_dir):
            return True

        # 第 0 层（TASK-097）：强制包含。
        if self._allowlist is not None and self._allowlist.allows(normalized, is_dir=is_dir):
            return False

        # 第 2 层：.gitignore。
        decision = self._gitignore.decide(normalized, is_dir=is_dir, initial=False)
        # 第 1 层：.novaignore（最高优先级的**排除**层，可覆盖第 2 层）。
        return self._custom.decide(normalized, is_dir=is_dir, initial=decision)

    def reason_for(self, path: str) -> str | None:
        """被忽略的**原因标签**（``"gitignore"`` / ``"novaignore"`` / ``"builtin"``）。

        未忽略返回 ``None``。注意：命中白名单的路径**不**产生原因（它没被忽略）。
        """
        normalized = _normalize(path)
        if not normalized:
            return None
        is_dir = (self._root / normalized).is_dir()
        if not self.is_ignored(normalized, is_dir=is_dir):
            return None
        # 内置剪枝优先于白名单（见 is_ignored 的短路），因此这里也要先判内置。
        if self._builtin_dir_hit(normalized, is_dir=is_dir):
            return "builtin"
        if self._custom.decide(normalized, is_dir=is_dir, initial=False):
            return "novaignore"
        if self._gitignore.decide(normalized, is_dir=is_dir, initial=False):
            return "gitignore"
        return "builtin"

    def _builtin_dir_hit(self, path: str, *, is_dir: bool) -> bool:
        """路径自身（目录）或其任一祖先目录是否命中内置目录规则。"""
        parts = path.split("/")
        directory_parts = parts if is_dir else parts[:-1]
        return any(
            self._matches_builtin_dir("/".join(parts[:depth]))
            for depth in range(1, len(directory_parts) + 1)
        )

    def _matches_builtin_dir(self, path: str) -> bool:
        parts = path.split("/")
        if any(part in self._builtin_dirs for part in parts):
            return True
        name = parts[-1]
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self._builtin_dir_patterns)


class _Layer:
    """一层规则（已编译，可顺序求值）。"""

    __slots__ = ("_matchers",)

    def __init__(self, entries: Sequence[tuple[str, str]]) -> None:
        matchers: list[_Compiled] = []
        for line, base in entries:
            compiled = _compile(line, base=base)
            if compiled is None:
                continue
            matchers.append(_compile_matcher(compiled))
        self._matchers = tuple(matchers)

    def __bool__(self) -> bool:
        return bool(self._matchers)

    def decide(self, path: str, *, is_dir: bool, initial: bool) -> bool:
        """顺序扫描：命中规则即把结论刷新为该规则的取反值（``!`` → 取消忽略）。

        ``!`` 不能救回**父目录被排除**的文件（git 明文语义："It is not possible to re-include
        a file if a parent directory of that file is excluded"）。该判定只在真的被 ``!`` 救回过
        （``rescued``）时才走--否则每个路径都要穷举祖先，代价无意义。

        注：``DirectorySource`` 先判目录再剪枝，因此它不会对"已忽略目录下的文件"调用本方法；
        这条规则是为 ``is_ignored`` 作为**独立 API** 的语义正确性而实现（R42 要求两侧行为一致）。
        """
        result = initial
        rescued = False
        for compiled in self._matchers:
            pattern = compiled.pattern
            if pattern.directory_only:
                # 尾 ``/`` 规则：目录自身 / 其下任何内容都算命中；同名普通文件不算（git 语义）。
                matched = bool(
                    compiled.subtree.match(path)
                    or (is_dir and compiled.itself is not None and compiled.itself.match(path))
                )
            else:
                matched = bool(compiled.subtree.match(path))
            if matched:
                result = not pattern.negated
                rescued = pattern.negated
        if rescued and self._excluded_ancestor(path):
            return True
        return result

    def _excluded_ancestor(self, path: str) -> bool:
        """本层规则是否排除了 ``path`` 的某个祖先目录（仅祖先，不含自身）。"""
        parts = path.split("/")
        for depth in range(1, len(parts)):
            ancestor = "/".join(parts[:depth])
            for compiled in self._matchers:
                if compiled.itself is not None and compiled.itself.match(ancestor):
                    if not compiled.pattern.negated:
                        return True
        return False


def _as_pairs(entries: Sequence[str | tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """允许 ``IgnoreRules`` 直接吃裸行（测试/手工构造）或 ``(line, base)`` 对。"""
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        if isinstance(entry, str):
            pairs.append((entry, ""))
        else:
            pairs.append((entry[0], entry[1]))
    return tuple(pairs)


def _normalize(path: str) -> str:
    return path.strip("/").replace("\\", "/")


def _walk_dirs(root: Path):
    """遍历 ``root`` 下的目录（含 root），**跳过默认噪声目录**（否则 ``node_modules`` 会拖死构建）。

    这里只用最内层的内置目录名做剪枝：忽略规则文件本身不可能出现在 ``.git`` / ``.venv`` 里。
    """
    if not root.is_dir():
        return
    stack = [root]
    seen: set[Path] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        yield current
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.is_symlink():
                continue
            if entry.name in DEFAULT_SKIP_DIRS:
                continue
            stack.append(entry)


# ---------------------------------------------------------------------------
# 通用过滤阈值（R43）
# ---------------------------------------------------------------------------


class IndexScope:
    """大小 / 二进制阈值（Module/05 §3.1 同口径，参数可配置）。

    ``should_read`` 只在**读取之前**做纯元数据判定（大小）；二进制判定需要字节，因此由
    :meth:`check_bytes` 在拿到内容后判定——两段的返回值都是 ``(可索引, 原因)``。
    """

    __slots__ = ("_max_bytes", "_binary_ratio", "_probe_bytes")

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_FILE_BYTES,
        binary_ratio: float = BINARY_RATIO,
        probe_bytes: int = BINARY_PROBE_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError(f"max_bytes 必须为正整数，收到 {max_bytes}")
        if not 0.0 <= binary_ratio <= 1.0:
            raise ValueError(f"binary_ratio 必须在 [0, 1]，收到 {binary_ratio}")
        if probe_bytes <= 0:
            raise ValueError(f"probe_bytes 必须为正整数，收到 {probe_bytes}")
        self._max_bytes = max_bytes
        self._binary_ratio = binary_ratio
        self._probe_bytes = probe_bytes

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> IndexScope:
        """从环境变量构造（``NOVA_MAX_FILE_BYTES`` / ``NOVA_BINARY_RATIO``）。

        可配置性是 R43 的明文要求（"参数可配置"）；读取失败**回落默认值**而不是报错——
        一个打错的调试环境变量不该让索引起不来。
        """
        import os

        source = os.environ if env is None else env
        raw_bytes = source.get("NOVA_MAX_FILE_BYTES")
        raw_ratio = source.get("NOVA_BINARY_RATIO")
        max_bytes = DEFAULT_MAX_FILE_BYTES
        binary_ratio = BINARY_RATIO
        if raw_bytes:
            try:
                parsed = int(raw_bytes)
            except ValueError:
                parsed = 0
            if parsed > 0:
                max_bytes = parsed
        if raw_ratio:
            try:
                ratio = float(raw_ratio)
            except ValueError:
                ratio = -1.0
            if 0.0 <= ratio <= 1.0:
                binary_ratio = ratio
        return cls(max_bytes=max_bytes, binary_ratio=binary_ratio)

    def should_read(self, path: str, size: int) -> tuple[bool, str | None]:
        """纯元数据判定：``size > max_bytes`` → ``(False, "oversize:<size>")``。"""
        if size > self._max_bytes:
            return False, oversize_reason(size)
        return True, None

    def check_bytes(self, data: bytes) -> tuple[bool, str | None]:
        """内容判定：前 ``probe_bytes`` 中不可打印字符比例 ``> binary_ratio`` → 二进制。"""
        if _is_binary(data, ratio=self._binary_ratio, probe=self._probe_bytes):
            return False, binary_reason()
        return True, None


def _is_binary(data: bytes, *, ratio: float, probe: int) -> bool:
    """前 ``probe`` 字节里不可打印字符比例是否 ``> ratio``。

    判据与 notace 参数同口径（Module/05 §3.1）：先看 NUL——一个 NUL 就足以定性（文本文件不会有），
    否则按不可打印字符（``str.isprintable()`` 之外的 ASCII 控制符）比例判定。UTF-8 多字节字符的
    高位字节**不算**不可打印（否则中文文档会被整片误判为二进制）。
    """
    window = data[:probe]
    if not window:
        return False
    if b"\x00" in window:
        return True
    control = sum(1 for byte in window if byte < 0x20 and byte not in (0x09, 0x0A, 0x0D))
    return control / len(window) > ratio
