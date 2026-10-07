"""C 抽取器（TASK-003）。

规则来源：docs/design/Module/01 §2.2（C 表）+ 任务卡 TASK-003"抽取规则"。

本模块同时是 TASK-004（C++）的复用面（卡内约定：可复用工具放本文件，不得放进 base.py）：

- ``IncludeDirective`` / ``parse_include_directive`` / ``resolve_include``：include 解析
  （相对当前文件目录优先 → 仓库内唯一同名猜测 → 解不开返回 None，不猜）；
- ``FunctionPointerMap``：文件内函数指针候选表（赋值 / 声明初始化 / 设计化初始化）；
- ``filter_visible``：跨文件名称匹配的可见性过滤（static 仅文件内可见）；
- ``declarator_name`` / ``is_function_pointer_declarator``：C 声明符工具。

口径记录（完整版见任务卡执行记录）：

- 符号 = ``function_definition`` / 有 body 的 ``struct_specifier``·``union_specifier``·
  ``enum_specifier`` / ``type_definition``（typedef）/ ``preproc_function_def`` / ``preproc_def``；
  函数原型（无 body 的声明）与全局变量**不成符号**（按卡内符号表，V1 口径）；
- union 归入 kind=``struct``（设计 kind 表无 union，二者同为记录类型）；
- 匿名 struct/enum 不成符号（无稳定 fqn），其外层 typedef 承载名字；
- ``typedef struct Point {...} Point;`` 同名 tag 时只保留 struct 符号
  （避免同 fqn 同 start_line 撞 chunk_id）；
- fqn = 短名（C 无嵌套作用域，chunk_id 已含 path，D-04）；
- static 符号 ``is_exported=False``（跨文件同名匹配必须穿过可见性过滤）；
- 头文件宏（.h/.hpp/...）``is_exported=True``，源文件宏 False（宏不跨编译单元）；
- 函数指针：赋值 / 声明初始化 / 设计化初始化收集候选，调用点对**全部**候选合成
  ``calls`` 边（provenance='synthesized'，宁多勿漏；rerank 对 synthesized 降权）；
- 复杂宏（``##`` token pasting / 可变参数）不展开，建符号 + ``parse_errors`` 如实标注；
- 解不开的 include → ``unresolved(kind='import')``，不做猜测性连边。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

import tree_sitter as ts

from nova_core.parsing.base import (
    Extraction,
    FileContext,
    TreeSitterParser,
    end_line_of,
    line_of,
)
from nova_core.types import SymbolDef

#: 条件编译容器：其中的声明同样抽取（真实仓库大量 `#ifdef` 包裹）
_PREPROC_CONTAINERS = frozenset(
    {"preproc_ifdef", "preproc_if", "preproc_else", "preproc_elif", "linkage_specification"}
)
#: 记录类型（union 归入 struct，见模块 docstring）
_TAG_TYPES = frozenset({"struct_specifier", "union_specifier", "enum_specifier"})
#: 声明符链上可能出现的中间节点
_DECLARATOR_TYPES = frozenset(
    {
        "init_declarator",
        "function_declarator",
        "pointer_declarator",
        "parenthesized_declarator",
        "array_declarator",
        "attributed_declarator",
    }
)
_IDENTIFIER_TYPES = frozenset({"identifier", "field_identifier", "type_identifier"})
#: 头文件扩展名（宏可见性判定）
_HEADER_SUFFIXES = (".h", ".hh", ".hpp", ".hxx", ".h++", ".inc", ".def")

_INCLUDE_PATTERN = re.compile(r"^\s*#\s*include\s*(?:\"(?P<quoted>[^\"]+)\"|<(?P<angle>[^>]+)>)")


class CParser(TreeSitterParser):
    """C（tree-sitter）抽取器。"""

    language = "c"
    grammar_module = "tree_sitter_c"
    recover_syntax_errors = True

    def extract(self, root: ts.Node, ctx: FileContext, out: Extraction) -> None:
        pointers = FunctionPointerMap()
        self._walk(root, ctx, out, pointers)  # 符号 + include + 宏
        _collect_pointers(root, ctx, pointers)  # 函数指针候选（声明初始化 + 赋值，全文扫）
        self._scan_calls(root, ctx, out, ctx.path, pointers)
    # -- pass 1：符号 / include / 宏 / fp 候选 ---------------------------------

    def _walk(
        self, node: ts.Node, ctx: FileContext, out: Extraction, pointers: FunctionPointerMap
    ) -> None:
        for child in node.named_children:
            node_type = child.type
            if node_type == "function_definition":
                self._function(child, ctx, out)
            elif node_type == "declaration":
                self._declaration(child, ctx, out)
            elif node_type == "type_definition":
                self._type_definition(child, ctx, out)
            elif node_type in _TAG_TYPES:
                self._tag(child, ctx, out)
            elif node_type == "preproc_include":
                self._include(child, ctx, out)
            elif node_type == "preproc_def":
                self._object_macro(child, ctx, out)
            elif node_type == "preproc_function_def":
                self._function_macro(child, ctx, out)
            elif node_type in _PREPROC_CONTAINERS:
                self._walk(child, ctx, out, pointers)

    def _function(self, node: ts.Node, ctx: FileContext, out: Extraction) -> None:
        name = declarator_name(node.child_by_field_name("declarator"), ctx)
        if name is None:
            return
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=name,
                kind="function",
                start_line=line_of(node),
                end_line=_node_end_line(node, ctx),
                is_exported=not _has_static(node, ctx),
            )
        )

    def _declaration(self, node: ts.Node, ctx: FileContext, out: Extraction) -> None:
        type_node = node.child_by_field_name("type")
        if type_node is not None and type_node.type in _TAG_TYPES:
            self._tag(type_node, ctx, out)  # `struct Point { ... } p;` 内联定义

    def _type_definition(self, node: ts.Node, ctx: FileContext, out: Extraction) -> None:
        type_node = node.child_by_field_name("type")
        declarator = node.child_by_field_name("declarator")
        name = declarator_name(declarator, ctx)
        tag = None
        if type_node is not None and type_node.type in _TAG_TYPES:
            self._tag(type_node, ctx, out)
            tag_node = type_node.child_by_field_name("name")
            tag = ctx.text(tag_node) if tag_node is not None else None
        if name and name != tag:  # `typedef struct Point {...} Point;` 只留 struct 符号
            out.add_symbol(
                SymbolDef(
                    name=name,
                    fqn=name,
                    kind="typedef",
                    start_line=line_of(node),
                    end_line=_node_end_line(node, ctx),
                    is_exported=True,
                )
            )

    def _tag(self, node: ts.Node, ctx: FileContext, out: Extraction) -> None:
        if node.child_by_field_name("body") is None:
            return  # 前向声明（`struct Ops;`）不成符号：没有可切分的内容
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return  # 匿名 struct/enum：无稳定 fqn，由外层 typedef 承载
        name = ctx.text(name_node)
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=name,
                kind="struct" if node.type != "enum_specifier" else "enum",
                start_line=line_of(node),
                end_line=_node_end_line(node, ctx),
                is_exported=True,
            )
        )

    def _include(self, node: ts.Node, ctx: FileContext, out: Extraction) -> None:
        path_node = node.child_by_field_name("path")
        if path_node is None:
            return
        directive = parse_include_directive(ctx.text(node), line_of(node))
        if directive is None:
            out.add_unresolved(ctx.path, ctx.text(node), "import", line_of(node))
            return
        target = resolve_include(directive, ctx.path)
        if target is None:
            out.add_unresolved(ctx.path, directive.spelled, "import", line_of(node))
        else:
            out.add_edge(ctx.path, target, "imports", line_of(node))

    def _object_macro(self, node: ts.Node, ctx: FileContext, out: Extraction) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = ctx.text(name_node)
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=name,
                kind="macro",
                start_line=line_of(node),
                end_line=_node_end_line(node, ctx),
                is_exported=_macro_exported(ctx.path),
            )
        )
        value = node.child_by_field_name("value")
        if value is not None and "##" in ctx.text(value):
            out.parse_errors.append(
                f"L{line_of(node)}: macro {name} uses ## (token pasting); not expanded"
            )

    def _function_macro(self, node: ts.Node, ctx: FileContext, out: Extraction) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = ctx.text(name_node)
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=name,
                kind="macro",
                start_line=line_of(node),
                end_line=_node_end_line(node, ctx),
                is_exported=_macro_exported(ctx.path),
            )
        )
        parameters = node.child_by_field_name("parameters")
        value = node.child_by_field_name("value")
        reasons = []
        if parameters is not None and "..." in ctx.text(parameters):
            reasons.append("variadic")
        if value is not None and "##" in ctx.text(value):
            reasons.append("token pasting")
        if reasons:
            out.parse_errors.append(
                f"L{line_of(node)}: macro {name} uses {'/'.join(reasons)}; not expanded"
            )

    # -- pass 2：调用边（含函数指针合成） --------------------------------------

    def _scan_calls(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        owner: str,
        pointers: FunctionPointerMap,
    ) -> None:
        for child in node.named_children:
            if child.type == "function_definition":
                name = declarator_name(child.child_by_field_name("declarator"), ctx)
                body = child.child_by_field_name("body")
                if body is not None:
                    self._scan_calls(body, ctx, out, name or ctx.path, pointers)
                continue
            if child.type == "call_expression":
                self._call(child, ctx, out, owner, pointers)
            self._scan_calls(child, ctx, out, owner, pointers)

    def _call(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        owner: str,
        pointers: FunctionPointerMap,
    ) -> None:
        function = node.child_by_field_name("function")
        line = line_of(node)
        if function is None:
            out.add_unresolved(owner, ctx.text(node), "call", line)
            return
        if function.type == "identifier":
            name = ctx.text(function)
            candidates = pointers.get(name)
            if candidates:
                _synthesize(out, owner, candidates, line)
                return
            out.add_edge(owner, name, "calls", line)
            return
        if function.type == "field_expression":
            field_node = function.child_by_field_name("field")
            field_name = ctx.text(field_node) if field_node is not None else ""
            candidates = pointers.get(field_name)
            if candidates:  # 设计化初始化登记的函数指针字段（宁多勿漏）
                _synthesize(out, owner, candidates, line)
                return
            out.add_edge(owner, ctx.text(function), "calls", line)
            return
        if function.type == "subscript_expression":
            argument = function.child_by_field_name("argument")
            if argument is not None and argument.type == "identifier":
                candidates = pointers.get(ctx.text(argument))
                if candidates:  # 函数指针数组：下标不定型，全候选合成
                    _synthesize(out, owner, candidates, line)
                    return
            out.add_unresolved(owner, ctx.text(function), "call", line)
            return
        out.add_unresolved(owner, ctx.text(function), "call", line)


def _synthesize(out: Extraction, owner: str, candidates: Sequence[str], line: int) -> None:
    for candidate in candidates:
        out.add_edge(owner, candidate, "calls", line, provenance="synthesized")


# -- 复用工具：include 解析 ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class IncludeDirective:
    """`#include` 指令：raw 为指令原文，spelled 为目标的书写形态（含定界符），target 为裸名。"""

    raw: str
    spelled: str
    target: str
    system: bool
    line: int


def parse_include_directive(text: str, line: int) -> IncludeDirective | None:
    """解析 `#include "x.h"` / `#include <x.h>`；宏形式的 include 返回 None。"""
    match = _INCLUDE_PATTERN.match(text)
    if match is None:
        return None
    if match.group("quoted") is not None:
        target = match.group("quoted")
        return IncludeDirective(
            raw=text.strip(), spelled=f'"{target}"', target=target, system=False, line=line
        )
    target = match.group("angle")
    return IncludeDirective(
        raw=text.strip(), spelled=f"<{target}>", target=target, system=True, line=line
    )


def resolve_include(
    directive: IncludeDirective, from_path: str, repo_paths: Iterable[str] | None = None
) -> str | None:
    """解析 include 目标为仓库相对路径；解不开返回 None（调用方进 unresolved，不猜）。

    规则（任务卡）：``"x.h"`` 相对当前文件目录优先；``<x.h>`` 无 build 信息时按仓库内唯一同名
    猜测；``repo_paths`` 为 None 表示单文件解析（无仓库信息），此时 ``<>`` 一律解不开。
    """
    if directive.system:
        return _unique_basename(directive.target, repo_paths)
    relative = _join_relative(from_path, directive.target)
    if repo_paths is None:
        return relative  # 单文件模式：相对路径是确定性结果，是否存在交 TASK-006 用文件表校验
    known = set(repo_paths)
    if relative is not None and relative in known:
        return relative
    return _unique_basename(directive.target, repo_paths)


def _join_relative(from_path: str, target: str) -> str | None:
    """相对当前文件目录拼接并折叠 ./ ..；越出仓库根返回 None。"""
    base = from_path.rsplit("/", 1)[0] if "/" in from_path else ""
    stack: list[str] = []
    for part in (*base.split("/"), *target.split("/")):
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                return None
            stack.pop()
            continue
        stack.append(part)
    return "/".join(stack) or None


def _unique_basename(target: str, repo_paths: Iterable[str] | None) -> str | None:
    """仓库内唯一同名猜测；0 个或多个命中都返回 None（歧义不猜）。"""
    if repo_paths is None:
        return None
    basename = target.rsplit("/", 1)[-1]
    matches = [path for path in repo_paths if path.rsplit("/", 1)[-1] == basename]
    return matches[0] if len(matches) == 1 else None


# -- 复用工具：声明符 / 可见性 / 函数指针表 ------------------------------------


def declarator_name(node: ts.Node | None, ctx: FileContext) -> str | None:
    """沿声明符链取符号名（``(*fp)(int)`` → ``fp``；``A::foo`` 由 C++ 侧处理）。"""
    current = node
    while current is not None:
        if current.type in _IDENTIFIER_TYPES:
            return ctx.text(current)
        inner = current.child_by_field_name("declarator")
        if inner is None:
            inner = next(
                (child for child in current.named_children if child.type in _DECLARATOR_TYPES),
                None,
            )
        current = inner
    return None


def is_function_pointer_declarator(declarator: ts.Node) -> bool:
    """声明符是否为函数指针（``(*name)(...)`` / 函数指针数组／typedef 函数指针别名）。"""
    for node in _declarator_chain(declarator):
        if node.type != "function_declarator":
            continue
        inner = node.child_by_field_name("declarator")
        # 判据：参数列表内侧是「括号包裹的指针声明符」；`int *foo(void)`（返回指针的函数）不满足
        if inner is not None and _contains_declarator_kind(inner, "pointer_declarator"):
            return True
    return False


def filter_visible(
    candidates: Sequence[tuple[str, SymbolDef]], from_path: str
) -> tuple[SymbolDef, ...]:
    """跨文件匹配的可见性过滤：``is_exported=False`` 的符号（C 的 static）只在同文件可见。"""
    return tuple(
        symbol for path, symbol in candidates if symbol.is_exported or path == from_path
    )


class FunctionPointerMap:
    """文件内函数指针候选表：变量名 / 设计化字段名 → 目标名（顺序稳定，供 TASK-004 复用）。"""

    def __init__(self) -> None:
        self._targets: dict[str, list[str]] = {}

    def add(self, key: str, targets: Sequence[str]) -> None:
        if not key or not targets:
            return
        bucket = self._targets.setdefault(key, [])
        for target in targets:
            if target not in bucket:
                bucket.append(target)

    def get(self, key: str) -> tuple[str, ...]:
        return tuple(self._targets.get(key, ()))


def _declarator_chain(node: ts.Node) -> list[ts.Node]:
    chain: list[ts.Node] = []
    current: ts.Node | None = node
    while current is not None and current.type in _DECLARATOR_TYPES:
        chain.append(current)
        inner = current.child_by_field_name("declarator")
        if inner is None:
            inner = next(
                (child for child in current.named_children if child.type in _DECLARATOR_TYPES),
                None,
            )
        current = inner
    return chain


def _contains_declarator_kind(node: ts.Node, kind: str) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == kind:
            return True
        stack.extend(current.children)
    return False


def _node_end_line(node: ts.Node, ctx: FileContext) -> int:
    """符号结束行：预处理指令节点把行尾换行算进节点，这里回退一行（function/struct 不受影响）。"""
    end = end_line_of(node)
    if ctx.text(node).endswith("\n"):
        end -= 1
    return max(end, line_of(node))


def _has_static(node: ts.Node, ctx: FileContext) -> bool:
    return any(
        child.type == "storage_class_specifier" and ctx.text(child) == "static"
        for child in node.children
    )


def _macro_exported(path: str) -> bool:
    return path.lower().endswith(_HEADER_SUFFIXES)


def _function_targets(node: ts.Node, ctx: FileContext) -> tuple[str, ...]:
    """初始化表达式 → 候选函数名（``func`` / ``&func`` / 括号包裹）。"""
    if node.type == "identifier":
        return (ctx.text(node),)
    if node.type in ("unary_expression", "pointer_expression"):
        operator = node.child_by_field_name("operator")
        argument = node.child_by_field_name("argument")
        if argument is not None and (operator is None or ctx.text(operator) == "&"):
            return _function_targets(argument, ctx)
        return ()
    if node.type == "parenthesized_expression":
        inner = next((child for child in node.named_children), None)
        return _function_targets(inner, ctx) if inner is not None else ()
    if node.type == "cast_expression":
        value = node.child_by_field_name("value")
        return _function_targets(value, ctx) if value is not None else ()
    return ()


def _initializer_targets(
    node: ts.Node, ctx: FileContext, pointers: FunctionPointerMap, variable_name: str
) -> tuple[str, ...]:
    """声明初始化表达式 → 候选：设计化初始化登记到字段名，位置式登记到变量名。"""
    if node.type != "initializer_list":
        return _function_targets(node, ctx)
    positional: list[str] = []
    for element in node.named_children:
        if element.type == "initializer_pair":
            designator = element.child_by_field_name("designator")
            value = element.child_by_field_name("value")
            field_name = _field_designator_name(designator, ctx)
            if field_name and value is not None:
                pointers.add(field_name, _function_targets(value, ctx))
            continue
        positional.extend(_function_targets(element, ctx))
    return tuple(positional)


def _field_designator_name(node: ts.Node | None, ctx: FileContext) -> str | None:
    if node is None:
        return None
    for child in node.named_children:
        if child.type in _IDENTIFIER_TYPES:
            return ctx.text(child)
    return None


def _collect_pointers(node: ts.Node, ctx: FileContext, pointers: FunctionPointerMap) -> None:
    """全文收集函数指针候选：声明初始化（``fp = func`` / ``= {add, sub}`` / ``.apply = mul``）
    与赋值语句（``fp = &func``）。文件内全局收集 → 调用点全候选合成（宁多勿漏）。"""
    _collect_pointers_inner(node, ctx, pointers, _function_pointer_typedefs(node, ctx))


def _function_pointer_typedefs(node: ts.Node, ctx: FileContext) -> frozenset[str]:
    """函数指针 typedef 别名（``typedef int (*binop)(int, int);`` → binop），供类型名推导。"""
    names: set[str] = set()
    for current in _walk_nodes(node):
        if current.type != "type_definition":
            continue
        declarator = current.child_by_field_name("declarator")
        if declarator is None or not is_function_pointer_declarator(declarator):
            continue
        name = declarator_name(declarator, ctx)
        if name:
            names.add(name)
    return frozenset(names)


def _walk_nodes(node: ts.Node) -> Iterator[ts.Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _collect_pointers_inner(
    node: ts.Node, ctx: FileContext, pointers: FunctionPointerMap, fp_typedefs: frozenset[str]
) -> None:
    for child in node.named_children:
        if child.type == "declaration":
            type_node = child.child_by_field_name("type")
            type_name = ctx.text(type_node) if type_node is not None else ""
            for declarator in child.named_children:
                if declarator.type != "init_declarator":
                    continue
                name = declarator_name(declarator, ctx)
                value = declarator.child_by_field_name("value")
                if name is None or value is None:
                    continue
                # 设计化初始化（``.apply = mul``）无论声明符形态都登记（宁多勿漏）
                positional = _initializer_targets(value, ctx, pointers, name)
                if is_function_pointer_declarator(declarator) or type_name in fp_typedefs:
                    pointers.add(name, positional)
        elif child.type == "assignment_expression":
            operator = child.child_by_field_name("operator")
            left = child.child_by_field_name("left")
            right = child.child_by_field_name("right")
            if (
                left is not None
                and right is not None
                and left.type == "identifier"
                and (operator is None or ctx.text(operator) == "=")
            ):
                pointers.add(ctx.text(left), _function_targets(right, ctx))
        _collect_pointers_inner(child, ctx, pointers, fp_typedefs)
