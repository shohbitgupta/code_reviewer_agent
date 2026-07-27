"""
Dart / Flutter language parser — tree-sitter powered.

Extracts:
  - import / export / part statements  → ParsedSymbol(symbol_type="import")
  - class / mixin / extension / enum   → ParsedSymbol(symbol_type="class_head")
  - top-level functions                 → ParsedSymbol(symbol_type="function")
  - instance methods, getters, setters  → ParsedSymbol(symbol_type="method")
  - constructors                        → ParsedSymbol(symbol_type="method")

Flutter-specific tagging:
  - @flutter_widget on class_head when base is in FLUTTER_WIDGET_BASES
  - @flutter_lifecycle on build/initState/dispose/etc.

Grammar: the `tree-sitter-dart` PyPI package (see requirements.txt).
Falls back to [] (not an exception) when the grammar package is not installed.

Node type reference (tree-sitter-dart grammar — verified against the
installed grammar directly, since it differs from tree-sitter-kotlin/-rust
in several load-bearing ways):
  - Classes:     "class_definition" (NOT "class_declaration")
  - Methods:     "method_signature" wraps ONE of "function_signature" /
                 "getter_signature" / "setter_signature" / a factory
                 constructor — the name field lives on that inner node, not
                 on "method_signature" itself.
  - Constructors (non-factory): a "declaration" node wraps
                 "constructor_signature" (named/plain) or
                 "constant_constructor_signature" (const). The class-name and
                 dotted-variant-name identifiers are both tagged field="name",
                 so both must be collected (not just the first match) to
                 build "ClassName.namedVariant".
  - Factory constructors: "method_signature" wraps
                 "factory_constructor_signature", whose identifiers carry NO
                 field name — the class name and (optional) variant name are
                 the first and second bare "identifier" children, in order.
  - Params:      "formal_parameter_list" → "formal_parameter" children; the
                 parameter's own name is field="name", but its type has no
                 field label — take the first type-shaped child instead.
"""

import logging
from typing import List, Optional, Set

from core.models import ParsedSymbol
from stage1_ingestion.parsers.treesitter_base import TreeSitterParser

logger = logging.getLogger(__name__)

# Flutter widget base classes
FLUTTER_WIDGET_BASES: Set[str] = {
    "StatelessWidget", "StatefulWidget", "State",
    "InheritedWidget", "InheritedModel", "InheritedNotifier",
    "RenderBox", "RenderObject", "RenderSliver",
    "ConsumerWidget", "ConsumerStatefulWidget", "ConsumerState",
    "HookWidget", "HookConsumerWidget",
    "ChangeNotifier", "ValueNotifier",
    "Cubit", "Bloc",
    "StateNotifier",
    "GetxController", "GetController",
    "ViewModel",
}

FLUTTER_LIFECYCLE: Set[str] = {
    "build", "initState", "dispose",
    "didChangeDependencies", "didUpdateWidget",
    "deactivate", "reassemble", "activate",
    "createState",
}

_CALL_SKIP: Set[str] = {
    "print", "debugPrint", "super", "this", "setState",
    "String", "int", "double", "bool", "num",
    "List", "Map", "Set", "Iterable", "Future", "Stream",
}

# Dart class-like node types
_TYPE_NODE_TYPES = {
    "class_definition",       # NOT "class_declaration" — see module docstring
    "mixin_declaration",
    "extension_declaration",
    "enum_declaration",
}

# formal_parameter's type token has no field label in this grammar — the
# first child of one of these types is taken as the parameter's type.
_PARAM_TYPE_NODE_TYPES = {
    "type_identifier", "void_type", "nullable_type", "function_type",
}


