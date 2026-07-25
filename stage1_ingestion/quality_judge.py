"""
Step 1k+2 — Ingestion Quality Judge

Scores the pipeline output across 7 signal dimensions, produces an overall
quality score (0–100), and emits a PROCEED / WARN / ABORT decision.

  PROCEED  (≥ 70)  Stage 3 runs normally.
  WARN     (50–69) Stage 3 runs; low-confidence issues are surfaced in the report.
  ABORT    (< 50)  Pipeline halts before any LLM calls; report shows diagnostics.

Language-aware thresholds
─────────────────────────
Each dimension has a default threshold table, plus per-language overrides stored
in _LANG_THRESHOLDS.  The primary language is inferred from the most-common entry
in ingestion_stats["language_breakdown"].  Where no override exists, the default
applies.  This keeps scoring deterministic while accounting for real differences:

  • Dart/Flutter repos ship many *.g.dart / *.freezed.dart generated files that
    inflate the parseable-file count without contributing symbols → IQ-01/02 relax.
  • Java enforces one public class per file → IQ-02 relaxes to 3 sym/file.
  • JavaScript is dynamically typed → call resolution is genuinely harder → IQ-03 relaxes.
  • Kotlin sealed classes / data objects are leaf nodes with no callers → IQ-06 relaxes.

Dimension weights (sum = 100):
  IQ-01  Parse Coverage        20  — are files actually yielding symbols?
  IQ-02  Symbol Density        10  — enough granularity for line-level review?
  IQ-03  Call Resolution       20  — can Stage 3 build cross-file context?
  IQ-04  Layer Clarity         10  — are architectural layers classifiable?
  IQ-05  Chunk Size Health     20  — will chunks fit the LLM context window?
  IQ-06  Graph Health          10  — is the dependency graph coherent?
  IQ-07  Embedding Coverage    10  — will retrieval actually find related chunks?
                                     (skipped/neutral when skip_qdrant=True)

Usage::

    from stage1_ingestion.quality_judge import IngestionQualityJudge

    judge  = IngestionQualityJudge()
    report = judge.score(qm, stats, chunks, dep_graph, skip_qdrant)

    if report.decision == "ABORT":
        sys.exit(1)
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Weights ───────────────────────────────────────────────────────────────────
_WEIGHTS = {
    "IQ-01": 20,
    "IQ-02": 10,
    "IQ-03": 20,
    "IQ-04": 10,
    "IQ-05": 20,
    "IQ-06": 10,
    "IQ-07": 10,
}

# Scores per status
_STATUS_SCORE = {"PASS": 100, "WARN": 50, "FAIL": 0, "SKIP": 100}

# ── Language-aware threshold table ────────────────────────────────────────────
# Format per dimension:
#   IQ-01 … IQ-05, IQ-07 → (pass_threshold, warn_threshold)
#   IQ-06               → (max_cycles: int, max_orphan_rate: float)
#
# Direction (higher/lower is better) is fixed per dimension in each scorer.
# "default" applies when no language-specific entry exists.
_LANG_THRESHOLDS: Dict[str, Dict[str, tuple]] = {

    # IQ-01  Parse Coverage  (higher is better)
    # Relaxed for generated-file-heavy stacks (Dart, JS).
    "IQ-01": {
        "default":    (0.80, 0.60),
        "dart":       (0.68, 0.48),  # *.g.dart / *.freezed.dart inflate parseable count
        "javascript": (0.68, 0.48),  # many auto-generated / config files
        "typescript": (0.73, 0.53),
    },

    # IQ-02  Symbol Density  sym/file  (higher is better)
    # Java/Swift one-class-per-file and Flutter single-widget files lower density naturally.
    "IQ-02": {
        "default":    (5.0,  2.0),
        "dart":       (3.0,  1.5),   # one widget class + one or two helpers per file
        "java":       (3.0,  1.5),   # single public class per file convention
        "kotlin":     (4.0,  2.0),   # data classes add one symbol each
        "swift":      (4.0,  2.0),
        "javascript": (4.0,  1.5),
        "typescript": (4.0,  1.5),
        "python":     (5.0,  2.0),   # same as default — modules commonly have many fns
        "rust":       (6.0,  3.0),   # Rust modules tend to be function-dense
    },

    # IQ-03  Call Resolution Rate  (higher is better)
    # Dynamically typed languages resolve fewer calls — lower the bar accordingly.
    "IQ-03": {
        "default":    (0.50, 0.30),
        "kotlin":     (0.60, 0.40),  # strong typing + explicit imports
        "java":       (0.60, 0.40),
        "dart":       (0.55, 0.35),  # strong typing but many generated stubs
        "swift":      (0.55, 0.35),
        "rust":       (0.60, 0.40),  # explicit trait impls are easy to resolve
        "python":     (0.42, 0.22),  # dynamic — duck typing hides call targets
        "javascript": (0.32, 0.18),  # very dynamic; prototype chains invisible to static analysis
        "typescript": (0.44, 0.24),  # better than JS but still dynamic at runtime
    },

    # IQ-04  Unknown layer rate  (lower is better — unknown% should be small)
    # Feature-based / mixed structures (Dart, JS) leave more files unclassifiable.
    "IQ-04": {
        "default":    (0.20, 0.40),
        "dart":       (0.30, 0.52),  # feature-first structure often defeats layer heuristics
        "javascript": (0.35, 0.55),  # flat src/ folders are common
        "typescript": (0.30, 0.50),
        "python":     (0.25, 0.45),  # scripts / utils blur layers
    },

    # IQ-05  p95 chunk size in lines  (lower is better)
    # Java is legitimately verbose; Dart/TS favour short methods.
    "IQ-05": {
        "default":    (150, 250),
        "dart":       (120, 200),
        "kotlin":     (130, 220),
        "java":       (200, 350),   # getters/setters + boilerplate inflate size
        "swift":      (130, 220),
        "python":     (120, 200),
        "javascript": (100, 180),
        "typescript": (100, 180),
        "rust":       (150, 260),   # match arms can be long
    },

    # IQ-06  Graph Health  (max_cycles, max_orphan_rate)
    # UI-framework repos (Flutter, SwiftUI) have many standalone widget nodes
    # that have no callers by design, so raise the orphan tolerance.
    "IQ-06": {
        "default":    (5,  0.10),
        "dart":       (5,  0.18),   # Flutter widgets are often root nodes
        "kotlin":     (5,  0.15),   # sealed class leaves and data objects
        "swift":      (5,  0.15),   # SwiftUI views are leaf nodes
        "javascript": (8,  0.12),   # module graphs are naturally cyclier in JS
        "typescript": (6,  0.12),
    },

    # IQ-07  Embedding Coverage  (higher is better)
    # Process-quality signal — not language-specific.  No overrides.
}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DimensionResult:
    id:              str    # "IQ-01" … "IQ-07"
    name:            str
    status:          str    # "PASS" | "WARN" | "FAIL" | "SKIP"
    score:           int    # 0, 50, or 100
    value:           float  # raw measured value
    threshold_pass:  float  # value that earns PASS (language-adjusted)
    threshold_warn:  float  # value that earns WARN (language-adjusted)
    unit:            str    # display unit, e.g. "%" or "lines" or "sym/file"
    message:         str    # one-line human summary e.g. "72% of files parsed"
    fix_hint:        str    # actionable next step


@dataclass
class IngestionQualityReport:
    overall_score:    int                   # 0–100 weighted average
    decision:         str                   # "PROCEED" | "WARN" | "ABORT"
    dimensions:       List[DimensionResult]
    top_fix_hint:     str                   # hint from the worst-scoring dimension
    skip_qdrant:      bool
    primary_language: str = "unknown"       # language thresholds were applied for
    generated_at:     str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall_score":    self.overall_score,
            "decision":         self.decision,
            "top_fix_hint":     self.top_fix_hint,
            "skip_qdrant":      self.skip_qdrant,
            "primary_language": self.primary_language,
            "generated_at":     self.generated_at,
            "dimensions": [
                {
                    "id":             d.id,
                    "name":           d.name,
                    "status":         d.status,
                    "score":          d.score,
                    "value":          round(d.value, 4),
                    "threshold_pass": d.threshold_pass,
                    "threshold_warn": d.threshold_warn,
                    "unit":           d.unit,
                    "message":        d.message,
                    "fix_hint":       d.fix_hint,
                }
                for d in self.dimensions
            ],
        }


# ── Judge ─────────────────────────────────────────────────────────────────────

class IngestionQualityJudge:
    """
    Stateless, language-aware scorer.  Call .score() after the pipeline completes.
    """

    def score(
        self,
        qm:          Any,           # QualityMetrics from core.models
        stats:       Dict,
        chunks:      List,          # List[CodeChunk]
        dep_graph:   Any,           # DependencyGraph
        skip_qdrant: bool = False,
    ) -> IngestionQualityReport:

        language = _resolve_language(stats)

        dims = [
            self._iq01_parse_coverage(qm, stats, language),
            self._iq02_symbol_density(qm, stats, language),
            self._iq03_call_resolution(qm, language),
            self._iq04_layer_clarity(stats, language),
            self._iq05_chunk_size(chunks, language),
            self._iq06_graph_health(stats, dep_graph, language),
            self._iq07_embedding_coverage(chunks, skip_qdrant),
        ]

        # Weighted average — SKIP dimensions contribute at 100 (neutral)
        total_weight = sum(_WEIGHTS[d.id] for d in dims)
        weighted_sum = sum(_WEIGHTS[d.id] * d.score for d in dims)
        overall = round(weighted_sum / total_weight) if total_weight else 0

        if overall >= 70:
            decision = "PROCEED"
        elif overall >= 50:
            decision = "WARN"
        else:
            decision = "ABORT"

        # Top fix hint = hint from the lowest-scoring non-SKIP dimension
        worst = min(
            (d for d in dims if d.status != "SKIP"),
            key=lambda d: d.score,
            default=None,
        )
        top_fix = worst.fix_hint if worst else ""

        report = IngestionQualityReport(
            overall_score    = overall,
            decision         = decision,
            dimensions       = dims,
            top_fix_hint     = top_fix,
            skip_qdrant      = skip_qdrant,
            primary_language = language,
        )

        self._log(report)
        return report

    # ── Dimension scorers ─────────────────────────────────────────────────────

    def _iq01_parse_coverage(self, qm, stats, language: str) -> DimensionResult:
        """% of is_parseable files that produced at least one symbol."""
        p, w      = _get_thresholds("IQ-01", language)
        parseable = max(qm.parseable_files, 1)
        value     = qm.parse_success_count / parseable

        status, score = _threshold(value, pass_=p, warn_=w, higher_is_better=True)
        return DimensionResult(
            id             = "IQ-01",
            name           = "Parse Coverage",
            status         = status,
            score          = score,
            value          = value,
            threshold_pass = p,
            threshold_warn = w,
            unit           = "%",
            message        = (
                f"{qm.parse_success_count}/{parseable} files yielded symbols "
                f"({value*100:.0f}%)"
                + (f"  [threshold: ≥{p*100:.0f}%]" if _lang_overridden("IQ-01", language) else "")
            ),
            fix_hint       = (
                "Install tree-sitter-languages for full grammar support: "
                "`pip install tree-sitter-languages`"
            ),
        )

    def _iq02_symbol_density(self, qm, stats, language: str) -> DimensionResult:
        """Average symbols per parseable file — granularity proxy."""
        p, w  = _get_thresholds("IQ-02", language)
        value = qm.parse_symbol_rate   # symbols / parseable_files

        status, score = _threshold(value, pass_=p, warn_=w, higher_is_better=True)
        return DimensionResult(
            id             = "IQ-02",
            name           = "Symbol Density",
            status         = status,
            score          = score,
            value          = value,
            threshold_pass = p,
            threshold_warn = w,
            unit           = "sym/file",
            message        = (
                f"{qm.total_symbols_extracted} symbols across "
                f"{qm.parseable_files} files ({value:.1f} sym/file)"
                + (f"  [threshold: ≥{p:.1f}]" if _lang_overridden("IQ-02", language) else "")
            ),
            fix_hint       = (
                "Check parser regex patterns — low density means only class "
                "declarations are found, not methods/functions"
            ),
        )

    def _iq03_call_resolution(self, qm, language: str) -> DimensionResult:
        """Fraction of call references resolved to a local chunk."""
        p, w  = _get_thresholds("IQ-03", language)
        value = qm.dep_resolution_rate  # resolved_calls / total_raw_calls

        status, score = _threshold(value, pass_=p, warn_=w, higher_is_better=True)
        return DimensionResult(
            id             = "IQ-03",
            name           = "Call Resolution Rate",
            status         = status,
            score          = score,
            value          = value,
            threshold_pass = p,
            threshold_warn = w,
            unit           = "%",
            message        = (
                f"{qm.resolved_calls}/{qm.total_raw_calls} calls resolved "
                f"({value*100:.0f}%) · "
                f"{qm.low_confidence_edges} low-confidence edges"
                + (f"  [threshold: ≥{p*100:.0f}%]" if _lang_overridden("IQ-03", language) else "")
            ),
            fix_hint       = (
                "Ensure all sub-packages are included in the repo scan; "
                "check call_resolver.py for missing import alias patterns"
            ),
        )

    def _iq04_layer_clarity(self, stats, language: str) -> DimensionResult:
        """Fraction of files with an unknown architectural layer."""
        p, w       = _get_thresholds("IQ-04", language)
        layer_dist = stats.get("layer_distribution", {})
        total      = max(sum(layer_dist.values()), 1)
        unknown    = layer_dist.get("unknown", 0)
        value      = unknown / total   # unknown rate — lower is better

        status, score = _threshold(value, pass_=p, warn_=w, higher_is_better=False)
        return DimensionResult(
            id             = "IQ-04",
            name           = "Layer Clarity",
            status         = status,
            score          = score,
            value          = value,
            threshold_pass = p,
            threshold_warn = w,
            unit           = "% unknown",
            message        = (
                f"{unknown}/{total} files unclassified ({value*100:.0f}% unknown) "
                f"· layers: {_fmt_dist(layer_dist)}"
                + (f"  [threshold: <{p*100:.0f}%]" if _lang_overridden("IQ-04", language) else "")
            ),
            fix_hint       = (
                "Add project-specific path patterns to "
                "stage1_ingestion/analyzers/layer_classifier.py"
            ),
        )

    def _iq05_chunk_size(self, chunks, language: str) -> DimensionResult:
        """p95 chunk size in lines — proxy for LLM truncation risk."""
        p, w = _get_thresholds("IQ-05", language)

        from core.models import ChunkType
        sizes = [
            len(c.content.splitlines())
            for c in chunks
            if c.chunk_type not in (ChunkType.MODULE,)
        ]
        if not sizes:
            return DimensionResult(
                id="IQ-05", name="Chunk Size Health", status="SKIP",
                score=100, value=0, threshold_pass=p, threshold_warn=w,
                unit="lines (p95)", message="No non-MODULE chunks to measure",
                fix_hint="",
            )

        p95   = statistics.quantiles(sizes, n=100)[94] if len(sizes) >= 2 else sizes[0]
        value = float(p95)

        status, score = _threshold(value, pass_=p, warn_=w, higher_is_better=False)
        return DimensionResult(
            id             = "IQ-05",
            name           = "Chunk Size Health",
            status         = status,
            score          = score,
            value          = value,
            threshold_pass = p,
            threshold_warn = w,
            unit           = "lines (p95)",
            message        = (
                f"p95={p95:.0f} lines across {len(sizes)} chunks "
                f"(min={min(sizes)}, max={max(sizes)}, "
                f"median={statistics.median(sizes):.0f})"
                + (f"  [threshold: <{p:.0f} lines]" if _lang_overridden("IQ-05", language) else "")
            ),
            fix_hint       = (
                "Lower MAX_FUNC_LINES in "
                "stage1_ingestion/symbol_boundary_resolver.py (currently 100)"
            ),
        )

    def _iq06_graph_health(self, stats, dep_graph, language: str) -> DimensionResult:
        """Cycle count + orphan rate — graph coherence proxy."""
        max_cycles, max_orphan_rate = _get_thresholds("IQ-06", language)

        cycles_found = stats.get("cycles_found", 0)
        orphans      = stats.get("orphans_found", 0)
        try:
            total_nodes = dep_graph.graph.number_of_nodes()
        except Exception:
            total_nodes = max(orphans, 1)

        orphan_rate = orphans / max(total_nodes, 1)

        cycle_ok  = cycles_found < max_cycles
        orphan_ok = orphan_rate  < max_orphan_rate

        if cycle_ok and orphan_ok:
            status, score = "PASS", 100
        elif cycle_ok or orphan_ok:
            status, score = "WARN", 50
        else:
            status, score = "FAIL", 0

        value = orphan_rate
        return DimensionResult(
            id             = "IQ-06",
            name           = "Graph Health",
            status         = status,
            score          = score,
            value          = value,
            threshold_pass = max_orphan_rate,
            threshold_warn = max_orphan_rate * 2,
            unit           = "orphan rate",
            message        = (
                f"{cycles_found} cycle(s) · "
                f"{orphans}/{total_nodes} orphan nodes "
                f"({orphan_rate*100:.0f}% orphan rate)"
                + (
                    f"  [thresholds: cycles<{max_cycles}, orphans<{max_orphan_rate*100:.0f}%]"
                    if _lang_overridden("IQ-06", language) else ""
                )
            ),
            fix_hint       = (
                "High orphan rate → check DependencyExtractor resolve logic; "
                "many cycles → review ARCH001 violations in dependency_graph.json"
            ),
        )

    def _iq07_embedding_coverage(self, chunks, skip_qdrant: bool) -> DimensionResult:
        """Fraction of chunks with a non-null embedding vector."""
        if skip_qdrant:
            return DimensionResult(
                id="IQ-07", name="Embedding Coverage", status="SKIP",
                score=100, value=1.0, threshold_pass=0.95, threshold_warn=0.80,
                unit="%", message="Skipped (skip_qdrant=True)",
                fix_hint="",
            )

        total    = max(len(chunks), 1)
        embedded = sum(1 for c in chunks if c.embedding is not None)
        value    = embedded / total

        status, score = _threshold(value, pass_=0.95, warn_=0.80, higher_is_better=True)
        return DimensionResult(
            id             = "IQ-07",
            name           = "Embedding Coverage",
            status         = status,
            score          = score,
            value          = value,
            threshold_pass = 0.95,
            threshold_warn = 0.80,
            unit           = "%",
            message        = f"{embedded}/{total} chunks have embeddings ({value*100:.0f}%)",
            fix_hint       = (
                "Check EmbeddingTool logs for rate-limit or timeout failures; "
                "retry with --skip-summaries to isolate the issue"
            ),
        )

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, report: IngestionQualityReport) -> None:
        dec_symbol = {"PROCEED": "✓", "WARN": "!", "ABORT": "✗"}[report.decision]
        lines = [
            "",
            "── Ingestion Quality Judge ──────────────────────────────────",
            f"  Language      : {report.primary_language}",
        ]
        for d in report.dimensions:
            bar = "●" * (d.score // 10) + "○" * (10 - d.score // 10)
            lines.append(
                f"  {d.id}  {d.status:<4}  [{bar}]  {d.message}"
            )
        lines += [
            f"  Overall score : {report.overall_score}/100",
            f"  Decision      : {dec_symbol} {report.decision}",
        ]
        if report.decision != "PROCEED":
            lines.append(f"  Top fix       : {report.top_fix_hint}")
        lines.append("─────────────────────────────────────────────────────────────")
        for line in lines:
            logger.info(line)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_language(stats: Dict) -> str:
    """
    Infer the primary language from ingestion_stats["language_breakdown"].

    Returns the lowercase name of the most-common language, or "default" when
    no breakdown is available.
    """
    breakdown = stats.get("language_breakdown", {})
    if not breakdown:
        return "default"
    primary = max(breakdown, key=lambda k: breakdown[k])
    return primary.lower()


def _get_thresholds(dim_id: str, language: str) -> tuple:
    """
    Look up (pass_threshold, warn_threshold) for a dimension + language pair.

    Falls back to the "default" entry if no language-specific override exists.
    For IQ-06 the returned tuple is (max_cycles, max_orphan_rate).
    """
    dim_table = _LANG_THRESHOLDS.get(dim_id, {})
    return dim_table.get(language) or dim_table.get("default", (0.80, 0.60))


def _lang_overridden(dim_id: str, language: str) -> bool:
    """True when a language-specific threshold (not the default) is active."""
    dim_table = _LANG_THRESHOLDS.get(dim_id, {})
    return language in dim_table and language != "default"


def _threshold(
    value:            float,
    pass_:            float,
    warn_:            float,
    higher_is_better: bool,
) -> Tuple[str, int]:
    """Return (status, score) given a value and directional thresholds."""
    if higher_is_better:
        if value >= pass_:
            return "PASS", 100
        if value >= warn_:
            return "WARN", 50
        return "FAIL", 0
    else:
        if value < pass_:
            return "PASS", 100
        if value < warn_:
            return "WARN", 50
        return "FAIL", 0


def _fmt_dist(dist: Dict[str, int], top: int = 3) -> str:
    """'presentation:45 domain:30 unknown:20'"""
    top_items = sorted(dist.items(), key=lambda x: -x[1])[:top]
    return " ".join(f"{k}:{v}" for k, v in top_items)
