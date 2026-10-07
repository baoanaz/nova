"""Chunk 切分：id 格式 / 类骨架 / 三通道输入 / content_hash 复用 / 兜底切分（TASK-006-A）。

DoD 覆盖：chunk_id 与重载消歧、类=骨架+方法、模块级兜底、content_hash 复用语义、
>800 行兜底切分不超限且不重叠、markdown SpecBlock 1:1、确定性。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from nova_core.chunking import (
    CLASS_SKELETON_KIND,
    FALLBACK_KIND,
    ID_DISAMBIGUATION_SEP,
    MODULE_FQN,
    SPEC_BLOCK_KIND,
    chunk_id,
    embedding_text,
    spec_block_id,
    split_file,
)
from nova_core.hashing import chunk_content_hash
from nova_core.parsing.cpp import CppParser
from nova_core.parsing.fallback import FALLBACK_MAX_CHARS, FALLBACK_MAX_LINES
from nova_core.storage import Store
from nova_core.types import ParsedFile

MODULE_SOURCE = '''import os

CONFIG: dict = {}


def refresh(token):
    """刷新 token。"""
    return token


class TokenService:
    """Token 服务。"""

    NAME = "token"

    def refresh(self):
        return "a"

    def revoke(self):
        return "b"
'''


def _by_fqn(chunks, fqn):  # type: ignore[no-untyped-def]
    return {chunk.symbol_fqn: chunk for chunk in chunks if chunk.symbol_fqn == fqn}


def _chunks_by_symbol(parsed: ParsedFile, source: str):  # type: ignore[no-untyped-def]
    """fqn → chunk（fixture 内 fqn 唯一）+ 同时校验 id 与符号行号口径一致。"""
    chunks = split_file(parsed, source)
    by_fqn = {chunk.symbol_fqn: chunk for chunk in chunks if chunk.symbol_fqn}
    for symbol in parsed.symbols:
        assert by_fqn[symbol.fqn].id == chunk_id(parsed.path, symbol.fqn, symbol.start_line)
    return by_fqn


def test_chunk_id_format_and_overload_disambiguation() -> None:
    from nova_core.types import SymbolDef

    parsed = ParsedFile(
        path="src/a.c",
        language="c",
        symbols=(
            SymbolDef(name="parse", fqn="parse", kind="function", start_line=1, end_line=3),
            SymbolDef(name="parse", fqn="parse", kind="function", start_line=4, end_line=7),
        ),
    )
    source = "\n".join(f"line {i}" for i in range(1, 8))
    chunks = split_file(parsed, source)

    assert [chunk.id for chunk in chunks] == ["src/a.c:parse:1", "src/a.c:parse:4"]
    assert chunk_id("src/a.c", "parse", 1) == "src/a.c:parse:1"
    assert len({chunk.id for chunk in chunks}) == 2


def test_python_class_is_skeleton_plus_method_chunks(parse_source, store: Store) -> None:
    parsed = parse_source("mod.py", MODULE_SOURCE)
    chunks = split_file(parsed, MODULE_SOURCE)
    kinds = {chunk.symbol_fqn: chunk.symbol_kind for chunk in chunks}

    assert kinds["TokenService"] == CLASS_SKELETON_KIND
    assert kinds["TokenService.refresh"] == "method"
    assert kinds["TokenService.revoke"] == "method"
    assert kinds["refresh"] == "function"
    assert kinds["CONFIG"] == "variable"

    skeleton = _by_fqn(chunks, "TokenService")["TokenService"]
    # 骨架只覆盖声明区（class 行 → 首个成员前一行），不含方法体
    assert skeleton.content.splitlines()[-1].strip() == 'NAME = "token"'
    assert "return" not in skeleton.content
    assert skeleton.signature == "class TokenService:"
    assert skeleton.docstring == "Token 服务。"


def test_python_module_level_code_gets_fallback_chunk(parse_source) -> None:
    parsed = parse_source("mod.py", MODULE_SOURCE)
    chunks = split_file(parsed, MODULE_SOURCE)
    module_chunks = [chunk for chunk in chunks if chunk.symbol_fqn == MODULE_FQN]

    assert [chunk.content for chunk in module_chunks] == ["import os\n"]
    assert module_chunks[0].content == MODULE_SOURCE.splitlines(keepends=True)[0]
    assert module_chunks[0].symbol_kind == FALLBACK_KIND
    assert module_chunks[0].id == "mod.py:(module):1"
    assert module_chunks[0].signature == "" and module_chunks[0].docstring == ""


def test_chunk_order_and_determinism(parse_source) -> None:
    parsed = parse_source("mod.py", MODULE_SOURCE)
    first = split_file(parsed, MODULE_SOURCE)
    second = split_file(parsed, MODULE_SOURCE)

    assert first == second
    keys = [(chunk.start_line, chunk.end_line, chunk.id) for chunk in first]
    assert keys == sorted(keys)


def test_content_hash_reuse_semantics(parse_source) -> None:
    """改函数体 → 该 chunk hash 变；改文件别处 → 该 chunk hash 不变。"""
    original = parse_source("mod.py", MODULE_SOURCE)
    original_chunks = _chunks_by_symbol(original, MODULE_SOURCE)
    original_hashes = {chunk.id: chunk.content_hash for chunk in original_chunks.values()}

    trimmed = MODULE_SOURCE.replace('return "a"', 'return "changed"')
    modified = parse_source("mod.py", trimmed)
    modified_hashes = {
        chunk.id: chunk.content_hash for chunk in split_file(modified, trimmed)
    }

    assert set(original_hashes) == set(modified_hashes)
    changed = {key for key in original_hashes if original_hashes[key] != modified_hashes[key]}
    changed_method = original_chunks["TokenService.refresh"]
    assert changed == {changed_method.id}
    # 类骨架不含方法体 → 方法体变更不影响骨架 chunk
    assert original_hashes
    skeleton = original_chunks["TokenService"]
    assert original_hashes[skeleton.id] == modified_hashes[skeleton.id]

    appended_source = MODULE_SOURCE + "\n\ndef extra():\n    return 0\n"
    appended = parse_source("mod.py", appended_source)
    appended_hashes = {
        chunk.id: chunk.content_hash for chunk in split_file(appended, appended_source)
    }
    for key, value in original_hashes.items():
        assert appended_hashes[key] == value


def test_content_hash_matches_contract_helper(parse_source) -> None:
    parsed = parse_source("mod.py", MODULE_SOURCE)
    for chunk in split_file(parsed, MODULE_SOURCE):
        assert chunk.content_hash == chunk_content_hash(chunk.content)


def test_spec_blocks_become_chunks_with_shared_id(parse_source, ingest_file) -> None:
    source = """# 架构

