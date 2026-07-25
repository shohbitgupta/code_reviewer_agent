"""
Ingestion Agent — entry point for Stage 1 of the code review pipeline.

Wraps IngestionPipeline and writes results into the shared ReviewState dict.
This is the interface the top-level orchestrator calls.

Usage:
    from stage1_ingestion.agent import run_ingestion

    state = run_ingestion(state, embed_tool=embed_tool, qdrant_tool=qdrant_tool)
    # state["chunks"], state["dependency_graph"], state["ingestion_stats"], …
"""

import logging
from typing import Any, Dict, Optional

from stage1_ingestion.pipeline import IngestionPipeline
from tools.embedding_tool import EmbeddingTool
from tools.qdrant_tool import QdrantTool

logger = logging.getLogger(__name__)


def run_ingestion(
    state:          Dict[str, Any],
    embed_tool:     Optional[EmbeddingTool] = None,
    qdrant_tool:    Optional[QdrantTool]    = None,
    llm_client                              = None,
    skip_summaries: bool                    = False,
    skip_qdrant:    bool                    = False,
) -> Dict[str, Any]:
    """
    Run the full ingestion pipeline and write results into ReviewState.

    Expected state inputs:
        state["repo_url"]   str  — remote GitHub URL
        state["repo_name"]  str  — optional name override
        state["run_id"]     str  — optional run_id override
        state["base_sha"]   str  — (optional) base commit SHA for diff-aware mode
        state["head_sha"]   str  — (optional) head commit SHA for diff-aware mode

    Written to state on success:
        state["local_repo_path"]    str
        state["file_manifest"]      List[FileMeta]
        state["chunks"]             List[CodeChunk]
        state["chunk_map"]          Dict[str, CodeChunk]
        state["dependency_graph"]   DependencyGraph
        state["workspace_layout"]   WorkspaceLayout
        state["ingestion_stats"]    Dict
        state["changed_chunk_ids"]  Set[str] | None  — Priority 1
        state["bm25_index_path"]    str | None        — Priority 3

    Written to state on failure:
        state["error"]   str — exception message

    Args:
        state:          Shared ReviewState dict.
        embed_tool:     EmbeddingTool instance (created from config if None).
        qdrant_tool:    QdrantTool instance (created from config if None).
        llm_client:     anthropic.AsyncAnthropic client for Step 1h.
        skip_summaries: Skip LLM summary generation (Step 1h).
        skip_qdrant:    Skip Qdrant upsert (Step 1k) — useful for dry runs.

    Returns:
        Updated state dict.
    """
    repo_url = state.get("repo_url")
    if not repo_url:
        raise ValueError("state['repo_url'] is required")

    logger.info("[IngestionAgent] Starting ingestion for %s", repo_url)

    try:
        pipeline = IngestionPipeline(
            repo_url  = repo_url,
            repo_name = state.get("repo_name"),
            run_id    = state.get("run_id"),
            base_sha  = state.get("base_sha"),    # Priority 1
            head_sha  = state.get("head_sha"),    # Priority 1
        )
        result = pipeline.run(
            embed_tool     = embed_tool,
            qdrant_tool    = qdrant_tool,
            llm_client     = llm_client,
            skip_summaries = skip_summaries,
            skip_qdrant    = skip_qdrant,
        )

        return {
            **state,
            "local_repo_path":   result.local_repo_path,
            "file_manifest":     result.file_metas,
            "chunks":            result.chunks,
            "chunk_map":         result.chunk_map,
            "dependency_graph":  result.dependency_graph,
            "workspace_layout":  result.layout,
            "ingestion_stats":   result.stats,
            # Propagate run_id so downstream agents can reference workspace
            "run_id":            result.layout.run_id,
            "repo_name":         result.stats.get("repo_name"),
            # Priority 1 — diff-aware: set of chunk_ids from changed files
            "changed_chunk_ids": result.changed_chunk_ids,
            # Priority 3 — hybrid retrieval: path to BM25 index file
            "bm25_index_path":   result.bm25_index_path,
            # Step 1k+2 — quality gate for Stage 3
            "ingestion_quality": result.quality_report,
        }

    except Exception as exc:
        logger.exception("[IngestionAgent] Ingestion failed: %s", exc)
        return {
            **state,
            "error": str(exc),
        }
