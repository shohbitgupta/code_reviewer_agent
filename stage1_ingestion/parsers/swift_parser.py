"""
Swift language parser -- regex heuristics.

Extracts:
  - import statements       -> grouped import symbol
  - class/struct/enum/...   -> class_head symbol
  - func declarations       -> function or method symbol
  - call sites in bodies    -> ParsedSymbol.calls list (feeds IQ-03 call resolution)

Upgrade path: replace regex with tree-sitter-swift when available.
"""
import logging
import re
from typing import List, Optional, Set

from core.models import ParsedSymbol
from stage1_ingestion.parsers.base import BaseParser

logger = logging.getLogger(__name__)

# ── Lexical patterns ──────────────────────────────────────────────────────────

_IMPORT_RE = re.compile(r"^import\s+\w+")
_WHERE_RE   = re.compile(r"^\s*where\s+(.*)")

_TYPE_RE = re.compile(
    r"^(?:public\s+|private\s+|internal\s+|open\s+|fileprivate\s+)?"
    r"(?:final\s+)?(?:class|struct|enum|protocol|actor|extension)\s+(\w+)"
    r"(?:\s*:\s*([\w,\s]+))?"
)

# "class" doubles as both the type-declaration keyword AND a member modifier
# ("class func foo()", "class var bar: Int" — Swift's equivalent of `static`).
# _TYPE_RE can't tell these apart syntactically, so reject a match whose
# captured "name" is actually one of these continuation keywords — no real
# Swift type is ever named exactly "func"/"var"/"let".
_TYPE_RE_FALSE_POSITIVES: Set[str] = {"func", "var", "let"}

_FUNC_RE = re.compile(
    r"^(?:public\s+|private\s+|internal\s+|open\s+|fileprivate\s+)?"
    r"(?:static\s+|class\s+)?(?:override\s+)?(?:mutating\s+)?(?:async\s+)?"
    r"func\s+(\w+)\s*[<(]"
)

# ── Call-site extraction patterns ─────────────────────────────────────────────

# Swift keywords that syntactically look like calls but aren't
_KEYWORDS: Set[str] = {
    "if", "for", "while", "guard", "switch", "catch", "return",
    "throw", "try", "await", "defer", "repeat", "where", "print",
    "super", "self", "init",
}

# Any identifier immediately followed by "(" or "<" is a call site, regardless
# of what precedes it — a receiver-specific scheme (self./TypeName./bare) missed
# the majority of real Swift calls: lowercase-receiver dot-calls
# ("manager.cache.store(id)") and optional chaining ("session?.invalidate()"),
# since neither is "self.", an uppercase-led "TypeName.", nor unprefixed. `\b`
# already marks the boundary correctly whether preceded by ".", "?.", "!", or
# nothing, so one pattern covers all of them.
_CALL_EXTRACT_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*[<(]")


def _extract_calls(body_lines: List[str]) -> List[str]:
    """
    Scan the body of a function for call sites and return unique callee names.

    Matches any `name(` or `name<...>(` regardless of receiver — covers
    self.method(...), TypeName.method(...), instance.method(...),
    optional-chained instance?.method(...), and bare calls alike.
    Filters out Swift keywords and single-letter names.
    """
    seen: Set[str] = set()

    for line in body_lines:
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue  # skip comments

        for m in _CALL_EXTRACT_RE.finditer(line):
            name = m.group(1)
            if name not in _KEYWORDS and len(name) > 1:
                seen.add(name)

    return sorted(seen)


class SwiftParser(BaseParser):
    """Regex-based Swift parser for functions, classes, structs, and imports."""

    @property
    def language(self) -> str:
        return "swift"

    def parse(self, source: str, raw_lines: List[str]) -> List[ParsedSymbol]:
        """
        Parse Swift source via regex heuristics into imports, types, and functions.

        Args:
            source:    Full file content as a single string.
            raw_lines: Source split by newline (1-indexed when used with [i-1]).

        Returns:
            List[ParsedSymbol] — never raises; unmatched lines are simply
            skipped by the regex patterns.
        """
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
        """
        Single-pass line scan emitting class_head and function/method symbols.

        Tracks the innermost enclosing type name so functions are tagged as
        methods with the correct parent_symbol. For type declarations, also
        resolves base classes/protocols from the inline ': Base, Proto' clause
        and — since Swift permits generic constraints on a following line —
        scans up to 5 lines ahead for a multi-line `where` clause, folding any
        constrained protocol conformances into the bases list.

        Args:
            source:    Full file content as a single string.
            raw_lines: Source split by newline (1-indexed when used with [i-1]).

        Returns:
            List[ParsedSymbol] with symbol_type "class_head", "function", or
            "method".
        """
        symbols: List[ParsedSymbol] = []
        current_type: Optional[str] = None

        for i, line in enumerate(raw_lines):
            lineno   = i + 1
            stripped = line.strip()

            # ── Type declarations ─────────────────────────────────────────────
            m = _TYPE_RE.match(stripped)
            if m and m.group(1) in _TYPE_RE_FALSE_POSITIVES:
                m = None
            if m:
                name  = m.group(1)
                bases = [b.strip() for b in (m.group(2) or "").split(",") if b.strip()]
                end   = self._find_block_end(raw_lines, lineno)

                # Re-scan for `: BaseClass` when generic params hid group 2
                colon_pos = stripped.find(":")
                if colon_pos >= 0 and not bases:
                    inheritance_part = stripped[colon_pos + 1:].split("{")[0].strip()
                    where_split = inheritance_part.split(" where ")
                    raw_bases = [b.strip() for b in where_split[0].split(",") if b.strip()]
                    bases = [b.split("<")[0].strip() for b in raw_bases if b]
                    if len(where_split) > 1:
                        for constraint in where_split[1].split(","):
                            parts = constraint.split(":")
                            if len(parts) == 2:
                                bases.extend(
                                    p.strip().split("<")[0].strip()
                                    for p in parts[1].split("&") if p.strip()
                                )

                # Scan up to 5 lines ahead for a multi-line `where` clause
                for look in raw_lines[lineno:min(lineno + 5, end)]:
                    wm = _WHERE_RE.match(look)
                    if wm:
                        where_text  = wm.group(1).split("{")[0]
                        constraints = [c.strip() for c in where_text.split(",") if c.strip()]
                        for constraint in constraints:
                            parts = constraint.split(":")
                            if len(parts) == 2:
                                bases.extend(
                                    p.strip().split("<")[0].strip()
                                    for p in parts[1].split("&") if p.strip()
                                )
                        break
                    if "{" in look:
                        break

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

            # ── Function/method declarations ──────────────────────────────────
            m = _FUNC_RE.match(stripped)
            if m:
                name = m.group(1)
                end  = self._find_block_end(raw_lines, lineno)
                body = raw_lines[lineno:end]          # lines inside the func body
                calls = _extract_calls(body)

                symbols.append(ParsedSymbol(
                    symbol_type = "method" if current_type else "function",
                    name        = name,
                    start_line  = lineno,
                    end_line    = end,
                    source      = "\n".join(raw_lines[lineno - 1:end]),
                    parent_name = current_type,
                    calls       = calls,
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
