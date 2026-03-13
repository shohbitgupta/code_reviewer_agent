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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ingestion.chunker import HierarchicalChunkBuilder
from ingestion.dependency_extractor import DependencyExtractor
from ingestion.file_filter import FileFilter
from ingestion.file_parser import FileParser
from ingestion.graph_builder import DependencyGraph
from ingestion.language_detector import LanguageDetector
from ingestion.models import CodeChunk, FileMeta, WorkspaceLayout
from ingestion.repo_scanner import RepoScanner
from ingestion.summary_generator import SummaryGenerator
from ingestion.workspace import WorkspaceManager
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


class IngestionPipeline:
    """
    Runs all 11 ingestion steps in order and returns IngestionPipelineResult.

    Args:
        repo_url:  Remote Git URL.
        repo_name: Optional name override; derived from URL slug if None.
        run_id:    Optional run_id; auto-generated from timestamp + SHA if None.
    """

    def __init__(
        self,
        repo_url:  str,
        repo_name: Optional[str] = None,
        run_id:    Optional[str] = None,
    ):
        self.repo_url  = repo_url
        self._repo_name = repo_name
        self._run_id    = run_id

    # ── Public entry point ────────────────────────────────────────────────────

    def run(
        self,
        embed_tool:   Optional[EmbeddingTool]  = None,
        qdrant_tool:  Optional[QdrantTool]     = None,
        llm_client                             = None,
        skip_summaries: bool                   = False,
        skip_qdrant:    bool                   = False,
    ) -> IngestionPipelineResult:
        """
        Execute the full ingestion pipeline.

        Args:
            embed_tool:      EmbeddingTool instance.  Created from config if None.
            qdrant_tool:     QdrantTool instance.  Created from config if None.
            llm_client:      anthropic.AsyncAnthropic client for Step 1h.
            skip_summaries:  Set True to skip Step 1h (no LLM calls).
            skip_qdrant:     Set True to skip Step 1k (no Qdrant upsert).

        Returns:
            IngestionPipelineResult
        """
        pipeline_start = time.monotonic()

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
        parser       = FileParser()
        parsed_files = parser.parse_many(file_metas, cache_dir=layout.parsed_dir)

        # ── Step 1g: Chunking ─────────────────────────────────────────────────
        logger.info("=== Step 1g: Chunking ===")
        builder    = HierarchicalChunkBuilder(repo_name=repo_name)
        chunks     = builder.chunk_many(
            parsed_files,
            jsonl_path=layout.chunks_dir / "chunks.jsonl",
        )

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
        edges = extractor.extract(parsed_files, chunk_map)

        # ── Step 1j: Dependency Graph Building ────────────────────────────────
        logger.info("=== Step 1j: Graph Building ===")
        dep_graph = DependencyGraph()
        dep_graph.build(chunks, edges)
        dep_graph.analyse()
        dep_graph.save(layout.graphs_dir / "dependency_graph.json")

        # ── Step 1k: Vector Upsert ────────────────────────────────────────────
        if not skip_qdrant:
            logger.info("=== Step 1k: Vector Upsert ===")
            qdrant_tool.ensure_collection()
            qdrant_tool.upsert_chunks(chunks)
        else:
            logger.info("=== Step 1k: Skipped (skip_qdrant=True) ===")

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
            "qdrant_points_upserted":  len(chunks) if not skip_qdrant else 0,
            "duration_seconds":        round(duration, 2),
        }

        logger.info(
            "=== Ingestion complete in %.1fs: %d chunks, %d edges ===",
            duration, len(chunks), len(edges),
        )

        return IngestionPipelineResult(
            local_repo_path  = clone_result.local_repo_path,
            file_metas       = file_metas,
            chunks           = chunks,
            chunk_map        = chunk_map,
            dependency_graph = dep_graph,
            layout           = layout,
            stats            = stats,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _edge_type_counts(edges) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for e in edges:
        key = e.edge_type.value
        counts[key] = counts.get(key, 0) + 1
    return counts