用 `TokenService.refresh()` 刷新。

## 认证

细节。
"""
    parsed, _ = ingest_file("docs/a.md", source)
    chunks = split_file(parsed, source)

    assert [chunk.symbol_kind for chunk in chunks] == [SPEC_BLOCK_KIND] * len(parsed.spec_blocks)
    by_fqn = {chunk.symbol_fqn: chunk for chunk in chunks}
    assert set(by_fqn) == {"架构", "架构 > 认证"}
    assert by_fqn["架构 > 认证"].signature == "架构 > 认证"
    # 双表同 id（CF-01 规划期裁定 2）
    assert any(
        by_fqn[block.heading_path].id == spec_block_id(block) for block in parsed.spec_blocks
    )


def test_markdown_has_no_residual_fallback_chunks(parse_source) -> None:
    source = "前言\n\n# 标题\n\n正文\n"
    parsed = parse_source("docs/a.md", source)
    chunks = split_file(parsed, source)

    assert [chunk.symbol_kind for chunk in chunks] == [SPEC_BLOCK_KIND, SPEC_BLOCK_KIND]
    assert all(chunk.symbol_fqn != MODULE_FQN for chunk in chunks)


def test_namespace_symbol_does_not_become_chunk(parse_source) -> None:
    source = "namespace ns {\n\nint add(int a, int b) { return a + b; }\n\n}\n"
    parsed = parse_source("src/b.cpp", source)
    fqns = {chunk.symbol_fqn for chunk in split_file(parsed, source)}

    assert fqns == {"ns::add", MODULE_FQN}


def test_cpp_struct_with_method_splits_skeleton_and_method(parse_source) -> None:
    source = """struct Vec {
    double x;
    int size() const { return 1; }
};

