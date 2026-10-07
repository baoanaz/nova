"""Markdown SpecBlock 抽取器（TASK-005）。

设计依据：``docs/design/Module/01-切片存储.md`` §2.2（Markdown / SpecBlock 一等检索公民）。
契约：``nova_core.interfaces.Parser`` + ``nova_core.types.{ParsedFile,SpecBlockDef,CodeFence}``。

口径（任务卡"执行记录"同步记录）：
- 仅识别 ATX 标题（``#``..``######``，允许 ≤3 空格缩进，遵循 CommonMark 的"# 后必须有空白"）；
  setext 标题（``===``/``---`` 下划线）不识别，按正文处理；
- 代码围栏（``` 或 ~~~，≥3 字符）内的 ``#`` 不产生标题；围栏归属最内层包含它的 SpecBlock；
- YAML front matter（首行 ``---`` 起、下一个 ``---`` 止）单独成块，
  heading_path="(front matter)"，level=0；
- 首个标题之前的前言正文单独成块，heading_path="(preamble)"，level=0
  （避免无标题文件零产出/丢内容）；
- mentioned = 行内 code（反引号）+ 符号形态词（camelCase / snake_case / A::b / 带扩展名路径），
  按首次出现位置去重；"宁多勿漏"（下游是弱引用，错挂代价是多余候选，D-06）。

本模块不修改 ``parsing/base.py`` / ``registry.py`` / ``__init__.py``（归 TASK-002）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from nova_core.types import CodeFence, ParsedFile, SpecBlockDef

# ---------------------------------------------------------------------------
# 结构扫描（标题 / 围栏 / front matter）
# ---------------------------------------------------------------------------

_ATX_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_ATX_CLOSING_RE = re.compile(r"[ \t]+#+[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FRONT_MATTER_DELIM = "---"

_EXTENSIONS = (
    "md|markdown|py|pyi|c|h|cc|cpp|cxx|hpp|hxx|rs|go|java|kt|js|jsx|ts|tsx|"
    "json|yaml|yml|toml|ini|cfg|sql|sh|bash|zsh|txt|rst|proto|html|css|scss|xml"
)

_INLINE_CODE_RE = re.compile(r"`+([^`\n]+?)`+")
_MENTION_PATTERNS = (
    # camelCase / PascalCase（含数字，如 TOKEN_KEY 交给 snake 规则）
    re.compile(r"\b(?:[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)\b"),
    # snake_case / SCREAMING_SNAKE_CASE
    re.compile(r"\b(?:[a-z][a-z0-9]*)(?:_[a-z0-9]+)+\b|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"),
    # C++ 作用域 A::b / A::B::c
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)+\b"),
    # 带扩展名路径（带目录分隔符，或裸文件名 + 已知代码扩展名）
    re.compile(
        rf"(?:[A-Za-z0-9_.@~-]+/)+[A-Za-z0-9_.@~-]+\.(?:{_EXTENSIONS})\b"
        rf"|[A-Za-z0-9_-]+\.(?:{_EXTENSIONS})\b"
    ),
)


@dataclass(frozen=True, slots=True)
class _Heading:
    level: int
    title: str
    line: int  # 1-based


@dataclass(frozen=True, slots=True)
class _BlockDraft:
    heading: str
    heading_path: str
    level: int
    start_line: int
    end_line: int
    content: str


def _clean_title(match: re.Match[str]) -> str:
    title = match.group(2) or ""
    title = _ATX_CLOSING_RE.sub("", title)
    return title.strip()


def _scan(lines: list[str], start: int) -> tuple[list[_Heading], list[CodeFence]]:
    """扫描 ATX 标题与代码围栏；围栏内的 # 不产生标题。

    ``start`` 为 0-based 起始行（front matter 区域不参与扫描：YAML 里的 ``#`` 不是标题）。
    """
    headings: list[_Heading] = []
    fences: list[CodeFence] = []
    index = start
    total = len(lines)
    while index < total:
        fence_match = _FENCE_RE.match(lines[index])
        if fence_match and not (
            fence_match.group(1)[0] == "`" and "`" in fence_match.group(2)
        ):
            marker = fence_match.group(1)
            char, length = marker[0], len(marker)
            info = fence_match.group(2).strip()
            body_start = index + 1
            close = body_start
            while close < total:
                close_match = _FENCE_RE.match(lines[close])
                if (
                    close_match
                    and close_match.group(1)[0] == char
                    and len(close_match.group(1)) >= length
                    and not close_match.group(2).strip()
                ):
                    break
                close += 1
            body_end = close if close < total else total  # 未闭合围栏：延伸到文件末尾
            fences.append(
                CodeFence(
                    lang=info.split()[0] if info else "",
                    content="\n".join(lines[body_start:body_end]),
                    line=index + 1,
                )
            )
            index = close + 1
            continue
        heading_match = _ATX_RE.match(lines[index])
        if heading_match:
            headings.append(
                _Heading(
                    level=len(heading_match.group(1)),
                    title=_clean_title(heading_match),
                    line=index + 1,
                )
            )
        index += 1
    return headings, fences


def _front_matter(lines: list[str]) -> tuple[int, str] | None:
    """返回 (闭合行号, 内容)；无 YAML front matter 返回 None。"""
    if not lines or lines[0].strip() != _FRONT_MATTER_DELIM:
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONT_MATTER_DELIM:
            return index + 1, "\n".join(lines[1:index])
    return None


def _heading_paths(headings: list[_Heading]) -> list[str]:
    """标题链（"A > B > C"）：仅保留严格更高级的祖先标题。"""
    paths: list[str] = []
    stack: list[_Heading] = []
    for heading in headings:
        while stack and stack[-1].level >= heading.level:
            stack.pop()
        chain = [item.title for item in stack] + [heading.title]
        paths.append(" > ".join(chain))
        stack.append(heading)
    return paths


def _build_drafts(
    lines: list[str],
    headings: list[_Heading],
    front_matter: tuple[int, str] | None,
) -> list[_BlockDraft]:
    drafts: list[_BlockDraft] = []
    body_start = (front_matter[0] + 1) if front_matter else 1

    if front_matter:
        drafts.append(
            _BlockDraft(
                heading="",
                heading_path="(front matter)",
                level=0,
                start_line=1,
                end_line=front_matter[0],
                content=front_matter[1],
            )
        )

    preamble_end = (headings[0].line - 1) if headings else len(lines)
    if preamble_end >= body_start:
        content = "\n".join(lines[body_start - 1 : preamble_end])
        if content.strip():
            drafts.append(
                _BlockDraft(
                    heading="",
                    heading_path="(preamble)",
                    level=0,
                    start_line=body_start,
                    end_line=preamble_end,
                    content=content,
                )
            )

    paths = _heading_paths(headings)
    for position, heading in enumerate(headings):
        end_line = len(lines)
        for later in headings[position + 1 :]:
            if later.level <= heading.level:
                end_line = later.line - 1
                break
        drafts.append(
            _BlockDraft(
                heading=heading.title,
                heading_path=paths[position],
                level=heading.level,
                start_line=heading.line,
                end_line=end_line,
                content="\n".join(lines[heading.line - 1 : end_line]),
            )
        )
    return drafts


def _extract_mentioned(text: str) -> tuple[str, ...]:
    """行内 code + 符号形态词，按首次出现位置去重（宁多勿漏）。"""
    found: list[tuple[int, str]] = []
    for match in _INLINE_CODE_RE.finditer(text):
        value = match.group(1).strip()
        if value:
            found.append((match.start(1), value))
    for pattern in _MENTION_PATTERNS:
        for match in pattern.finditer(text):
            found.append((match.start(), match.group(0)))
    seen: set[str] = set()
    result: list[str] = []
    for _, value in sorted(found, key=lambda item: item[0]):
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


# ---------------------------------------------------------------------------
# doctype（Module/01 §2.2-① 的有序规则表，首个命中生效）
# ---------------------------------------------------------------------------

#: "AI 指令文档"名表（Module/01 §2.2-① 的 doctype 规则表首行，命中即 ``agent-instructions``）。
#:
#: TASK-097 §C 扩充（与索引白名单的默认清单对齐）：新增 ``.agent.md``（agent 专用变体）、
#: ``handoff.md``（项目交接文档，用户 2026-09-14 明确指定）、``skill.md``（技能说明文档）。
#: 命中后享受**现有**的 ``agent-instructions`` rerank 加分（``+0.8`` 是既有值，R30 冻结不改），
#: 改的只是名表——**不新增 doctype 取值**，因此 CF-03 的 ``doctype`` 枚举不变。
#:
#: 为什么 ``skill.md`` 不单独开一个 doctype：它确实是一类独立文档（技能说明），但新增枚举值
#: 会同时改 CF-03 契约（``docs/contracts/contextpack.schema.json`` 的 ``doctype`` enum）与
#: ``retrieval/rerank.py`` 的 ``HIGH_VALUE_DOCTYPES``（R30 冻结）。本卡选择"先归入
#: ``agent-instructions``、把单独 doctype 的诉求写进执行记录"：语义上二者都是"给 AI 读的指令"、
#: rerank 档位与装填策略一致，暂没有实际损失。
_AGENT_INSTRUCTION_NAMES = frozenset(
    {"agents.md", "claude.md", ".cursorrules", ".agent.md", "handoff.md", "skill.md"}
)


def classify_doctype(path: str) -> str:
    """按路径/文件名规则表判定 doctype；顺序即优先级，首个命中生效。"""
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1].lower()
    slashed = f"/{normalized}"
    if name in _AGENT_INSTRUCTION_NAMES:  # agent-instructions
        return "agent-instructions"
    if name.startswith("readme"):  # readme（README*）
        return "readme"
    if name.startswith("architecture") or "/docs/design/" in slashed:  # design
        return "design"
    if "/docs/adr/" in slashed:  # adr
        return "adr"
    if name.startswith("api") or name.startswith("protocol") or name.startswith("openapi"):
        return "api"
    if name.startswith("changelog"):  # changelog
        return "changelog"
    return "guide"


def spec_block_id(block: SpecBlockDef) -> str:
    """SpecBlock 的稳定 id：``{path}:{heading_path}:{start_line}``

    （同名标题靠 start_line 消歧）。
    """
    return f"{block.path}:{block.heading_path}:{block.start_line}"


# ---------------------------------------------------------------------------
# Parser 实现
# ---------------------------------------------------------------------------


class MarkdownParser:
    """Markdown 抽取器：SpecBlock（标题树下完整小节）为一等检索资产。"""

    language = "markdown"

    def parse(self, path: str, content: str) -> ParsedFile:
        """解析单个 Markdown 文件；失败时返回 ``fallback=True`` 而不抛异常。"""
        try:
            return self._parse(path, content)
        except Exception as exc:  # noqa: BLE001 - Parser 契约：不抛异常，由 TASK-006 兜底
            return ParsedFile(
                path=path,
                language=self.language,
                parse_errors=(f"{type(exc).__name__}: {exc}",),
                fallback=True,
            )

    def _parse(self, path: str, content: str) -> ParsedFile:
        lines = content.splitlines()
        doctype = classify_doctype(path)
        front_matter = _front_matter(lines)
        body_start = (front_matter[0] + 1) if front_matter else 1
        headings, fences = _scan(lines, body_start - 1)
        drafts = _build_drafts(lines, headings, front_matter)

        blocks: list[SpecBlockDef] = []
        for draft in drafts:
            # fence 归属最内层包含它的 SpecBlock，避免父子块重复登记
            owned = [
                fence
                for fence in fences
                if draft.start_line <= fence.line <= draft.end_line
                and not any(
                    other.start_line > draft.start_line
                    and other.start_line <= fence.line <= other.end_line
                    for other in drafts
                )
            ]
            blocks.append(
                SpecBlockDef(
                    path=path,
                    heading=draft.heading,
                    heading_path=draft.heading_path,
                    level=draft.level,
                    start_line=draft.start_line,
                    end_line=draft.end_line,
                    content=draft.content,
                    doctype=doctype,
                    code_fences=tuple(owned),
                    mentioned=_extract_mentioned(draft.content),
                )
            )
        return ParsedFile(
            path=path,
            language=self.language,
            spec_blocks=tuple(blocks),
            fallback=False,
        )
