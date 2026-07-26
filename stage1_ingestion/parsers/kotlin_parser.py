"""
Kotlin language parser — regex heuristics.

Extracts:
  - package declaration    → stored as module-level metadata
  - import statements      → grouped import symbol
  - class / interface / object / enum class declarations → class_head symbol
  - fun declarations       → function (top-level) or method (inside type)
  - companion object       → class_head symbol with @companion decorator

Android / Compose extras:
  - Android lifecycle methods (onCreate, onResume, onDestroy, etc.)
    tagged @android_lifecycle
  - @Composable functions tagged @composable
  - ViewModel / Activity / Fragment base class detection →
    @android_component decorator on the class_head

Modifier keywords recognised:
  public private protected internal
  abstract open final sealed data inner inline value
  fun (suspend, inline, infix, operator, tailrec, external)
  override annotation enum

Upgrade path: replace regex with tree-sitter-kotlin when available.
"""

import logging
import re
from typing import Dict, List, Optional, Set, Tuple

from core.models import ParsedSymbol
from stage1_ingestion.parsers.base import BaseParser

logger = logging.getLogger(__name__)

# ── Android / Compose tagging ─────────────────────────────────────────────────

# Base classes that indicate an Android component
ANDROID_COMPONENT_BASES: Set[str] = {
    "Activity", "AppCompatActivity", "ComponentActivity", "FragmentActivity",
    "Fragment", "DialogFragment", "BottomSheetDialogFragment",
    "Service", "IntentService", "JobIntentService",
    "BroadcastReceiver", "ContentProvider",
    "ViewModel", "AndroidViewModel",
    "Application", "MultiDexApplication",
    "RecyclerView.Adapter", "ListAdapter", "PagingDataAdapter",
    "RecyclerView.ViewHolder",
    "Worker", "CoroutineWorker", "ListenableWorker",
}

# Android lifecycle method names
ANDROID_LIFECYCLE_METHODS: Set[str] = {
    "onCreate", "onStart", "onResume", "onPause",
    "onStop", "onDestroy", "onRestart",
    "onCreateView", "onViewCreated", "onDestroyView",
    "onAttach", "onDetach",
    "onActivityCreated", "onActivityResult",
    "onRequestPermissionsResult",
    "onSaveInstanceState", "onRestoreInstanceState",
    "onNewIntent", "onBackPressed",
    "onBind", "onUnbind", "onRebind",
    "onStartCommand", "onHandleIntent",
    "onReceive",
    "onCleared",                            # ViewModel
    "onWorkerMainThread",                   # Worker
}

# ── Regex patterns ─────────────────────────────────────────────────────────────

# Visibility / modifier prefixes (optional, any order subset)
_MODIFIERS = (
    r"(?:(?:public|private|protected|internal|abstract|open|final|sealed|"
    r"data|inner|inline|value|enum|annotation|external|actual|expect)\s+)*"
)

# package declaration (top of file)
_PACKAGE_RE = re.compile(r"^package\s+([\w.]+)")

# import lines
_IMPORT_RE = re.compile(r"^import\s+[\w.*]+")

# class / interface / object / companion object
_TYPE_RE = re.compile(
    _MODIFIERS +
    r"(class|interface|object|fun\s+interface)\s+(\w+)"
    r"(?:\s*<[^>]*>)?"                       # optional generics
    r"(?:\s*(?:constructor\s*)?\([^)]*\))?"  # optional primary constructor
    r"(?:\s*:\s*([\w<>\[\]()\s,.*?]+?))?(?=\s*[{\n]|$)",  # optional supertypes (allow parens for Kotlin ctor calls)
)

# companion object (no name, or "companion object Foo")
_COMPANION_RE = re.compile(r"companion\s+object(?:\s+(\w+))?")

# fun declaration — captures name; handles extension funs and generics
_FUN_RE = re.compile(
    _MODIFIERS +
    r"(?:suspend\s+)?(?:inline\s+)?(?:infix\s+)?(?:operator\s+)?"
    r"(?:tailrec\s+)?(?:override\s+)?"
    r"fun\s+"
    r"(?:<[^>]*>\s+)?"                       # optional type params before receiver
    r"(?:[\w<>\[\]?.]+\s*\.\s*)?(?:\([^)]*\)\s*\.\s*)?" # optional receiver
    r"(\w+)\s*[<(]"                          # function name
)

