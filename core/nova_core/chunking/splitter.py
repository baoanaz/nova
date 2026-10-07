"""Chunk 切分：``ParsedFile`` + 原文 → ``ChunkDef`` 列表（TASK-006）。

设计依据：``docs/design/Module/01-切片存储.md`` §2.2（各语言切片规则）、§2.3（四层模型）、
§2.4（chunk_id / 三通道差异化输入 + D-05/D-43）；契约：``nova_core.types.ChunkDef``（CF-08）。

本模块的口径（评审与下游消费关键）：

- ``chunk_id = {path}:{symbol_fqn}:{start_line}``（D-04，代内唯一）；markdown 的
  ``symbol_fqn`` 取 ``heading_path``；spec 块 id 与 ``Store`` 写入 ``spec_blocks`` 的 id 同源
  （见 :func:`spec_block_id`），保证双表同 id（CF-01 规划期裁定 2）。
- **id 唯一性是硬不变量**：``split_file`` 出口检测重复 id，命中即抛带明细的 ``ValueError``
  （TASK-018 §B）——既不静默去重、不静默丢弃，也不把冲突留到写库变成 ``IntegrityError``。
- **符号 = 1 chunk**（``symbol_kind`` 取符号 kind；``class`` → ``class_skeleton``）；
  ``namespace`` 只做结构容器、不成 chunk（Module/01 §2.2 的语言规则表未把命名空间列为检索单元）。
- **类/结构体带方法 = 骨架 chunk + 每方法 1 chunk**：骨架 chunk 只覆盖**声明区**
  （类起始行 → 首个直接成员的前一行），这样"改一个方法体"不会让骨架 chunk 的
  content_hash 变化，Module/01 §4.1 的"改 1 个函数只重嵌入 1 个 chunk"对方法同样成立；
  无方法的 ``struct``（C 记录类型）按普通符号处理，kind 原样保留。
- **模块级/未覆盖代码 = 兜底 chunk**：不属于任何符号或 spec 块的行按"最大连续段"递归字符切分
  （复用 TASK-002 的 :func:`nova_core.parsing.fallback.split_fallback`，≤800 行/块），
  标 ``fallback_block``，伪 fqn 统一为 ``(module)``（与 TASK-005 的 ``(preamble)`` /
  ``(front matter)`` 伪路径命名同风格）；纯空白段跳过。
- **解析失败/未知语言**（``parsed.fallback=True``）→ 整个文件走兜底切分，符号与 spec 块一律不信。
- ``signature`` / ``docstring`` 由本模块从源码文本按语言启发式提取（``SymbolDef`` 无这两个字段）：
  Python 取"装饰器 + def/class 头"与紧随其后的字符串字面量；C/C++ 取到 ``{`` / ``;`` 之前的声明文本
  （无 docstring 概念，恒为空串）；spec 块 ``signature = heading_path``；兜底块两者皆为 ""。
- ``embedding_text(chunk)`` = signature + docstring + 截断体（去掉签名后的正文，按字符上限截断；
  最终 token 级截断仍由 EmbeddingProvider 按 ``max_input_tokens`` 执行，D-05）。
- 纯函数、无 I/O、确定性：同一 ``(parsed, content)`` 两次调用结果逐字段相等；输出按
  ``(start_line, end_line, id)`` 稳定排序（同 id 的兜底硬切块按文档内顺序附 ``#N``）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

from nova_core.hashing import chunk_content_hash, normalize_newlines
from nova_core.parsing.fallback import split_fallback
from nova_core.types import ChunkDef, ParsedFile, SpecBlockDef, SymbolDef

__all__ = [
    "CLASS_SKELETON_KIND",
    "EMBEDDING_BODY_MAX_CHARS",
    "FALLBACK_KIND",
    "ID_DISAMBIGUATION_SEP",
    "MODULE_FQN",
    "SPEC_BLOCK_KIND",
    "chunk_id",
    "embedding_text",
    "spec_block_id",
    "split_file",
]

#: 无对应 symbol 行的兜底块伪 fqn（与 ``(preamble)`` / ``(front matter)`` 命名同风格）。
MODULE_FQN = "(module)"
SPEC_BLOCK_KIND = "spec_block"
FALLBACK_KIND = "fallback_block"
CLASS_SKELETON_KIND = "class_skeleton"

#: 需要"骨架 + 成员"切分的符号 kind（类/记录类型）。
SKELETON_KINDS = frozenset({"class", "struct"})
#: 只做结构容器、不成 chunk 的符号 kind（Module/01 §2.2 未列为检索单元）。
CONTAINER_KINDS = frozenset({"namespace"})

#: 同 id 兜底块的后缀分隔符（Module/01 §2.2「重载消歧：chunk_id 后缀消歧」）。
ID_DISAMBIGUATION_SEP = "#"

#: embedding 输入的正文字符上限（≈2048 词元安全上界；token 级截断归 provider，D-05）。
EMBEDDING_BODY_MAX_CHARS = 8_000

#: 签名跨行上限（防病态输入把整个文件当签名）。
_SIGNATURE_MAX_LINES = 40

_PY_STRING_PREFIX_RE = re.compile(r'^(?:[rRuUbBfF]{0,3})("""|\'\'\'|"|\')')


