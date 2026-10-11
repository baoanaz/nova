"""JS/TS/TSX 结构与保守 ESM 绑定（D-53），不模拟运行时类型系统。"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace

import tree_sitter as ts

from nova_core.parsing.base import (
    NAME_LIMIT,
    Extraction,
    FileContext,
    TreeSitterParser,
    end_line_of,
    line_of,
    normalize_name,
)
from nova_core.types import SymbolDef

_FUNCTIONS = {"function_declaration", "generator_function_declaration", "function_expression",
              "generator_function", "arrow_function"}
_CLASSES = {"class_declaration", "abstract_class_declaration", "class", "interface_declaration"}
_METHODS = {"method_definition", "method_signature", "abstract_method_signature"}


class JavaScriptParser(TreeSitterParser):
    language = "javascript"
    grammar_module = "tree_sitter_javascript"

    def extract(self, root: ts.Node, ctx: FileContext, out: Extraction) -> None:
        collector = _Collector(ctx, out)
        collector.visit(root, (), ctx.path, None)
        collector.finish()


class TypeScriptParser(JavaScriptParser):
    language = "typescript"
    grammar_module = "tree_sitter_typescript"
    grammar_function = "language_typescript"


class TSXParser(TypeScriptParser):
    language = "tsx"
    grammar_function = "language_tsx"


class _Collector:
    def __init__(self, ctx: FileContext, out: Extraction):
        self.ctx, self.out = ctx, out
        self.bindings: dict[str, tuple[str, str, bool]] = {}
        self.shadows: dict[tuple[str, ...], set[str]] = defaultdict(set)
        self.mutated: dict[tuple[str, ...], set[str]] = defaultdict(set)
        self.calls: list[tuple[str, str, str, int, tuple[str, ...], str | None]] = []
        self.exports: list[tuple[str, str, int]] = []
        self.export_flags: set[str] = set()
        self.mutated_members: set[str] = set()
        self.function_scopes: dict[str, tuple[str, ...]] = {ctx.path: ()}

    def fqn(self, scope: tuple[str, ...]) -> str:
        return self.ctx.path + "::" + ".".join(scope)

    def name(self, node: ts.Node | None) -> str | None:
        if node is None:
            return None
        text = self.ctx.text(node)
        if node.type in {"identifier", "type_identifier", "property_identifier", "this"}:
            return text if text.replace("$", "_").isidentifier() else None
        if node.type == "member_expression":
            obj = self.name(node.child_by_field_name("object"))
            prop = self.name(node.child_by_field_name("property"))
            return f"{obj}.{prop}" if obj and prop else None
        return None

    def binding_names(self, node):
        if node is None:
            return set()
        if node.type in {"identifier", "shorthand_property_identifier_pattern"}:
            return {self.ctx.text(node)}
        if node.type == "pair_pattern":
            return self.binding_names(node.child_by_field_name("value"))
        if node.type in {"assignment_pattern", "object_assignment_pattern"}:
            return self.binding_names(node.child_by_field_name("left"))
        if node.type in {"required_parameter", "optional_parameter"}:
            return self.binding_names(node.child_by_field_name("pattern"))
        names = set()
        if node.type in {"object_pattern", "array_pattern", "rest_pattern", "formal_parameters"}:
            for child in node.named_children:
                names.update(self.binding_names(child))
        return names

    def mutate(self, left, class_owner):
        if left is None:
            return
        if left.type == "pair_pattern":
            self.mutate(left.child_by_field_name("value"), class_owner)
            return
        if left.type in {"assignment_pattern", "object_assignment_pattern"}:
            self.mutate(left.child_by_field_name("left"), class_owner)
            return
        if left.type in {"object_pattern", "array_pattern", "rest_pattern"}:
            for child in left.named_children:
                self.mutate(child, class_owner)
            return
        self.mutated[()].update(self.binding_names(left))
        target = self.name(left)
        if target:
            self.mutated[()].add(target.partition(".")[0])
            if target.startswith("this.") and class_owner:
                self.mutated_members.add(class_owner + target[4:])
        elif left and left.type == "subscript_expression":
            root = self.name(left.child_by_field_name("object"))
            if root:
                self.mutated[()].add(root.partition(".")[0])
                if root == "this" and class_owner:
                    self.mutated_members.add(class_owner + ".*")

    def define(self, node, name, scope, owner, class_owner, *, kind, exported, start=None):
        nested = (*scope, name)
        self.out.add_symbol(SymbolDef(
            name=name, fqn=self.fqn(nested), kind=kind,
            start_line=start or line_of(node), end_line=end_line_of(node), is_exported=exported,
        ))
        if kind != "method":
            self.shadows[scope].add(name)
        if kind in {"class", "interface"}:
            class_owner = self.fqn(nested)
        elif node.type != "arrow_function" and kind != "method":
            class_owner = None  # 普通嵌套 function 的 this 不能继承外层 class
        parameters = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
        if kind in {"function", "method"}:
            self.function_scopes[self.fqn(nested)] = nested
        self.shadows[nested].update(self.binding_names(parameters))
        for child in node.named_children:
            if child != node.child_by_field_name("name"):
                self.visit(child, nested, self.fqn(nested), class_owner)

    def visit(self, node, scope, owner, class_owner, *, exported=False, start=None):
        kind = node.type
        if kind == "statement_block":
            block_scope = (*scope, f"$block{node.start_byte}")
            for child in node.named_children:
                self.visit(child, block_scope, owner, class_owner)
            return
        if kind in {"catch_clause", "for_statement", "for_in_statement"}:
            nested = (*scope, f"$scope{node.start_byte}")
            binding = node.child_by_field_name("parameter") or node.child_by_field_name("left")
            bound = self.binding_names(binding)
            declarations = {child.type for child in node.children} & {"var", "let", "const"}
            if "var" in declarations:
                self.shadows[self.function_scopes.get(owner, scope)].update(bound)
            elif kind == "for_in_statement" and not declarations:
                self.mutate(binding, class_owner)  # 裸循环目标是赋值，不能重造局部绑定
            else:
                self.shadows[nested].update(bound)
            for child in node.named_children:
                self.visit(child, nested, owner, class_owner)
            return
        if kind == "import_statement":
            self.import_statement(node)
            return
        if kind == "export_statement":
            self.export_statement(node, scope, owner, class_owner)
            return
        if kind in (_FUNCTIONS | _CLASSES | _METHODS
                    | {"type_alias_declaration", "enum_declaration"}):
            name = self.name(node.child_by_field_name("name"))
            if not name or (kind in _METHODS and class_owner is None):
                self.out.add_unresolved(
                    owner, "anonymous callback/object method", "call", line_of(node),
                )
                return
            symbol_kind = ("class" if kind in _CLASSES else "method" if kind in _METHODS
                           else "typedef" if kind == "type_alias_declaration"
                           else "enum" if kind == "enum_declaration" else "function")
            if kind == "interface_declaration":
                symbol_kind = "interface"
            self.define(node, name, scope, owner, class_owner, kind=symbol_kind,
                        exported=exported, start=start)
            return
        if kind in {"variable_declarator", "public_field_definition", "field_definition"}:
            declaration_scope = scope
            if node.parent and node.parent.type == "variable_declaration":
                declaration_scope = self.function_scopes.get(owner, scope)
            pattern = node.child_by_field_name("name")
            self.shadows[declaration_scope].update(self.binding_names(pattern))
            name = self.name(
                node.child_by_field_name("name") or node.child_by_field_name("property")
            )
            value = node.child_by_field_name("value")
            if name and value and value.type in _FUNCTIONS | _CLASSES:
                self.define(value, name, declaration_scope, owner, class_owner,
                            kind="class" if value.type in _CLASSES else "method" if class_owner
                            and kind != "variable_declarator" else "function",
                            exported=exported, start=start or line_of(node))
                return
            if name:
                self.shadows[declaration_scope].add(name)
                if not declaration_scope:
                    self.out.add_symbol(SymbolDef(
                        name, self.fqn((name,)), "variable",
                        start or line_of(node), end_line_of(node), exported,
                    ))
        if kind in {"assignment_expression", "augmented_assignment_expression",
                    "update_expression"}:
            left = node.child_by_field_name("left") or node.child_by_field_name("argument")
            self.mutate(left, class_owner)
        if kind in {"call_expression", "new_expression"}:
            target = self.name(node.child_by_field_name("function")
                               or node.child_by_field_name("constructor"))
            if target and target not in {"eval", "Function", "require"}:
                self.calls.append((owner, target, "calls", line_of(node), scope, class_owner))
            else:
                self.out.add_unresolved(
                    owner, target or "dynamic callee/import", "call", line_of(node),
                )
        if kind in {"class_heritage", "extends_clause", "implements_clause"}:
            for child in node.named_children:
                target = self.name(child)
                if target:
                    self.calls.append((owner, target, "implements" if kind == "implements_clause"
                                       else "extends", line_of(child), scope, class_owner))
        for child in node.named_children:
            self.visit(child, scope, owner, class_owner, exported=exported, start=start)

    def import_statement(self, node):
        source = node.child_by_field_name("source")
        if source is None:
            return
        module = self.ctx.text(source)[1:-1]
        type_only = self.ctx.text(node).lstrip().startswith("import type ")
        for clause in node.named_children:
            if clause.type != "import_clause":
                continue
            for part in clause.named_children:
                if part.type == "identifier":
                    entries = [(self.ctx.text(part), "default", type_only)]
                elif part.type == "namespace_import":
                    entries = [(self.ctx.text(part.named_children[-1]), "*", type_only)]
                else:
                    entries = []
                    for spec in part.named_children:
                        name = self.name(spec.child_by_field_name("name"))
                        alias = self.name(spec.child_by_field_name("alias")) or name
                        if name and alias:
                            entries.append((
                                alias, name, type_only or self.ctx.text(spec).startswith("type "),
                            ))
                for local, imported, only_type in entries:
                    if local in self.bindings:
                        self.mutated[()].add(local)
                    self.bindings[local] = (module, imported, only_type)
                    target = f"js:{module}#{imported}"
                    if len(target) <= NAME_LIMIT and normalize_name(target) == target:
                        self.out.add_edge(self.ctx.path, target, "imports", line_of(node))
                    else:
                        self.out.add_unresolved(
                            self.ctx.path, "long import", "import", line_of(node),
                        )

    def export_statement(self, node, scope, owner, class_owner):
        source = node.child_by_field_name("source")
        declaration = node.child_by_field_name("declaration")
        value = node.child_by_field_name("value")
        default = any(c.type == "default" for c in node.children)
        if declaration:
            name = self.name(declaration.child_by_field_name("name"))
            if default and name is None and declaration.type in _FUNCTIONS | _CLASSES:
                self.define(declaration, "default", (), owner, None,
                            kind="class" if declaration.type in _CLASSES else "function",
                            exported=True, start=line_of(node))
            else:
                self.visit(declaration, scope, owner, class_owner,
                           exported=not default, start=line_of(node))
                if default and name:
                    self.export_alias(node, "default", name)
            return
        if default and value:
            if value.type in _FUNCTIONS | _CLASSES:
                self.define(value, "default", (), owner, None,
                            kind="class" if value.type in _CLASSES else "function",
                            exported=True, start=line_of(node))
            else:
                name = self.name(value)
                if name:
                    self.export_alias(node, "default", name)
                else:
                    self.out.add_unresolved(
                        owner, "dynamic default export", "export", line_of(node),
                    )
            return
        found = False
        for clause in node.named_children:
            if clause.type != "export_clause":
                continue
            for spec in clause.named_children:
                name = self.name(spec.child_by_field_name("name"))
                alias = self.name(spec.child_by_field_name("alias")) or name
                if name and alias:
                    found = True
                    target = f"js:{self.ctx.text(source)[1:-1]}#{name}" if source else name
                    self.export_alias(node, alias, target)
        if not found:
            self.out.add_unresolved(owner, "wildcard/namespace export", "export", line_of(node))

    def export_alias(self, node, alias, target):
        if alias == target:
            self.export_flags.add(self.fqn((alias,)))
            return
        self.out.add_symbol(SymbolDef(alias, self.fqn((alias,)), "variable", line_of(node),
                                      end_line_of(node), True))
        self.exports.append((self.fqn((alias,)), target, line_of(node)))

    def finish(self):
        self.out.symbols = [
            replace(s, is_exported=True) if s.fqn in self.export_flags else s
            for s in self.out.symbols
        ]
        for symbol in self.out.symbols:
            if (symbol.name in self.mutated[()]
                    and symbol.fqn == self.fqn((symbol.name,))):
                self.out.add_edge(symbol.fqn, "unstable ESM binding", "binding_boundary",
                                  symbol.start_line)
                self.out.add_unresolved(symbol.fqn, "mutable ESM binding", "reference",
                                        symbol.start_line)
        counts = Counter(s.fqn for s in self.out.symbols)
        definitions = {s.fqn: s for s in self.out.symbols
                       if s.kind != "variable" and counts[s.fqn] == 1}
        for owner, target, kind, line, scope, class_owner in self.calls:
            resolved = self.resolve(target, scope, class_owner, definitions, kind)
            if resolved and len(resolved) <= NAME_LIMIT and normalize_name(resolved) == resolved:
                self.out.add_edge(owner, resolved, kind, line)
            else:
                self.out.add_unresolved(owner, target, "call" if kind == "calls" else kind, line)
        for owner, target, line in self.exports:
            resolved = target if target.startswith("js:") else self.resolve(
                target, (), None, definitions, "exports",
            )
            if resolved and len(resolved) <= NAME_LIMIT and normalize_name(resolved) == resolved:
                self.out.add_edge(owner, resolved, "exports", line)
            else:
                self.out.add_unresolved(owner, target, "export", line)

    def resolve(self, target, scope, class_owner, definitions, kind):
        if target.startswith("this.") and class_owner:
            name = class_owner + target[4:]
            return name if (name in definitions and name not in self.mutated_members
                            and class_owner + ".*" not in self.mutated_members) else None
        root, _, member = target.partition(".")
        for depth in range(len(scope), -1, -1):
            at = scope[:depth]
            if root in self.mutated[at]:
                return None
            if root in self.shadows[at]:
                name = self.fqn((*at, target))
                if member or name not in definitions:
                    return None
                if (kind == "calls"
                        and definitions[name].kind not in {"function", "method", "class"}):
                    return None
                return name
        binding = self.bindings.get(root)
        if binding:
            module, imported, only_type = binding
            if only_type and kind == "calls":
                return None
            if member:
                if imported != "*" or "." in member:
                    return None
                imported = member
            if imported == "*":
                return None
            return f"js:{module}#{imported}"
        return None
