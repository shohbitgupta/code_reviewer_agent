"""
Dart / Flutter language parser — regex heuristics.

Extracts:
  - import / export / part statements         → grouped import symbol
  - class / abstract class / sealed class /
    base class / final class / interface class
    / mixin / extension / enum declarations   → class_head symbol
  - instance methods, static methods          → method symbol
  - top-level functions                        → function symbol
  - factory / named / const constructors      → method symbol
  - getters and setters                        → method symbol
  - async* / sync* generator functions        → function / method symbol

Flutter-specific constructs handled:
  - Widget subclasses (StatelessWidget, StatefulWidget, State<T>,
    ConsumerWidget, HookWidget, ChangeNotifier, Cubit, Bloc<E,S>, etc.)
  - build(BuildContext context) → Widget  (core Flutter method)
  - Lifecycle methods: initState, dispose, didChangeDependencies,
    didUpdateWidget, deactivate, reassemble
  - @override and other annotations captured as decorators
  - Dart 3 class modifiers: sealed, base, final, interface
  - Dart enums (simple and enhanced with methods)
  - Extension-on clauses: extension on List<String> { ... }
  - Generic type bounds in base classes: Bloc<MyEvent, MyState>

Upgrade path: replace regex with tree-sitter-dart when stable bindings
are available. The ParsedSymbol contract is unchanged.
"""

import logging
import re
from typing import List, Optional

from core.models import ParsedSymbol
from stage1_ingestion.parsers.base import BaseParser

logger = logging.getLogger(__name__)

# ── Regex patterns ────────────────────────────────────────────────────────────

# import / export / part 'package:...'
_IMPORT_RE = re.compile(r"^(?:import|export|part)\s+['\"]")

# Class / mixin / extension / enum — including Dart 3 modifiers.
# Group 1: type name (optional for anonymous extensions)
# Group 2: base classes / interfaces / mixins / on-target
_TYPE_RE = re.compile(
    r"^(?:(?:sealed|base|final|interface|abstract)\s+)*"  # Dart 3 + abstract
    r"(?:class|mixin|extension type|extension|enum)\s+"
    r"(\w+)"                           # type name
    r"(?:<[^{]*?>)?"                   # optional generic params (non-greedy)
    r"(?:\s+(?:extends|implements|with|on)\s+"
    r"([\w<>\s,?.]+?))??"              # optional base/interface clause
    r"\s*[{(]"                         # opening brace or paren
)

# Annotation lines: @override, @protected, @deprecated, @visibleForTesting …
_ANNOTATION_RE = re.compile(r"^@(\w+)")

# factory / named / const constructors:
#   factory ClassName.method(  |  ClassName.method(  |  const ClassName(
_CONSTRUCTOR_RE = re.compile(
    r"^(?:const\s+)?(?:factory\s+)?(\w+)(?:\.(\w+))?\s*\("
)

# Getter:  ReturnType get name => ...  or  ReturnType get name {
_GETTER_RE = re.compile(
    r"^(?:static\s+)?(?:[\w<>?,\s]+?)\s+get\s+(\w+)\s*(?:=>|\{|;)"
)

# Setter:  set name(Type value)
_SETTER_RE = re.compile(
    r"^(?:static\s+)?set\s+(\w+)\s*\("
)

# Regular method / function.
# Handles:
#   - Any return type including Widget, Future<Widget>, List<Widget>, Stream<T>
#   - async / async* / sync* keywords after closing paren (not here, end of sig)
#   - static, @annotations handled separately
_FUNC_RE = re.compile(
    r"^(?:static\s+)?"
    r"(?:Future(?:<[^>]*>)?|Stream(?:<[^>]*>)?|Iterable(?:<[^>]*>)?"
    r"|void|bool|int|double|num|String|dynamic|never|Object"
    r"|Widget(?:[?])?|List<[^>]+>|Map<[^,>]+,[^>]+>|Set<[^>]+>"
    r"|[\w<>?]+(?:\s*<[^>]+>)?)"   # catch-all for custom types e.g. MyType<T>
    r"\s+"
    r"(\w+)"                        # method / function name
    r"\s*(?:<[^>]*>)?\s*\("        # optional generic params + opening paren
)

# Control-flow keywords that look like functions but aren't
_CONTROL_FLOW = frozenset({
    "if", "for", "while", "switch", "return", "assert",
    "throw", "await", "yield", "print", "debugPrint",
})

# Regex for extracting all call sites from Dart source.
# Matches: regular calls `name(`, cascade calls `..name(`, null-safe `?.name(`
_CALL_EXTRACT_RE = re.compile(r'(?:(?:\.\.|(?:\?\.))\s*)?(\b\w+)\s*\(')

