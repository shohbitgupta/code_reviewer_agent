"""
Ingestion Pipeline — orchestrates Steps 1a → 1k in strict sequence.

Each step depends on the output of its predecessor (see SKILL_v1.md dependency
table).  No step begins before its predecessor completes.

The pipeline produces an IngestionPipelineResult that is written into the
ReviewState by agents/ingestion_agent.py.

Usage:
    pipeline = IngestionPipeline(repo_url="https://github.com/owner/repo")
    result   = pipeline.run(qdrant_tool=qdrant_tool, embed_tool=embed_tool)
"""

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from stage1_ingestion.chunker import HierarchicalChunkBuilder
from stage1_ingestion.dependency_extractor import DependencyExtractor
from stage1_ingestion.file_filter import FileFilter
from stage1_ingestion.file_parser import FileParser
from stage1_ingestion.graph_builder import DependencyGraph
from stage1_ingestion.language_detector import LanguageDetector
from core.models import ChunkType, CodeChunk, FileMeta, QualityMetrics, WorkspaceLayout
from stage1_ingestion.repo_scanner import RepoScanner
from stage1_ingestion.summary_generator import SummaryGenerator
from stage1_ingestion.workspace import WorkspaceManager
from tools.embedding_tool import EmbeddingTool
from tools.git_tool import GitExecutor
from tools.qdrant_tool import QdrantTool

logger = logging.getLogger(__name__)


@dataclass
class IngestionPipelineResult:
    """All outputs written into ReviewState after a successful pipeline run."""
    local_repo_path:  str
    file_metas:       List[FileMeta]
    chunks:           List[CodeChunk]
    chunk_map:        Dict[str, CodeChunk]   # chunk_id → CodeChunk
    dependency_graph: DependencyGraph
    layout:           WorkspaceLayout
    stats:            Dict = field(default_factory=dict)
    symbol_table:     object = None          # ProjectSymbolTable
    analysis_result:  object = None          # AnalysisResult
    quality_metrics:  Optional[QualityMetrics] = None
    # Priority 1 — diff-aware ingestion
    changed_chunk_ids: Optional[set] = None  # chunk_ids from changed files; None = full run
    # Priority 3 — hybrid retrieval
    bm25_index_path:  Optional[str] = None   # path to persisted BM25 index
    # Step 1k+2 — Quality Judge report
    quality_report:   object        = None   # IngestionQualityReport