struct Plain {
    int y;
};
"""
    parsed = parse_source("src/b.hpp", source)
    kinds = {chunk.symbol_fqn: chunk.symbol_kind for chunk in split_file(parsed, source)}

    assert kinds["Vec"] == CLASS_SKELETON_KIND
    assert kinds["Vec::size"] == "method"
    assert kinds["Plain"] == "struct"  # 无方法的 C 记录类型保持原 kind


def test_fallback_split_of_large_file(parse_source) -> None:
    """>800 行兜底文件：切分不超限、块间不重叠、内容连续且覆盖全文。"""
    source = "\n".join(f"line {index}" for index in range(1, 1901))
    parsed = ParsedFile(path="junk.xyz", language="fallback", fallback=True)
    chunks = split_file(parsed, source)

    assert len(chunks) > 1
    assert all(chunk.symbol_kind == FALLBACK_KIND for chunk in chunks)
    assert all(chunk.end_line - chunk.start_line + 1 <= FALLBACK_MAX_LINES for chunk in chunks)
    for earlier, later in zip(chunks, chunks[1:], strict=False):
        assert earlier.end_line < later.start_line
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 1900
    for chunk in chunks:
        assert f"line {chunk.start_line}" in chunk.content
        assert f"line {chunk.end_line}" in chunk.content


def test_partial_parse_uses_structural_and_fallback_chunks() -> None:
    source = """int before() { return 1; }
