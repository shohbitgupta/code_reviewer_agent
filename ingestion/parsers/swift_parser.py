"""
Swift language parser — regex heuristics.

Extracts:
  - import statements  → grouped import symbol
  - class / struct / enum / protocol declarations → class_head symbol
  - func declarations  → function or method symbol (inside class = method)

Upgrade path: replace regex with tree-sitter-swift when available.
"""

import logging
import re
from typing import List, Optional

from ingestion.models import ParsedSymbol
from ingestion.parsers.base import BaseParser

logger = logging.getLogger(__name__)

# Matches Swift import lines
_IMPORT_RE = re.compile(r"^import\s+\w+")

# Matches class, struct, enum, protocol, actor declarations
_TYPE_RE = re.compile(
    r"^(?:public\s+|private\s+|internal\s+|open\s+|fileprivate\s+)?"
    r"(?:final\s+)?(?:class|struct|enum|protocol|actor)\s+(\w+)"
    r"(?:\s*:\s*([\w,\s]+))?"
)

# Matches func declarations (handles generic type params)
_FUNC_RE = re.compile(
    r"^(?:public\s+|private\s+|internal\s+|open\s+|fileprivate\s+)?"
    r"(?:static\s+|class\s+)?(?:override\s+)?(?:mutating\s+)?(?:async\s+)?"
    r"func\s+(\w+)\s*[<(]"
)


class SwiftParser(BaseParser):
    """Regex-based Swift parser for functions, classes, structs, and imports."""

    @property
    def language(self) -> str:
        return "swift"

    def parse(self, source: str, raw_lines: List[str]) -> List[ParsedSymbol]:
        symbols: List[ParsedSymbol] = []
        symbols.extend(self._imports(raw_lines))
        symbols.extend(self._types_and_funcs(source, raw_lines))
        return symbols

    def _imports(self, raw_lines: List[str]) -> List[ParsedSymbol]:
        """Group contiguous import lines into one symbol."""
        import_lines = [
            i + 1 for i, line in enumerate(raw_lines)
            if _IMPORT_RE.match(line.strip())
        ]
        if not import_lines:
            return []
        groups, current = [], [import_lines[0]]
        for ln in import_lines[1:]:
            if ln <= current[-1] + 2:
                current.append(ln)
            else:
                groups.append(current)
                current = [ln]
        groups.append(current)

        result = []
        for group in groups:
            start, end = group[0], group[-1]
            result.append(ParsedSymbol(
                symbol_type = "import",
                name        = "imports",
                start_line  = start,
                end_line    = end,
                source      = "\n".join(raw_lines[start - 1:end]),
                parent_name = None,
            ))
        return result

    def _types_and_funcs(self, source: str, raw_lines: List[str]) -> List[ParsedSymbol]:
        symbols: List[ParsedSymbol] = []
        current_type: Optional[str] = None   # track enclosing type name

        for i, line in enumerate(raw_lines):
            lineno      = i + 1
            stripped    = line.strip()

            # Type declarations
            m = _TYPE_RE.match(stripped)
            if m:
                name   = m.group(1)
                bases  = [b.strip() for b in (m.group(2) or "").split(",") if b.strip()]
                end    = self._find_block_end(raw_lines, lineno)
                symbol = ParsedSymbol(
                    symbol_type = "class_head",
                    name        = name,
                    start_line  = lineno,
                    end_line    = min(lineno + 4, end),
                    source      = "\n".join(raw_lines[lineno - 1:min(lineno + 4, end)]),
                    parent_name = None,
                    bases       = bases,
                )
                symbols.append(symbol)
                current_type = name
                continue

            # Function declarations
            m = _FUNC_RE.match(stripped)
            if m:
                name = m.group(1)
                end  = self._find_block_end(raw_lines, lineno)
                symbols.append(ParsedSymbol(
                    symbol_type = "method" if current_type else "function",
                    name        = name,
                    start_line  = lineno,
                    end_line    = end,
                    source      = "\n".join(raw_lines[lineno - 1:end]),
                    parent_name = current_type,
                ))

        return symbols

    @staticmethod
    def _find_block_end(raw_lines: List[str], start_line: int) -> int:
        """Brace-depth scan to find the closing } of a Swift block."""
        depth = 0
        for i, line in enumerate(raw_lines[start_line - 1:], start=start_line):
            depth += line.count("{") - line.count("}")
            if depth <= 0 and i > start_line:
                return i
        return len(raw_lines)
