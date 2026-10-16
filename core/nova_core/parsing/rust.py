"""Rust 抽取器。

口径（与 Python/C++ 抽取器同一套符号/边模型；节点类型表参考 CodeGraph 的 Rust 配置）：

- 符号：``function_item``（模块级 kind=function；``impl`` / ``trait`` 内 kind=method）、
  ``function_signature_item``（trait 里的无函数体声明）、``struct_item`` / ``union_item``
  （struct）、``enum_item``（enum）、``trait_item``（trait）、``mod_item``（namespace，
  仅作容器：成员各自成 chunk）、``const_item`` / ``static_item``（variable）、``type_item``
  （typedef）、``macro_definition``（macro）；
- fqn 用 ``::`` 连接：``module::Type::method``。``impl Trait for Type`` 与 ``impl Type`` 的方法
  都挂在实现类型 ``Type`` 下，``impl`` 块本身不成符号（块里的方法才是检索目标）；
- 符号起始行包含紧邻其上的属性（``#[tauri::command]``、``#[derive(..)]``）与文档注释
  （``///``），它们跟着符号一起被切片和嵌入；
- ``is_exported``：模块级且带 ``pub`` 可见性的项（方法按 Python 口径一律 False）；
- 边：``calls``（``foo()`` 给裸名、``a::b()`` 给整条路径、``x.m()`` 给方法名）、``imports``
  （``use`` 的完整路径）、``implements``（``impl Trait for Type``）、
  ``extends``（trait 的父 trait）；宏调用不当作函数调用。
"""

from __future__ import annotations

import tree_sitter as ts

from nova_core.parsing.base import (
    Extraction,
    FileContext,
    TreeSitterParser,
    end_line_of,
    line_of,
)
from nova_core.types import SymbolDef

_KIND_BY_TYPE = {
    "struct_item": "struct",
    "union_item": "struct",
    "enum_item": "enum",
    "trait_item": "trait",
    "type_item": "typedef",
    "macro_definition": "macro",
    "const_item": "variable",
    "static_item": "variable",
}
_FUNCTION_TYPES = frozenset({"function_item", "function_signature_item"})
#: 递归扫描时遇到这些节点要回到「定义」分支，而不是当普通表达式下钻。
_DEFINITION_TYPES = frozenset(
    {*_KIND_BY_TYPE, *_FUNCTION_TYPES, "impl_item", "mod_item"}
)
_LEADING_TYPES = frozenset({"attribute_item", "line_comment", "block_comment"})


