"""
Swift language parser — tree-sitter powered.

Extracts:
  - import declarations            → ParsedSymbol(symbol_type="import")
  - class / struct / enum /
    protocol / actor / extension   → ParsedSymbol(symbol_type="class_head")
  - func declarations (top-level
    and instance methods)          → ParsedSymbol(symbol_type="function"|"method")
  - init / deinit                  → ParsedSymbol(symbol_type="method")
  - computed property get/set      → ParsedSymbol(symbol_type="method")

Swift-specific tagging:
  - @swift_ui_view on class_head when base is View, UIViewController, etc.
  - @swift_lifecycle on init/deinit and viewDidLoad/viewWillAppear etc.

Grammar: tree-sitter-languages provides "swift" via get_language("swift").
Falls back to [] (not an exception) when tree-sitter-languages is not installed.

Pattern: mirrors KotlinTsParser — subclass TreeSitterParser, implement
_load_language() and _extract_symbols().
"""

import logging
from typing import List, Optional, Set

from core.models import ParsedSymbol
from stage1_ingestion.parsers.treesitter_base import TreeSitterParser

logger = logging.getLogger(__name__)

# Swift UI / ViewController base class names
_SWIFT_UI_BASES: Set[str] = {
    "View", "UIView", "UIViewController", "UITableViewController",
    "UICollectionViewController", "UINavigationController",
    "NSViewController", "NSView",
    "ObservableObject", "EnvironmentObject",
}

_SWIFT_LIFECYCLE_NAMES: Set[str] = {
    "init", "deinit",
    "viewDidLoad", "viewWillAppear", "viewDidAppear",
    "viewWillDisappear", "viewDidDisappear",
    "viewDidLayoutSubviews", "awakeFromNib",
    "body",  # SwiftUI View.body
}

_CALL_SKIP: Set[str] = {"print", "super", "self", "Swift", "debugPrint"}

# Node types that represent class-like declarations
_TYPE_NODE_TYPES = {
    "class_declaration",
    "struct_declaration",
    "enum_declaration",
    "protocol_declaration",
    "actor_declaration",
    "extension_declaration",
}


class SwiftTsParser(TreeSitterParser):
    """Tree-sitter powered Swift parser."""

    _language_obj = None
    _language_loaded: bool = False

    @property
    def language(self) -> str:
        return "swift"

    @classmethod
    def _load_language(cls):
        import tree_sitter_swift
        from tree_sitter import Language
        return Language(tree_sitter_swift.language())

    def _extract_symbols(
        self,
        tree,
        source_bytes: bytes,
        raw_lines: List[str],
    ) -> List[ParsedSymbol]:
        symbols: List[ParsedSymbol] = []
        root = tree.root_node

        # ── Import declarations ──────────────────────────────────────────────
        import_lines = []
        for child in root.children:
            if child.type == "import_declaration":
                start, end = self._node_lines(child)
                import_lines.append((start, end, self._node_text(child, source_bytes)))

        if import_lines:
            # Group contiguous imports
            groups: List[List] = [[import_lines[0]]]
            for entry in import_lines[1:]:
                if entry[0] <= groups[-1][-1][1] + 2:
                    groups[-1].append(entry)
                else:
                    groups.append([entry])
            for group in groups:
                start = group[0][0]
                end   = group[-1][1]
                paths = [n.replace("import", "").strip() for _, _, n in group]
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
        for child in node.children:
            ntype = child.type

            # ── Type declarations ────────────────────────────────────────────
            if ntype in _TYPE_NODE_TYPES:
                name_node = child.child_by_field_name("name")
                name = self._node_text(name_node, source_bytes) if name_node else "anonymous"
                start, end = self._node_lines(child)
                head_end = min(start + 4, end)

                bases = self._collect_bases(child, source_bytes)
                is_ui = bool(_SWIFT_UI_BASES & set(bases))
                decorators = []
                if is_ui:
                    decorators.append("@swift_ui_view")

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
                # Recurse into the body to find methods
                self._walk(child, source_bytes, raw_lines, symbols, current_type=name)
                continue

            # ── Function / method declarations ───────────────────────────────
            if ntype in ("function_declaration", "init_declaration", "deinit_declaration"):
                if ntype == "init_declaration":
                    func_name = "init"
                elif ntype == "deinit_declaration":
                    func_name = "deinit"
                else:
                    name_node = child.child_by_field_name("name")
                    func_name = self._node_text(name_node, source_bytes) if name_node else "_unknown"

                start, end = self._node_lines(child)
                is_lifecycle = func_name in _SWIFT_LIFECYCLE_NAMES
                sym_source = "\n".join(raw_lines[start - 1:end])

                param_types, return_type = self._extract_signature(child, source_bytes)

                symbols.append(ParsedSymbol(
                    symbol_type = "method" if current_type else "function",
                    name        = func_name,
                    start_line  = start,
                    end_line    = end,
                    source      = sym_source,
                    parent_name = current_type,
                    decorators  = ["@swift_lifecycle"] if is_lifecycle else [],
                    calls       = self._collect_calls(child, source_bytes, _CALL_SKIP),
                    param_types = param_types,
                    return_type = return_type,
                ))
                continue

            # ── Computed property (var with get/set body) ────────────────────
            if ntype == "property_declaration" and current_type:
                name_node = child.child_by_field_name("name")
                if name_node:
                    prop_name = self._node_text(name_node, source_bytes)
                    start, end = self._node_lines(child)
                    sym_source = "\n".join(raw_lines[start - 1:end])
                    # Only emit if it has a computed body (contains get/set)
                    text = self._node_text(child, source_bytes)
                    if "get" in text or "set" in text or "willSet" in text or "didSet" in text:
                        symbols.append(ParsedSymbol(
                            symbol_type = "method",
                            name        = prop_name,
                            start_line  = start,
                            end_line    = end,
                            source      = sym_source,
                            parent_name = current_type,
                        ))
                continue

            # Recurse into other containers (e.g. extension body without name match)
            self._walk(child, source_bytes, raw_lines, symbols, current_type)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _collect_bases(self, node, source_bytes: bytes) -> List[str]:
        """Extract base class / protocol names from a type declaration."""
        bases: List[str] = []
        for child in node.children:
            if child.type == "inheritance_specifier":
                # tree-sitter-swift 0.7+: one node per base type
                text = self._node_text(child, source_bytes).strip()
                name = text.split("<")[0].strip()
                if name:
                    bases.append(name)
            elif child.type in ("type_inheritance_clause", "type_constraints"):
                # Older grammar fallback: single comma-separated clause
                text = self._node_text(child, source_bytes)
                raw = text.lstrip(":").strip()
                for part in raw.split(","):
                    name = part.strip().split("<")[0].strip()
                    if name:
                        bases.append(name)
        return bases

    def _extract_signature(
        self, node, source_bytes: bytes
    ):
        """Return (param_types, return_type) from a function declaration node."""
        param_types: List[str] = []
        return_type: Optional[str] = None

        for child in node.children:
            if child.type == "parameter_clause":
                for param in child.children:
                    if param.type == "parameter":
                        type_node = param.child_by_field_name("type")
                        if type_node:
                            param_types.append(
                                self._node_text(type_node, source_bytes).strip()
                            )
            elif child.type == "function_result":
                return_type = self._node_text(child, source_bytes).strip().lstrip("->").strip()

        return param_types, return_type