class DartTsParser(TreeSitterParser):
    """Tree-sitter powered Dart / Flutter parser."""

    _language_obj = None
    _language_loaded: bool = False

    @property
    def language(self) -> str:
        return "dart"

    @classmethod
    def _load_language(cls):
        from tree_sitter import Language
        import tree_sitter_dart
        return Language(tree_sitter_dart.language())

    def _extract_symbols(
        self,
        tree,
        source_bytes: bytes,
        raw_lines: List[str],
    ) -> List[ParsedSymbol]:
        """
        Extract grouped import/export/part/library directives, then walk the
        CST for type and function/method declarations via _walk().

        Returns:
            List[ParsedSymbol] combining directive groups with class_head
            and function/method symbols.
        """
        symbols: List[ParsedSymbol] = []
        root = tree.root_node

        # ── Import / export / part directives ────────────────────────────────
        import_nodes = [
            c for c in root.children
            if c.type in ("import_or_export", "part_directive", "library_directive")
        ]
        if import_nodes:
            groups: List[List] = [[import_nodes[0]]]
            for node in import_nodes[1:]:
                s, _ = self._node_lines(node)
                _, prev_end = self._node_lines(groups[-1][-1])
                if s <= prev_end + 2:
                    groups[-1].append(node)
                else:
                    groups.append([node])
            for group in groups:
                start, _ = self._node_lines(group[0])
                _, end   = self._node_lines(group[-1])
                paths = []
                for n in group:
                    text = self._node_text(n, source_bytes)
                    import re
                    m = re.search(r"""['"]([^'"]+)['"]""", text)
                    if m:
                        paths.append(m.group(1))
                symbols.append(ParsedSymbol(
                    symbol_type = "import",
                    name        = "imports",
                    start_line  = start,
                    end_line    = end,
                    source      = "\n".join(raw_lines[start - 1:end]),
                    parent_name = None,
                    imports     = paths,
                ))

        # ── Type and function declarations ───────────────────────────────────
        self._walk(root, source_bytes, raw_lines, symbols, current_type=None)
        return symbols

    def _walk(
        self,
        node,
        source_bytes: bytes,
        raw_lines: List[str],
        symbols: List[ParsedSymbol],
        current_type: Optional[str],
    ) -> None:
        """
        Recursively descend the CST, appending symbols to *symbols* in place.

        Tracks *current_type* (the innermost enclosing class/mixin/extension/
        enum name) so methods and constructors are emitted with the correct
        parent_symbol; top-level `function_signature` nodes are only treated
        as functions when no enclosing type is active. Since Dart's grammar
        separates a declaration's signature from its body, uses
        _find_next_body() to locate the following sibling body node and
        extends end_line to cover it. Container nodes that don't match a
        known declaration type are recursed into unconditionally.
        """
        for child in node.children:
            ntype = child.type

            # ── Type declarations ────────────────────────────────────────────
            if ntype in _TYPE_NODE_TYPES:
                # "mixin_declaration" is the one type node in this grammar
                # whose name carries no field label — fall back to the first
                # identifier child (right after the "mixin" keyword).
                name_node = child.child_by_field_name("name") or next(
                    (c for c in child.children if c.type == "identifier"), None
                )
                name = self._node_text(name_node, source_bytes) if name_node else "anonymous"
                start, end = self._node_lines(child)
                head_end = min(start + 4, end)

                bases = self._collect_bases(child, source_bytes)
                is_widget = bool(FLUTTER_WIDGET_BASES & set(bases))
                decorators = ["@flutter_widget"] if is_widget else []

                symbols.append(ParsedSymbol(
                    symbol_type = "class_head",
                    name        = name,
                    start_line  = start,
                    end_line    = head_end,
                    source      = "\n".join(raw_lines[start - 1:head_end]),
                    parent_name = None,
                    bases       = bases,
                    decorators  = decorators,
                ))
                self._walk(child, source_bytes, raw_lines, symbols, current_type=name)
                continue

            # ── Function declarations (top-level) ────────────────────────────
            if ntype == "function_signature" and not current_type:
                name_node = child.child_by_field_name("name")
                if name_node:
                    func_name = self._node_text(name_node, source_bytes)
                    start, end = self._node_lines(child)
                    # function_signature is just the header; walk siblings for body
                    body = self._find_next_body(node, child)
                    if body is not None:
                        _, end = self._node_lines(body)
                    sym_source = "\n".join(raw_lines[start - 1:end])
                    param_types, return_type = self._extract_params(child, source_bytes)
                    symbols.append(ParsedSymbol(
                        symbol_type = "function",
                        name        = func_name,
                        start_line  = start,
                        end_line    = end,
                        source      = sym_source,
                        parent_name = None,
                        calls       = self._collect_dart_calls(body, source_bytes, _CALL_SKIP)
                                      if body is not None else [],
                        param_types = param_types,
                        return_type = return_type,
                    ))
                continue

            # ── Method / getter / setter / factory-constructor declarations ──
            # "method_signature" is a thin wrapper — the actual name field
            # lives on its one child, EXCEPT for factory constructors, whose
            # identifiers carry no field label at all.
            if ntype == "method_signature" and current_type:
                inner = child.children[0] if child.children else None
                if inner is None:
                    continue

                if inner.type == "factory_constructor_signature":
                    idents = [c for c in inner.children if c.type == "identifier"]
                    if not idents:
                        continue
                    ctor_name = self._node_text(idents[0], source_bytes)
                    if len(idents) > 1:
                        ctor_name += "." + self._node_text(idents[1], source_bytes)
                    start, end = self._node_lines(child)
                    body = self._find_next_body(node, child)
                    if body is not None:
                        _, end = self._node_lines(body)
                    symbols.append(ParsedSymbol(
                        symbol_type = "method",
                        name        = ctor_name,
                        start_line  = start,
                        end_line    = end,
                        source      = "\n".join(raw_lines[start - 1:end]),
                        parent_name = current_type,
                        calls       = self._collect_dart_calls(body, source_bytes, _CALL_SKIP)
                                      if body is not None else [],
                    ))
                    continue

                name_node = inner.child_by_field_name("name")
                if name_node is None:
                    continue
                method_name = self._node_text(name_node, source_bytes)
                start, end = self._node_lines(child)
                body = self._find_next_body(node, child)
                if body is not None:
                    _, end = self._node_lines(body)
                is_lifecycle = method_name in FLUTTER_LIFECYCLE
                sym_source = "\n".join(raw_lines[start - 1:end])
                param_types, return_type = self._extract_params(inner, source_bytes)
                symbols.append(ParsedSymbol(
                    symbol_type = "method",
                    name        = method_name,
                    start_line  = start,
                    end_line    = end,
                    source      = sym_source,
                    parent_name = current_type,
                    decorators  = ["@flutter_lifecycle"] if is_lifecycle else [],
                    calls       = self._collect_dart_calls(body, source_bytes, _CALL_SKIP)
                                  if body is not None else [],
                    param_types = param_types,
                    return_type = return_type,
                ))
                continue

            # ── Non-factory constructor declarations ─────────────────────────
            # These sit inside a generic "declaration" member wrapper shared
            # with plain field declarations — only act when the wrapped node
            # is actually a constructor signature.
            if ntype == "declaration" and current_type:
                inner = child.children[0] if child.children else None
                if inner is not None and inner.type in (
                    "constructor_signature", "constant_constructor_signature",
                ):
                    # Both the class name and a named-constructor variant are
                    # tagged field="name" — collect every identifier match, in
                    # source order. The "." separator is ALSO tagged
                    # field="name" in this grammar, so it must be excluded
                    # explicitly rather than trusting the field label alone.
                    name_nodes = [
                        inner.child(i) for i in range(inner.child_count)
                        if inner.field_name_for_child(i) == "name"
                        and inner.child(i).type == "identifier"
                    ]
                    ctor_name = (
                        ".".join(self._node_text(n, source_bytes) for n in name_nodes)
                        if name_nodes else current_type
                    )
                    start, end = self._node_lines(child)
                    body = self._find_next_body(node, child)
                    if body is not None:
                        _, end = self._node_lines(body)
                    symbols.append(ParsedSymbol(
                        symbol_type = "method",
                        name        = ctor_name,
                        start_line  = start,
                        end_line    = end,
                        source      = "\n".join(raw_lines[start - 1:end]),
                        parent_name = current_type,
                        calls       = self._collect_dart_calls(body, source_bytes, _CALL_SKIP)
                                      if body is not None else [],
                    ))
                continue   # plain field declarations don't nest further symbols

            # Recurse into other containers
            self._walk(child, source_bytes, raw_lines, symbols, current_type)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _collect_dart_calls(self, node, source_bytes: bytes, skip_set: Set[str]) -> List[str]:
        """
        Dart-specific call-site collection.

        This grammar has no unified "call_expression" node (unlike Kotlin's/
        Rust's, which TreeSitterParser._collect_calls targets) — a call is a
        flat run of siblings: a base identifier, zero or more `.member`
        selectors (each wrapping "unconditional_assignable_selector"), an
        optional bare `<TypeArgs>` selector for a generic call, and a final
        selector wrapping "argument_part" (the parens). Cascades (`..foo()`)
        use a distinct "cascade_section"/"cascade_selector" shape instead.

        For a single-hop `Base.method(...)` where Base looks like a type name
        (starts uppercase — the common named/factory-constructor and static-
        call shape, e.g. `UserModel.fromJson(...)`), records BOTH "method"
        and "Base.method" so the resolver can match either the bare name or
        the qualified constructor/method symbol name.

        Known gap: `Foo.bar<T>(x)` (a generic static call, e.g.
        `Provider.of<AuthProvider>(context)`) is genuinely ambiguous in this
        grammar without semantic analysis and gets misparsed as a relational
        expression (`<`/`>` as comparison operators) — "bar" is not
        recovered in that specific shape. Chained calls after it still are.
        """
        seen: set = set()
        result: List[str] = []

        def _add(name: str) -> None:
            name = name.strip()
            if name and name not in skip_set and name not in seen:
                seen.add(name)
                result.append(name)

        def _is_call_selector(sel) -> bool:
            return any(c.type == "argument_part" for c in sel.children)

        def _member_name(sel) -> Optional[str]:
            inner = next(
                (c for c in sel.children if c.type == "unconditional_assignable_selector"),
                None,
            )
            if inner is None:
                return None
            ident = next((c for c in inner.children if c.type == "identifier"), None)
            return self._node_text(ident, source_bytes) if ident is not None else None

        def _walk(n) -> None:
            children = n.children
            for i, child in enumerate(children):
                nxt = children[i + 1] if i + 1 < len(children) else None

                # Base identifier called directly, no ".member" in between —
                # e.g. simpleCall(), CheckoutEvent().
                if child.type in ("identifier", "type_identifier"):
                    if nxt is not None and nxt.type == "selector" and _is_call_selector(nxt):
                        _add(self._node_text(child, source_bytes))

                # ".member" selector immediately followed by a call selector
                # (possibly a generic-args call like ".read<T>()").
                elif child.type == "selector":
                    member = _member_name(child)
                    if member is not None and nxt is not None and nxt.type == "selector" \
                            and _is_call_selector(nxt):
                        _add(member)
                        prev = children[i - 1] if i > 0 else None
                        if prev is not None and prev.type in ("identifier", "type_identifier"):
                            base_name = self._node_text(prev, source_bytes)
                            if base_name[:1].isupper():
                                _add(f"{base_name}.{member}")

                # Cascade notation: `..method(...)`.
                elif child.type == "cascade_section":
                    csel = next((c for c in child.children if c.type == "cascade_selector"), None)
                    if csel is not None:
                        ident = next((c for c in csel.children if c.type == "identifier"), None)
                        if ident is not None:
                            _add(self._node_text(ident, source_bytes))

                _walk(child)

        _walk(node)
        return result

    def _collect_bases(self, node, source_bytes: bytes) -> List[str]:
        """Extract base class / interface names from a type declaration."""
        bases: List[str] = []
        for child in node.children:
            if child.type in ("superclass", "interfaces", "mixin_application", "on_clause"):
                text = self._node_text(child, source_bytes)
                for part in text.replace("extends", "").replace("implements", "").replace(
                    "with", ","
                ).replace("on", "").split(","):
                    name = part.strip().split("<")[0].strip()
                    if name:
                        bases.append(name)
        return bases

    @staticmethod
    def _find_next_body(parent, after_node):
        """
        Find the function/method body node immediately following *after_node*
        in *parent*'s children list.
        """
        found = False
        for child in parent.children:
            if found and child.type in (
                "function_body", "block", "arrow_function_body", "empty_statement"
            ):
                return child
            if child is after_node:
                found = True
        return None

    def _extract_params(self, node, source_bytes: bytes):
        """
        Return (param_types, return_type) for a function/getter/setter
        signature node (e.g. the node wrapped by "method_signature").

        Neither a parameter's type nor a signature's return type carries a
        field label in this grammar — a parameter's type is its first
        type-shaped child (see _PARAM_TYPE_NODE_TYPES), and the return type,
        when present, is the signature node's own first child appearing
        before the (field-labelled) name.
        """
        param_types: List[str] = []
        return_type: Optional[str] = None

        if node.children and node.children[0].type in _PARAM_TYPE_NODE_TYPES:
            return_type = self._node_text(node.children[0], source_bytes).strip()

        for child in node.children:
            if child.type != "formal_parameter_list":
                continue
            for param in child.children:
                if param.type not in (
                    "formal_parameter", "normal_formal_parameter", "optional_formal_parameter",
                ):
                    continue
                type_node = next(
                    (c for c in param.children if c.type in _PARAM_TYPE_NODE_TYPES),
                    None,
                )
                if type_node is not None:
                    param_types.append(self._node_text(type_node, source_bytes).strip())

        return param_types, return_type
