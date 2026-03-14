"""
ProjectSymbolTable — built from all ParsedFiles after Step 1f.

Provides O(1) lookup by qualified name and O(k) lookup by unqualified name.
The qualified name key is "file_path::symbol_name".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ingestion.models import CodeChunk, ParsedFile


@dataclass
class SymbolEntry:
    """One symbol extracted from a parsed file."""
    name:           str            # unqualified symbol name
    qualified_name: str            # "file_path::symbol_name"
    file_path:      str
    symbol_type:    str            # "function"|"method"|"class_head"|"import"
    parent_name:    Optional[str]
    bases:          List[str]
    param_types:    List[str]
    return_type:    Optional[str]
    layer:          str = "unknown"     # assigned by LayerClassifier
    chunk_id:       Optional[str] = None  # linked after chunking


class ProjectSymbolTable:
    """
    Symbol table for a full project, built from a list of ParsedFile objects.

    Storage layout
    --------------
    _by_qualified : Dict[str, SymbolEntry]
        Key  = "file_path::symbol_name"   — O(1) exact lookup.
    _by_name      : Dict[str, List[SymbolEntry]]
        Key  = symbol_name                 — O(k) lookup by short name.
        Values are sorted by file_path for deterministic ordering.
    _by_file      : Dict[str, List[SymbolEntry]]
        Key  = file_path                   — enumerate all symbols in a file.
    """

    def __init__(self) -> None:
        self._by_qualified: Dict[str, SymbolEntry] = {}
        self._by_name: Dict[str, List[SymbolEntry]] = {}
        self._by_file: Dict[str, List[SymbolEntry]] = {}

    # ------------------------------------------------------------------
    # Building
    # ------------------------------------------------------------------

    def build(self, parsed_files: List[ParsedFile]) -> None:
        """
        Populate the table from a list of ParsedFile objects.

        Re-calling build() on an already-populated table merges entries
        (later entries with the same qualified name overwrite earlier ones).
        """
        for pf in parsed_files:
            file_path = pf.file_meta.file_path
            for sym in pf.symbols:
                qualified = f"{file_path}::{sym.name}"
                entry = SymbolEntry(
                    name=sym.name,
                    qualified_name=qualified,
                    file_path=file_path,
                    symbol_type=sym.symbol_type,
                    parent_name=sym.parent_name,
                    bases=list(sym.bases),
                    param_types=list(sym.param_types),
                    return_type=sym.return_type,
                )

                # Overwrite if already present (idempotent rebuild).
                self._by_qualified[qualified] = entry

                if sym.name not in self._by_name:
                    self._by_name[sym.name] = []
                # Avoid duplicates when rebuild is called multiple times.
                existing_quals = {e.qualified_name for e in self._by_name[sym.name]}
                if qualified not in existing_quals:
                    self._by_name[sym.name].append(entry)

                if file_path not in self._by_file:
                    self._by_file[file_path] = []
                existing_quals_file = {e.qualified_name for e in self._by_file[file_path]}
                if qualified not in existing_quals_file:
                    self._by_file[file_path].append(entry)

        # Keep _by_name lists sorted by file_path for determinism.
        for entries in self._by_name.values():
            entries.sort(key=lambda e: e.file_path)

    # ------------------------------------------------------------------
    # Linking chunks
    # ------------------------------------------------------------------

    def link_chunks(self, chunks: List[CodeChunk]) -> None:
        """
        Fill chunk_id on SymbolEntry objects after chunking completes.

        Matching is by qualified name ("file_path::symbol_name").
        A chunk's symbol_name may be a short name; we try the full qualified
        key first, then fall back to "file_path::symbol_name" construction.
        """
        for chunk in chunks:
            qualified = f"{chunk.file_path}::{chunk.symbol_name}"
            entry = self._by_qualified.get(qualified)
            if entry is not None:
                entry.chunk_id = chunk.chunk_id

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def lookup_by_name(
        self,
        name: str,
        prefer_file: str = "",
    ) -> List[SymbolEntry]:
        """
        Return all SymbolEntry objects whose unqualified name equals *name*.

        If *prefer_file* is given, entries from that file are returned first
        (same-file resolution). The remainder are appended in file_path order.
        Returns an empty list when no match exists.
        """
        entries = self._by_name.get(name, [])
        if not entries:
            return []
        if not prefer_file:
            return list(entries)

        same_file = [e for e in entries if e.file_path == prefer_file]
        other = [e for e in entries if e.file_path != prefer_file]
        return same_file + other

    def lookup_by_qualified(self, qualified: str) -> Optional[SymbolEntry]:
        """Return the SymbolEntry for *qualified* ("file_path::symbol_name"), or None."""
        return self._by_qualified.get(qualified)

    def get_file_symbols(self, file_path: str) -> List[SymbolEntry]:
        """Return all SymbolEntry objects that belong to *file_path*."""
        return list(self._by_file.get(file_path, []))

    def all_entries(self) -> List[SymbolEntry]:
        """Return every SymbolEntry in the table (order: insertion order of qualified keys)."""
        return list(self._by_qualified.values())

    # ------------------------------------------------------------------
    # Layer assignment (called by LayerClassifier)
    # ------------------------------------------------------------------

    def set_layer(self, file_path: str, layer: str) -> None:
        """Assign *layer* to every symbol entry that belongs to *file_path*."""
        for entry in self._by_file.get(file_path, []):
            entry.layer = layer

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._by_qualified)

    def __repr__(self) -> str:  # pragma: no cover
        return f"ProjectSymbolTable(symbols={len(self)}, files={len(self._by_file)})"
