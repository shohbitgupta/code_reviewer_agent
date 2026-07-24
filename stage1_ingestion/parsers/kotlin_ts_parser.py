"""
Kotlin language parser — tree-sitter powered (tree-sitter==0.23.2).

Extracts:
  - import groups                  → ParsedSymbol(symbol_type="import")
  - class / interface / object /
    enum class declarations        → ParsedSymbol(symbol_type="class_head")
  - fun declarations               → ParsedSymbol(symbol_type="function"|"method")
  - companion_object               → ParsedSymbol(symbol_type="class_head",
                                                   decorators=["@companion"])

Android / Compose tagging:
  - @android_component on class_head when a base class is in ANDROID_COMPONENT_BASES
  - @android_lifecycle on methods whose name is in ANDROID_LIFECYCLE_METHODS
  - @Composable propagated from annotations already present in decorators

Node type reference (tree-sitter-kotlin grammar):
  - Imports:   node.type == "import"               (NOT "import_header")
  - Names:     node.type == "identifier"            (NOT "simple_identifier")
  - Bases:     node.type == "delegation_specifiers" wrapping "delegation_specifier"
  - Classes:   "class_declaration", "object_declaration", "interface_declaration"
  - Companion: "companion_object"
  - Functions: "function_declaration"
  - Params:    "function_value_parameters" → "parameter" children
  - Annots:    "annotation" inside "modifiers"
  - Return:    child nodes of type user_type / nullable_type / function_type
               that appear after ":" following the parameter list

Falls back to [] (not an exception) when tree-sitter-kotlin is not installed.
"""

import logging
import re
from typing import List, Optional, Set, Tuple

from core.models import ParsedSymbol
from stage1_ingestion.parsers.treesitter_base import TreeSitterParser

logger = logging.getLogger(__name__)

# ── Android / Compose constants ───────────────────────────────────────────────

ANDROID_COMPONENT_BASES: Set[str] = {
    "AppCompatActivity", "ComponentActivity", "FragmentActivity", "Activity",
    "Fragment", "DialogFragment",
    "ViewModel", "AndroidViewModel",
    "Service",
    "BroadcastReceiver",
    "RecyclerView.Adapter", "ListAdapter",
    "Worker", "CoroutineWorker",
}

ANDROID_LIFECYCLE_METHODS: Set[str] = {
    "onCreate", "onStart", "onResume", "onPause",
    "onStop", "onDestroy",
    "onCreateView", "onViewCreated", "onDestroyView",
    "onAttach", "onDetach",
    "onActivityResult", "onBackPressed",
    "onBind", "onReceive",
    "onCleared",
    "onSaveInstanceState",
}

_CALL_SKIP: Set[str] = {"println", "print", "super", "this", "it"}

# Node types that represent a class-like declaration
_CLASS_NODE_TYPES = {
    "class_declaration",
    "object_declaration",
    "interface_declaration",
    # enum class is represented as class_declaration with "enum" modifier
}

# Return-type node types (appear as direct children of function_declaration)
_RETURN_TYPE_NODE_TYPES = {
    "user_type", "nullable_type", "function_type",
    "parenthesized_type", "type_reference",
}

# Generic-arg / constructor-call stripping for bases
_GENERICS_RE = re.compile(r"<[^<>]*>")
_CTOR_ARGS_RE = re.compile(r"\([^)]*\)")


def _strip_base(text: str) -> str:
    """Remove generic type arguments and constructor-call parens from a base name."""
    text = _GENERICS_RE.sub("", text)
    text = _CTOR_ARGS_RE.sub("", text)
    return text.strip()


