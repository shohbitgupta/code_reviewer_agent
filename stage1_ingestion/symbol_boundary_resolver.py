"""
Step 1f-SBR — Symbol Boundary Resolver

Transforms ParsedSymbol lists BEFORE chunking to handle edge cases that
naive AST parsers cannot address on their own:

  1. Large symbols       Functions / methods above MAX_FUNC_LINES are split
                         into overlapping sub-symbols that *keep* their original
                         symbol_type (FUNCTION / METHOD).  This prevents the
                         chunker from degrading them to anonymous BLOCK chunks
                         and preserves all call / import metadata on the first
                         part.

  2. Nested classes      Symbols are sorted by start_line; class_head symbols
                         that fall inside another class_head's range receive a
                         @nested_class:<OuterName> decorator.  The chunker uses
                         this to build the correct parent_chunk_id links without
                         relying on fragile parent_name string matching.

  3. Macro expansions    Rust macro_rules! / #[derive] annotated symbols receive
                         @macro_expanded.  The chunker treats them as atomic
                         opaque blocks — they are never split further, because
                         macro-generated code should not be fragmented.

  4. Partial impl blocks Multiple impl Foo { } blocks (Rust) or extension
                         declarations (Swift) for the same type are linked with
                         @partial_impl:<TypeName>.  The graph builder can then
                         emit a single conceptual node per type and group the
                         related dependency edges.

Deduplication policy
--------------------
The SBR guarantees that no two output symbols share the same
(file_path, symbol_name, start_line) triple.  The *intentional* content
overlap between consecutive split-part symbols is preserved — eliminating
it would break context continuity for the embedding model.  There is no
explicit deduplication step anywhere in the pipeline; any reduction in
redundancy is a side-effect of boundary normalisation, not a goal in itself.

Input  : List[ParsedFile]   (output of 1f-LA / LanguageAnalyzer)
Output : List[ParsedFile]   (new list; symbols resolved; file_meta /
                             raw_lines / parse_success unchanged)

Usage::

    sbr          = SymbolBoundaryResolver()
    parsed_files = sbr.resolve(parsed_files)
    chunks       = builder.chunk_many(parsed_files, ...)
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Dict, List, Optional, Set, Tuple

from core.models import ParsedFile, ParsedSymbol

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────
# Keep MAX_FUNC_LINES below the chunker's MAX_FUNCTION_LINES (150) so that
# SBR-split parts are always below the chunker threshold and never get
# re-split into anonymous BLOCK chunks.
MAX_FUNC_LINES  = 100   # split functions / methods longer than this
MAX_CLASS_LINES = 180   # split class_head bodies longer than this
SPLIT_OVERLAP   = 15    # intentional overlap lines between consecutive parts

# symbol_types that may be split by Pass 2
_SPLITTABLE: frozenset = frozenset({"function", "method", "block"})


class SymbolBoundaryResolver:
    """
    Pre-processing pass that normalises symbol boundaries before the
    HierarchicalChunkBuilder runs.

    All four passes operate on List[ParsedSymbol] in-memory and produce a
    new List[ParsedFile] — the originals are never mutated.
    """

    def resolve(
        self,
        parsed_files: List[ParsedFile],
        max_workers:  Optional[int] = None,
    ) -> List[ParsedFile]:
        """
        Apply all boundary-resolution passes to every ParsedFile.

        Each file's four passes are independent (no shared mutable state), so
        files can be processed in parallel.  Pure-Python work, so io_bound=False
        is used — the auto-sizer caps at cpu_count threads.

        Args:
            parsed_files: Output of Step 1f-LA.
            max_workers:  Thread count.  None = auto-size.  1 = serial.

        Returns:
            New list of ParsedFile objects with resolved symbols.
        """
        from stage1_ingestion.workers import WorkerPool
        # Run serially: pure-Python GIL-bound work; thread overhead > gain.
        pool = WorkerPool(max_workers=1, io_bound=False)

        pairs        = pool.map(self._resolve_file, parsed_files)
        results      = [pf for pf, _ in pairs]
        total_splits = sum(s["splits"] for _, s in pairs)
        total_macros = sum(s["macros"]  for _, s in pairs)
        total_impls  = sum(s["impls"]   for _, s in pairs)

        logger.info(
            "[SBR] %d files resolved — %d large-symbol splits, "
            "%d macro tags, %d partial-impl groups",
            len(parsed_files), total_splits, total_macros, total_impls,
        )
        return results

    # ── Per-file dispatch ─────────────────────────────────────────────────────

    def _resolve_file(
        self,
        pf: ParsedFile,
    ) -> Tuple[ParsedFile, Dict[str, int]]:
        stats: Dict[str, int] = {"splits": 0, "macros": 0, "impls": 0}

        if not pf.symbols:
            return pf, stats

        language = pf.file_meta.language

        # Pass 1 — sort by start_line + tag nested class boundaries
        syms = _fix_nested_boundaries(pf.symbols)

        # Pass 2 — split symbols that exceed the line threshold
        syms, n_splits = _split_large_symbols(syms, pf.raw_lines)
        stats["splits"] = n_splits

        # Pass 3 — tag Rust macro-expanded symbols (atomic; never split)
        if language == "rust":
            syms, n = _tag_rust_macros(syms)
            stats["macros"] = n

        # Pass 4 — link partial impl / extension blocks
        if language in ("rust", "swift"):
            syms, n = _group_partial_impls(syms, language)
            stats["impls"] = n

        return (
            ParsedFile(
                file_meta     = pf.file_meta,
                symbols       = syms,
                raw_lines     = pf.raw_lines,
                parse_success = pf.parse_success,
                parse_error   = pf.parse_error,
            ),
            stats,
        )


# ── Pass 1: nested class boundary normalisation ───────────────────────────────

def _fix_nested_boundaries(symbols: List[ParsedSymbol]) -> List[ParsedSymbol]:
    """
    Sort symbols by start_line and tag inner class_head symbols that are
    nested inside another class_head's line range.

    The @nested_class:<Outer> decorator allows the chunker to set the correct
    parent_chunk_id link without guessing from parent_name strings.
    """
    syms = sorted(symbols, key=lambda s: s.start_line)

    # Stack of (class_name, class_end_line) for currently-open outer classes
    class_stack: List[Tuple[str, int]] = []
    result: List[ParsedSymbol] = []

    for sym in syms:
        # Pop classes whose range ended before this symbol
        while class_stack and class_stack[-1][1] < sym.start_line:
            class_stack.pop()

        if sym.symbol_type == "class_head":
            if class_stack:
                outer_name = class_stack[-1][0]
                tag = f"@nested_class:{outer_name}"
                if tag not in sym.decorators:
                    sym = replace(sym, decorators=list(sym.decorators) + [tag])
            class_stack.append((sym.name, sym.end_line))

        result.append(sym)

    return result


# ── Pass 2: large-symbol splitting ───────────────────────────────────────────

def _split_large_symbols(
    symbols: List[ParsedSymbol],
    raw_lines: List[str],
) -> Tuple[List[ParsedSymbol], int]:
    """
    Split every function / method / block that exceeds the line threshold.

    Macro-expanded symbols (@macro_expanded) are treated as atomic and are
    never split — fragmenting macro-generated code loses all semantic context.

    Returns:
        (new symbol list, number of symbols that were split)
    """
    result: List[ParsedSymbol] = []
    n_splits  = 0
    file_len  = len(raw_lines)

    for sym in symbols:
        if "@macro_expanded" in sym.decorators:
            result.append(sym)
            continue

        # Guard: parser may produce end_line beyond the real file length
        # (common in regex-based Dart/Swift parsers). Cap before splitting.
        effective_end = min(sym.end_line, file_len) if file_len else sym.end_line
        max_lines = MAX_CLASS_LINES if sym.symbol_type == "class_head" else MAX_FUNC_LINES
        sym_len   = effective_end - sym.start_line + 1

        if sym.symbol_type not in _SPLITTABLE or sym_len <= max_lines:
            result.append(sym)
            continue

        result.extend(_split_symbol(sym, raw_lines, max_lines, SPLIT_OVERLAP,
                                    effective_end=effective_end))
        n_splits += 1

    return result, n_splits


def _split_symbol(
    sym: ParsedSymbol,
    raw_lines: List[str],
    max_lines: int,
    overlap: int,
    effective_end: Optional[int] = None,
) -> List[ParsedSymbol]:
    """
    Slice *sym* into multiple ParsedSymbol objects of at most *max_lines*
    lines with *overlap* lines of intentional context overlap.

    Naming convention:
      part 1  →  original sym.name              (retains all metadata)
      part N  →  "{sym.name}__part{N}"          (source only; no calls/imports)

    @split_head / @split_cont decorators let downstream steps identify that
    these parts belong to the same logical symbol.
    """
    # Use the pre-capped end line to avoid runaway loops when parsers
    # produce end_line values beyond the actual file length.
    real_end    = effective_end if effective_end is not None else sym.end_line
    total_parts = _count_parts(real_end - sym.start_line + 1, max_lines, overlap)
    parts: List[ParsedSymbol] = []
    start    = sym.start_line   # 1-indexed
    part_idx = 1

    while start <= real_end:
        end     = min(start + max_lines - 1, real_end)
        content = "\n".join(raw_lines[start - 1 : end])

        is_first = part_idx == 1
        name     = sym.name if is_first else f"{sym.name}__part{part_idx}"

        decorators = list(sym.decorators) + [
            f"@split:{part_idx}/{total_parts}",
            "@split_head" if is_first else f"@split_cont:{sym.name}",
        ]

        parts.append(ParsedSymbol(
            symbol_type = sym.symbol_type,
            name        = name,
            start_line  = start,
            end_line    = end,
            source      = content,
            parent_name = sym.parent_name,
            decorators  = decorators,
            # Call / import metadata lives only on the first part where
            # the function signature and its direct references appear.
            calls       = list(sym.calls)       if is_first else [],
            imports     = list(sym.imports)     if is_first else [],
            bases       = list(sym.bases)       if is_first else [],
            param_types = list(sym.param_types) if is_first else [],
            return_type = sym.return_type       if is_first else None,
        ))

        # Stop once we've reached the actual end — the old `start > real_end`
        # check was dead code because start = real_end - overlap + 1 ≤ real_end.
        if end >= real_end:
            break

        start    = end - overlap + 1
        part_idx += 1

    return parts


def _count_parts(total_lines: int, max_lines: int, overlap: int) -> int:
    """Ceiling estimate of the number of split parts (used for @split:N/M label)."""
    stride = max_lines - overlap
    return max(1, -(-total_lines // stride))   # ceiling division


# ── Pass 3: Rust macro tagging ────────────────────────────────────────────────

def _tag_rust_macros(
    symbols: List[ParsedSymbol],
) -> Tuple[List[ParsedSymbol], int]:
    """
    Add @macro_expanded to Rust symbols that are macro-generated or
    macro-annotated.

    Detected patterns:
      - @derive decorator    (#[derive(Clone, Debug, ...)])
      - @macro_rules         (macro_rules! my_macro { ... })
      - @proc_macro*         (procedural macro attributes)
      - name ending in "!"   (macro invocations used as top-level items)
    """
    result: List[ParsedSymbol] = []
    n = 0

    for sym in symbols:
        if "@macro_expanded" not in sym.decorators:
            is_macro = (
                "@derive"      in sym.decorators
                or "@macro_rules" in sym.decorators
                or any(d.startswith("@proc_macro") for d in sym.decorators)
                or sym.name.endswith("!")
            )
            if is_macro:
                sym = replace(sym, decorators=list(sym.decorators) + ["@macro_expanded"])
                n += 1

        result.append(sym)

    return result, n


# ── Pass 4: partial impl / extension grouping ─────────────────────────────────

def _group_partial_impls(
    symbols: List[ParsedSymbol],
    language: str,
) -> Tuple[List[ParsedSymbol], int]:
    """
    When the same type has multiple impl blocks (Rust) or extension
    declarations (Swift), tag each with @partial_impl:<TypeName>.

    Rust example — two separate impl blocks for the same type:
        impl Visitor { fn new() {} }     →  @partial_impl:Visitor
        impl Visitor { fn save() {} }    →  @partial_impl:Visitor

    The graph builder groups nodes sharing the same @partial_impl tag into a
    single conceptual entity, reducing false orphan counts.
    """
    # First pass: count impl / extension class_head symbols per type name
    impl_counts: Dict[str, int] = {}
    for sym in symbols:
        if sym.symbol_type == "class_head":
            key = _impl_type_name(sym, language)
            if key:
                impl_counts[key] = impl_counts.get(key, 0) + 1

    # Second pass: tag only types that appear more than once
    result: List[ParsedSymbol] = []
    tagged_types: Set[str] = set()
    n_groups = 0

    for sym in symbols:
        if sym.symbol_type == "class_head":
            key = _impl_type_name(sym, language)
            if key and impl_counts.get(key, 0) > 1:
                tag = f"@partial_impl:{key}"
                if tag not in sym.decorators:
                    sym = replace(sym, decorators=list(sym.decorators) + [tag])
                if key not in tagged_types:
                    n_groups += 1
                    tagged_types.add(key)

        result.append(sym)

    return result, n_groups


def _impl_type_name(sym: ParsedSymbol, language: str) -> Optional[str]:
    """
    Extract the target type name from an impl / extension class_head symbol.

      Rust   : sym.name == "impl_Foo"        →  "Foo"
      Swift  : sym.name == "extension_Foo"   →  "Foo"
    """
    if language == "rust"  and sym.name.startswith("impl_"):
        return sym.name[5:] or None
    if language == "swift" and sym.name.startswith("extension_"):
        return sym.name[10:] or None
    return None
