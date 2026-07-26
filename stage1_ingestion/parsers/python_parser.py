"""
Python language parser — uses the stdlib `ast` module for full fidelity.

Handles all valid Python syntax: decorators, async/await, type hints,
walrus operator, dataclasses, and nested functions.

Two-pass class handling:
  Pass 1 → emit CLASS_HEAD (signature + docstring only, not the body)
  Pass 2 → emit each method as an independent METHOD symbol

This ensures methods can be retrieved and reviewed independently of
their parent class.
"""

import ast
import logging
from typing import List, Optional

from core.models import ParsedSymbol
from stage1_ingestion.parsers.base import BaseParser

logger = logging.getLogger(__name__)

# Trivial dunder methods — not worth independent symbols
SKIP_SYMBOL_NAMES = {
    "__repr__", "__str__", "__eq__", "__hash__",
    "__len__", "__iter__", "__next__", "__bool__",
    "__contains__", "__del__",
}

# Common stdlib builtins — exclude from calls[] to reduce noise
SKIP_CALLS = {
    "print", "len", "range", "enumerate", "zip", "map", "filter",
    "sorted", "reversed", "list", "dict", "set", "tuple", "frozenset",
    "str", "int", "float", "bool", "bytes",
    "type", "isinstance", "issubclass",
    "getattr", "setattr", "hasattr", "delattr",
    "super", "vars", "dir", "id", "hash",
    "open", "input", "format", "repr",
}


class PythonParser(BaseParser):
    """
    Full-fidelity Python parser using the stdlib ast module.

    Zero external dependencies — works with any Python 3.8+ installation.
    """

    @property
    def language(self) -> str:
        return "python"

    def parse(self, source: str, raw_lines: List[str]) -> List[ParsedSymbol]:
        """
        Parse Python source into grouped imports and top-level/class symbols.

        Uses ast.parse() for full-fidelity parsing; on SyntaxError the file is
        treated as unparseable so the chunker falls back to sliding-window
        chunking. Classes are emitted as a CLASS_HEAD symbol (signature and
        docstring only) followed by each method as an independent symbol
        (see module docstring for the two-pass rationale).

        Args:
            source:    Full file content as a single string.
            raw_lines: Source split by newline (1-indexed when used with [i-1]).

        Returns:
            List[ParsedSymbol] — empty list on a SyntaxError, never None.
        """
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            logger.debug("[PythonParser] SyntaxError: %s", exc)
            return []

        symbols: List[ParsedSymbol] = []
        symbols.extend(self._import_groups(tree, raw_lines))

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sym = self._function(node, raw_lines, parent_name=None)
                if sym:
                    symbols.append(sym)
            elif isinstance(node, ast.ClassDef):
                symbols.append(self._class_head(node, raw_lines))
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        sym = self._function(child, raw_lines, parent_name=node.name)
                        if sym:
                            symbols.append(sym)

        return symbols

    # ── Symbol extractors ─────────────────────────────────────────────────────

    def _import_groups(self, tree: ast.AST, raw_lines: List[str]) -> List[ParsedSymbol]:
        """Group contiguous import statements into single import symbols."""
        import_nodes = [
            n for n in ast.iter_child_nodes(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))
        ]
        if not import_nodes:
            return []

        groups: list = []
        current = [import_nodes[0]]
        for node in import_nodes[1:]:
            # Allow up to 1 blank line between statements
            if node.lineno <= current[-1].end_lineno + 2:
                current.append(node)
            else:
                groups.append(current)
                current = [node]
        groups.append(current)

        symbols: List[ParsedSymbol] = []
        for group in groups:
            start = group[0].lineno
            end   = group[-1].end_lineno  # type: ignore[attr-defined]
            paths: List[str] = []
            for n in group:
                if isinstance(n, ast.ImportFrom) and n.module:
                    paths.append(n.module)
                elif isinstance(n, ast.Import):
                    paths.extend(a.name for a in n.names)
            symbols.append(ParsedSymbol(
                symbol_type = "import",
                name        = "imports",
                start_line  = start,
                end_line    = end,
                source      = "\n".join(raw_lines[start - 1:end]),
                parent_name = None,
                imports     = paths,
            ))
        return symbols

    def _class_head(self, node: ast.ClassDef, raw_lines: List[str]) -> ParsedSymbol:
        """Emit CLASS_HEAD: class signature + docstring only (not the body)."""
        method_starts = [
            child.lineno
            for child in ast.iter_child_nodes(node)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        end_line = (min(method_starts) - 1) if method_starts else node.end_lineno  # type: ignore[attr-defined]
        end_line = max(end_line, node.lineno)

        return ParsedSymbol(
            symbol_type = "class_head",
            name        = node.name,
            start_line  = node.lineno,
            end_line    = end_line,
            source      = "\n".join(raw_lines[node.lineno - 1:end_line]),
            parent_name = None,
            bases       = self._bases(node),
        )

    def _function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        raw_lines: List[str],
        parent_name: Optional[str],
    ) -> Optional[ParsedSymbol]:
        """
        Build a ParsedSymbol for a function or method definition.

        Records decorators (via ast.unparse when available) and the set of
        called symbol names, deduplicated and with common builtins excluded
        (see SKIP_CALLS).

        Args:
            node:        The FunctionDef or AsyncFunctionDef node.
            raw_lines:   Full file source split by newline.
            parent_name: Enclosing class name, or None for a top-level function.

        Returns:
            A ParsedSymbol, or None if the name is a trivial dunder method
            (see SKIP_SYMBOL_NAMES).
        """
        if node.name in SKIP_SYMBOL_NAMES:
            return None

        end_line   = node.end_lineno  # type: ignore[attr-defined]
        decorators = (
            [f"@{ast.unparse(d)}" for d in node.decorator_list]
            if hasattr(ast, "unparse") else []
        )

        calls: List[str] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                name = self._call_name(child)
                if name and name not in SKIP_CALLS and name not in calls:
                    calls.append(name)

        return ParsedSymbol(
            symbol_type = "method" if parent_name else "function",
            name        = node.name,
            start_line  = node.lineno,
            end_line    = end_line,
            source      = "\n".join(raw_lines[node.lineno - 1:end_line]),
            parent_name = parent_name,
            decorators  = decorators,
            calls       = calls,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _call_name(node: ast.Call) -> Optional[str]:
        """
        Resolve the callee name for a Call node.

        Plain calls return the bare name; single-level attribute calls return
        "receiver.attr" (e.g. "self.save"); chained attribute calls (where the
        receiver is itself an Attribute, e.g. "a.b.c()") return only the final
        attribute name. Returns None for calls whose target isn't a Name or
        Attribute (e.g. calling the result of a subscript or another call).
        """
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                return f"{node.func.value.id}.{node.func.attr}"
            return node.func.attr
        return None

    @staticmethod
    def _bases(node: ast.ClassDef) -> List[str]:
        """
        Return base class names as source-text strings.

        Prefers ast.unparse (Python 3.9+) for full-fidelity output, which
        correctly handles generics and dotted paths; falls back to
        reconstructing simple Name and single-level Attribute bases on older
        interpreters where ast.unparse is unavailable.
        """
        result = []
        for b in node.bases:
            if hasattr(ast, "unparse"):
                result.append(ast.unparse(b))
            elif isinstance(b, ast.Name):
                result.append(b.id)
            elif isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name):
                result.append(f"{b.value.id}.{b.attr}")
        return result