# Names to skip when building the calls list (builtins, lifecycle, Dart core types)
_DART_CALL_SKIP = frozenset({
    "if", "for", "while", "switch", "return", "assert", "throw", "await",
    "yield", "print", "debugPrint", "super", "this", "setState",
    "String", "int", "double", "bool", "num",
    "List", "Map", "Set", "Iterable", "Future", "Stream",
    "Duration", "DateTime", "Color", "Size", "Offset",
    "BuildContext", "Widget", "State", "Key",
})

# Flutter Widget base classes — used to tag the class_head as a widget
FLUTTER_WIDGET_BASES = frozenset({
    "StatelessWidget", "StatefulWidget", "State",
    "InheritedWidget", "InheritedModel", "InheritedNotifier",
    "RenderBox", "RenderObject", "RenderSliver",
    "ConsumerWidget", "ConsumerStatefulWidget", "ConsumerState",  # Riverpod
    "HookWidget", "HookConsumerWidget",                           # flutter_hooks
    "ChangeNotifier", "ValueNotifier",                            # state mgmt
    "Cubit", "Bloc",                                              # BLoC
    "StateNotifier",                                              # Riverpod
    "GetxController", "GetController",                            # GetX
    "ViewModel",                                                  # MVVM
})

# Flutter lifecycle method names — called out explicitly for the reviewer
FLUTTER_LIFECYCLE = frozenset({
    "build", "initState", "dispose",
    "didChangeDependencies", "didUpdateWidget",
    "deactivate", "reassemble", "activate",
    "createState",
})


