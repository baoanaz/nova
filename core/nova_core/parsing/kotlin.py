"""Kotlin 抽取器。

口径（与 Python/Rust 抽取器同一套模型；节点类型表参考 CodeGraph 的 Kotlin 配置）：

- 符号：``class_declaration``（class / interface / enum 按关键字区分）、``object_declaration``
  （class）、``companion_object``（namespace 容器，成员挂在外层类下）、``function_declaration``
  （顶层 kind=function；类/对象内 kind=method；扩展函数 ``fun String.ext()`` 仍以 ``ext`` 为名）、
  ``secondary_constructor``（method，名 ``constructor``）、``property_declaration``（仅顶层与
  ``const``，kind=variable：类内普通属性不成符号）、``type_alias``（typedef）；
- fqn 用 ``.`` 连接：``Outer.Inner.method``；伴生对象的成员直接归外层类；
- 符号起始行包含紧邻其上的注解（``@Composable``）、修饰符与 KDoc（``/** */``）；
- ``is_exported``：顶层且不是 ``private`` / ``internal``（Kotlin 默认 public）；成员一律 False；
- 边：``calls``（``f()`` 给裸名、``a.b()`` 给整条链）、``imports``、``extends`` / ``implements``
  （委托说明符：带 ``()`` 的构造调用视为父类，其余视为接口）；
- 语法树里 ``ERROR`` 节点由基类的错误恢复机制处理（本语法对少数写法会产生局部 ERROR）。
"""

from __future__ import annotations

import re

import tree_sitter as ts

from nova_core.parsing.base import (
    Extraction,
    FileContext,
    TreeSitterParser,
    end_line_of,
    line_of,
)
from nova_core.types import SymbolDef

_LEADING_TYPES = frozenset({"block_comment", "line_comment", "multiline_comment"})
_PRIVATE_MODIFIERS = ("private", "internal", "protected")


class KotlinParser(TreeSitterParser):
    """Kotlin（tree-sitter）抽取器。"""

    language = "kotlin"
    grammar_module = "tree_sitter_kotlin"
    recover_syntax_errors = True

    #: Kotlin 2.x 的显式幕后字段（属性下一行 ``field = init``）grammar 不认识，会让整个 object / 类
    #: 被一个 ERROR 吞掉（gkd 的 ``SubscriptionRepository`` 整文件退化为兜底块），改成等长局部属性。
    _BACKING_FIELD = re.compile(r"(?m)^(\s+)field(\s*=)")

    def repair_source(self, content: str) -> str | None:
        repaired = self._BACKING_FIELD.sub(r"\1val f\2", content)
        return repaired if repaired != content else None

    def extract(self, root: ts.Node, ctx: FileContext, out: Extraction) -> None:
        self._members(root, ctx, out, scope=(), owner=ctx.path, in_type=False, top=True)

    # -- 定义 ---------------------------------------------------------------

    def _members(
        self,
        block: ts.Node,
        ctx: FileContext,
        out: Extraction,
        *,
        scope: tuple[str, ...],
        owner: str,
        in_type: bool,
        top: bool,
    ) -> None:
        for child in block.named_children:
            self._member(child, ctx, out, scope, owner, in_type, top)

    def _member(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        scope: tuple[str, ...],
        owner: str,
        in_type: bool,
        top: bool,
    ) -> None:
        kind = node.type
        if kind == "function_declaration":
            self._function(node, ctx, out, scope, in_type, top)
        elif kind in ("class_declaration", "object_declaration"):
            self._type(node, ctx, out, scope, top)
        elif kind == "companion_object":
            self._companion(node, ctx, out, scope, owner)
        elif kind == "secondary_constructor":
            self._constructor(node, ctx, out, scope)
        elif kind == "property_declaration":
            self._property(node, ctx, out, scope, owner, in_type, top)
        elif kind == "type_alias":
            self._alias(node, ctx, out, scope, top)
        elif kind == "import":
            self._import(node, ctx, out, owner)
        elif kind == "ERROR":
            # 错误子树里常裹着正常的声明（语法对个别写法会局部失败）：继续按成员处理。
            self._members(node, ctx, out, scope=scope, owner=owner, in_type=in_type, top=top)
        else:
            self._scan(node, ctx, out, owner, scope)

    def _function(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        scope: tuple[str, ...],
        in_type: bool,
        top: bool,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = ctx.text(name_node)
        fqn = ".".join((*scope, name))
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=fqn,
                kind="method" if in_type else "function",
                start_line=_leading_start(node),
                end_line=end_line_of(node),
                is_exported=top and not in_type and _is_public(node, ctx),
            )
        )
        for child in node.named_children:
            if child is not name_node:
                self._scan(child, ctx, out, fqn, scope)

    def _constructor(
        self, node: ts.Node, ctx: FileContext, out: Extraction, scope: tuple[str, ...]
    ) -> None:
        fqn = ".".join((*scope, "constructor"))
        out.add_symbol(
            SymbolDef(
                name="constructor",
                fqn=fqn,
                kind="method",
                start_line=_leading_start(node),
                end_line=end_line_of(node),
            )
        )
        for child in node.named_children:
            self._scan(child, ctx, out, fqn, scope)

    def _type(
        self, node: ts.Node, ctx: FileContext, out: Extraction, scope: tuple[str, ...], top: bool
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = ctx.text(name_node)
        fqn = ".".join((*scope, name))
        keywords = {child.type for child in node.children if not child.is_named}
        kind = "class"
        if "interface" in keywords:
            kind = "interface"
        elif _has_modifier(node, ctx, "enum"):
            kind = "enum"
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=fqn,
                kind=kind,
                start_line=_leading_start(node),
                end_line=end_line_of(node),
                is_exported=top and _is_public(node, ctx),
            )
        )
        for child in node.named_children:
            if child.type == "delegation_specifiers":
                self._delegation(child, fqn, ctx, out)
            elif child.type in ("class_body", "enum_class_body"):
                self._members(
                    child, ctx, out, scope=(*scope, name), owner=fqn, in_type=True, top=False
                )
            elif child.type == "primary_constructor":
                self._scan(child, ctx, out, fqn, scope)

    def _companion(
        self, node: ts.Node, ctx: FileContext, out: Extraction, scope: tuple[str, ...], owner: str
    ) -> None:
        # 伴生对象的成员在调用方看来属于外层类（``Foo.make()``），不再多一层名字。
        for child in node.named_children:
            if child.type == "class_body":
                self._members(child, ctx, out, scope=scope, owner=owner, in_type=True, top=False)

    def _property(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        scope: tuple[str, ...],
        owner: str,
        in_type: bool,
        top: bool,
    ) -> None:
        declaration = next(
            (c for c in node.named_children if c.type == "variable_declaration"), None
        )
        name_node = (
            next((c for c in declaration.named_children if c.type == "identifier"), None)
            if declaration is not None
            else None
        )
        if name_node is not None and (top and not in_type or _has_modifier(node, ctx, "const")):
            name = ctx.text(name_node)
            fqn = ".".join((*scope, name))
            out.add_symbol(
                SymbolDef(
                    name=name,
                    fqn=fqn,
                    kind="variable",
                    start_line=_leading_start(node),
                    end_line=end_line_of(node),
                    is_exported=top and not in_type and _is_public(node, ctx),
                )
            )
            owner = fqn
        for child in node.named_children:
            if child is not declaration:
                self._scan(child, ctx, out, owner, scope)

    def _alias(
        self, node: ts.Node, ctx: FileContext, out: Extraction, scope: tuple[str, ...], top: bool
    ) -> None:
        name_node = node.child_by_field_name("type")
        if name_node is None:
            return
        name = ctx.text(name_node)
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=".".join((*scope, name)),
                kind="typedef",
                start_line=_leading_start(node),
                end_line=end_line_of(node),
                is_exported=top and _is_public(node, ctx),
            )
        )

    # -- 边 -----------------------------------------------------------------

    def _delegation(
        self, node: ts.Node, fqn: str, ctx: FileContext, out: Extraction
    ) -> None:
        for specifier in node.named_children:
            inner = specifier.named_children[0] if specifier.named_children else None
            if inner is None:
                continue
            is_class = inner.type == "constructor_invocation"
            type_node = inner.named_children[0] if is_class and inner.named_children else inner
            target = _simple_type_name(type_node, ctx)
            if target:
                out.add_edge(fqn, target, "extends" if is_class else "implements", line_of(inner))

    def _import(self, node: ts.Node, ctx: FileContext, out: Extraction, owner: str) -> None:
        name = next((c for c in node.named_children if c.type == "qualified_identifier"), None)
        if name is not None:
            out.add_edge(owner, ctx.text(name), "imports", line_of(node))

    def _scan(
        self, node: ts.Node, ctx: FileContext, out: Extraction, owner: str, scope: tuple[str, ...]
    ) -> None:
        """递归收集 calls；遇到嵌套的类 / 函数定义交回 :meth:`_member`。"""
        if node.type in ("class_declaration", "object_declaration", "function_declaration"):
            self._member(node, ctx, out, scope, owner, False, False)
            return
        if node.type == "call_expression" and node.named_children:
            target = _call_target(node.named_children[0], ctx)
            if target:
                out.add_edge(owner, target, "calls", line_of(node))
        for child in node.named_children:
            self._scan(child, ctx, out, owner, scope)