def chunk_id(path: str, symbol_fqn: str, start_line: int) -> str:
    """``{path}:{fqn}:{start_line}``（D-04）。"""
    return f"{path}:{symbol_fqn}:{start_line}"


def spec_block_id(block: SpecBlockDef) -> str:
    """spec 块 id：``{path}:{heading_path}:{start_line}``（与 ``Store`` 写库口径同源）。"""
    return chunk_id(block.path, block.heading_path, block.start_line)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def split_file(parsed: ParsedFile, content: str) -> list[ChunkDef]:
    """把解析结果切成可入库的 ``ChunkDef`` 列表（同文件全量，供 ``Store.apply_file_change``）。

    ``content`` 为该文件原文（CRLF/CR 会被规范化成 LF）。
    出口处校验 id 唯一性：命中重复直接抛 :class:`ValueError`（TASK-018 §B）。
    """
    lines = normalize_newlines(content).splitlines()
    chunks = (
        _fallback_chunks(parsed.path, "\n".join(lines))
        if parsed.fallback
        else _structural_chunks(parsed, lines)
    )
    ordered = sorted(chunks, key=_chunk_sort_key)
    unique = _disambiguate_fallback_ids(ordered)
    _reject_duplicate_ids(parsed.path, unique)
    return unique


def _disambiguate_fallback_ids(chunks: Sequence[ChunkDef]) -> list[ChunkDef]:
    """给"同一行被硬切出的多个兜底块"补 ``#N`` 后缀（按 Module/01 §2.2 的后缀消歧）。

    ``FALLBACK_MAX_CHARS`` 的字符硬切只在**找不到任何分隔符**时触发——也就是同一物理行内部。
    N 个片因此算出同一个 ``{path}:(module):{start_line}``：这是 D-04 的 id 方案
    （只用 ``start_line`` 消歧）在"一行多块"时的固有缺口，不是数据不一致。

    为什么不再让它整文件失败（TASK-018 §B 当时的处置）：实测代价是**静默丢掉整个文件**。
    2026-09-11 在 Obsidian 靶场命中 7 个压缩 JS/CSS（``make-md/styles.css`` 149KB 单行、
    ``make-md/main.js``、``obsidian-git/main.js``、``templater-obsidian/main.js``、
    ``heatmap-tracker``、``dataview``、``obsidian-custom-attachment-location``），
    全部被整文件跳过。它也不是"超大文件才有的病"——任何单行 > ``FALLBACK_MAX_CHARS``
    的文件（≈41KB 单行 JSON/CSS/压缩产物）都命中同一路径。

    **其它来源的重复不在此列**（同名同起始行的符号、同 heading 同起始行的 spec 块）：
    那些是真不一致，继续交给 :func:`_reject_duplicate_ids` 显式失败，不静默掩盖。

    后缀号 = 该 id 在 ``chunks`` 中的出现次序；入参已按 :func:`_chunk_sort_key` 排过序，
    而同一行硬切出的块 ``(start_line, end_line)`` 完全相同、``sorted`` 稳定，
    因此次序就是文档内顺序，后缀赋值是确定性的。
    """
    counts: dict[str, int] = {}
    result: list[ChunkDef] = []
    for chunk in chunks:
        counts[chunk.id] = counts.get(chunk.id, 0) + 1
        occurrence = counts[chunk.id]
        if occurrence == 1 or chunk.symbol_kind != FALLBACK_KIND:
            result.append(chunk)
            continue
        result.append(replace(chunk, id=f"{chunk.id}{ID_DISAMBIGUATION_SEP}{occurrence}"))
    return result