class KotlinTsParser(TreeSitterParser):
    """Tree-sitter powered Kotlin parser."""

    # Per-subclass cache — must not share with siblings
    _language_obj = None
    _language_loaded: bool = False

    @property
    def language(self) -> str:
        return "kotlin"

    # ── Grammar loader ────────────────────────────────────────────────────────

    @classmethod
    def _load_language(cls):
        from tree_sitter import Language
        import tree_sitter_kotlin as tskotlin
        return Language(tskotlin.language())

    # ── Top-level extraction ──────────────────────────────────────────────────

    def _extract_symbols(
        self,
        tree,
        source_bytes: bytes,
        raw_lines: List[str],
    ) -> List[ParsedSymbol]:
        symbols: List[ParsedSymbol] = []
        root = tree.root_node

        symbols.extend(self._extract_imports(root, source_bytes, raw_lines))
        symbols.extend(
            self._extract_declarations(root, source_bytes, raw_lines, parent_name=None)
        )
        return symbols

    # ── Import extraction ─────────────────────────────────────────────────────

    def _extract_imports(
        self, root, source_bytes: bytes, raw_lines: List[str]
    ) -> List[ParsedSymbol]:
        """Group contiguous 'import' nodes into single ParsedSymbol(s)."""
        # In tree-sitter-kotlin the node type for an import line is "import"
        import_nodes = [c for c in root.children if c.type == "import"]
        if not import_nodes:
            return []

        # Group by contiguity (gap <= 1 blank line)
        groups: list = []
        current: list = [import_nodes[0]]
        for node in import_nodes[1:]:
            prev_end   = current[-1].end_point[0] + 1   # 1-indexed
            this_start = node.start_point[0] + 1
            if this_start <= prev_end + 2:
                current.append(node)
            else:
                groups.append(current)
                current = [node]
        groups.append(current)

        result: List[ParsedSymbol] = []
        for group in groups:
            start_line = group[0].start_point[0] + 1
            end_line   = group[-1].end_point[0] + 1
            paths: List[str] = []
            for node in group:
                text = self._node_text(node, source_bytes).strip()
                if text.startswith("import "):
                    text = text[len("import "):].strip()
                paths.append(text)
            result.append(ParsedSymbol(
                symbol_type = "import",
                name        = "imports",
                start_line  = start_line,
                end_line    = end_line,
                source      = "\n".join(raw_lines[start_line - 1:end_line]),
                parent_name = None,
                imports     = paths,
            ))
        return result

    # ── Declaration extraction (recursive) ───────────────────────────────────

    def _extract_declarations(
        self,
        node,
        source_bytes: bytes,
        raw_lines: List[str],
        parent_name: Optional[str],
    ) -> List[ParsedSymbol]:
        """Recursively extract class, object, interface, and function symbols."""
        symbols: List[ParsedSymbol] = []

        for child in node.children:
            ct = child.type

            if ct == "companion_object":
                syms = self._handle_companion(child, source_bytes, raw_lines, parent_name)
                symbols.extend(syms)
                comp_name = self._get_identifier(child, source_bytes) or "Companion"
                symbols.extend(
                    self._extract_declarations(child, source_bytes, raw_lines, comp_name)
                )

            elif ct in _CLASS_NODE_TYPES:
                syms = self._handle_class(child, source_bytes, raw_lines, parent_name)
                symbols.extend(syms)
                cls_name = self._get_identifier(child, source_bytes)
                if cls_name:
                    symbols.extend(
                        self._extract_declarations(child, source_bytes, raw_lines, cls_name)
                    )

            elif ct == "function_declaration":
                fn_name = self._get_identifier(child, source_bytes)
                if fn_name:
                    symbols.extend(
                        self._handle_function(child, source_bytes, raw_lines, parent_name)
                    )

            else:
                # Recurse into any container node (class_body, object_body, etc.)
                if child.child_count > 0:
                    symbols.extend(
                        self._extract_declarations(child, source_bytes, raw_lines, parent_name)
                    )

        return symbols

    # ── Class / interface / object handling ───────────────────────────────────

    def _handle_class(
        self,
        node,
        source_bytes: bytes,
        raw_lines: List[str],
        parent_name: Optional[str],
    ) -> List[ParsedSymbol]:
        name = self._get_identifier(node, source_bytes)
        if not name:
            return []

        start_line, block_end = self._node_lines(node)
        decorators = self._collect_annotations(node, source_bytes)
        bases      = self._collect_bases(node, source_bytes)

        if any(b.split(".")[-1] in ANDROID_COMPONENT_BASES for b in bases):
            decorators.append("@android_component")

        head_end = min(start_line + 4, block_end)
        return [ParsedSymbol(
            symbol_type = "class_head",
            name        = name,
            start_line  = start_line,
            end_line    = head_end,
            source      = "\n".join(raw_lines[start_line - 1:head_end]),
            parent_name = parent_name,
            bases       = bases,
            decorators  = decorators,
        )]

    # ── Companion object handling ──────────────────────────────────────────────

    def _handle_companion(
        self,
        node,
        source_bytes: bytes,
        raw_lines: List[str],
        parent_name: Optional[str],
    ) -> List[ParsedSymbol]:
        name = self._get_identifier(node, source_bytes) or "Companion"
        start_line, block_end = self._node_lines(node)
        decorators = self._collect_annotations(node, source_bytes) + ["@companion"]
        head_end = min(start_line + 3, block_end)
        return [ParsedSymbol(
            symbol_type = "class_head",
            name        = name,
            start_line  = start_line,
            end_line    = head_end,
            source      = "\n".join(raw_lines[start_line - 1:head_end]),
            parent_name = parent_name,
            decorators  = decorators,
        )]

    # ── Function / method handling ────────────────────────────────────────────

    # Keyword tokens that can appear before the receiver type in function_declaration
    _MODIFIER_TEXTS: Set[str] = {
        "fun", "suspend", "inline", "private", "public", "internal",
        "protected", "open", "override", "tailrec", "operator", "infix",
        "external", "expect", "actual", "abstract", "final",
    }

    @staticmethod
    def _get_extension_receiver(fn_node, source_bytes: bytes) -> Optional[str]:
        """Return the receiver type name for Kotlin extension functions, or None.

        For `fun String.validate()` returns `"String"`.
        Detects by finding a '.' literal among direct children of the
        function_declaration and inspecting the preceding sibling.
        """
        children = list(fn_node.children)
        for i, child in enumerate(children):
            raw = source_bytes[child.start_byte:child.end_byte].decode(
                "utf-8", errors="replace"
            ).strip()
            if raw == "." and i > 0:
                # Walk backward to the first non-trivial sibling
                for j in range(i - 1, -1, -1):
                    prev = children[j]
                    prev_text = source_bytes[prev.start_byte:prev.end_byte].decode(
                        "utf-8", errors="replace"
                    ).strip()
                    if prev_text and prev_text not in KotlinTsParser._MODIFIER_TEXTS \
                            and prev.type not in ("modifiers", "function_modifiers", "type_parameters"):
                        # Strip generic params: "List<String>" → "List"
                        return prev_text.split("<")[0].strip()
        return None

    def _handle_function(
        self,
        node,
        source_bytes: bytes,
        raw_lines: List[str],
        parent_name: Optional[str],
    ) -> List[ParsedSymbol]:
        name = self._get_identifier(node, source_bytes)
        if not name:
            return []

        start_line, end_line = self._node_lines(node)
        decorators  = self._collect_annotations(node, source_bytes)
        param_types = self._collect_param_types(node, source_bytes)
        return_type = self._collect_return_type(node, source_bytes)
        calls       = self._collect_calls(node, source_bytes, _CALL_SKIP)

        if name in ANDROID_LIFECYCLE_METHODS:
            decorators.append("@android_lifecycle")

        # Tag extension functions so the review agent can identify them
        receiver = self._get_extension_receiver(node, source_bytes)
        if receiver:
            decorators.append(f"@extension:{receiver}")

        symbol_type = "method" if parent_name else "function"

        return [ParsedSymbol(
            symbol_type = symbol_type,
            name        = name,
            start_line  = start_line,
            end_line    = end_line,
            source      = "\n".join(raw_lines[start_line - 1:end_line]),
            parent_name = parent_name,
            decorators  = decorators,
            calls       = calls,
            param_types = param_types,
            return_type = return_type,
        )]

    # ── Parameter-type extraction ──────────────────────────────────────────────

    def _collect_param_types(self, fn_node, source_bytes: bytes) -> List[str]:
        """Return the type-annotation text for each parameter."""
        param_types: List[str] = []
        params_node = None
        for child in fn_node.children:
            if child.type == "function_value_parameters":
                params_node = child
                break
        if params_node is None:
            return param_types

        for param in params_node.named_children:
            if param.type != "parameter":
                continue
            # parameter → identifier ':' type_node
            # The type node is the last named child (after the identifier and ':')
            type_node = None
            for c in param.children:
                if c.type not in ("identifier", ":", ",") and c.is_named:
                    type_node = c
            if type_node is not None:
                t = self._extract_type_text(type_node, source_bytes)
                if t:
                    param_types.append(t)
        return param_types

    # ── Return-type extraction ────────────────────────────────────────────────

    def _collect_return_type(self, fn_node, source_bytes: bytes) -> Optional[str]:
        """
        Return the return-type annotation text, or None.

        In the Kotlin grammar, the return type appears as a direct child of
        function_declaration with types like user_type / nullable_type /
        function_type, following the ':' token that comes after the parameter
        list.  There is no dedicated 'return_type' field name in this grammar.
        """
        found_params = False
        found_colon  = False
        for child in fn_node.children:
            ct = child.type
            if ct == "function_value_parameters":
                found_params = True
                continue
            if found_params and ct == ":":
                found_colon = True
                continue
            if found_colon and ct in _RETURN_TYPE_NODE_TYPES:
                return self._extract_type_text(child, source_bytes)
            # Stop at the function body
            if ct == "function_body":
                break
        return None

    # ── Base class extraction ─────────────────────────────────────────────────

    def _collect_bases(self, class_node, source_bytes: bytes) -> List[str]:
        """Collect base/interface names from delegation_specifiers children."""
        bases: List[str] = []
        for child in class_node.children:
            if child.type == "delegation_specifiers":
                for spec in child.children:
                    if spec.type == "delegation_specifier":
                        raw = self._node_text(spec, source_bytes)
                        clean = _strip_base(raw)
                        if clean:
                            bases.append(clean.split(".")[-1])
        return bases

    # ── Annotation collection ─────────────────────────────────────────────────

    def _collect_annotations(self, node, source_bytes: bytes) -> List[str]:
        """
        Collect @Annotation decorator strings from modifiers attached to *node*.

        In the tree-sitter-kotlin grammar annotations live inside a 'modifiers'
        node as 'annotation' children.  The annotation text may include
        constructor arguments: strip those for the decorator tag.
        """
        decorators: List[str] = []
        for child in node.children:
            if child.type == "modifiers":
                for mod in child.children:
                    if mod.type == "annotation":
                        text = self._node_text(mod, source_bytes).strip()
                        # Keep only "@Name" part; drop arguments
                        name_part = text.split("(")[0].strip()
                        if not name_part.startswith("@"):
                            name_part = "@" + name_part.lstrip("@")
                        decorators.append(name_part)
        return decorators

    # ── Identifier helper ─────────────────────────────────────────────────────

    @staticmethod
    def _get_identifier(node, source_bytes: bytes) -> Optional[str]:
        """Return the text of the first 'identifier' direct child, or None."""
        for child in node.children:
            if child.type == "identifier":
                return source_bytes[child.start_byte:child.end_byte].decode(
                    "utf-8", errors="replace"
                ).strip()
        return None
