"""
Pass 2 — resolves raw string references on ParsedSymbol objects to typed
ResolvedCall / qualified-name objects.

Three resolution passes live in one class to share the symbol table:

  1. resolve_calls()    — ParsedSymbol.calls[]   → resolved_calls dict
  2. resolve_bases()    — ParsedSymbol.bases[]   → resolved_bases dict
  3. resolve_imports()  — ParsedSymbol.imports[] → resolved_imports + external_deps dicts

Call resolution strategy (tries in order, stops at first success):
  1. Exact qualified   : symbol_table.lookup_by_qualified("{caller_file}::{callee}")       conf 1.0
  2. Same-file by name : lookup_by_name(callee, prefer_file=caller_file) → file matches   conf 1.0
  3. Project-wide name : lookup_by_name(callee)                                            conf 0.8
  4. Strip self./cls.  : retry steps 1-3 with stripped name                               same conf
  5. Unresolved        : is_external=True                                                  conf 0.5
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Set, Tuple

from ingestion.analyzers.base import AnalysisResult, ResolvedCall
from ingestion.models import ParsedFile
from ingestion.symbol_table import ProjectSymbolTable, SymbolEntry


class Resolver:
    """
    Resolves calls, bases and imports for the entire project in one pass.

    Usage::

        resolver = Resolver()
        resolver.resolve(parsed_files, symbol_table, layer_map, result)
    """

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def resolve(
        self,
        parsed_files: List[ParsedFile],
        symbol_table: ProjectSymbolTable,
        layer_map: Dict[str, str],
        result: AnalysisResult,
    ) -> None:
        """
        Run all three resolution passes and populate *result* in-place.

        Parameters
        ----------
        parsed_files:
            All ParsedFile objects for the project.
        symbol_table:
            Pre-built ProjectSymbolTable.
        layer_map:
            file_path → layer (output of LayerClassifier, may be empty dict).
        result:
            AnalysisResult to mutate.
        """
        self._resolve_calls(parsed_files, symbol_table, result)
        self._resolve_bases(parsed_files, symbol_table, result)
        self._resolve_imports(parsed_files, symbol_table, result)

    # ------------------------------------------------------------------
    # Pass 1: call resolution
    # ------------------------------------------------------------------

    def _resolve_calls(
        self,
        parsed_files: List[ParsedFile],
        symbol_table: ProjectSymbolTable,
        result: AnalysisResult,
    ) -> None:
        """
        Populate result.resolved_calls.

        Key  = "caller_file::caller_symbol"
        Value = List[ResolvedCall]
        """
        for pf in parsed_files:
            caller_file = pf.file_meta.file_path
            for sym in pf.symbols:
                if not sym.calls:
                    continue
                caller_qual = f"{caller_file}::{sym.name}"
                resolved_list: List[ResolvedCall] = []

                for callee_raw in sym.calls:
                    rc = self._resolve_single_call(
                        callee_raw, caller_file, symbol_table
                    )
                    resolved_list.append(rc)
                    if rc.is_external:
                        result.total_calls_external += 1
                    elif rc.resolved_chunk_id is not None or rc.resolved_file is not None:
                        result.total_calls_resolved += 1
                    else:
                        result.total_calls_unresolved += 1

                if resolved_list:
                    result.resolved_calls.setdefault(caller_qual, []).extend(resolved_list)

    def _resolve_single_call(
        self,
        callee_raw: str,
        caller_file: str,
        symbol_table: ProjectSymbolTable,
    ) -> ResolvedCall:
        """
        Try the five-step resolution cascade for one callee name string.
        Returns a ResolvedCall (is_external=True if all steps fail).
        """
        callee = callee_raw.strip()

        # Step 1 & 2 & 3 with the raw name.
        result = self._try_resolve(callee, caller_file, symbol_table)
        if result is not None:
            return result

        # Step 4: strip common prefixes (self.foo → foo, cls.bar → bar, this.baz → baz)
        stripped = self._strip_receiver_prefix(callee)
        if stripped and stripped != callee:
            result = self._try_resolve(stripped, caller_file, symbol_table)
            if result is not None:
                return result

        # Step 5: unresolved / external
        return ResolvedCall(
            callee_name=callee_raw,
            resolved_chunk_id=None,
            resolved_file=None,
            is_external=True,
            confidence=0.5,
        )

    def _try_resolve(
        self,
        callee: str,
        caller_file: str,
        symbol_table: ProjectSymbolTable,
    ) -> Optional[ResolvedCall]:
        """
        Attempt steps 1, 2, 3 for *callee*.  Return ResolvedCall or None.
        """
        # Step 1: exact qualified match using caller_file as the namespace.
        qualified = f"{caller_file}::{callee}"
        entry = symbol_table.lookup_by_qualified(qualified)
        if entry is not None:
            return _entry_to_resolved_call(callee, entry, confidence=1.0)

        # Steps 2 & 3: name lookup, same-file preferred.
        candidates = symbol_table.lookup_by_name(callee, prefer_file=caller_file)
        if candidates:
            best = candidates[0]  # first = same-file if available, then sorted
            conf = 1.0 if best.file_path == caller_file else 0.8
            return _entry_to_resolved_call(callee, best, confidence=conf)

        return None

    # ------------------------------------------------------------------
    # Pass 2: base-class resolution
    # ------------------------------------------------------------------

    def _resolve_bases(
        self,
        parsed_files: List[ParsedFile],
        symbol_table: ProjectSymbolTable,
        result: AnalysisResult,
    ) -> None:
        """
        Populate result.resolved_bases.

        Key   = class qualified_name ("file::ClassName")
        Value = list of resolved base class qualified_names
        """
        for pf in parsed_files:
            file_path = pf.file_meta.file_path
            for sym in pf.symbols:
                if sym.symbol_type != "class_head" or not sym.bases:
                    continue
                class_qual = f"{file_path}::{sym.name}"
                resolved_base_quals: List[str] = []

                for base_raw in sym.bases:
                    # Strip generics: "StateNotifier<X>" → "StateNotifier"
                    base_name = base_raw.split("<")[0].split("[")[0].strip()

                    # Try same-file first, then project-wide.
                    candidates = symbol_table.lookup_by_name(
                        base_name, prefer_file=file_path
                    )
                    if candidates:
                        resolved_base_quals.append(candidates[0].qualified_name)
                    # If not found, we skip (external / stdlib base class).

                if resolved_base_quals:
                    result.resolved_bases.setdefault(class_qual, []).extend(
                        resolved_base_quals
                    )

    # ------------------------------------------------------------------
    # Pass 3: import resolution
    # ------------------------------------------------------------------

    def _resolve_imports(
        self,
        parsed_files: List[ParsedFile],
        symbol_table: ProjectSymbolTable,
        result: AnalysisResult,
    ) -> None:
        """
        Populate result.resolved_imports and result.external_deps.

        resolved_imports : file_path → list of resolved local file_paths
        external_deps    : file_path → set of external package names
        """
        # Build a set of all known file paths for fast membership testing.
        known_files: Set[str] = {pf.file_meta.file_path for pf in parsed_files}

        for pf in parsed_files:
            file_path = pf.file_meta.file_path
            local_resolved: List[str] = []
            external: Set[str] = set()

            for sym in pf.symbols:
                if sym.symbol_type != "import" or not sym.imports:
                    continue
                for import_path in sym.imports:
                    resolved_file = self._resolve_import_path(
                        import_path, file_path, known_files
                    )
                    if resolved_file is not None:
                        if resolved_file not in local_resolved:
                            local_resolved.append(resolved_file)
                    else:
                        # Treat top-level package name as external dep.
                        pkg = _top_level_package(import_path)
                        if pkg:
                            external.add(pkg)

            if local_resolved:
                result.resolved_imports.setdefault(file_path, []).extend(local_resolved)
            if external:
                result.external_deps.setdefault(file_path, set()).update(external)

    @staticmethod
    def _resolve_import_path(
        import_path: str,
        caller_file: str,
        known_files: Set[str],
    ) -> Optional[str]:
        """
        Attempt to map an import path string to a file_path in the project.

        Handles:
        - Relative dot-notation: "from .sibling import X"  → resolved against caller dir
        - Absolute dot-notation: "from package.module import X" → "package/module.py" etc.
        - Direct file paths (already normalised)
        """
        # Normalise separators.
        norm = import_path.replace("\\", "/").strip()

        # Direct hit (already a file path stored by a parser).
        if norm in known_files:
            return norm

        # Dart/Flutter package: imports → lib/file_path
        # e.g. "package:visitor_tracker/screens/foo.dart" → "lib/screens/foo.dart"
        if norm.startswith("package:"):
            after_package = norm[len("package:"):]
            slash_idx = after_package.find("/")
            if slash_idx != -1:
                file_rel = after_package[slash_idx + 1:]  # strip "pkg_name/"
                for prefix in ("lib/", ""):
                    candidate = f"{prefix}{file_rel}"
                    if candidate in known_files:
                        return candidate
            return None

        # Relative import: starts with "." or ".."
        if norm.startswith("."):
            resolved = _resolve_relative_import(norm, caller_file)
            if resolved and resolved in known_files:
                return resolved
            # Try with common extensions.
            for ext in (".py", ".dart", ".kt", ".swift", ".rs", ".ts", ".js"):
                candidate = (resolved or norm.lstrip(".")) + ext
                if candidate in known_files:
                    return candidate
            return None

        # Dot-separated module path → try as slash-separated file.
        slash_path = norm.replace(".", "/")
        for ext in ("", ".py", ".dart", ".kt", ".swift", ".rs", ".ts", ".js"):
            candidate = slash_path + ext
            if candidate in known_files:
                return candidate
            # Also try with src/ prefix.
            candidate_src = "src/" + slash_path + ext
            if candidate_src in known_files:
                return candidate_src

        return None

    # ------------------------------------------------------------------
    # Static utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_receiver_prefix(name: str) -> str:
        """
        Strip 'self.', 'cls.', 'this.' from the beginning of a call name.
        Returns the stripped name, or the original if no prefix found.
        """
        for prefix in ("self.", "cls.", "this.", "super."):
            if name.startswith(prefix):
                return name[len(prefix):]
        return name


# ── Module-level helpers ──────────────────────────────────────────────────────

def _entry_to_resolved_call(
    callee_name: str,
    entry: SymbolEntry,
    confidence: float,
) -> ResolvedCall:
    return ResolvedCall(
        callee_name=callee_name,
        resolved_chunk_id=entry.chunk_id,  # may be None until link_chunks() is called
        resolved_file=entry.file_path,
        is_external=False,
        confidence=confidence,
    )


def _top_level_package(import_path: str) -> str:
    """Extract the top-level package name from a dotted or slash-separated import path."""
    norm = import_path.replace("\\", "/").lstrip("./")
    # Take the first component.
    parts = norm.replace(".", "/").split("/")
    return parts[0] if parts else ""


def _resolve_relative_import(import_path: str, caller_file: str) -> Optional[str]:
    """
    Resolve a relative import path (starting with '.' or '..') against the
    directory of *caller_file*.

    e.g. import_path=".sibling", caller_file="pkg/module.py" → "pkg/sibling"
    """
    caller_dir = "/".join(caller_file.replace("\\", "/").split("/")[:-1])

    # Count leading dots to determine how many directories to go up.
    stripped = import_path.lstrip(".")
    dots = len(import_path) - len(stripped)

    # 1 dot  → current package (same dir)
    # 2 dots → parent package
    # etc.
    base_dir = caller_dir
    for _ in range(dots - 1):
        base_dir = "/".join(base_dir.split("/")[:-1]) if "/" in base_dir else ""

    if stripped:
        # Convert remaining dot-separated path to slash-separated.
        rel = stripped.replace(".", "/")
        return f"{base_dir}/{rel}" if base_dir else rel
    else:
        # "from . import X" — just the current package dir; caller handles name
        return base_dir or None