def _structural_chunks(parsed: ParsedFile, lines: list[str]) -> list[ChunkDef]:
    """符号 / spec 块 + 未覆盖行的兜底块（未排序、未去重）。"""
    chunks: list[ChunkDef] = []
    covered: list[tuple[int, int]] = []

    def add(chunk: ChunkDef) -> None:
        chunks.append(chunk)

    for spec in parsed.spec_blocks:
        span = _clamp(spec.start_line, spec.end_line, len(lines))
        if span is None:
            continue
        covered.append(span)
        add(
            ChunkDef(
                id=spec_block_id(spec),
                file_path=parsed.path,
                symbol_fqn=spec.heading_path,
                symbol_kind=SPEC_BLOCK_KIND,
                start_line=span[0],
                end_line=span[1],
                signature=spec.heading_path,
                docstring="",
                content=_slice(lines, *span),
                content_hash=chunk_content_hash(_slice(lines, *span)),
            )
        )

    members = _first_member_lines(parsed.symbols)
    for symbol in parsed.symbols:
        span = _clamp(symbol.start_line, symbol.end_line, len(lines))
        if span is None or symbol.kind in CONTAINER_KINDS:
            continue
        covered.append(span)
        chunk_span, kind = _symbol_span(symbol, span, members)
        text = _slice(lines, *chunk_span)
        signature, docstring = _header(lines, chunk_span, parsed.language)
        add(
            ChunkDef(
                id=chunk_id(parsed.path, symbol.fqn, symbol.start_line),
                file_path=parsed.path,
                symbol_fqn=symbol.fqn,
                symbol_kind=kind,
                start_line=chunk_span[0],
                end_line=chunk_span[1],
                signature=signature,
                docstring=docstring,
                content=text,
                content_hash=chunk_content_hash(text),
            )
        )

    for start, end in _gaps(len(lines), covered):
        for block in split_fallback(_slice(lines, start, end)):
            text = block.content
            if not text.strip():
                continue
            offset = start - 1
            block_start = block.start_line + offset
            block_end = block.end_line + offset
            add(
                ChunkDef(
                    id=chunk_id(parsed.path, MODULE_FQN, block_start),
                    file_path=parsed.path,
                    symbol_fqn=MODULE_FQN,
                    symbol_kind=FALLBACK_KIND,
                    start_line=block_start,
                    end_line=block_end,
                    signature="",
                    docstring="",
                    content=text,
                    content_hash=chunk_content_hash(text),
                )
            )

    return chunks


def _reject_duplicate_ids(path: str, chunks: Sequence[ChunkDef]) -> None:
    """重复 chunk id 显式失败（TASK-018 §B）：**禁止静默去重/静默丢弃**。

    兜底块 fqn 恒为 ``(module)``，一旦行号回跳或单行超长硬切，多个块会算出同一个
    ``{path}:(module):{start_line}``；若把冲突留到写库，只会变成难以定位的
    ``sqlite3.IntegrityError``（且整次 ingest 连带失败）。

    "单行硬切"这一类已由 :func:`_disambiguate_fallback_ids` 在出口前消歧（保数据、不给 id 撞车）；
    本函数是**最后一道不变量**：走到这里还有重复，就是切分逻辑真的错了，必须显式失败。
    """
    counts: dict[str, int] = {}
    for chunk in chunks:
        counts[chunk.id] = counts.get(chunk.id, 0) + 1
    duplicates = sorted((chunk_id, count) for chunk_id, count in counts.items() if count > 1)
    if not duplicates:
        return
    detail = "; ".join(f"{chunk_id} × {count}" for chunk_id, count in duplicates)
    raise ValueError(f"{path}: 切分产物出现重复 chunk id（禁止静默去重/丢弃）：{detail}")


