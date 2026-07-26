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

Grammar: tree-sitter-languages provides "dart" via get_language("dart").
Falls back to [] (not an exception) when tree-sitter-languages is not installed.

Pattern: mirrors KotlinTsParser / SwiftTsParser.
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
    "class_declaration",
    "mixin_declaration",
    "extension_declaration",
    "enum_declaration",
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
        from tree_sitter_languages import get_language
        return get_language("dart")

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
                name_node = child.child_by_field_name("name")
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
                        calls       = self._collect_calls(child, source_bytes, _CALL_SKIP),
                        param_types = param_types,
                        return_type = return_type,
                    ))
                continue

            # ── Method declarations (inside class/mixin) ─────────────────────
            if ntype == "method_signature" and current_type:
                name_node = child.child_by_field_name("name")
                if name_node:
                    method_name = self._node_text(name_node, source_bytes)
                    start, end = self._node_lines(child)
                    body = self._find_next_body(node, child)
                    if body is not None:
                        _, end = self._node_lines(body)
                    is_lifecycle = method_name in FLUTTER_LIFECYCLE
                    sym_source = "\n".join(raw_lines[start - 1:end])
                    param_types, return_type = self._extract_params(child, source_bytes)
                    symbols.append(ParsedSymbol(
                        symbol_type = "method",
                        name        = method_name,
                        start_line  = start,
                        end_line    = end,
                        source      = sym_source,
                        parent_name = current_type,
                        decorators  = ["@flutter_lifecycle"] if is_lifecycle else [],
                        calls       = self._collect_calls(child, source_bytes, _CALL_SKIP),
                        param_types = param_types,
                        return_type = return_type,
                    ))
                continue

            # ── Constructor declarations ─────────────────────────────────────
            if ntype == "constructor_signature" and current_type:
                name_node = child.child_by_field_name("name")
                ctor_name = (
                    self._node_text(name_node, source_bytes)
                    if name_node else current_type
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
                ))
                continue

            # Recurse into other containers
            self._walk(child, source_bytes, raw_lines, symbols, current_type)

    # ── Helpers ───────────────────────────────────────────────────────────────

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
        """Return (param_types, return_type) for a function/method signature node."""
        param_types: List[str] = []
        return_type: Optional[str] = None

        for child in node.children:
            if child.type == "formal_parameter_list":
                for param in child.children:
                    if param.type in ("normal_formal_parameter", "optional_formal_parameter"):
                        type_node = param.child_by_field_name("type")
                        if type_node:
                            param_types.append(
                                self._node_text(type_node, source_bytes).strip()
                            )
            elif child.type == "type_annotation":
                return_type = self._node_text(child, source_bytes).strip()

        return param_types, return_type