class RustParser(TreeSitterParser):
    """Rust（tree-sitter）抽取器。"""

    language = "rust"
    grammar_module = "tree_sitter_rust"

    def extract(self, root: ts.Node, ctx: FileContext, out: Extraction) -> None:
        self._items(root, ctx, out, scope=(), owner=ctx.path, impl_type=None, top=True)

    # -- 定义 ---------------------------------------------------------------

    def _items(
        self,
        block: ts.Node,
        ctx: FileContext,
        out: Extraction,
        *,
        scope: tuple[str, ...],
        owner: str,
        impl_type: str | None,
        top: bool,
    ) -> None:
        for child in block.named_children:
            self._item(child, ctx, out, scope, owner, impl_type, top)

    def _item(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        scope: tuple[str, ...],
        owner: str,
        impl_type: str | None,
        top: bool,
    ) -> None:
        kind = node.type
        if kind in _FUNCTION_TYPES:
            self._function(node, ctx, out, scope, impl_type, top)
        elif kind == "impl_item":
            self._impl(node, ctx, out, scope, owner)
        elif kind == "mod_item":
            self._module(node, ctx, out, scope, top)
        elif kind in _KIND_BY_TYPE:
            self._type_like(node, ctx, out, scope, owner, top)
        elif kind == "use_declaration":
            self._use(node, ctx, out, owner)
        else:
            self._scan(node, ctx, out, owner, scope)

    def _function(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        scope: tuple[str, ...],
        impl_type: str | None,
        top: bool,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = ctx.text(name_node)
        parts = (*scope, impl_type, name) if impl_type else (*scope, name)
        fqn = "::".join(parts)
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=fqn,
                kind="method" if impl_type else "function",
                start_line=_leading_start(node),
                end_line=end_line_of(node),
                is_exported=top and not impl_type and _is_pub(node),
            )
        )
        for field in ("parameters", "return_type"):
            part = node.child_by_field_name(field)
            if part is not None:
                self._scan(part, ctx, out, fqn, scope)
        body = node.child_by_field_name("body")
        if body is not None:
            # 函数体里的嵌套 fn / 类型：fqn 挂在外层函数下，且不再是模块级。
            self._items(
                body, ctx, out, scope=(*parts[:-1], name), owner=fqn, impl_type=None, top=False
            )

    def _impl(
        self, node: ts.Node, ctx: FileContext, out: Extraction, scope: tuple[str, ...], owner: str
    ) -> None:
        type_name = _type_name(node.child_by_field_name("type"), ctx)
        trait_node = node.child_by_field_name("trait")
        if type_name and trait_node is not None:
            trait_name = _type_name(trait_node, ctx)
            if trait_name:
                out.add_edge(
                    "::".join((*scope, type_name)), trait_name, "implements", line_of(node)
                )
        body = node.child_by_field_name("body")
        if body is not None:
            self._items(
                body, ctx, out, scope=scope, owner=owner, impl_type=type_name, top=False
            )

    def _module(
        self, node: ts.Node, ctx: FileContext, out: Extraction, scope: tuple[str, ...], top: bool
    ) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None:
            return
        name = ctx.text(name_node)
        fqn = "::".join((*scope, name))
        if body is None:  # ``mod foo;``：声明，不是容器
            return
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=fqn,
                kind="namespace",
                start_line=_leading_start(node),
                end_line=end_line_of(node),
                is_exported=top and _is_pub(node),
            )
        )
        self._items(body, ctx, out, scope=(*scope, name), owner=fqn, impl_type=None, top=top)

    def _type_like(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        scope: tuple[str, ...],
        owner: str,
        top: bool,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = ctx.text(name_node)
        fqn = "::".join((*scope, name))
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=fqn,
                kind=_KIND_BY_TYPE[node.type],
                start_line=_leading_start(node),
                end_line=end_line_of(node),
                is_exported=top and _is_pub(node),
            )
        )
        if node.type == "trait_item":
            bounds = node.child_by_field_name("bounds")
            if bounds is not None:
                for bound in bounds.named_children:
                    target = _type_name(bound, ctx)
                    if target:
                        out.add_edge(fqn, target, "extends", line_of(bound))
            body = node.child_by_field_name("body")
            if body is not None:
                self._items(body, ctx, out, scope=scope, owner=fqn, impl_type=name, top=False)
        elif node.type in ("const_item", "static_item"):
            value = node.child_by_field_name("value")
            if value is not None:
                self._scan(value, ctx, out, fqn, scope)

    # -- 边 -----------------------------------------------------------------

    def _use(self, node: ts.Node, ctx: FileContext, out: Extraction, owner: str) -> None:
        argument = node.child_by_field_name("argument")
        if argument is not None:
            out.add_edge(owner, ctx.text(argument), "imports", line_of(node))

    def _scan(
        self, node: ts.Node, ctx: FileContext, out: Extraction, owner: str, scope: tuple[str, ...]
    ) -> None:
        """递归收集 calls；遇到定义节点交回 :meth:`_item`（由它建符号）。"""
        if node.type in _DEFINITION_TYPES or node.type == "use_declaration":
            self._item(node, ctx, out, scope, owner, None, False)
            return
        if node.type == "call_expression":
            target = _call_target(node.child_by_field_name("function"), ctx)
            if target:
                out.add_edge(owner, target, "calls", line_of(node))
        for child in node.named_children:
            self._scan(child, ctx, out, owner, scope)


# -- 辅助 -------------------------------------------------------------------


def _is_pub(node: ts.Node) -> bool:
    return any(child.type == "visibility_modifier" for child in node.children)


def _leading_start(node: ts.Node) -> int:
    """起始行：并入紧邻上方的属性与文档注释（``///``、``/**``），不跨空行。"""
    start = line_of(node)
    prev = node.prev_sibling
    while prev is not None and prev.type in _LEADING_TYPES:
        if end_line_of(prev) < start - 1:
            break
        if prev.type != "attribute_item" and not _is_doc_comment(prev):
            break
        start = line_of(prev)
        prev = prev.prev_sibling
    return start


def _is_doc_comment(node: ts.Node) -> bool:
    return any(child.type == "doc_comment" for child in node.named_children)


def _type_name(node: ts.Node | None, ctx: FileContext) -> str | None:
    """类型节点 → 简单类型名（``Foo<T>`` / ``a::Foo`` / ``&Foo`` 都得到 ``Foo``）。"""
    if node is None:
        return None
    if node.type in ("type_identifier", "identifier"):
        return ctx.text(node)
    if node.type in ("generic_type", "reference_type"):
        return _type_name(node.child_by_field_name("type"), ctx)
    if node.type in ("scoped_type_identifier", "scoped_identifier"):
        return _type_name(node.child_by_field_name("name"), ctx)
    return None


def _call_target(function: ts.Node | None, ctx: FileContext) -> str | None:
    if function is None:
        return None
    if function.type == "identifier":
        return ctx.text(function)
    if function.type == "scoped_identifier":
        return ctx.text(function)
    if function.type == "field_expression":
        field = function.child_by_field_name("field")
        return ctx.text(field) if field is not None else None
    if function.type == "generic_function":
        return _call_target(function.child_by_field_name("function"), ctx)
    return None
