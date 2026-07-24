"""
Base types and abstract class for all Language Analyzer passes.

AnalysisResult, ResolvedCall are pure data classes — no heavy logic.
LanguageAnalyzer is the abstract base every language-specific analyzer subclasses.

Import graph (no cycles):
  analyzers.base  →  ingestion.models
  analyzers.base  →  ingestion.symbol_table  (TYPE_CHECKING only)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Set

from core.models import ParsedFile

if TYPE_CHECKING:
    from stage1_ingestion.symbol_table import ProjectSymbolTable


# ── Resolved call ─────────────────────────────────────────────────────────────

@dataclass
class ResolvedCall:
    """A single outgoing call from a symbol, with resolution metadata."""
    callee_name:       str
    resolved_chunk_id: Optional[str]  # None when is_external=True
    resolved_file:     Optional[str]  # file_path of the resolved symbol
    is_external:       bool
    confidence:        float  # 1.0=exact, 0.8=name_match, 0.5=heuristic


# ── Analysis result container ─────────────────────────────────────────────────

@dataclass
class AnalysisResult:
    """
    Aggregate output of all analyzer passes for a single project.

    All dicts are keyed by strings only — no object references —
    so this dataclass can be serialised to JSON without custom encoders.
    """

    # Maps qualified_name ("file::symbol") → list of resolved calls
    resolved_calls: Dict[str, List[ResolvedCall]] = field(default_factory=dict)

    # Maps file_path → architectural layer string
    layer_map: Dict[str, str] = field(default_factory=dict)

    # Maps class qualified_name → list of resolved base class qualified_names
    resolved_bases: Dict[str, List[str]] = field(default_factory=dict)

    # Maps file_path → list of resolved local import file_paths
    resolved_imports: Dict[str, List[str]] = field(default_factory=dict)

    # Maps file_path → set of external package names (unresolvable)
    external_deps: Dict[str, Set[str]] = field(default_factory=dict)

    # Aggregate counters — updated by Resolver passes
    total_calls_resolved:   int = 0
    total_calls_external:   int = 0
    total_calls_unresolved: int = 0


# ── Abstract analyzer base ────────────────────────────────────────────────────

class LanguageAnalyzer(ABC):
    """
    Base class for all language-specific analysis passes.

    Each subclass receives the full list of ParsedFiles, the shared
    ProjectSymbolTable, and a mutable AnalysisResult, and is expected
    to enrich that result in-place.

    Subclasses must not import from stage1_ingestion.pipeline or ingestion.parsers.
    """

    @property
    @abstractmethod
    def language(self) -> str:
        """
        The canonical language name this analyzer handles, e.g. "python".
        Return "*" to opt in to all languages.
        """
        ...

    @abstractmethod
    def enrich(
        self,
        parsed_files: List[ParsedFile],
        symbol_table: "ProjectSymbolTable",
        result: AnalysisResult,
    ) -> None:
        """
        Language-specific enrichment pass.  Mutates *result* in-place.

        Args:
            parsed_files:  All ParsedFile objects for this project.
            symbol_table:  Pre-built ProjectSymbolTable (read-only in this pass).
            result:        Shared AnalysisResult to mutate.
        """
        ...
