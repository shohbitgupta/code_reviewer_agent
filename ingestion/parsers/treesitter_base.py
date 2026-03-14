"""
Shared base class for tree-sitter-powered language parsers.

Provides:
  - TreeSitterParser(BaseParser) — abstract base with lazy-loaded Language instance
  - Common helpers: _node_text, _node_lines, _collect_calls, _extract_type_text
  - Graceful fallback when the tree-sitter grammar package is not installed

Subclasses must implement:
  - language            @property  → str
  - _load_language()    classmethod → Language | None
  - _extract_symbols()  → List[ParsedSymbol]
"""

import logging
import threading
from abc import abstractmethod
from typing import List, Optional, Set

from ingestion.models import ParsedSymbol
from ingestion.parsers.base import BaseParser

logger = logging.getLogger(__name__)

# One module-level lock is sufficient: language loading happens at most once per
# subclass, then the fast path (cls._language_loaded is True) is taken lock-free.
_LANGUAGE_INIT_LOCK = threading.Lock()


class TreeSitterParser(BaseParser):
    """
    Abstract base class for tree-sitter-powered parsers.

    Class-level attributes
    ----------------------
    _language_obj : Language | None
        Lazy-loaded, cached.  Set to None if the grammar package is missing.
    """

    _language_obj = None          # subclass fills this in _load_language()
    _language_loaded: bool = False  # guard so we only attempt once

    # ── Abstract interface ────────────────────────────────────────────────────

    @classmethod
    @abstractmethod
    def _load_language(cls):
        """
        Return a tree_sitter.Language instance for this language, or None if the
        grammar package is not installed.  Called at most once per subclass.
        """
        ...

    @abstractmethod
    def _extract_symbols(
        self,
        tree,
        source_bytes: bytes,
        raw_lines: List[str],
    ) -> List[ParsedSymbol]:
        """Walk the CST and return ParsedSymbol objects."""
        ...

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def _make_parser(cls):
        """
        Return a configured tree_sitter.Parser, or None if unavailable.

        Importing Parser is deferred here so that importing this module never
        raises even when tree-sitter itself is not installed.
        """
        lang = cls._get_language()
        if lang is None:
            return None
        try:
            from tree_sitter import Parser
            return Parser(lang)
        except Exception as exc:  # pragma: no cover
            logger.warning("tree-sitter Parser construction failed: %s", exc)
            return None

    @classmethod
    def _get_language(cls):
        """
        Lazy-load and cache the Language object (once per subclass).

        Thread-safe: uses double-checked locking so that concurrent calls from
        parallel worker threads never load the grammar twice.
        """
        if cls._language_loaded:          # fast path — no lock needed after first load
            return cls._language_obj
        with _LANGUAGE_INIT_LOCK:
            if not cls._language_loaded:  # re-check inside the lock
                cls._language_loaded = True
                try:
                    cls._language_obj = cls._load_language()
                except Exception as exc:
                    logger.warning(
                        "tree-sitter grammar not available for %s: %s",
                        cls.__name__,
                        exc,
                    )
                    cls._language_obj = None
        return cls._language_obj

    # ── BaseParser.parse implementation ──────────────────────────────────────

    def parse(self, source: str, raw_lines: List[str]) -> List[ParsedSymbol]:
        """
        Parse *source* and return a list of ParsedSymbol objects.

        Falls back to [] (not an exception) when the grammar is unavailable or
        parsing raises unexpectedly.
        """
        parser = self._make_parser()
        if parser is None:
            logger.warning(
                "%s: tree-sitter grammar unavailable — returning empty symbol list",
                self.__class__.__name__,
            )
            return []
        try:
            source_bytes = source.encode("utf-8")
            tree = parser.parse(source_bytes)
            return self._extract_symbols(tree, source_bytes, raw_lines)
        except Exception as exc:  # pragma: no cover
            logger.warning("%s.parse() raised: %s", self.__class__.__name__, exc)
            return []

    # ── Shared helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _node_text(node, source_bytes: bytes) -> str:
        """Return the UTF-8 text covered by *node*."""
        return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    @staticmethod
    def _node_lines(node) -> tuple:
        """
        Return (start_line, end_line) as 1-indexed integers.

        node.start_point and node.end_point are (row, col) 0-indexed tuples.
        """
        start_line = node.start_point[0] + 1
        end_line   = node.end_point[0] + 1
        return start_line, end_line

    @classmethod
    def _collect_calls(
        cls,
        node,
        source_bytes: bytes,
        skip_set: Set[str],
    ) -> List[str]:
        """
        Recursively walk *node* and collect callee names from call_expression
        nodes, excluding names in *skip_set*.

        Returns a deduplicated list preserving first-occurrence order.
        """
        seen:   list = []
        result: list = []

        def _walk(n):
            if n.type == "call_expression":
                # The function/callee is typically the first child
                callee_node = n.child_by_field_name("function") or (
                    n.children[0] if n.children else None
                )
                if callee_node is not None:
                    raw = cls._node_text(callee_node, source_bytes).strip()
                    # Take the rightmost component for method chains: "foo.bar" → "bar"
                    name = raw.split(".")[-1].strip()
                    # Strip generic brackets: "foo<T>" → "foo"
                    if "<" in name:
                        name = name[:name.index("<")]
                    name = name.strip()
                    if name and name not in skip_set and name not in seen:
                        seen.append(name)
                        result.append(name)
            for child in n.children:
                _walk(child)

        _walk(node)
        return result

    @classmethod
    def _extract_type_text(cls, node, source_bytes: bytes) -> Optional[str]:
        """
        Return the text of a type-annotation node, or None if *node* is None.

        Strips leading/trailing whitespace and a leading ':' or '->' delimiter
        if present (some grammars include those in the annotation node).
        """
        if node is None:
            return None
        text = cls._node_text(node, source_bytes).strip()
        # Drop leading ':' or '->' that some grammars attach to the type node
        for prefix in ("->", ":"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        return text if text else None
