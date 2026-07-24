"""
Rust language parser — regex heuristics.

Extracts:
  - use statements   → grouped import symbol
  - struct / enum / trait / impl declarations → class_head symbol
  - fn declarations  → function or method symbol

Upgrade path: replace regex with tree-sitter-rust when available.
"""

import logging
import re
from typing import List, Optional

from core.models import ParsedSymbol
from stage1_ingestion.parsers.base import BaseParser

logger = logging.getLogger(__name__)

_USE_RE = re.compile(r"^use\s+[\w::{},\s*]+;")

_TYPE_RE = re.compile(
    r"^(?:pub(?:\([\w]+\))?\s+)?"
    r"(?:struct|enum|trait|union)\s+(\w+)"
    r"(?:<[^>]*>)?"
    r"(?:\s*:\s*([\w+\s,<>]+))?"
)

_IMPL_RE = re.compile(
    r"^(?:pub(?:\([\w]+\))?\s+)?impl(?:<[^>]*>)?\s+"
    r"(?:([\w:]+)\s+for\s+)?([\w:]+)"
)

_FN_RE = re.compile(
    r"^(?:pub(?:\([\w]+\))?\s+)?(?:async\s+)?(?:unsafe\s+)?"
    r"(?:extern\s+\"[^\"]+\"\s+)?fn\s+(\w+)"
)


class RustParser(BaseParser):
    """Regex-based Rust parser for functions, structs, traits, and use statements."""

    @property
    def language(self) -> str:
        return "rust"

    def parse(self, source: str, raw_lines: List[str]) -> List[ParsedSymbol]:
        symbols: List[ParsedSymbol] = []
        symbols.extend(self._uses(raw_lines))
        symbols.extend(self._items(raw_lines))
        return symbols

    def _uses(self, raw_lines: List[str]) -> List[ParsedSymbol]:
        use_lines = [
            i + 1 for i, line in enumerate(raw_lines)
            if _USE_RE.match(line.strip())
        ]
        if not use_lines:
            return []
        groups, current = [], [use_lines[0]]
        for ln in use_lines[1:]:
            if ln <= current[-1] + 2:
                current.append(ln)
            else:
                groups.append(current)
                current = [ln]
        groups.append(current)

        result = []
        for group in groups:
            start, end = group[0], group[-1]
            paths = [
                raw_lines[ln - 1].strip().removeprefix("use ").rstrip(";")
                for ln in group
            ]
            result.append(ParsedSymbol(
                symbol_type = "import",
                name        = "imports",
                start_line  = start,
                end_line    = end,
                source      = "\n".join(raw_lines[start - 1:end]),
                parent_name = None,
                imports     = paths,
            ))
        return result

    def _items(self, raw_lines: List[str]) -> List[ParsedSymbol]:
        symbols: List[ParsedSymbol] = []
        current_type: Optional[str] = None

        for i, line in enumerate(raw_lines):
            lineno   = i + 1
            stripped = line.strip()

            # struct / enum / trait
            m = _TYPE_RE.match(stripped)
            if m:
                name  = m.group(1)
                bases = [b.strip() for b in (m.group(2) or "").split("+") if b.strip()]
                end   = self._find_block_end(raw_lines, lineno)
                symbols.append(ParsedSymbol(
                    symbol_type = "class_head",
                    name        = name,
                    start_line  = lineno,
                    end_line    = min(lineno + 4, end),
                    source      = "\n".join(raw_lines[lineno - 1:min(lineno + 4, end)]),
                    parent_name = None,
                    bases       = bases,
                ))
                current_type = name
                continue

            # impl blocks
            m = _IMPL_RE.match(stripped)
            if m:
                current_type = m.group(2) or m.group(1) or current_type
                continue

            # fn declarations
            m = _FN_RE.match(stripped)
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
        depth = 0
        for i, line in enumerate(raw_lines[start_line - 1:], start=start_line):
            depth += line.count("{") - line.count("}")
            if depth <= 0 and i > start_line:
                return i
        return len(raw_lines)
