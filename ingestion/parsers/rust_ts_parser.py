"""
Rust language parser — tree-sitter powered (tree-sitter==0.23.2).

Extracts:
  - use_declaration groups          → ParsedSymbol(symbol_type="import")
  - struct / enum / trait / type    → ParsedSymbol(symbol_type="class_head")
  - impl blocks                     → ParsedSymbol(symbol_type="class_head",
                                                    name="impl …")
  - fn declarations                 → ParsedSymbol(symbol_type="function"|"method")

Rust-specific extras:
  - async fn tagged with @async decorator
  - #[test] and other outer attributes surfaced as decorators
  - Methods inside impl blocks get symbol_type="method" and parent_name set
  - param_types populated (skipping self / &self / &mut self receivers)
  - return_type populated from -> annotation

Node type reference (tree-sitter-rust grammar):
  - Outer attributes: "attribute_item" — appear as *siblings before* the item,
    not as children of the item itself.
  - Struct/enum/trait:  "struct_item", "enum_item", "trait_item", "type_item"
  - Impl:               "impl_item"
  - Functions:          "function_item"
  - Async modifier:     "function_modifiers" child containing "async"
  - Return type:        sibling of "->" token, e.g. "type_identifier"
  - Parameters:         "parameters" node with "parameter" / "self_parameter" children

Falls back to [] (not an exception) when tree-sitter-rust is not installed.
"""

import logging
from typing import List, Optional, Set, Tuple

from ingestion.models import ParsedSymbol
from ingestion.parsers.treesitter_base import TreeSitterParser

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_CALL_SKIP: Set[str] = {
    "println", "print", "vec", "format", "panic",
    "assert", "unwrap", "expect", "clone", "into", "from",
}

# Node types for type-like items
_TYPE_NODE_TYPES = {
    "struct_item",
    "enum_item",
    "trait_item",
    "type_item",
}

# Return-type node types (appear after "->" token)
_RETURN_TYPE_NODE_TYPES = {
    "type_identifier", "primitive_type", "reference_type",
    "generic_type", "scoped_type_identifier", "tuple_type",
    "array_type", "pointer_type", "abstract_type", "dynamic_type",
    "never_type", "macro_invocation",
}

# Self-parameter text patterns to skip for param_types
_SELF_PARAM_PREFIXES = ("self", "&self", "&mut self", "mut self")