struct Broken {
  int value GUARDED_BY(mu);
};
int after() { return 2; }
"""
    parsed = CppParser().parse("partial.cpp", source)
    chunks = split_file(parsed, source)

    assert parsed.fallback is False
    assert [chunk.symbol_fqn for chunk in chunks] == ["before", MODULE_FQN, "after"]
    assert [chunk.symbol_kind for chunk in chunks] == ["function", FALLBACK_KIND, "function"]
    assert "GUARDED_BY" in chunks[1].content


def test_parse_failure_ignores_symbols_entirely() -> None:
    from nova_core.types import SymbolDef

    parsed = ParsedFile(
        path="broken.py",
        language="python",
        symbols=(SymbolDef(name="ghost", fqn="ghost", kind="function", start_line=1, end_line=1),),
        parse_errors=("L1: syntax error",),
        fallback=True,
    )
    chunks = split_file(parsed, "def broken(:\n")

    assert [chunk.symbol_kind for chunk in chunks] == [FALLBACK_KIND]
    assert chunks[0].symbol_fqn == MODULE_FQN


def test_empty_file_produces_no_chunks() -> None:
    parsed = ParsedFile(path="empty.py", language="python")
    assert split_file(parsed, "\n\n") == []


def test_embedding_text_includes_signature_docstring_and_capped_body(parse_source) -> None:
    parsed = parse_source("mod.py", MODULE_SOURCE)
    chunks = _chunks_by_symbol(parsed, MODULE_SOURCE)
    text = embedding_text(chunks["refresh"])

    assert text.startswith("def refresh(token):")
    assert "刷新" in text
    assert "return token" in text

    full_body = embedding_text(chunks["refresh"]).split("\n\n")[-1]
    capped = embedding_text(chunks["refresh"], body_max_chars=5)
    assert capped.split("\n\n")[-1] == full_body[:5]
    assert len(capped) < len(embedding_text(chunks["refresh"]))
    assert embedding_text(chunks["refresh"], body_max_chars=0).endswith("刷新 token。")


def test_ingest_of_split_chunks_writes_consistent_rows(ingest_file, store: Store) -> None:
    parsed, delta = ingest_file("mod.py", MODULE_SOURCE)
    chunks = split_file(parsed, MODULE_SOURCE)

    assert set(delta.new_chunk_ids) == {chunk.id for chunk in chunks}
    assert store.counts()["chunks"] == len(chunks)
    assert store.counts()["symbols"] == len(parsed.symbols)
    for chunk in chunks:
        stored = store.chunk_by_id(chunk.id)
        assert stored is not None
        assert stored.content_hash == chunk.content_hash


def test_split_file_reports_symbol_kinds_for_each_language(parse_source) -> None:
    c_source = "#define MAX(a, b) ((a) > (b) ? (a) : (b))\n\nenum Color { RED };\n"
    parsed = parse_source("src/a.c", c_source)
    kinds = {chunk.symbol_fqn: chunk.symbol_kind for chunk in split_file(parsed, c_source)}

    assert kinds == {"MAX": "macro", "Color": "enum"}


def test_callable_fixtures_are_wired(parse_source: Callable[[str, str], ParsedFile]) -> None:
    assert parse_source("mod.py", MODULE_SOURCE).path == "mod.py"


# ---------------------------------------------------------------------------
# TASK-018 §B：chunk id 唯一性防御
# ---------------------------------------------------------------------------


def test_duplicate_chunk_ids_raise_value_error() -> None:
    """人为重复 id → 带明细的 ValueError，而不是静默去重/写库时才爆 IntegrityError。"""
    from nova_core.types import SymbolDef

    duplicate = SymbolDef(name="dup", fqn="dup", kind="function", start_line=1, end_line=3)
    parsed = ParsedFile(path="src/dup.py", language="python", symbols=(duplicate, duplicate))
    source = "def dup():\n    a = 1\n    return a\n"

    with pytest.raises(ValueError) as excinfo:
        split_file(parsed, source)

    message = str(excinfo.value)
    assert message.startswith("src/dup.py: ")
    assert "src/dup.py:dup:1" in message
    assert "重复 chunk id" in message


def test_oversized_single_line_fallback_is_disambiguated_not_lost() -> None:
    """单行超长硬切出的兜底块同 id（fqn 恒为 ``(module)``）→ 补 ``#N`` 后缀，**不丢文件**。

    TASK-018 §B 当时的选择是抛 ValueError 让该文件整份跳过（当时的代价小、能先解除阻断）；
    TASK-036 §B 在 Obsidian 靶场量到真实代价：7 个压缩 JS/CSS 因此全部被跳过。硬切只发生在
    "找不到任何分隔符"时——就是同一物理行内部，不是数据不一致，而是 D-04 的 id 方案
    （只用 ``start_line`` 消歧）在一行多块时的固有缺口；Module/01 §2.2 已预留后缀消歧。
    """
    parsed = ParsedFile(path="assets/blob.bin", language="fallback", fallback=True)
    source = "x" * (FALLBACK_MAX_CHARS * 2 + 10)

    chunks = split_file(parsed, source)

    assert [chunk.id for chunk in chunks] == [
        f"assets/blob.bin:{MODULE_FQN}:1",
        f"assets/blob.bin:{MODULE_FQN}:1{ID_DISAMBIGUATION_SEP}2",
        f"assets/blob.bin:{MODULE_FQN}:1{ID_DISAMBIGUATION_SEP}3",
    ], "首块保留原 id，后续块按出现次序附 #N"
    assert "".join(chunk.content for chunk in chunks) == source, "消歧不得丢字符/重排"
    assert {chunk.start_line for chunk in chunks} == {1}, "硬切块共享同一物理行（缺陷现场）"


def test_disambiguation_is_deterministic_and_ordered() -> None:
    """同输入两次调用逐字段相等（纯函数），且后缀号按文档内顺序递增。"""
    parsed = ParsedFile(path="m.bin", language="fallback", fallback=True)
    source = "y" * (FALLBACK_MAX_CHARS * 4 + 7)

    first = split_file(parsed, source)
    second = split_file(parsed, source)

    assert first == second
    ids = [chunk.id for chunk in first]
    assert ids == sorted(ids, key=_id_occurrence)
    assert "".join(chunk.content for chunk in first) == source


def _id_occurrence(chunk_id: str) -> int:
    """从 ``...#N`` 取出现次序（无后缀 = 1），用于断言文档内顺序。"""
    _, _, tail = chunk_id.rpartition(ID_DISAMBIGUATION_SEP)
    return int(tail) if tail.isdigit() else 1


def test_structural_duplicates_still_fail_loudly() -> None:
    """非兜底来源的真不一致仍显式失败（消歧不把 bug 掩盖成“能跑”）。"""
    from nova_core.types import SymbolDef

    duplicate = SymbolDef(name="dup", fqn="dup", kind="function", start_line=1, end_line=3)
    parsed = ParsedFile(path="src/dup.py", language="python", symbols=(duplicate, duplicate))

    with pytest.raises(ValueError, match="重复 chunk id"):
        split_file(parsed, "def dup():\n    a = 1\n    return a\n")