class DartParser(BaseParser):
    """
    Regex-based Dart / Flutter parser.

    Recognises all standard Dart constructs plus Flutter-specific patterns
    (widget subclasses, lifecycle methods, BLoC/Cubit, Riverpod, etc.).
    """

    @property
    def language(self) -> str:
        return "dart"

    def parse(self, source: str, raw_lines: List[str]) -> List[ParsedSymbol]:
        symbols: List[ParsedSymbol] = []
        symbols.extend(self._imports(raw_lines))
        symbols.extend(self._declarations(raw_lines))
        return symbols

    # ── Import grouping ───────────────────────────────────────────────────────

    def _imports(self, raw_lines: List[str]) -> List[ParsedSymbol]:
        """Group contiguous import/export/part lines into one import symbol."""
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
            # Extract package paths for dependency analysis
            paths = []
            for ln in group:
                m = re.search(r"['\"]([^'\"]+)['\"]", raw_lines[ln - 1])
                if m:
                    paths.append(m.group(1))
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

    # ── Type and function declarations ────────────────────────────────────────

    def _declarations(self, raw_lines: List[str]) -> List[ParsedSymbol]:
        """
        Single-pass scan that tracks:
          - Annotation lines accumulator (@override, @protected …)
          - Current enclosing type name (for method parent_name)
          - Current enclosing type bases (for Flutter widget detection)

        Emits symbols for classes, methods, constructors, getters, setters,
        and top-level functions.
        """
        symbols:       List[ParsedSymbol] = []
        current_type:  Optional[str]      = None
        current_bases: List[str]          = []
        annotations:   List[str]          = []  # accumulated since last symbol

        for i, line in enumerate(raw_lines):
            lineno   = i + 1
            stripped = line.strip()

            if not stripped or stripped.startswith("//"):
                # Blank / comment line — keep accumulated annotations
                continue

            # ── Collect annotations ───────────────────────────────────────────
            ann_m = _ANNOTATION_RE.match(stripped)
            if ann_m:
                annotations.append(f"@{ann_m.group(1)}")
                continue

            # ── Type declarations (class / mixin / extension / enum) ──────────
            type_m = _TYPE_RE.match(stripped)
            if type_m:
                name   = type_m.group(1)
                bases_raw = type_m.group(2) or ""
                bases  = self._parse_bases(bases_raw)

                is_widget = bool(FLUTTER_WIDGET_BASES & set(bases))
                end = self._find_block_end(raw_lines, lineno)

                # Class head = signature up to opening brace + ~4 lines
                head_end = min(lineno + 4, end)
                symbol = ParsedSymbol(
                    symbol_type = "class_head",
                    name        = name,
                    start_line  = lineno,
                    end_line    = head_end,
                    source      = "\n".join(raw_lines[lineno - 1:head_end]),
                    parent_name = None,
                    bases       = bases,
                    decorators  = annotations + (["@flutter_widget"] if is_widget else []),
                )
                symbols.append(symbol)
                current_type  = name
                current_bases = bases
                annotations   = []
                continue

            # ── Getter ────────────────────────────────────────────────────────
            get_m = _GETTER_RE.match(stripped)
            if get_m:
                name = get_m.group(1)
                end  = self._find_expr_end(raw_lines, lineno)
                sym_source = "\n".join(raw_lines[lineno - 1:end])
                symbols.append(ParsedSymbol(
                    symbol_type = "method" if current_type else "function",
                    name        = f"get {name}",
                    start_line  = lineno,
                    end_line    = end,
                    source      = sym_source,
                    parent_name = current_type,
                    decorators  = annotations,
                    calls       = self._extract_calls(sym_source),
                ))
                annotations = []
                continue

            # ── Setter ────────────────────────────────────────────────────────
            set_m = _SETTER_RE.match(stripped)
            if set_m:
                name = set_m.group(1)
                end  = self._find_block_end(raw_lines, lineno)
                sym_source = "\n".join(raw_lines[lineno - 1:end])
                symbols.append(ParsedSymbol(
                    symbol_type = "method" if current_type else "function",
                    name        = f"set {name}",
                    start_line  = lineno,
                    end_line    = end,
                    source      = sym_source,
                    parent_name = current_type,
                    decorators  = annotations,
                    calls       = self._extract_calls(sym_source),
                ))
                annotations = []
                continue

            # ── Regular method / function ─────────────────────────────────────
            func_m = _FUNC_RE.match(stripped)
            if func_m:
                name = func_m.group(1)
                if name in _CONTROL_FLOW:
                    annotations = []
                    continue
                end  = self._find_block_end(raw_lines, lineno)
                is_lifecycle = name in FLUTTER_LIFECYCLE
                sym_source = "\n".join(raw_lines[lineno - 1:end])
                symbols.append(ParsedSymbol(
                    symbol_type = "method" if current_type else "function",
                    name        = name,
                    start_line  = lineno,
                    end_line    = end,
                    source      = sym_source,
                    parent_name = current_type,
                    decorators  = annotations + (["@flutter_lifecycle"] if is_lifecycle else []),
                    calls       = self._extract_calls(sym_source),
                ))
                annotations = []
                continue

            # ── Constructor (named / factory / const) ─────────────────────────
            # Only attempt if we're inside a class and the line looks like
            # `ClassName(` or `ClassName.named(` or `factory ClassName(`
            if current_type:
                ctor_m = _CONSTRUCTOR_RE.match(stripped)
                if ctor_m and ctor_m.group(1) == current_type:
                    ctor_suffix = ctor_m.group(2)
                    name = (
                        f"{current_type}.{ctor_suffix}" if ctor_suffix
                        else current_type
                    )
                    is_factory = stripped.startswith("factory")
                    end = self._find_block_end(raw_lines, lineno)
                    sym_source = "\n".join(raw_lines[lineno - 1:end])
                    symbols.append(ParsedSymbol(
                        symbol_type = "method",
                        name        = name,
                        start_line  = lineno,
                        end_line    = end,
                        source      = sym_source,
                        parent_name = current_type,
                        decorators  = annotations + (["@factory"] if is_factory else []),
                        calls       = self._extract_calls(sym_source),
                    ))
                    annotations = []
                    continue

            # Not a recognised declaration — discard accumulated annotations
            annotations = []

        return symbols

    # ── Call extraction ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_calls(source: str) -> List[str]:
        """Return deduplicated call names found in *source*.

        Captures regular calls `foo(`, cascade calls `..doSomething(`, and
        null-safe calls `?.method(`.  Skips Dart builtins and control-flow.
        """
        seen:   set  = set()
        result: list = []
        for m in _CALL_EXTRACT_RE.finditer(source):
            name = m.group(1)
            if name and name not in _DART_CALL_SKIP and name not in seen:
                seen.add(name)
                result.append(name)
        return result

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_bases(raw: str) -> List[str]:
        """
        Extract individual base-class / interface names from a raw clause string.

        Handles generic types like Bloc<MyEvent, MyState> — strips generic
        params so symbol resolution can match plain class names.

        Examples:
            "StatefulWidget"                     → ["StatefulWidget"]
            "Bloc<MyEvent, MyState>"             → ["Bloc"]
            "ChangeNotifier, MyMixin"            → ["ChangeNotifier", "MyMixin"]
        """
        # Strip generic params for cleaner matching
        clean = re.sub(r"<[^>]*>", "", raw)
        return [b.strip() for b in re.split(r"[,\s]+(?:extends|implements|with|on)?\s*|,", clean) if b.strip()]

    @staticmethod
    def _find_block_end(raw_lines: List[str], start_line: int) -> int:
        """
        Brace-depth scan to find the closing `}` of a Dart block.
        Falls back to end-of-file if braces are unbalanced.
        """
        depth = 0
        for i, line in enumerate(raw_lines[start_line - 1:], start=start_line):
            depth += line.count("{") - line.count("}")
            if depth <= 0 and i > start_line:
                return i
        return len(raw_lines)

    @staticmethod
    def _find_expr_end(raw_lines: List[str], start_line: int) -> int:
        """
        Find the end of an arrow-expression getter (`=> expr;`).
        Returns the line containing the semicolon, or the next block end.
        """
        for i, line in enumerate(raw_lines[start_line - 1:], start=start_line):
            if ";" in line:
                return i
            if "{" in line:
                # Block-style getter — delegate to brace scanner
                return DartParser._find_block_end(raw_lines, i)
        return start_line