def embedding_text(chunk: ChunkDef, *, body_max_chars: int = EMBEDDING_BODY_MAX_CHARS) -> str:
    """embedding 通道输入 = signature + docstring + 截断体（D-05）。

    ``body_max_chars`` 是字符级安全上界；token 级截断由 EmbeddingProvider 按
    ``EmbeddingProfile.max_input_tokens`` 执行（本函数不做 token 计数）。
    """
    if body_max_chars < 0:
        raise ValueError(f"body_max_chars 必须 ≥ 0，收到 {body_max_chars}")
    parts = [part for part in (chunk.signature, chunk.docstring) if part.strip()]
    body = _body_text(chunk)
    if body_max_chars and body.strip():
        parts.append(body[:body_max_chars])
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 内部：符号 / 类骨架
# ---------------------------------------------------------------------------


def _chunk_sort_key(chunk: ChunkDef) -> tuple[int, int, str]:
    return (chunk.start_line, chunk.end_line, chunk.id)


def _first_member_lines(symbols: Sequence[SymbolDef]) -> dict[str, int]:
    """容器符号（类/结构体）→ 首个直接成员符号的起始行。

    成员判定：fqn 以容器 fqn + ``::``/``.`` 开头（避免 ``Foo`` 误吞 ``Foobar``），
    且行区间落在容器内。只用于类骨架的声明区上界与"是否含成员"判断。
    """
    first: dict[str, int] = {}
    for container in symbols:
        if container.kind not in SKELETON_KINDS:
            continue
        for symbol in symbols:
            if symbol is container or not _is_member(container, symbol):
                continue
            current = first.get(container.fqn)
            if current is None or symbol.start_line < current:
                first[container.fqn] = symbol.start_line
    return first


def _is_member(container: SymbolDef, symbol: SymbolDef) -> bool:
    prefix = container.fqn
    if not symbol.fqn.startswith(prefix) or len(symbol.fqn) <= len(prefix):
        return False
    if symbol.fqn[len(prefix)] not in ":.":
        return False
    return container.start_line <= symbol.start_line <= container.end_line


def _symbol_span(
    symbol: SymbolDef, span: tuple[int, int], first_member: dict[str, int]
) -> tuple[tuple[int, int], str]:
    """返回 (chunk 行区间, symbol_kind)。

    - ``class`` 恒走骨架；``struct`` 仅在有直接成员时升格（C 记录类型保持 ``struct``）；
    - 骨架的声明区 = 容器起始行 → 首个直接成员的前一行（无成员则整段）；
    - 其余 kind 原样保留。
    """
    if symbol.kind != "class" and symbol.kind not in SKELETON_KINDS:
        return (span, symbol.kind)
    member_line = first_member.get(symbol.fqn)
    if symbol.kind != "class" and member_line is None:
        return (span, symbol.kind)
    if member_line is not None and member_line > span[0]:
        return ((span[0], member_line - 1), CLASS_SKELETON_KIND)
    return (span, CLASS_SKELETON_KIND)


# ---------------------------------------------------------------------------
# 内部：行的切片、覆盖区间与兜底
# ---------------------------------------------------------------------------


def _clamp(start: int, end: int, total: int) -> tuple[int, int] | None:
    """把符号行区间裁进 ``1..total``；越界/倒置 → None（诚实跳过，不猜）。"""
    if total <= 0 or start < 1 or end < start:
        return None
    return (start, min(end, total))


def _slice(lines: list[str], start: int, end: int) -> str:
    """行区间原文（1-based，含端点；块内以 ``\\n`` 连接，不含尾随换行）。"""
    return "\n".join(lines[start - 1 : end])


