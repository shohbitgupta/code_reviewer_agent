"""
Swift language-specific enrichment pass (minimal).

The regex-based Swift parser (swift_parser.py) does populate calls[] via a
broad identifier-before-"(" scan, but this pass predates that and still
focuses on what class_head symbol bases provide: protocol conformance
detection and delegate pattern detection. Extending it to reason about
calls[] (the way DartAnalyzer does for widget-composition/BLoC patterns)
is a reasonable follow-up, not yet done here.
"""
from __future__ import annotations

import logging
from typing import List

from stage1_ingestion.analyzers.base import AnalysisResult, LanguageAnalyzer
from core.models import ParsedFile
from stage1_ingestion.symbol_table import ProjectSymbolTable, SymbolEntry

logger = logging.getLogger(__name__)

# Well-known Swift/UIKit/Foundation classes (not protocols).
# Any base not in this set is treated as a potential protocol.
_KNOWN_CLASSES: frozenset[str] = frozenset({
    "NSObject", "UIViewController", "UIView", "UITableViewController",
    "UICollectionViewController", "UINavigationController",
    "UITabBarController", "UIControl", "UIScrollView",
    "UITableViewCell", "UICollectionViewCell",
    "ObservableObject",  # SwiftUI — technically a protocol, but often cited as a class
})


class SwiftAnalyzer(LanguageAnalyzer):
    """Enriches SymbolEntry decorators with Swift-specific patterns (minimal pass)."""

    @property
    def language(self) -> str:
        return "swift"

    def enrich(
        self,
        parsed_files: List[ParsedFile],
        symbol_table: ProjectSymbolTable,
        result: AnalysisResult,
    ) -> None:
        """
        Scan Swift files and add decorator tags to relevant SymbolEntry
        objects in the symbol table.

        Patterns detected:
          1. Protocol conformance — bases that are not known class names
          2. Delegate pattern — classes named *Delegate or conforming to *Delegate
        """
        if not parsed_files:
            return

        for pf in parsed_files:
            try:
                self._enrich_file(pf, symbol_table)
            except Exception:
                logger.exception(
                    "[SwiftAnalyzer] Unexpected error enriching %s",
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

        # Collect all class_head names in the whole project for disambiguation.
        all_project_class_names: set[str] = {
            e.name
            for e in symbol_table.all_entries()
            if e.symbol_type == "class_head"
        }

        for sym in pf.symbols:
            try:
                if sym.symbol_type != "class_head":
                    continue

                entry = symbol_table.lookup_by_qualified(
                    symbol_table.qualified_key(file_path, sym.parent_name, sym.name)
                )
                if entry is None:
                    continue

                # ── Pattern 1: Protocol conformance detection ─────────────
                for base in sym.bases:
                    base_name = base.split("<")[0].strip()
                    if not base_name:
                        continue
                    # Skip if it is a known class, or if it resolves to a
                    # class_head in the project (i.e. it IS a class, not protocol).
                    is_known_class = (
                        base_name in _KNOWN_CLASSES
                        or base_name in all_project_class_names
                    )
                    if not is_known_class:
                        # Treat as a protocol conformance.
                        protocol_tag = f"@conforms_to_{base_name}"
                        _add_decorator(entry, protocol_tag)
                        logger.debug(
                            "[SwiftAnalyzer] %s conforms to protocol %s",
                            sym.name,
                            base_name,
                        )

                        # ── Pattern 2: Delegate pattern ────────────────────
                        if base_name.endswith("Delegate"):
                            _add_decorator(entry, "@delegate")
                            logger.debug(
                                "[SwiftAnalyzer] delegate conformance: %s → %s",
                                sym.name,
                                base_name,
                            )

                # Also tag the class itself if its name ends in "Delegate"
                if sym.name.endswith("Delegate"):
                    _add_decorator(entry, "@delegate")
                    logger.debug(
                        "[SwiftAnalyzer] delegate class by name: %s",
                        sym.name,
                    )

            except Exception:
                logger.exception(
                    "[SwiftAnalyzer] Error enriching symbol %s in %s",
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
