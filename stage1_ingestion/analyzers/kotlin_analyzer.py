"""
Kotlin/Android language-specific enrichment pass.

Detects Repository pattern usage from ViewModels, coroutine scope usage,
and Hilt/Dagger dependency injection components, then tags SymbolEntry
objects with additional decorator metadata.
"""
from __future__ import annotations

import logging
from typing import List

from stage1_ingestion.analyzers.base import AnalysisResult, LanguageAnalyzer
from core.models import ParsedFile
from stage1_ingestion.symbol_table import ProjectSymbolTable, SymbolEntry

logger = logging.getLogger(__name__)

# Coroutine scope builders
_COROUTINE_CALLS = frozenset({"launch", "async", "withContext", "runBlocking",
                               "coroutineScope", "supervisorScope"})

# DI annotation strings that parsers may surface in decorators or source
_DI_ANNOTATIONS = frozenset({"@HiltViewModel", "@Singleton", "@Inject",
                               "@HiltAndroidApp", "@AndroidEntryPoint",
                               "@Component", "@Module", "@Provides"})


class KotlinAnalyzer(LanguageAnalyzer):
    """Enriches SymbolEntry decorators with Android/Kotlin-specific patterns."""

    @property
    def language(self) -> str:
        return "kotlin"

    def enrich(
        self,
        parsed_files: List[ParsedFile],
        symbol_table: ProjectSymbolTable,
        result: AnalysisResult,
    ) -> None:
        """
        Scan Kotlin files and add decorator tags to relevant SymbolEntry
        objects in the symbol table.

        Patterns detected:
          1. Repository pattern — ViewModels calling Repository classes
          2. Coroutine scope — methods that launch/async/withContext
          3. Hilt/Dagger DI — classes with @HiltViewModel, @Singleton, @Inject
        """
        if not parsed_files:
            return

        for pf in parsed_files:
            try:
                self._enrich_file(pf, symbol_table)
            except Exception:
                logger.exception(
                    "[KotlinAnalyzer] Unexpected error enriching %s",
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

        # Identify ViewModel classes in this file for pattern 1.
        viewmodel_classes: set[str] = set()
        for sym in pf.symbols:
            if sym.symbol_type != "class_head":
                continue
            for base in sym.bases:
                base_name = base.split("<")[0].strip()
                if "ViewModel" in base_name:
                    viewmodel_classes.add(sym.name)
                    break

        for sym in pf.symbols:
            try:
                entry = symbol_table.lookup_by_qualified(
                    symbol_table.qualified_key(file_path, sym.parent_name, sym.name)
                )

                # ── Pattern 1: Repository pattern validation ───────────────
                if (
                    sym.symbol_type in ("method", "function")
                    and sym.parent_name in viewmodel_classes
                ):
                    for callee in sym.calls:
                        # Resolve the callee to check if it belongs to a Repository
                        callee_bare = callee.split("(")[0].split(".")[-1]
                        candidates = symbol_table.lookup_by_name(
                            callee_bare, prefer_file=file_path
                        )
                        for candidate in candidates:
                            # Check if the owning class name ends in "Repository"
                            owner = candidate.parent_name or candidate.name
                            if owner.endswith("Repository"):
                                vm_entry = symbol_table.lookup_by_qualified(
                                    f"{file_path}::{sym.parent_name}"
                                )
                                if vm_entry is not None:
                                    _add_decorator(vm_entry, "@uses_repository")
                                    logger.debug(
                                        "[KotlinAnalyzer] ViewModel %s uses %s",
                                        sym.parent_name,
                                        owner,
                                    )
                                break

                # ── Pattern 2: Coroutine scope tagging ────────────────────
                coroutine_calls_found = [
                    c for c in sym.calls
                    if c.split("(")[0].split(".")[-1] in _COROUTINE_CALLS
                ]
                if coroutine_calls_found:
                    if entry is not None:
                        _add_decorator(entry, "@coroutine_scope")
                    logger.debug(
                        "[KotlinAnalyzer] coroutine scope in %s (%s)",
                        sym.name,
                        coroutine_calls_found,
                    )

                # ── Pattern 3: Hilt/Dagger DI detection ───────────────────
                if sym.symbol_type == "class_head":
                    # Check decorators parsed by the parser
                    for dec in sym.decorators:
                        if dec in _DI_ANNOTATIONS or any(
                            dec.startswith(a) for a in _DI_ANNOTATIONS
                        ):
                            if entry is not None:
                                _add_decorator(entry, "@di_component")
                            logger.debug(
                                "[KotlinAnalyzer] DI component: %s (via %s)",
                                sym.name,
                                dec,
                            )
                            break
                    # Also scan source text for annotation patterns
                    source = sym.source or ""
                    if entry is not None and "@di_component" not in _get_extra_tags(entry):
                        for ann in _DI_ANNOTATIONS:
                            if ann in source:
                                _add_decorator(entry, "@di_component")
                                logger.debug(
                                    "[KotlinAnalyzer] DI component (source scan): %s",
                                    sym.name,
                                )
                                break

            except Exception:
                logger.exception(
                    "[KotlinAnalyzer] Error enriching symbol %s in %s",
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


def _get_extra_tags(entry: SymbolEntry) -> list:
    """Return the _extra_tags list, or empty list if not set."""
    return getattr(entry, "_extra_tags", [])
