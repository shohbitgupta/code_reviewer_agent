"""
Pass 1 — assigns an architectural layer to every file_path in the project.

Rules are checked in order; first match wins:
  1. Base-class signals  — look at SymbolEntry.bases for class_head symbols
  2. Directory path segment signals — match against parts of relative_path

Files with no signal are assigned "unknown".
"""
from __future__ import annotations

from typing import Dict, List, Set

from ingestion.models import ParsedFile
from ingestion.symbol_table import ProjectSymbolTable, SymbolEntry


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
    ) -> Dict[str, str]:
        """
        Return a mapping of file_path → layer for every ParsedFile.

        Parameters
        ----------
        parsed_files:
            All ParsedFile objects for the project.
        symbol_table:
            Pre-built ProjectSymbolTable (used for base-class lookup).

        Returns
        -------
        Dict[str, str]
            file_path → "presentation" | "domain" | "data" | "infrastructure" | "unknown"
        """
        layer_map: Dict[str, str] = {}

        for pf in parsed_files:
            file_path = pf.file_meta.file_path
            layer = self._classify_file(file_path, symbol_table)
            layer_map[file_path] = layer

        return layer_map

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _classify_file(
        self,
        file_path: str,
        symbol_table: ProjectSymbolTable,
    ) -> str:
        """Apply the two-rule cascade for a single file."""

        # Rule 1 — base-class signals
        layer = self._layer_from_bases(file_path, symbol_table)
        if layer != "unknown":
            return layer

        # Rule 2 — directory path segments
        layer = self._layer_from_path(file_path)
        return layer  # may be "unknown" if no segment matched

    def _layer_from_bases(
        self,
        file_path: str,
        symbol_table: ProjectSymbolTable,
    ) -> str:
        """
        Inspect every class_head symbol in *file_path*.
        If any of its bases appear in BASE_CLASS_LAYERS, return that layer.
        When multiple layers are matched, "presentation" > "domain" > "data".
        """
        entries: List[SymbolEntry] = symbol_table.get_file_symbols(file_path)

        matched_layers: Set[str] = set()
        for entry in entries:
            if entry.symbol_type != "class_head":
                continue
            for base in entry.bases:
                # Strip any generic type parameters, e.g. "StateNotifier<X>" → "StateNotifier"
                base_name = base.split("<")[0].split("[")[0].strip()
                hit = _BASE_TO_LAYER.get(base_name)
                if hit:
                    matched_layers.add(hit)

        if not matched_layers:
            return "unknown"

        # Priority order: presentation > domain > data
        for preferred in ("presentation", "domain", "data"):
            if preferred in matched_layers:
                return preferred

        # Fallback: return any matched layer (deterministic: sorted first)
        return sorted(matched_layers)[0]

    @staticmethod
    def _layer_from_path(file_path: str) -> str:
        """
        Split *file_path* into directory/filename components and check each
        lowercase segment against PATH_LAYERS.

        The *last* matching segment in the path wins (most-specific directory),
        unless a higher-priority layer was already found further up the path —
        but since the spec says "first match wins among the ordered rules" and
        this is a single rule, we return the first segment match encountered
        when iterating left-to-right through the path parts.
        """
        # Split on both "/" and "\" to handle any OS path style stored as a string.
        import posixpath
        parts = file_path.replace("\\", "/").split("/")

        for part in parts:
            segment = part.lower()
            # Strip file extension from the last component.
            segment = segment.rsplit(".", 1)[0]
            hit = _SEGMENT_TO_LAYER.get(segment)
            if hit:
                return hit

        return "unknown"
