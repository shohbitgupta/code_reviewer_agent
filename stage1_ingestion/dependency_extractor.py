"""
Step 1i — Dependency Extraction

DependencyExtractor analyses ParsedFile symbols and the chunk_map to produce
typed directed edges between CodeChunk nodes.

Edge types produced:
  BELONGS_TO  method chunk → parent class_head chunk
  CALLS       function/method → another function/method it calls
  IMPORTS     file import chunk → MODULE chunk of the imported local file
  INHERITS    class_head chunk → base class_head chunk

Resolution strategy (priority order):
  1. Exact key: chunk_map["file_path::symbol_name"]
  2. Symbol name match: chunk where chunk.symbol_name == called_name
  3. Tail strip: "self.validate" → "validate"
  4. Drop: unresolvable (stdlib, pip packages, external)

Usage:
    extractor = DependencyExtractor(repo_root="./workspace/repos/owner__repo")
    edges     = extractor.extract(parsed_files, chunk_map)
    # edges written into chunk.outgoing_edges / chunk.incoming_edges as well
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from core.models import (
    ChunkType,
    CodeChunk,
    DependencyEdge,
    EdgeType,
    ParsedFile,
)

logger = logging.getLogger(__name__)


class DependencyExtractor:
    """
    Extracts typed dependency edges from parsed symbols and the chunk map.

    Args:
        repo_root: Absolute path to the cloned repo root directory.
                   Used for resolving relative import paths.
    """

    def __init__(self, repo_root: str):
        self.repo_root = Path(repo_root)

    # ── Public API ────────────────────────────────────────────────────────────

    def extract(
        self,
        parsed_files:    List[ParsedFile],
        chunk_map:       Dict[str, CodeChunk],
        analysis_result: Optional["AnalysisResult"] = None,
    ) -> List[DependencyEdge]:
        """
        Produce all dependency edges for the given parsed files.

        Also back-populates outgoing_edges / incoming_edges on each CodeChunk.

        Args:
            parsed_files:    Output of Step 1f.
            chunk_map:       Dict keyed by both chunk_id (UUID) and
                             "file_path::symbol_name" for fast lookup.
            analysis_result: Optional AnalysisResult from Step 1f-LA.
                             When provided, CALLS/INHERITS/IMPORTS edges are
                             generated from the resolved data instead of the
                             raw name-matching heuristics.

        Returns:
            Deduplicated List[DependencyEdge].
        """
        if analysis_result is not None:
            return self._extract_with_analysis(parsed_files, chunk_map, analysis_result)

        # ── Backward-compat path (no analysis_result) ────────────────────────
        edges: List[DependencyEdge] = []
        dropped = 0

        # Build reverse index: symbol_name → [CodeChunk] for O(1) resolution
        # (replaces the O(n) full scan in _resolve_any_symbol)
        by_name: Dict[str, List[CodeChunk]] = {}
        for v in chunk_map.values():
            if isinstance(v, CodeChunk):
                by_name.setdefault(v.symbol_name, []).append(v)

        for pf in parsed_files:
            # Build the set of file paths imported by this parsed file,
            # used to disambiguate same-name symbols across modules.
            caller_imports: Set[str] = set()
            for s in pf.symbols:
                if s.symbol_type == "import":
                    for imp in s.imports:
                        resolved = self._module_to_filepath(imp, pf.file_meta.file_path)
                        if resolved:
                            caller_imports.add(resolved)

            for sym in pf.symbols:
                # ── BELONGS_TO: method → class_head ──────────────────────────
                if sym.symbol_type == "method" and sym.parent_name:
                    edge = self._belongs_to_edge(sym, pf, chunk_map)
                    if edge:
                        edges.append(edge)
                    else:
                        dropped += 1

                # ── CALLS: function/method → called symbols ───────────────────
                if sym.symbol_type in ("function", "method"):
                    for called in sym.calls:
                        edge = self._calls_edge(
                            sym, called, pf, chunk_map, by_name,
                            caller_imports=caller_imports,
                        )
                        if edge:
                            edges.append(edge)
                        else:
                            dropped += 1

                # ── IMPORTS: import chunk → MODULE chunk of imported file ─────
                if sym.symbol_type == "import":
                    for imp_path in sym.imports:
                        edge = self._imports_edge(sym, imp_path, pf, chunk_map)
                        if edge:
                            edges.append(edge)
                        else:
                            dropped += 1

                # ── INHERITS: class_head → base class_head ────────────────────
                if sym.symbol_type == "class_head":
                    for base in sym.bases:
                        edge = self._inherits_edge(sym, base, pf, chunk_map)
                        if edge:
                            edges.append(edge)
                        else:
                            dropped += 1

        # Deduplicate
        edges = self._deduplicate(edges)

        # Back-populate edges onto chunks
        self._back_populate(edges, chunk_map)

        logger.info(
            "[DependencyExtractor] Extracted %d edges (%d unresolved dropped)",
            len(edges), dropped,
        )
        return edges

    # ── Analysis-result-aware extraction path ────────────────────────────────

    def _extract_with_analysis(
        self,
        parsed_files:    List[ParsedFile],
        chunk_map:       Dict[str, CodeChunk],
        analysis_result: "AnalysisResult",
    ) -> List[DependencyEdge]:
        """
        Generate edges using the pre-resolved data in analysis_result.

        BELONGS_TO edges still use the existing parent_name logic (unchanged).
        CALLS edges come from resolved_calls.
        INHERITS edges come from resolved_bases.
        IMPORTS edges come from resolved_imports.
        """
        edges: List[DependencyEdge] = []
        dropped = 0

        # ── BELONGS_TO: unchanged — still resolved from parsed symbols ────────
        for pf in parsed_files:
            for sym in pf.symbols:
                if sym.symbol_type == "method" and sym.parent_name:
                    edge = self._belongs_to_edge(sym, pf, chunk_map)
                    if edge:
                        edges.append(edge)
                    else:
                        dropped += 1

        # ── CALLS: from analysis_result.resolved_calls ───────────────────────
        external_calls = 0
        for caller_qual, resolved_calls in analysis_result.resolved_calls.items():
            # caller_qual = "file_path::symbol_name"
            from_chunk = chunk_map.get(caller_qual)
            if from_chunk is None:
                # Try looking up by chunk_id in case it was stored differently
                dropped += len(resolved_calls)
                continue

            for rc in resolved_calls:
                if rc.is_external:
                    external_calls += 1
                    continue  # skip external targets — no to_chunk in chunk_map

                to_chunk = None
                if rc.resolved_chunk_id:
                    to_chunk = chunk_map.get(rc.resolved_chunk_id)
                if to_chunk is None and rc.resolved_file and rc.callee_name:
                    # Fallback: file::callee_name lookup
                    to_chunk = chunk_map.get(f"{rc.resolved_file}::{rc.callee_name}")

                if to_chunk is None or from_chunk.chunk_id == to_chunk.chunk_id:
                    dropped += 1
                    continue

                is_cross_file = from_chunk.file_path != to_chunk.file_path
                is_cross_domain = self._cross_domain(from_chunk.file_path, to_chunk.file_path)
                edges.append(DependencyEdge(
                    from_chunk_id   = from_chunk.chunk_id,
                    to_chunk_id     = to_chunk.chunk_id,
                    edge_type       = EdgeType.CALLS,
                    from_symbol     = from_chunk.symbol_name,
                    to_symbol       = rc.callee_name,
                    from_file       = from_chunk.file_path,
                    to_file         = to_chunk.file_path,
                    is_cross_file   = is_cross_file,
                    is_cross_domain = is_cross_domain,
                    is_external     = False,
                    confidence      = rc.confidence,
                    resolved_via    = "exact" if rc.confidence >= 1.0 else (
                        "name_match" if rc.confidence >= 0.8 else "heuristic"
                    ),
                ))

        # ── INHERITS: from analysis_result.resolved_bases ────────────────────
        for class_qual, base_quals in analysis_result.resolved_bases.items():
            from_chunk = chunk_map.get(class_qual)
            if from_chunk is None:
                dropped += len(base_quals)
                continue
            for base_qual in base_quals:
                to_chunk = chunk_map.get(base_qual)
                if to_chunk is None:
                    dropped += 1
                    continue
                if from_chunk.chunk_id == to_chunk.chunk_id:
                    continue
                is_cross_file = from_chunk.file_path != to_chunk.file_path
                is_cross_domain = self._cross_domain(from_chunk.file_path, to_chunk.file_path)
                edges.append(DependencyEdge(
                    from_chunk_id   = from_chunk.chunk_id,
                    to_chunk_id     = to_chunk.chunk_id,
                    edge_type       = EdgeType.INHERITS,
                    from_symbol     = from_chunk.symbol_name,
                    to_symbol       = to_chunk.symbol_name,
                    from_file       = from_chunk.file_path,
                    to_file         = to_chunk.file_path,
                    is_cross_file   = is_cross_file,
                    is_cross_domain = is_cross_domain,
                    confidence      = 1.0,
                    resolved_via    = "exact",
                ))

        # ── IMPORTS: from analysis_result.resolved_imports ───────────────────
        for caller_file, target_files in analysis_result.resolved_imports.items():
            # Find the import chunk for this file (symbol_name="imports" or type=import)
            import_chunk = chunk_map.get(f"{caller_file}::imports")
            if import_chunk is None:
                # Try finding any IMPORT chunk for this file
                import_chunk = next(
                    (
                        c for c in chunk_map.values()
                        if isinstance(c, CodeChunk)
                        and c.file_path == caller_file
                        and c.chunk_type.value == "import"
                    ),
                    None,
                )
            if import_chunk is None:
                dropped += len(target_files)
                continue
            for target_file in target_files:
                to_chunk = self._resolve_module_chunk(target_file, chunk_map)
                if to_chunk is None:
                    dropped += 1
                    continue
                if import_chunk.chunk_id == to_chunk.chunk_id:
                    continue
                is_cross_file = import_chunk.file_path != to_chunk.file_path
                is_cross_domain = self._cross_domain(import_chunk.file_path, to_chunk.file_path)
                edges.append(DependencyEdge(
                    from_chunk_id   = import_chunk.chunk_id,
                    to_chunk_id     = to_chunk.chunk_id,
                    edge_type       = EdgeType.IMPORTS,
                    from_symbol     = caller_file,
                    to_symbol       = target_file,
                    from_file       = import_chunk.file_path,
                    to_file         = to_chunk.file_path,
                    is_cross_file   = is_cross_file,
                    is_cross_domain = is_cross_domain,
                    confidence      = 1.0,
                    resolved_via    = "exact",
                ))

        # Deduplicate
        edges = self._deduplicate(edges)

        # Back-populate edges onto chunks
        self._back_populate(edges, chunk_map)

        logger.info(
            "[DependencyExtractor] Extracted %d edges "
            "(%d unresolved dropped, %d external calls skipped)",
            len(edges), dropped, external_calls,
        )
        return edges

    # ── Edge factories ────────────────────────────────────────────────────────

    def _belongs_to_edge(
        self,
        sym:       object,          # ParsedSymbol
        pf:        ParsedFile,
        chunk_map: Dict[str, CodeChunk],
    ) -> Optional[DependencyEdge]:
        """Build the BELONGS_TO edge linking a method symbol to its parent class_head chunk, or None if either endpoint is unresolved."""
        from_chunk = self._resolve_symbol(
            sym.name, pf.file_meta.file_path, sym.symbol_type, chunk_map
        )
        to_chunk = self._resolve_symbol(
            sym.parent_name, pf.file_meta.file_path, "class_head", chunk_map
        )
        if not (from_chunk and to_chunk) or from_chunk.chunk_id == to_chunk.chunk_id:
            return None
        return self._make_edge(
            from_chunk, to_chunk,
            EdgeType.BELONGS_TO,
            from_sym=f"{sym.parent_name}.{sym.name}",
            to_sym=sym.parent_name,
        )

    def _calls_edge(
        self,
        sym:        object,
        called:     str,
        pf:         ParsedFile,
        chunk_map:  Dict[str, CodeChunk],
        by_name:    Dict[str, List[CodeChunk]] = None,
        caller_imports: Optional[Set[str]] = None,
    ) -> Optional[DependencyEdge]:
        """Build the CALLS edge from a symbol's call site to the resolved target chunk, or None if the callee can't be resolved."""
        from_chunk = self._resolve_symbol(
            sym.name, pf.file_meta.file_path, sym.symbol_type, chunk_map
        )
        # Strip "self." / "cls." prefix for method calls
        called_clean = called.split(".")[-1] if "." in called else called
        to_chunk = self._resolve_any_symbol(
            called_clean, pf.file_meta.file_path, chunk_map, by_name,
            caller_imports=caller_imports,
        )
        if not (from_chunk and to_chunk) or from_chunk.chunk_id == to_chunk.chunk_id:
            return None
        return self._make_edge(
            from_chunk, to_chunk,
            EdgeType.CALLS,
            from_sym=sym.name,
            to_sym=called_clean,
        )

    def _imports_edge(
        self,
        sym:       object,
        imp_path:  str,
        pf:        ParsedFile,
        chunk_map: Dict[str, CodeChunk],
    ) -> Optional[DependencyEdge]:
        """Build the IMPORTS edge from a file's import chunk to the resolved target module chunk, or None if the import path can't be resolved to a local file."""
        from_chunk = self._resolve_symbol(
            "imports", pf.file_meta.file_path, "import", chunk_map
        )
        # Convert dotted module path to file path
        file_path = self._module_to_filepath(imp_path, pf.file_meta.file_path)
        if not file_path:
            return None
        to_chunk = self._resolve_module_chunk(file_path, chunk_map)
        if not (from_chunk and to_chunk):
            return None
        return self._make_edge(
            from_chunk, to_chunk,
            EdgeType.IMPORTS,
            from_sym=pf.file_meta.file_path,
            to_sym=file_path,
            raw_import=imp_path,
        )

    def _inherits_edge(
        self,
        sym:       object,
        base:      str,
        pf:        ParsedFile,
        chunk_map: Dict[str, CodeChunk],
    ) -> Optional[DependencyEdge]:
        """Build the INHERITS edge from a class_head chunk to its resolved base class chunk, or None if the base class can't be resolved."""
        from_chunk = self._resolve_symbol(
            sym.name, pf.file_meta.file_path, "class_head", chunk_map
        )
        base_clean = base.split(".")[-1] if "." in base else base
        to_chunk   = self._resolve_any_symbol(base_clean, pf.file_meta.file_path, chunk_map, None)
        if not (from_chunk and to_chunk) or from_chunk.chunk_id == to_chunk.chunk_id:
            return None
        return self._make_edge(
            from_chunk, to_chunk,
            EdgeType.INHERITS,
            from_sym=sym.name,
            to_sym=base_clean,
        )

    # ── Resolution helpers ────────────────────────────────────────────────────

    @staticmethod
    def _resolve_symbol(
        name:      str,
        file_path: str,
        sym_type:  str,
        chunk_map: Dict[str, CodeChunk],
    ) -> Optional[CodeChunk]:
        """Exact file-scoped lookup: file_path::name."""
        key = f"{file_path}::{name}"
        return chunk_map.get(key)

    @staticmethod
    def _resolve_any_symbol(
        name:        str,
        prefer_file: str,
        chunk_map:   Dict[str, CodeChunk],
        by_name:     Optional[Dict[str, List[CodeChunk]]] = None,
        caller_imports: Optional[Set[str]] = None,
    ) -> Optional[CodeChunk]:
        """
        O(1) symbol lookup using the pre-built by_name index.

        Priority:
          1. Same file as caller (highest confidence)
          2. A file explicitly imported by the caller
          3. Any other file (lowest confidence — name-only match)
        """
        if by_name is not None:
            candidates = by_name.get(name, [])
        else:
            candidates = [
                c for c in chunk_map.values()
                if isinstance(c, CodeChunk) and c.symbol_name == name
            ]

        if not candidates:
            return None

        # Priority 1: same file
        same_file = next((c for c in candidates if c.file_path == prefer_file), None)
        if same_file:
            return same_file

        # Priority 2: a file the caller explicitly imports
        if caller_imports:
            imported = next(
                (c for c in candidates if c.file_path in caller_imports), None
            )
            if imported:
                return imported

        # Priority 3: first remaining candidate
        return candidates[0]

    @staticmethod
    def _resolve_module_chunk(
        file_path: str,
        chunk_map: Dict[str, CodeChunk],
    ) -> Optional[CodeChunk]:
        """Find the MODULE chunk for a given relative file path."""
        for chunk in chunk_map.values():
            if (
                isinstance(chunk, CodeChunk)
                and chunk.chunk_type == ChunkType.MODULE
                and chunk.file_path == file_path
            ):
                return chunk
        return None

    def _module_to_filepath(
        self, module_path: str, caller_file: str
    ) -> Optional[str]:
        """
        Convert a module path or import string to a relative file path.

        Handles Python dotted paths, JS/TS relative imports, and direct paths
        for Kotlin (.kt/.kts), Swift (.swift), Dart (.dart), and Rust (.rs).
        """
        # All source extensions to try, in priority order
        _ALL_EXTS = (
            ".py", ".ts", ".tsx", ".js", ".jsx",
            ".kt", ".kts", ".swift", ".dart", ".rs",
        )

        # Relative imports: ./sibling or ../parent/module
        if module_path.startswith("."):
            caller_dir = Path(caller_file).parent
            candidate  = (caller_dir / module_path).with_suffix("")
            for ext in _ALL_EXTS:
                p = candidate.with_suffix(ext)
                if (self.repo_root / p).exists():
                    return str(p)
            return None

        # Dart package: imports — handled by call_resolver; skip local resolution
        if module_path.startswith("package:"):
            return None

        # Dotted or slash-separated module path → file
        parts     = module_path.replace("-", "_").replace("/", ".").split(".")
        candidate = Path(*parts)
        for ext in _ALL_EXTS:
            p = candidate.with_suffix(ext)
            if (self.repo_root / p).exists():
                return str(p)
            # Python package __init__
            init = candidate / "__init__.py"
            if (self.repo_root / init).exists():
                return str(init)

        return None   # third-party or unresolvable

    # ── Edge construction ─────────────────────────────────────────────────────

    @staticmethod
    def _make_edge(
        from_chunk: CodeChunk,
        to_chunk:   CodeChunk,
        edge_type:  EdgeType,
        from_sym:   str,
        to_sym:     str,
        raw_import: Optional[str] = None,
    ) -> DependencyEdge:
        """Construct a DependencyEdge between two chunks, computing the is_cross_file / is_cross_domain flags."""
        is_cross_file   = from_chunk.file_path != to_chunk.file_path
        is_cross_domain = DependencyExtractor._cross_domain(
            from_chunk.file_path, to_chunk.file_path
        )
        return DependencyEdge(
            from_chunk_id   = from_chunk.chunk_id,
            to_chunk_id     = to_chunk.chunk_id,
            edge_type       = edge_type,
            from_symbol     = from_sym,
            to_symbol       = to_sym,
            from_file       = from_chunk.file_path,
            to_file         = to_chunk.file_path,
            is_cross_file   = is_cross_file,
            is_cross_domain = is_cross_domain,
            raw_import      = raw_import,
        )

    @staticmethod
    def _cross_domain(file_a: str, file_b: str) -> bool:
        """True when the two files sit under different top-level directories."""
        parts_a = Path(file_a).parts
        parts_b = Path(file_b).parts
        if len(parts_a) < 2 or len(parts_b) < 2:
            return False
        return parts_a[0] != parts_b[0]

    # ── Post-processing ───────────────────────────────────────────────────────

    @staticmethod
    def _deduplicate(edges: List[DependencyEdge]) -> List[DependencyEdge]:
        """Remove duplicate edges sharing the same (from_chunk_id, to_chunk_id, edge_type) key."""
        seen:   Set[Tuple] = set()
        unique: List[DependencyEdge] = []
        for e in edges:
            key = (e.from_chunk_id, e.to_chunk_id, e.edge_type)
            if key not in seen:
                seen.add(key)
                unique.append(e)
        return unique

    @staticmethod
    def _back_populate(
        edges:     List[DependencyEdge],
        chunk_map: Dict[str, CodeChunk],
    ) -> None:
        """Write outgoing_edges / incoming_edges back onto each CodeChunk."""
        for edge in edges:
            from_chunk = chunk_map.get(edge.from_chunk_id)
            to_chunk   = chunk_map.get(edge.to_chunk_id)
            if from_chunk and edge.to_chunk_id not in from_chunk.outgoing_edges:
                from_chunk.outgoing_edges.append(edge.to_chunk_id)
            if to_chunk and edge.from_chunk_id not in to_chunk.incoming_edges:
                to_chunk.incoming_edges.append(edge.from_chunk_id)