def _gaps(total: int, covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """未被覆盖的最大连续行段（1-based，含端点）。"""
    if total <= 0:
        return []
    merged: list[list[int]] = []
    for start, end in sorted(covered):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    gaps: list[tuple[int, int]] = []
    cursor = 1
    for start, end in merged:
        if start > cursor:
            gaps.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor <= total:
        gaps.append((cursor, total))
    return gaps


def _fallback_chunks(path: str, text: str) -> list[ChunkDef]:
    """整文件兜底切分（解析失败 / 未知语言）。"""
    chunks: list[ChunkDef] = []
    for block in split_fallback(text):
        if not block.content.strip():
            continue
        chunks.append(
            ChunkDef(
                id=chunk_id(path, MODULE_FQN, block.start_line),
                file_path=path,
                symbol_fqn=MODULE_FQN,
                symbol_kind=FALLBACK_KIND,
                start_line=block.start_line,
                end_line=block.end_line,
                signature="",
                docstring="",
                content=block.content,
                content_hash=chunk_content_hash(block.content),
            )
        )
    return chunks


# ---------------------------------------------------------------------------
# 内部：signature / docstring 提取（启发式，逐语言）
# ---------------------------------------------------------------------------


def _header(lines: list[str], span: tuple[int, int], language: str) -> tuple[str, str]:
    """返回 (signature, docstring)。"""
    if language == "python":
        signature, signature_end = _python_signature(lines, span)
        return (signature, _python_docstring(lines, signature_end + 1, span[1]))
    return (_c_signature(lines, span), "")


def _python_signature(lines: list[str], span: tuple[int, int]) -> tuple[str, int]:
    """装饰器 + def/class 头（到括号配平且以 ``:`` 收尾的行为止）。"""
    collected: list[str] = []
    depth = 0
    limit = min(span[1], span[0] + _SIGNATURE_MAX_LINES - 1)
    index = span[0]
    while index <= limit:
        line = lines[index - 1]
        collected.append(line)
        code = _python_code_part(line)
        depth += _bracket_delta(code)
        if depth <= 0 and code.rstrip().endswith(":"):
            break
        index += 1
    else:
        index = limit
    return ("\n".join(collected).rstrip(), index)


def _python_docstring(lines: list[str], start: int, end: int) -> str:
    """符号体首条字符串字面量（单行/多行/三引号/原始串；前缀与引号风格不限）。"""
    if start > end or start < 1 or start > len(lines):
        return ""
    stripped = lines[start - 1].strip()
    match = _PY_STRING_PREFIX_RE.match(stripped)
    if match is None:
        return ""
    quote = match.group(1)
    rest = stripped[match.end(1) :]
    if quote in rest:
        return rest.split(quote, 1)[0].strip()
    collected = [rest]
    for index in range(start + 1, min(end, len(lines)) + 1):
        line = lines[index - 1]
        if quote in line:
            collected.append(line.split(quote, 1)[0])
            break
        collected.append(line)
    return "\n".join(collected).strip()


def _python_code_part(line: str) -> str:
    """去掉行尾注释（引号内的 ``#`` 不算注释）。"""
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote is None:
            if char in "\"'":
                quote = char
            elif char == "#":
                return line[:index]
        elif char == "\\":
            index += 1
        elif char == quote:
            quote = None
        index += 1
    return line


def _bracket_delta(text: str) -> int:
    return (
        text.count("(")
        + text.count("[")
        + text.count("{")
        - text.count(")")
        - text.count("]")
        - text.count("}")
    )


def _c_signature(lines: list[str], span: tuple[int, int]) -> str:
    """声明文本：到 ``{`` / ``;`` 之前为止（多行声明按原样拼接）。"""
    collected: list[str] = []
    limit = min(span[1], span[0] + _SIGNATURE_MAX_LINES - 1)
    for index in range(span[0], limit + 1):
        line = lines[index - 1]
        cut = _body_cut(line)
        if cut is None:
            collected.append(line)
            continue
        prefix = line[:cut].rstrip()
        if prefix:
            collected.append(prefix)
        break
    return "\n".join(collected).rstrip() or _slice(lines, span[0], span[0]).strip()


def _body_cut(line: str) -> int | None:
    """行内 ``{`` 或 ``;`` 的位置（``#define`` 的续行反斜杠不影响本启发式）。"""
    positions = [pos for pos in (line.find("{"), line.find(";")) if pos >= 0]
    return min(positions) if positions else None


# ---------------------------------------------------------------------------
# 内部：embedding 正文
# ---------------------------------------------------------------------------


def _body_text(chunk: ChunkDef) -> str:
    """去掉已作为 signature 前置的声明文本后的正文（避免 embedding 输入重复声明）。"""
    content = chunk.content
    signature = chunk.signature
    if not signature:
        return content
    head = signature.strip()
    if content.lstrip().startswith(head):
        offset = content.index(head) + len(head)
        return content[offset:].lstrip("\n")
    return content