# val / var property declarations (class-level only, for getter/setter extraction)
_PROPERTY_RE = re.compile(
    _MODIFIERS +
    r"(?:override\s+)?(?:val|var)\s+(\w+)\s*(?::\s*[\w<>\[\]?,\s.]+?)?"
    r"\s*(?:=|by|get\(\)|set\()",
)

# annotation line (e.g. @Composable, @Override)
_ANNOTATION_RE = re.compile(r"^@(\w+)(?:\([^)]*\))?")


class KotlinParser(BaseParser):
    """
    Regex-based Kotlin parser for classes, objects, functions, and imports.

    Handles Android and Jetpack Compose annotations and lifecycle tagging.
    """

    @property
    def language(self) -> str:
        return "kotlin"

    def parse(self, source: str, raw_lines: List[str]) -> List[ParsedSymbol]:
        """
        Parse Kotlin source via regex heuristics into imports, types, and functions.

        Args:
            source:    Full file content as a single string.
            raw_lines: Source split by newline (1-indexed when used with [i-1]).

        Returns:
            List[ParsedSymbol] — never raises; unmatched lines are simply
            skipped by the regex patterns.
        """
        symbols: List[ParsedSymbol] = []
        symbols.extend(self._imports(raw_lines))
        symbols.extend(self._types_and_funcs(raw_lines))
        return symbols

    # ── Import grouping ───────────────────────────────────────────────────────

    def _imports(self, raw_lines: List[str]) -> List[ParsedSymbol]:
        """Group contiguous import lines into a single import symbol."""
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
            paths = [
                raw_lines[ln - 1].strip()[len("import "):].strip()
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

    # ── Types and functions ───────────────────────────────────────────────────

    def _types_and_funcs(self, raw_lines: List[str]) -> List[ParsedSymbol]:
        """
        Single-pass line scan emitting class_head, method, and property symbols.

        Maintains a stack of (type_name, closing_line) so nested types
        (companion objects, inner classes) resolve the correct enclosing
        parent_symbol, popping entries once their closing brace line has
        passed. Pending @Annotation lines are accumulated and attached as
        decorators to whichever declaration follows; a blank line clears the
        accumulator since annotations don't carry across unrelated
        declarations.

        Recognises, in priority order per line: companion objects, class/
        interface/object declarations (tagging @android_component when a
        base class is in ANDROID_COMPONENT_BASES), fun declarations (tagging
        @android_lifecycle / propagating @Composable), and class-level
        val/var properties that have an explicit get/set accessor block
        (plain `val x = 5` assignments are not emitted as symbols).

        Returns:
            List[ParsedSymbol] with symbol_type "class_head" or
            "method"/"function".
        """
        symbols: List[ParsedSymbol] = []

        # Stack of (type_name, closing_line) for nesting
        type_stack: List[Tuple[str, int]] = []

        # Pending annotations accumulated before a declaration
        pending_annotations: List[str] = []

        i = 0
        while i < len(raw_lines):
            lineno   = i + 1
            line     = raw_lines[i]
            stripped = line.strip()

            # Skip blank lines / single-line comments
            if not stripped or stripped.startswith("//"):
                if not stripped:
                    pending_annotations.clear()
                i += 1
                continue

            # Accumulate annotation lines
            ann_match = _ANNOTATION_RE.match(stripped)
            if ann_match:
                pending_annotations.append(f"@{ann_match.group(1)}")
                i += 1
                continue

            # Pop type_stack entries whose block has ended
            while type_stack and lineno > type_stack[-1][1]:
                type_stack.pop()

            current_type = type_stack[-1][0] if type_stack else None

            # ── companion object ──────────────────────────────────────────────
            comp_match = _COMPANION_RE.search(stripped)
            if comp_match and not stripped.lstrip().startswith("//"):
                name     = comp_match.group(1) or "Companion"
                end_line = self._find_block_end(raw_lines, lineno)
                decorators = list(pending_annotations) + ["@companion"]
                symbols.append(ParsedSymbol(
                    symbol_type = "class_head",
                    name        = name,
                    start_line  = lineno,
                    end_line    = min(lineno + 3, end_line),
                    source      = "\n".join(raw_lines[lineno - 1:min(lineno + 3, end_line)]),
                    parent_name = current_type,
                    decorators  = decorators,
                ))
                type_stack.append((name, end_line))
                pending_annotations = []
                i += 1
                continue

            # ── class / interface / object ────────────────────────────────────
            type_match = _TYPE_RE.match(stripped)
            if type_match:
                keyword   = type_match.group(1)  # class / interface / object / fun interface
                name      = type_match.group(2)
                bases_raw = type_match.group(3) or ""
                bases     = self._parse_bases(bases_raw)
                end_line  = self._find_block_end(raw_lines, lineno)

                decorators = list(pending_annotations)
                if any(b.split(".")[-1] in ANDROID_COMPONENT_BASES for b in bases):
                    decorators.append("@android_component")

                # enum class body ends at closing }; use class_head style
                head_end = min(lineno + 4, end_line)

                symbols.append(ParsedSymbol(
                    symbol_type = "class_head",
                    name        = name,
                    start_line  = lineno,
                    end_line    = head_end,
                    source      = "\n".join(raw_lines[lineno - 1:head_end]),
                    parent_name = current_type,
                    bases       = bases,
                    decorators  = decorators,
                ))
                type_stack.append((name, end_line))
                pending_annotations = []
                i += 1
                continue

            # ── fun declaration ───────────────────────────────────────────────
            fun_match = _FUN_RE.match(stripped)
            if fun_match:
                name     = fun_match.group(1)
                end_line = self._find_block_end(raw_lines, lineno)

                # Single-expression / abstract / interface funs have no body
                if end_line == lineno and "=" not in stripped and "{" not in stripped:
                    end_line = lineno

                decorators = list(pending_annotations)
                if name in ANDROID_LIFECYCLE_METHODS:
                    decorators.append("@android_lifecycle")
                if "@Composable" in decorators:
                    pass  # already captured

                symbols.append(ParsedSymbol(
                    symbol_type = "method" if current_type else "function",
                    name        = name,
                    start_line  = lineno,
                    end_line    = end_line,
                    source      = "\n".join(raw_lines[lineno - 1:end_line]),
                    parent_name = current_type,
                    decorators  = decorators,
                ))
                pending_annotations = []
                i += 1
                continue

            # ── class-level val / var property with accessor block ───────────
            # Only extract properties that have an explicit { ... } accessor
            # body (custom get/set).  Simple `val x = 5` assignments are skipped.
            if current_type and "{" in stripped:
                prop_match = _PROPERTY_RE.match(stripped)
                if prop_match:
                    name     = prop_match.group(1)
                    end_line = self._find_block_end(raw_lines, lineno)
                    decorators = list(pending_annotations)
                    symbols.append(ParsedSymbol(
                        symbol_type = "method",
                        name        = name,
                        start_line  = lineno,
                        end_line    = end_line,
                        source      = "\n".join(raw_lines[lineno - 1:end_line]),
                        parent_name = current_type,
                        decorators  = decorators,
                    ))
                    pending_annotations = []
                    i += 1
                    continue

            pending_annotations = []
            i += 1

        return symbols

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _find_block_end(raw_lines: List[str], start_line: int) -> int:
        """Brace-depth scan to find the closing } of a Kotlin block."""
        depth = 0
        for idx, line in enumerate(raw_lines[start_line - 1:], start=start_line):
            depth += line.count("{") - line.count("}")
            if depth <= 0 and idx > start_line:
                return idx
        return len(raw_lines)

    @staticmethod
    def _parse_bases(raw: str) -> List[str]:
        """
        Extract clean base class / interface names from the supertype list.

        Handles:
          'FlutterActivity()'     → ['FlutterActivity']
          'ViewModel(), Parcelable' → ['ViewModel', 'Parcelable']
          'List<String>'          → ['List']
        """
        if not raw.strip():
            return []
        # Strip constructor call args and generic arguments
        cleaned = re.sub(r"\([^)]*\)", "", raw)   # remove (...)
        cleaned = re.sub(r"<[^<>]*>", "", cleaned) # remove <...>
        parts = [p.strip() for p in cleaned.split(",") if p.strip()]
        # Take only the last dotted segment (e.g. 'io.foo.Bar' → 'Bar')
        return [p.split(".")[-1] for p in parts if p]
