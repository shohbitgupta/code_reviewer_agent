"""
Dart/Flutter language-specific enrichment pass.

Detects Flutter widget composition, BLoC/Cubit state machines, and
Riverpod provider consumers, then tags SymbolEntry objects with
additional decorator metadata.
"""
from __future__ import annotations

import logging
import re
from typing import List

from ingestion.analyzers.base import AnalysisResult, LanguageAnalyzer
from ingestion.models import ParsedFile
from ingestion.symbol_table import ProjectSymbolTable, SymbolEntry

logger = logging.getLogger(__name__)


class DartAnalyzer(LanguageAnalyzer):
    """Enriches SymbolEntry decorators with Flutter/Dart-specific patterns."""

    @property
    def language(self) -> str:
        return "dart"

    def enrich(
        self,
        parsed_files: List[ParsedFile],
        symbol_table: ProjectSymbolTable,
        result: AnalysisResult,
    ) -> None:
        """
        Scan Dart/Flutter files and add decorator tags to relevant SymbolEntry
        objects in the symbol table.

        Patterns detected:
          1. Widget composition — build() methods in @flutter_widget classes
          2. BLoC/Cubit state machines — classes inheriting Bloc or Cubit
          3. Riverpod consumers — classes calling ref.watch() or ref.read()
        """
        if not parsed_files:
            return

        # Collect names of all class_head symbols for widget-ref detection.
        all_class_names: set[str] = {
            entry.name
            for entry in symbol_table.all_entries()
            if entry.symbol_type == "class_head"
        }

        for pf in parsed_files:
            try:
                self._enrich_file(pf, symbol_table, all_class_names)
            except Exception:
                logger.exception(
                    "[DartAnalyzer] Unexpected error enriching %s",
                    pf.file_meta.file_path,
                )

    # ------------------------------------------------------------------
    # Per-file enrichment
    # ------------------------------------------------------------------

    def _enrich_file(
        self,
        pf: ParsedFile,
        symbol_table: ProjectSymbolTable,
        all_class_names: set[str],
    ) -> None:
        if not pf.symbols:
            return

        file_path = pf.file_meta.file_path

        # Build a set of class names that are BLoC/Cubit in this file.
        bloc_cubit_classes: set[str] = set()
        for sym in pf.symbols:
            if sym.symbol_type != "class_head":
                continue
            for base in sym.bases:
                base_name = base.split("<")[0].strip()
                if base_name in ("Bloc", "Cubit"):
                    bloc_cubit_classes.add(sym.name)
                    entry = symbol_table.lookup_by_qualified(
                        f"{file_path}::{sym.name}"
                    )
                    if entry is not None:
                        _add_decorator(entry, "@bloc_state_machine")
                    logger.debug(
                        "[DartAnalyzer] BLoC/Cubit class: %s in %s",
                        sym.name,
                        file_path,
                    )
                    break

        for sym in pf.symbols:
            try:
                entry = symbol_table.lookup_by_qualified(
                    f"{file_path}::{sym.name}"
                )

                # ── Pattern 1: widget composition via build() ──────────────
                if (
                    sym.symbol_type in ("method", "function")
                    and sym.name == "build"
                    and sym.parent_name is not None
                ):
                    parent_entry = symbol_table.lookup_by_qualified(
                        f"{file_path}::{sym.parent_name}"
                    )
                    is_flutter_widget = parent_entry is not None and (
                        "@flutter_widget" in parent_entry.bases
                        or any(
                            b in ("StatelessWidget", "StatefulWidget",
                                  "ConsumerWidget", "HookWidget")
                            for b in parent_entry.bases
                        )
                    )
                    if is_flutter_widget:
                        rendered_widgets = [
                            c for c in sym.calls
                            if c and c[0].isupper() and c in all_class_names
                        ]
                        if rendered_widgets:
                            logger.debug(
                                "[DartAnalyzer] %s.build() renders: %s",
                                sym.parent_name,
                                rendered_widgets,
                            )
                            # The CALLS edges from Resolver cover these; we
                            # just tag the build method for completeness.
                            if entry is not None:
                                _add_decorator(entry, "@widget_composer")

                # ── Pattern 2: BLoC/Cubit event handlers + emit calls ──────
                if (
                    sym.symbol_type in ("method", "function")
                    and sym.parent_name in bloc_cubit_classes
                ):
                    # Tag methods that call emit()
                    if any(c.startswith("emit(") or c == "emit" for c in sym.calls):
                        if entry is not None:
                            _add_decorator(entry, "@emits_state")
                        logger.debug(
                            "[DartAnalyzer] emit() call in %s.%s",
                            sym.parent_name,
                            sym.name,
                        )

                    # Tag on<EventType> handler registrations
                    # Regex: on<SomeEvent>( in calls or source text
                    on_event_pattern = re.compile(r"\bon<(\w+)>")
                    source_matches = on_event_pattern.findall(sym.source or "")
                    if source_matches and entry is not None:
                        _add_decorator(entry, "@bloc_event_handler")
                        logger.debug(
                            "[DartAnalyzer] event handler %s handles: %s",
                            sym.name,
                            source_matches,
                        )

                # ── Pattern 3: Riverpod consumers ─────────────────────────
                riverpod_calls = {"ref.watch", "ref.read", "ref.watch(", "ref.read("}
                if any(c in riverpod_calls or c.startswith("ref.watch") or c.startswith("ref.read")
                       for c in sym.calls):
                    # Tag the parent class, or the symbol itself for top-level fns.
                    target_name = sym.parent_name or sym.name
                    target_entry = symbol_table.lookup_by_qualified(
                        f"{file_path}::{target_name}"
                    )
                    if target_entry is not None:
                        _add_decorator(target_entry, "@riverpod_consumer")
                        logger.debug(
                            "[DartAnalyzer] Riverpod consumer: %s in %s",
                            target_name,
                            file_path,
                        )

            except Exception:
                logger.exception(
                    "[DartAnalyzer] Error enriching symbol %s in %s",
                    sym.name,
                    file_path,
                )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _add_decorator(entry: SymbolEntry, tag: str) -> None:
    """
    Append an enrichment *tag* to the SymbolEntry (idempotent).

    SymbolEntry is a plain (non-frozen) dataclass so dynamic attribute
    assignment is allowed.  Tags are stored in a list at entry._extra_tags
    using the ``@`` prefix convention so consumers can distinguish them from
    real base class names.
    """
    if not hasattr(entry, "_extra_tags"):
        entry._extra_tags: list = []  # type: ignore[attr-defined]
    extra: list = entry._extra_tags  # type: ignore[attr-defined]
    if tag not in extra:
        extra.append(tag)
