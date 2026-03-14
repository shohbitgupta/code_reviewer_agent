"""
Rust language-specific enrichment pass.

Detects trait implementation mappings, error propagation patterns, and
public API surface, then tags SymbolEntry objects with additional
decorator metadata.
"""
from __future__ import annotations

import logging
import re
from typing import List

from ingestion.analyzers.base import AnalysisResult, LanguageAnalyzer
from ingestion.models import ParsedFile
from ingestion.symbol_table import ProjectSymbolTable, SymbolEntry

logger = logging.getLogger(__name__)

# Regex to extract trait and type from an "impl Trait for Type" symbol name.
# Matches: "impl Display for MyStruct", "impl<T> Iterator for Vec<T>"
_IMPL_TRAIT_RE = re.compile(
    r"^impl(?:<[^>]*>)?\s+(\w[\w:]*)\s+for\s+(\w[\w:]*)(?:<.*>)?$"
)

# Calls that indicate fallible code / error propagation
_ERROR_CALLS = frozenset({"unwrap", "expect", "unwrap_or", "unwrap_or_else",
                           "unwrap_or_default", "ok_or", "map_err"})


class RustAnalyzer(LanguageAnalyzer):
    """Enriches SymbolEntry decorators with Rust-specific patterns."""

    @property
    def language(self) -> str:
        return "rust"

    def enrich(
        self,
        parsed_files: List[ParsedFile],
        symbol_table: ProjectSymbolTable,
        result: AnalysisResult,
    ) -> None:
        """
        Scan Rust files and add decorator tags to relevant SymbolEntry
        objects in the symbol table.

        Patterns detected:
          1. Trait implementation — "impl Trait for Type" → @impl_{TraitName}
          2. Error propagation — methods using unwrap/expect → @error_propagation
          3. Public API — symbols with "pub" keyword → @public_api
        """
        if not parsed_files:
            return

        for pf in parsed_files:
            try:
                self._enrich_file(pf, symbol_table)
            except Exception:
                logger.exception(
                    "[RustAnalyzer] Unexpected error enriching %s",
                    pf.file_meta.file_path,
                )

    # ------------------------------------------------------------------
    # Per-file enrichment
    # ------------------------------------------------------------------

    def _enrich_file(
        self,
        pf: ParsedFile,
        symbol_table: ProjectSymbolTable,
    ) -> None:
        if not pf.symbols:
            return

        file_path = pf.file_meta.file_path

        for sym in pf.symbols:
            try:
                entry = symbol_table.lookup_by_qualified(
                    f"{file_path}::{sym.name}"
                )

                # ── Pattern 1: Trait implementation mapping ───────────────
                # Rust parsers emit "impl Display for MyStruct" as the symbol name
                if sym.name.startswith("impl "):
                    m = _IMPL_TRAIT_RE.match(sym.name)
                    if m:
                        trait_name = m.group(1).split("::")[-1]  # strip path prefix
                        type_name = m.group(2).split("::")[-1]

                        # Find the SymbolEntry for the type being implemented
                        type_candidates = symbol_table.lookup_by_name(
                            type_name, prefer_file=file_path
                        )
                        for type_entry in type_candidates:
                            if type_entry.symbol_type == "class_head":
                                _add_decorator(type_entry, f"@impl_{trait_name}")
                                logger.debug(
                                    "[RustAnalyzer] %s implements %s",
                                    type_name,
                                    trait_name,
                                )
                                break

                        # Also tag the impl block entry itself
                        if entry is not None:
                            _add_decorator(entry, f"@impl_{trait_name}")

                # ── Pattern 2: Error propagation ──────────────────────────
                if sym.symbol_type in ("method", "function"):
                    error_calls_found = [
                        c for c in sym.calls
                        if c.split("(")[0].split(".")[-1] in _ERROR_CALLS
                    ]
                    # Also check for ? operator usage in source text
                    has_question_mark = "?" in (sym.source or "")

                    if error_calls_found or has_question_mark:
                        if entry is not None:
                            _add_decorator(entry, "@error_propagation")
                        logger.debug(
                            "[RustAnalyzer] error propagation in %s "
                            "(calls=%s, ?=%s)",
                            sym.name,
                            error_calls_found,
                            has_question_mark,
                        )

                # ── Pattern 3: Public API surface ─────────────────────────
                source = sym.source or ""
                # pub fn, pub struct, pub trait, pub const, pub type, pub enum
                if re.search(r"\bpub\b", source):
                    if entry is not None:
                        _add_decorator(entry, "@public_api")
                    logger.debug(
                        "[RustAnalyzer] public API symbol: %s in %s",
                        sym.name,
                        file_path,
                    )

            except Exception:
                logger.exception(
                    "[RustAnalyzer] Error enriching symbol %s in %s",
                    sym.name,
                    file_path,
                )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _add_decorator(entry: SymbolEntry, tag: str) -> None:
    """Append an enrichment *tag* to the SymbolEntry (idempotent)."""
    if not hasattr(entry, "_extra_tags"):
        entry._extra_tags: list = []  # type: ignore[attr-defined]
    extra: list = entry._extra_tags  # type: ignore[attr-defined]
    if tag not in extra:
        extra.append(tag)
