"""
Pass 1 — assigns an architectural layer to every file_path in the project.

Rules are checked in order; first match wins:
  1. Base-class signals  — look at SymbolEntry.bases for class_head symbols
  2. Directory path segment signals — match against parts of relative_path

Files with no signal are assigned "unknown".
"""
from __future__ import annotations

from typing import Dict, List, Set

from core.models import ParsedFile
from stage1_ingestion.symbol_table import ProjectSymbolTable, SymbolEntry


# ── Layer → base class name set ───────────────────────────────────────────────

BASE_CLASS_LAYERS: Dict[str, Set[str]] = {
    "presentation": {
        "StatefulWidget", "StatelessWidget", "ConsumerWidget", "HookWidget",
        "Activity", "AppCompatActivity", "ComponentActivity", "FragmentActivity",
        "Fragment", "DialogFragment", "BottomSheetDialogFragment",
        "ViewController", "UIViewController", "UIView",
    },
    "domain": {
        "ViewModel", "AndroidViewModel", "StateNotifier", "ChangeNotifier",
        "Bloc", "Cubit", "GetxController",
        "Interactor", "UseCase",
    },
    "data": {
        "Repository", "DataSource", "Dao", "ApiService",
        "RoomDatabase", "ContentProvider",
        "Worker", "CoroutineWorker",
    },
}

# ── Layer → directory-segment set (matched against ALL path parts) ────────────

PATH_LAYERS: Dict[str, Set[str]] = {
    "presentation": {
        "screens", "screen", "ui", "views", "view", "pages", "page",
        "widgets", "widget", "components", "component",
        "activities", "fragments", "viewcontrollers",
    },
    "domain": {
        "services", "service", "usecases", "usecase", "domain",
        "interactors", "interactor", "blocs", "bloc", "cubits", "cubit",
        "viewmodels", "viewmodel",
    },
    "data": {
        "models", "model", "repositories", "repository",
        "db", "database", "api", "network", "remote", "local",
        "datasources", "datasource", "dao",
    },
    "infrastructure": {
        "utils", "util", "helpers", "helper", "config", "configs",
        "constants", "constant", "di", "injection",
        "extensions", "extension",
    },
}

# Pre-compute the inverse mapping: base_class_name → layer (for fast lookup).
_BASE_TO_LAYER: Dict[str, str] = {
    base: layer
    for layer, bases in BASE_CLASS_LAYERS.items()
    for base in bases
}

# Pre-compute the inverse mapping: path_segment → layer.
_SEGMENT_TO_LAYER: Dict[str, str] = {
    segment: layer
    for layer, segments in PATH_LAYERS.items()
    for segment in segments
}


class LayerClassifier:
    """
    Assigns an architectural layer label to every file_path.

    Usage::

        classifier = LayerClassifier()
        layer_map = classifier.classify(parsed_files, symbol_table)
    """

    def classify(
        self,
        parsed_files: List[ParsedFile],
        symbol_table: ProjectSymbolTable,
    ) -> Dict[str, List[str]]:
        """
        Return a mapping of file_path → ordered list of layers for every ParsedFile.

        Cross-cutting files (e.g. mappers, DTOs, adapters) receive multiple
        labels.  The first label in the list is the primary (highest-priority)
        layer.  Single-label files are represented as a one-element list.

        Parameters
        ----------
        parsed_files:
            All ParsedFile objects for the project.
        symbol_table:
            Pre-built ProjectSymbolTable (used for base-class lookup).

        Returns
        -------
        Dict[str, List[str]]
            file_path → ordered list of layer labels, e.g.
            ["presentation"] or ["data", "domain"] for a mapper.
        """
        layer_map: Dict[str, List[str]] = {}

        for pf in parsed_files:
            file_path = pf.file_meta.file_path
            layer_map[file_path] = self._classify_file_multi(file_path, symbol_table)

        return layer_map

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _classify_file_multi(
        self,
        file_path: str,
        symbol_table: ProjectSymbolTable,
    ) -> List[str]:
        """
        Return an ordered list of layer labels for a single file.

        Step 1: collect all base-class signals (may yield multiple layers).
        Step 2: collect all path-segment signals (may add more).
        Step 3: if still empty → ["unknown"].

        The list is deduplicated and ordered by priority:
        presentation > domain > data > infrastructure > unknown.
        """
        matched: Set[str] = set()

        # Rule 1 — base-class signals (all matching layers, not just first)
        matched.update(self._layers_from_bases(file_path, symbol_table))

        # Rule 2 — directory path segments (supplement, not override)
        matched.update(self._layers_from_path(file_path))

        if not matched:
            return ["unknown"]

        # Return ordered by priority
        priority = ["presentation", "domain", "data", "infrastructure"]
        ordered = [p for p in priority if p in matched]
        # Append any remaining labels not in the priority list
        ordered += sorted(matched - set(priority))
        return ordered

    def _classify_file(
        self,
        file_path: str,
        symbol_table: ProjectSymbolTable,
    ) -> str:
        """Return the primary (highest-priority) layer for a file. Kept for compat."""
        return self._classify_file_multi(file_path, symbol_table)[0]

    def _layers_from_bases(
        self,
        file_path: str,
        symbol_table: ProjectSymbolTable,
    ) -> Set[str]:
        """
        Inspect every class_head symbol in *file_path* and return ALL matched
        layers (not just the highest-priority one).  A mapper that inherits from
        both a Repository base and a ViewModel base will get {"data", "domain"}.
        """
        entries: List[SymbolEntry] = symbol_table.get_file_symbols(file_path)
        matched: Set[str] = set()
        for entry in entries:
            if entry.symbol_type != "class_head":
                continue
            for base in entry.bases:
                base_name = base.split("<")[0].split("[")[0].strip()
                hit = _BASE_TO_LAYER.get(base_name)
                if hit:
                    matched.add(hit)
        return matched

    @staticmethod
    def _layers_from_path(file_path: str) -> Set[str]:
        """
        Return ALL layer labels signalled by directory path segments.

        A file at "src/data/mappers/UserMapper.kt" matches both "data"
        (from "data") and potentially others if further segments match.
        Collects all matches rather than returning on first hit.
        """
        parts = file_path.replace("\\", "/").split("/")
        matched: Set[str] = set()
        for part in parts:
            segment = part.lower().rsplit(".", 1)[0]
            hit = _SEGMENT_TO_LAYER.get(segment)
            if hit:
                matched.add(hit)
        return matched

    # Keep old single-value helpers as thin wrappers for any remaining callers.
    def _layer_from_bases(self, file_path: str, symbol_table: ProjectSymbolTable) -> str:
        layers = self._layers_from_bases(file_path, symbol_table)
        if not layers:
            return "unknown"
        for preferred in ("presentation", "domain", "data"):
            if preferred in layers:
                return preferred
        return sorted(layers)[0]

    @staticmethod
    def _layer_from_path(file_path: str) -> str:
        parts = file_path.replace("\\", "/").split("/")
        for part in parts:
            segment = part.lower().rsplit(".", 1)[0]
            hit = _SEGMENT_TO_LAYER.get(segment)
            if hit:
                return hit
        return "unknown"