class RustTsParser(TreeSitterParser):
    """Tree-sitter powered Rust parser."""

    # Per-subclass cache — must not share with siblings
    _language_obj = None
    _language_loaded: bool = False

    @property
    def language(self) -> str:
        return "rust"

    # ── Grammar loader ────────────────────────────────────────────────────────

    @classmethod
    def _load_language(cls):
        from tree_sitter import Language
        import tree_sitter_rust as tsrust
        return Language(tsrust.language())

    # ── Top-level extraction ──────────────────────────────────────────────────

    def _extract_symbols(
        self,
        tree,
        source_bytes: bytes,
        raw_lines: List[str],
    ) -> List[ParsedSymbol]:
        symbols: List[ParsedSymbol] = []
        root = tree.root_node

        symbols.extend(self._extract_uses(root, source_bytes, raw_lines))
        symbols.extend(
            self._extract_items(root, source_bytes, raw_lines, inside_impl=False, impl_name=None)
        )
        return symbols

    # ── Use-declaration grouping ──────────────────────────────────────────────

    def _extract_uses(self, root, source_bytes: bytes, raw_lines: List[str]) -> List[ParsedSymbol]:
        """Group contiguous use_declaration nodes into ParsedSymbol(s)."""
        use_nodes = [c for c in root.children if c.type == "use_declaration"]
        if not use_nodes:
            return []

        groups: list = []
        current: list = [use_nodes[0]]
        for node in use_nodes[1:]:
            prev_end   = current[-1].end_point[0] + 1
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
                if text.startswith("use "):
                    text = text[4:].strip()
                if text.endswith(";"):
                    text = text[:-1].strip()
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

    # ── Item extraction (recursive) ───────────────────────────────────────────

    def _extract_items(
        self,
        node,
        source_bytes: bytes,
        raw_lines: List[str],
        inside_impl: bool,
        impl_name: Optional[str],
    ) -> List[ParsedSymbol]:
        """Walk direct children of *node*, collecting preceding attribute_item siblings."""
        symbols: List[ParsedSymbol] = []
        children = list(node.children)
        pending_attrs: List[str] = []   # outer attributes accumulated from sibling attribute_item nodes

        for child in children:
            ct = child.type

            if ct == "attribute_item":
                # Accumulate; will be consumed by the next item
                text = self._node_text(child, source_bytes).strip()
                pending_attrs.append(text)
                continue

            if ct in _TYPE_NODE_TYPES:
                syms = self._handle_type_item(child, source_bytes, raw_lines, list(pending_attrs))
                symbols.extend(syms)
                pending_attrs = []

            elif ct == "impl_item":
                syms, new_impl_name = self._handle_impl_item(
                    child, source_bytes, raw_lines, list(pending_attrs)
                )
                symbols.extend(syms)
                pending_attrs = []
                # Recurse into the impl body
                symbols.extend(
                    self._extract_items(
                        child, source_bytes, raw_lines,
                        inside_impl=True,
                        impl_name=new_impl_name,
                    )
                )

            elif ct == "function_item":
                symbols.extend(
                    self._handle_function(
                        child, source_bytes, raw_lines,
                        inside_impl=inside_impl,
                        parent_name=impl_name,
                        outer_attrs=list(pending_attrs),
                    )
                )
                pending_attrs = []

            else:
                pending_attrs = []   # non-item node resets the accumulator
                # Recurse into containers (declaration_list, block, etc.)
                if child.child_count > 0 and ct not in (
                    "use_declaration", "string_literal", "line_comment", "block_comment"
                ):
                    symbols.extend(
                        self._extract_items(
                            child, source_bytes, raw_lines,
                            inside_impl=inside_impl,
                            impl_name=impl_name,
                        )
                    )

        return symbols

    # ── Struct / enum / trait / type handling ─────────────────────────────────

    def _handle_type_item(
        self,
        node,
        source_bytes: bytes,
        raw_lines: List[str],
        outer_attrs: List[str],
    ) -> List[ParsedSymbol]:
        name = self._get_type_identifier(node, source_bytes)
        if not name:
            return []

        start_line, block_end = self._node_lines(node)
        head_end = min(start_line + 4, block_end)

        bases: List[str] = []
        if node.type == "trait_item":
            bases = self._collect_trait_bounds(node, source_bytes)

        return [ParsedSymbol(
            symbol_type = "class_head",
            name        = name,
            start_line  = start_line,
            end_line    = head_end,
            source      = "\n".join(raw_lines[start_line - 1:head_end]),
            parent_name = None,
            bases       = bases,
            decorators  = outer_attrs,
        )]

    # ── Impl item handling ────────────────────────────────────────────────────

    def _handle_impl_item(
        self,
        node,
        source_bytes: bytes,
        raw_lines: List[str],
        outer_attrs: List[str],
    ) -> Tuple[List[ParsedSymbol], Optional[str]]:
        """
        Emit a class_head for the impl block itself.

        Returns (symbols_list, implementing_type_name).
        """
        start_line, block_end = self._node_lines(node)
        head_end = min(start_line + 4, block_end)

        type_name  = self._get_impl_type_name(node, source_bytes)
        trait_name = self._get_impl_trait_name(node, source_bytes)

        if trait_name:
            impl_display = "impl " + trait_name + " for " + type_name
            bases = [trait_name]
        else:
            impl_display = "impl " + type_name
            bases = []

        symbols = [ParsedSymbol(
            symbol_type = "class_head",
            name        = impl_display,
            start_line  = start_line,
            end_line    = head_end,
            source      = "\n".join(raw_lines[start_line - 1:head_end]),
            parent_name = None,
            bases       = bases,
            decorators  = outer_attrs,
        )]
        return symbols, type_name

    # ── Function / method handling ────────────────────────────────────────────

    def _handle_function(
        self,
        node,
        source_bytes: bytes,
        raw_lines: List[str],
        inside_impl: bool,
        parent_name: Optional[str],
        outer_attrs: List[str],
    ) -> List[ParsedSymbol]:
        name = self._get_fn_name(node, source_bytes)
        if not name:
            return []

        start_line, end_line = self._node_lines(node)

        decorators = list(outer_attrs)

        is_async = self._is_async_fn(node, source_bytes)
        if is_async and "@async" not in decorators:
            decorators.append("@async")

        param_types = self._collect_param_types(node, source_bytes)
        return_type = self._collect_return_type(node, source_bytes)
        calls       = self._collect_calls(node, source_bytes, _CALL_SKIP)

        symbol_type = "method" if inside_impl else "function"

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

    # ── Parameter-type extraction ─────────────────────────────────────────────

    def _collect_param_types(self, fn_node, source_bytes: bytes) -> List[str]:
        """Collect type strings for each non-self parameter."""
        param_types: List[str] = []
        params_node = fn_node.child_by_field_name("parameters")
        if params_node is None:
            for child in fn_node.children:
                if child.type == "parameters":
                    params_node = child
                    break
        if params_node is None:
            return param_types

        for param in params_node.named_children:
            if param.type == "self_parameter":
                continue
            if param.type not in ("parameter", "variadic_parameter"):
                continue
            param_text = self._node_text(param, source_bytes).strip()
            # Skip self receivers: "self", "&self", "&mut self"
            if param_text in _SELF_PARAM_PREFIXES or any(
                param_text.startswith(p) for p in _SELF_PARAM_PREFIXES
            ):
                if ":" not in param_text:
                    continue
            # type field or last named child
            type_node = param.child_by_field_name("type")
            if type_node is None:
                nc = [c for c in param.named_children if c.type != "identifier" and c.type != "mutable_specifier"]
                type_node = nc[-1] if nc else None
            if type_node is not None:
                t = self._extract_type_text(type_node, source_bytes)
                if t:
                    param_types.append(t)
        return param_types

    # ── Return-type extraction ────────────────────────────────────────────────

    def _collect_return_type(self, fn_node, source_bytes: bytes) -> Optional[str]:
        """Return the -> return type text, or None."""
        rt_node = fn_node.child_by_field_name("return_type")
        if rt_node is not None:
            return self._extract_type_text(rt_node, source_bytes)

        found_arrow = False
        for child in fn_node.children:
            raw = self._node_text(child, source_bytes).strip()
            if raw == "->":
                found_arrow = True
                continue
            if found_arrow and child.is_named:
                return self._extract_type_text(child, source_bytes)
        return None

    # ── Trait-bounds extraction ───────────────────────────────────────────────

    def _collect_trait_bounds(self, trait_node, source_bytes: bytes) -> List[str]:
        """Collect base trait names from a trait_item's trait_bounds."""
        bases: List[str] = []
        for child in trait_node.children:
            if child.type == "trait_bounds":
                for bound in child.named_children:
                    text = self._node_text(bound, source_bytes).strip()
                    if "<" in text:
                        text = text[:text.index("<")]
                    bases.append(text.strip())
        return bases

    # ── Identifier helpers ────────────────────────────────────────────────────

    @staticmethod
    def _get_type_identifier(node, source_bytes: bytes) -> Optional[str]:
        """Return the text of the first type_identifier child."""
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            return source_bytes[name_node.start_byte:name_node.end_byte].decode(
                "utf-8", errors="replace"
            ).strip()
        for child in node.children:
            if child.type == "type_identifier":
                return source_bytes[child.start_byte:child.end_byte].decode(
                    "utf-8", errors="replace"
                ).strip()
        return None

    @staticmethod
    def _get_fn_name(node, source_bytes: bytes) -> Optional[str]:
        """Return the text of the name/identifier child of a function_item."""
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            return source_bytes[name_node.start_byte:name_node.end_byte].decode(
                "utf-8", errors="replace"
            ).strip()
        for child in node.children:
            if child.type == "identifier":
                return source_bytes[child.start_byte:child.end_byte].decode(
                    "utf-8", errors="replace"
                ).strip()
        return None

    @staticmethod
    def _get_impl_type_name(node, source_bytes: bytes) -> str:
        """Return the implementing type name from an impl_item."""
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            return source_bytes[type_node.start_byte:type_node.end_byte].decode(
                "utf-8", errors="replace"
            ).strip()
        # Fallback: last type_identifier child before any block
        last = None
        for child in node.children:
            if child.type == "type_identifier":
                last = child
            elif child.type in ("declaration_list", "field_declaration_list"):
                break
        if last is not None:
            return source_bytes[last.start_byte:last.end_byte].decode(
                "utf-8", errors="replace"
            ).strip()
        return "Unknown"

    @staticmethod
    def _get_impl_trait_name(node, source_bytes: bytes) -> Optional[str]:
        """Return the trait name if this is a 'impl Trait for Type' block."""
        trait_node = node.child_by_field_name("trait")
        if trait_node is not None:
            text = source_bytes[trait_node.start_byte:trait_node.end_byte].decode(
                "utf-8", errors="replace"
            ).strip()
            if "<" in text:
                text = text[:text.index("<")]
            return text.strip() or None

        # Heuristic: look for "for" keyword among children
        children = list(node.children)
        for idx, child in enumerate(children):
            raw = source_bytes[child.start_byte:child.end_byte].decode(
                "utf-8", errors="replace"
            ).strip()
            if raw == "for" and idx > 0:
                trait_candidate = children[idx - 1]
                tc_type = trait_candidate.type
                # Skip keywords and type parameter lists
                if tc_type in ("type_parameters", "impl"):
                    continue
                tc_text = source_bytes[trait_candidate.start_byte:trait_candidate.end_byte].decode(
                    "utf-8", errors="replace"
                ).strip()
                if tc_text in ("impl", ""):
                    continue
                if "<" in tc_text:
                    tc_text = tc_text[:tc_text.index("<")]
                return tc_text.strip() or None
        return None

    @staticmethod
    def _is_async_fn(node, source_bytes: bytes) -> bool:
        """Return True if the function_item has an async modifier."""
        for child in node.children:
            if child.type == "function_modifiers":
                for mod in child.children:
                    if source_bytes[mod.start_byte:mod.end_byte].decode(
                        "utf-8", errors="replace"
                    ).strip() == "async":
                        return True
            raw = source_bytes[child.start_byte:child.end_byte].decode(
                "utf-8", errors="replace"
            ).strip()
            if raw == "async":
                return True
        return False