class IngestionPipeline:
    """
    Runs all 11 ingestion steps in order and returns IngestionPipelineResult.

    Args:
        repo_url:  Remote Git URL.
        repo_name: Optional name override; derived from URL slug if None.
        run_id:    Optional run_id; auto-generated from timestamp + SHA if None.
        base_sha:  Base commit SHA for diff-aware incremental ingestion (Priority 1).
                   When provided together with head_sha, only changed files are
                   re-parsed and re-chunked; unchanged chunks stay in Qdrant.
        head_sha:  Head commit SHA for diff-aware incremental ingestion (Priority 1).
    """

    def __init__(
        self,
        repo_url:  str,
        repo_name: Optional[str] = None,
        run_id:    Optional[str] = None,
        base_sha:  Optional[str] = None,
        head_sha:  Optional[str] = None,
    ):
        self.repo_url   = repo_url
        self._repo_name = repo_name
        self._run_id    = run_id
        self._base_sha  = base_sha
        self._head_sha  = head_sha

    # ── Public entry point ────────────────────────────────────────────────────

    def run(
        self,
        embed_tool:     Optional[EmbeddingTool] = None,
        qdrant_tool:    Optional[QdrantTool]    = None,
        llm_client                              = None,
        skip_summaries: bool                    = False,
        skip_qdrant:    bool                    = False,
        max_workers:    Optional[int]           = None,
    ) -> IngestionPipelineResult:
        """
        Execute the full ingestion pipeline.

        Args:
            embed_tool:      EmbeddingTool instance.  Created from config if None.
            qdrant_tool:     QdrantTool instance.  Created from config if None.
            llm_client:      anthropic.AsyncAnthropic client for Step 1h.
            skip_summaries:  Set True to skip Step 1h (no LLM calls).
            skip_qdrant:     Set True to skip Step 1k (no Qdrant upsert).
            max_workers:     Parallel thread count for file-level steps (1f,
                             1f-SBR, 1g).  None = auto-size; 1 = serial.

        Returns:
            IngestionPipelineResult
        """
        pipeline_start = time.monotonic()
        step_times: Dict = {}

        def _t(name: str, start: float) -> None:
            step_times[name] = round(time.monotonic() - start, 3)

        # ── Lazy-create tools if not injected ────────────────────────────────
        if embed_tool is None:
            embed_tool = EmbeddingTool()
        if qdrant_tool is None and not skip_qdrant:
            qdrant_tool = QdrantTool(embed_tool=embed_tool)

        # ── Step 1a: Fetch Repo ───────────────────────────────────────────────
        logger.info("=== Step 1a: Fetch Repo ===")
        executor     = GitExecutor(self.repo_url)
        clone_result = executor.clone_or_pull()
        repo_name    = self._repo_name or clone_result.repo_name

        # ── Priority 1: Diff-aware changed-file detection ─────────────────────
        # When base_sha + head_sha are provided, resolve which files changed so
        # that later steps can skip re-parsing unchanged files.
        _changed_files: Optional[set] = None   # relative paths, or None = full run
        if self._base_sha and self._head_sha:
            try:
                changed_paths = executor.get_changed_files(self._base_sha, self._head_sha)
                _changed_files = set(changed_paths)
                logger.info(
                    "[Pipeline] Diff-aware mode: %d changed files between %s..%s",
                    len(_changed_files), self._base_sha[:8], self._head_sha[:8],
                )
            except Exception as exc:
                logger.warning(
                    "[Pipeline] get_changed_files failed (%s) — falling back to full run", exc
                )

        # ── Step 1b: Workspace ────────────────────────────────────────────────
        logger.info("=== Step 1b: Workspace ===")
        ws = WorkspaceManager(
            repo_name        = repo_name,
            local_repo_path  = clone_result.local_repo_path,
            commit_sha       = clone_result.commit_sha,
            run_id           = self._run_id,
            repo_url         = self.repo_url,
        )
        layout = ws.setup()

        # ── Step 1c: Repo Scanner ─────────────────────────────────────────────
        logger.info("=== Step 1c: Repo Scanner ===")
        scanner   = RepoScanner(raw_dir=layout.raw_dir, repo_name=repo_name)
        inventory = scanner.scan()

        # ── Step 1d: Language Detection ───────────────────────────────────────
        logger.info("=== Step 1d: Language Detection ===")
        detector    = LanguageDetector()
        lang_result = detector.detect(inventory)

        # ── Step 1e: File Filter ──────────────────────────────────────────────
        logger.info("=== Step 1e: File Filter ===")
        ff         = FileFilter(inventory, lang_result, repo_name)
        file_metas = ff.run()

        # ── Step 1f: File Parser ──────────────────────────────────────────────
        logger.info("=== Step 1f: File Parser ===")
        _s = time.monotonic()
        parser = FileParser()

        # Priority 1: invalidate parse cache for changed files so they are
        # re-parsed from disk rather than served from a stale cached result.
        if _changed_files and layout.parsed_dir.exists():
            import hashlib
            for fm in file_metas:
                if fm.file_path in _changed_files:
                    digest = hashlib.md5(fm.absolute_path.encode()).hexdigest()[:16]
                    cache_file = layout.parsed_dir / f"{digest}.json"
                    if cache_file.exists():
                        cache_file.unlink()
                        logger.debug("[Pipeline] Invalidated parse cache for %s", fm.file_path)

        parsed_files = parser.parse_many(
            file_metas, cache_dir=layout.parsed_dir, max_workers=max_workers
        )
        _t("1f_parse", _s)

        # ── Step 1f-ST: Project Symbol Table ──────────────────────────────────
        logger.info("=== Step 1f-ST: Project Symbol Table ===")
        _s = time.monotonic()
        from stage1_ingestion.symbol_table import ProjectSymbolTable
        from stage1_ingestion.language_analyzer import LanguageAnalyzerOrchestrator

        symbol_table = ProjectSymbolTable()
        symbol_table.build(parsed_files)
        logger.info("[SymbolTable] %d symbols indexed", len(symbol_table))
        _t("1f_st", _s)

        # ── Step 1f-LA: Language Analyzer (Tier 1) ────────────────────────────
        logger.info("=== Step 1f-LA: Language Analyzer ===")
        _s = time.monotonic()
        orchestrator    = LanguageAnalyzerOrchestrator(symbol_table=symbol_table)
        analysis_result = orchestrator.analyze(parsed_files)
        _t("1f_la", _s)

        # ── Step 1f-SBR: Symbol Boundary Resolver ────────────────────────────
        logger.info("=== Step 1f-SBR: Symbol Boundary Resolver ===")
        _s = time.monotonic()
        from stage1_ingestion.symbol_boundary_resolver import SymbolBoundaryResolver
        sbr          = SymbolBoundaryResolver()
        parsed_files = sbr.resolve(parsed_files, max_workers=max_workers)
        _t("1f_sbr", _s)

        # ── Step 1g: Chunking ─────────────────────────────────────────────────
        logger.info("=== Step 1g: Chunking ===")
        _s = time.monotonic()
        builder    = HierarchicalChunkBuilder(repo_name=repo_name)
        chunks     = builder.chunk_many(
            parsed_files,
            jsonl_path=layout.chunks_dir / "chunks.jsonl",
            analysis_result=analysis_result,
            max_workers=max_workers,
        )
        _t("1g_chunk", _s)

        # chunk_map keyed by UUID — used for O(1) lookup by ID
        chunk_map: Dict[str, CodeChunk] = {c.chunk_id: c for c in chunks}
        # Secondary key "file_path::symbol_name" — used by DependencyExtractor.
        # NOTE: when two chunks share the same symbol_name in the same file
        # (e.g. overloaded methods), last-write-wins. This is acceptable for
        # BELONGS_TO resolution; CALLS uses the by_name index instead.
        for c in chunks:
            key = f"{c.file_path}::{c.symbol_name}"
            if key not in chunk_map:           # first definition wins
                chunk_map[key] = c

        # Fill chunk_ids on symbol table entries (needed for Resolver to link calls)
        symbol_table.link_chunks(chunks)

        # ── Priority 5: Propagate multi-label layers onto chunks ──────────────
        # analysis_result.layer_map values are now List[str] (not str).
        # Primary label already on chunk.layer; write full list to chunk.layers.
        if analysis_result and analysis_result.layer_map:
            for chunk in chunks:
                labels = analysis_result.layer_map.get(chunk.file_path)
                if labels and isinstance(labels, list):
                    chunk.layers = labels
                    chunk.layer  = labels[0]  # primary label

        # ── Priority 2: Mechanical rule pre-pass ──────────────────────────────
        logger.info("=== Step 1g-RC: Mechanical Rule Checker ===")
        _s = time.monotonic()
        from stage1_ingestion.rule_checker import MechanicalRuleChecker
        checker = MechanicalRuleChecker()
        flagged_count = checker.check_many(chunks)
        _t("1g_rc", _s)
        logger.info(
            "[RuleChecker] %d/%d chunks pre-flagged with mechanical violations",
            flagged_count, len(chunks),
        )

        # ── Priority 1: Track changed_chunk_ids ───────────────────────────────
        changed_chunk_ids: Optional[set] = None
        if _changed_files is not None:
            changed_chunk_ids = {
                c.chunk_id for c in chunks
                if c.file_path in _changed_files
            }
            logger.info(
                "[Pipeline] %d chunks belong to %d changed files",
                len(changed_chunk_ids), len(_changed_files),
            )

        # ── Step 1h: Metadata Extraction (summaries) ──────────────────────────
        if not skip_summaries:
            logger.info("=== Step 1h: Metadata Extraction ===")
            generator = SummaryGenerator(
                embed_tool = embed_tool,
                llm_client = llm_client,
            )
            chunks = generator.run(chunks)
        else:
            logger.info("=== Step 1h: Skipped (skip_summaries=True) ===")
            for c in chunks:
                if c.summary is None:
                    c.summary = ""

        # ── Step 1i: Dependency Extraction ────────────────────────────────────
        logger.info("=== Step 1i: Dependency Extraction ===")
        extractor = DependencyExtractor(
            repo_root=clone_result.local_repo_path
        )
        edges = extractor.extract(parsed_files, chunk_map, analysis_result=analysis_result)

        # ── Step 1j: Dependency Graph Building ────────────────────────────────
        logger.info("=== Step 1j: Graph Building ===")
        dep_graph = DependencyGraph()
        dep_graph.build(chunks, edges)
        dep_graph.analyse()
        dep_graph.save(layout.graphs_dir / "dependency_graph.json")

        # ── Step 1k: Vector Upsert (O4 — incremental) ────────────────────────
        to_upsert      = []
        skipped_upsert = 0
        if not skip_qdrant:
            logger.info("=== Step 1k: Vector Upsert (incremental) ===")
            qdrant_tool.ensure_collection()

            # O4: fetch existing content hashes to skip unchanged chunks
            existing_hashes = qdrant_tool.get_existing_content_hashes(
                [c.chunk_id for c in chunks]
            )
            to_upsert = [
                c for c in chunks
                if existing_hashes.get(c.chunk_id) != c.content_hash
            ]
            skipped_upsert = len(chunks) - len(to_upsert)
            if skipped_upsert:
                logger.info(
                    "[Pipeline] Step 1k: %d/%d chunks skipped (unchanged in Qdrant), "
                    "upserting %d new/modified",
                    skipped_upsert, len(chunks), len(to_upsert),
                )

            if to_upsert:
                qdrant_tool.upsert_chunks(to_upsert)
            else:
                logger.info("[Pipeline] Step 1k: all chunks up to date — no upsert needed")
        else:
            logger.info("=== Step 1k: Skipped (skip_qdrant=True) ===")

        # ── Priority 3: Build BM25 sparse index ──────────────────────────────
        logger.info("=== Step 1k+1: BM25 Index Build ===")
        _s = time.monotonic()
        bm25_index_path: Optional[str] = None
        try:
            from tools.bm25_tool import BM25Index
            bm25_index = BM25Index()
            bm25_index.build(chunks)
            if len(bm25_index) > 0:
                _bm25_path = layout.graphs_dir / "bm25_index.pkl"
                bm25_index.save(_bm25_path)
                bm25_index_path = str(_bm25_path)
        except Exception as exc:
            logger.warning("[Pipeline] BM25 index build failed: %s", exc)
        _t("1k1_bm25", _s)

        # ── Build stats ───────────────────────────────────────────────────────
        duration = time.monotonic() - pipeline_start
        graph_stats = dep_graph.get_stats()
        chunk_type_counts: Dict[str, int] = {}
        for c in chunks:
            key = c.chunk_type.value
            chunk_type_counts[key] = chunk_type_counts.get(key, 0) + 1

        stats = {
            "run_id":                  layout.run_id,
            "repo_name":               repo_name,
            "commit_sha":              clone_result.commit_sha,
            "files_total":             inventory.total_files,
            "files_parseable":         sum(1 for f in file_metas if f.is_parseable),
            "files_skipped":           inventory.total_files - len(file_metas),
            "language_breakdown":      lang_result.language_stats,
            "chunks_total":            len(chunks),
            "chunks_by_type":          chunk_type_counts,
            "summaries_generated":     sum(1 for c in chunks if c.summary),
            "summaries_skipped":       sum(1 for c in chunks if not c.summary),
            "edges_total":             len(edges),
            "edges_by_type":           _edge_type_counts(edges),
            "cycles_found":            graph_stats["cycles_found"],
            "layer_violations":        graph_stats["layer_violations"],
            "orphans_found":           graph_stats["orphans_found"],
            "qdrant_points_upserted":  len(to_upsert) if not skip_qdrant else 0,
            "qdrant_points_skipped":   skipped_upsert if not skip_qdrant else 0,
            "duration_seconds":        round(duration, 2),
            "symbols_indexed":         len(symbol_table),
            "calls_resolved":          analysis_result.total_calls_resolved,
            "calls_external":          analysis_result.total_calls_external,
            "layer_distribution":      dict(Counter(
                                           labels[0]
                                           for labels in analysis_result.layer_map.values()
                                           if isinstance(labels, list) and labels
                                       )) if analysis_result.layer_map else {},
            "step_times_seconds":      step_times,
            "max_workers":             max_workers,
            # Priority 1
            "diff_aware_mode":         _changed_files is not None,
            "changed_files_count":     len(_changed_files) if _changed_files else 0,
            "changed_chunks_count":    len(changed_chunk_ids) if changed_chunk_ids else 0,
            # Priority 2
            "pre_flagged_chunks":      sum(1 for c in chunks if c.pre_flagged_violations),
            "pre_flagged_violations":  sum(len(c.pre_flagged_violations) for c in chunks),
            # Priority 3
            "bm25_index_built":        bm25_index_path is not None,
        }

        logger.info(
            "=== Ingestion complete in %.1fs: %d chunks, %d edges ===",
            duration, len(chunks), len(edges),
        )

        # ── Quality Metrics ───────────────────────────────────────────────────
        qm = _compute_quality_metrics(file_metas, parsed_files, chunks, edges)
        _print_quality_metrics(qm)

        # ── Step 1k+2: Ingestion Quality Judge ───────────────────────────────
        logger.info("=== Step 1k+2: Ingestion Quality Judge ===")
        from stage1_ingestion.quality_judge import IngestionQualityJudge
        quality_report = IngestionQualityJudge().score(
            qm          = qm,
            stats       = stats,
            chunks      = chunks,
            dep_graph   = dep_graph,
            skip_qdrant = skip_qdrant,
        )

        return IngestionPipelineResult(
            local_repo_path   = clone_result.local_repo_path,
            file_metas        = file_metas,
            chunks            = chunks,
            chunk_map         = chunk_map,
            dependency_graph  = dep_graph,
            layout            = layout,
            stats             = stats,
            symbol_table      = symbol_table,
            analysis_result   = analysis_result,
            quality_metrics   = qm,
            changed_chunk_ids = changed_chunk_ids,
            bm25_index_path   = bm25_index_path,
            quality_report    = quality_report,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _edge_type_counts(edges) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for e in edges:
        key = e.edge_type.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def _compute_quality_metrics(
    file_metas,
    parsed_files,
    chunks,
    edges,
) -> QualityMetrics:
    """Compute QualityMetrics from pipeline outputs."""
    from core.models import EdgeType

    qm = QualityMetrics()

    # ── Parsing metrics ───────────────────────────────────────────────────────
    qm.parseable_files        = sum(1 for f in file_metas if f.is_parseable)
    qm.parse_success_count    = sum(1 for pf in parsed_files if pf.parse_success)
    qm.total_symbols_extracted = sum(len(pf.symbols) for pf in parsed_files)
    qm.parse_symbol_rate      = (
        qm.total_symbols_extracted / qm.parseable_files
        if qm.parseable_files > 0 else 0.0
    )

    # ── Chunking metrics ──────────────────────────────────────────────────────
    qm.total_lines = sum(
        pf.file_meta.line_count for pf in parsed_files
    )
    covered_lines: set = set()
    gap_fill_count = 0
    for c in chunks:
        if c.chunk_type == ChunkType.MODULE:
            continue   # MODULE chunks intentionally partial — don't count
        if c.chunk_type == ChunkType.BLOCK and "_block_" in c.symbol_name:
            # Gap-fill or large-function blocks
            gap_fill_count += 1
        covered_lines.update(
            f"{c.file_path}:{ln}"
            for ln in range(c.start_line, c.end_line + 1)
        )
    qm.lines_covered_by_chunks = len(covered_lines)
    qm.gap_fills_count         = gap_fill_count
    qm.chunk_coverage_rate     = (
        qm.lines_covered_by_chunks / qm.total_lines
        if qm.total_lines > 0 else 0.0
    )

    # ── Dependency metrics ────────────────────────────────────────────────────
    qm.total_raw_calls = sum(
        len(sym.calls)
        for pf in parsed_files
        for sym in pf.symbols
        if sym.symbol_type in ("function", "method")
    )
    calls_edges = [e for e in edges if e.edge_type == EdgeType.CALLS]
    qm.resolved_calls       = len(calls_edges)
    qm.cross_file_resolved  = sum(1 for e in calls_edges if e.is_cross_file)
    qm.dep_resolution_rate  = (
        qm.resolved_calls / qm.total_raw_calls
        if qm.total_raw_calls > 0 else 0.0
    )
    qm.cross_file_dep_rate  = (
        qm.cross_file_resolved / qm.resolved_calls
        if qm.resolved_calls > 0 else 0.0
    )
    qm.low_confidence_edges = sum(1 for e in edges if e.confidence < 0.6)

    return qm


def _print_quality_metrics(qm: QualityMetrics) -> None:
    """Print a quality summary table to the logger (INFO level)."""

    def _pct(rate: float) -> str:
        return f"{rate * 100:.1f}%"

    def _flag(rate: float, target: float) -> str:
        return "✓" if rate >= target else "!"

    lines = [
        "",
        "── Quality Metrics ──────────────────────────────────────────",
        f"  Parse symbol rate    : {_pct(qm.parse_symbol_rate):>6}  {_flag(qm.parse_symbol_rate, 0.88)}  (target ≥ 88%)",
        f"  Chunk coverage       : {_pct(qm.chunk_coverage_rate):>6}  {_flag(qm.chunk_coverage_rate, 0.90)}  (target ≥ 90%)",
        f"  Dep resolution rate  : {_pct(qm.dep_resolution_rate):>6}  {_flag(qm.dep_resolution_rate, 0.80)}  (target ≥ 80%)",
        f"  Cross-file dep rate  : {_pct(qm.cross_file_dep_rate):>6}",
        f"  Low-confidence edges : {qm.low_confidence_edges}",
        f"  Gap-fill chunks      : {qm.gap_fills_count}",
        "─────────────────────────────────────────────────────────────",
    ]
    for line in lines:
        logger.info(line)