# -- 辅助 -------------------------------------------------------------------


def _modifier_text(node: ts.Node, ctx: FileContext) -> str:
    for child in node.named_children:
        if child.type == "modifiers":
            return ctx.text(child)
    return ""


def _has_modifier(node: ts.Node, ctx: FileContext, word: str) -> bool:
    return word in _modifier_text(node, ctx).split()


def _is_public(node: ts.Node, ctx: FileContext) -> bool:
    text = _modifier_text(node, ctx)
    return not any(word in text.split() for word in _PRIVATE_MODIFIERS)


def _leading_start(node: ts.Node) -> int:
    """起始行：并入紧邻上方的 KDoc / 注释，不跨空行（注解与修饰符本就在节点内）。"""
    start = line_of(node)
    prev = node.prev_sibling
    while prev is not None and prev.type in _LEADING_TYPES:
        if end_line_of(prev) < start - 1:
            break
        start = line_of(prev)
        prev = prev.prev_sibling
    return start


def _simple_type_name(node: ts.Node, ctx: FileContext) -> str | None:
    if node.type == "identifier":
        return ctx.text(node)
    for child in node.named_children:
        found = _simple_type_name(child, ctx)
        if found:
            return found
    return None


def _call_target(callee: ts.Node, ctx: FileContext) -> str | None:
    if callee.type == "identifier":
        return ctx.text(callee)
    if callee.type == "navigation_expression":
        names = [c for c in callee.named_children if c.type == "identifier"]
        if not names:
            return None
        # ``a.b.c(..)`` 给整条链（交二阶段解析）；``this.m()`` / ``x().m()`` 只给方法名。
        chain = _identifier_chain(callee, ctx)
        return chain or ctx.text(names[-1])
    return None


def _identifier_chain(node: ts.Node, ctx: FileContext) -> str | None:
    if node.type == "identifier":
        return ctx.text(node)
    if node.type == "navigation_expression":
        parts = [_identifier_chain(c, ctx) for c in node.named_children]
        return ".".join(p for p in parts if p) if all(parts) else None
    return None
