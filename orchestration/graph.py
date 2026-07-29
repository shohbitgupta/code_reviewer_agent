"""
Pipeline Orchestrator — wires all 5 stages into a single callable.

This is a lightweight sequential runner designed to be replaced with a
LangGraph StateGraph once `langgraph` is added to requirements.  The public
API (`ReviewPipeline.run`) will not change.

Stage execution order
─────────────────────
  1. Ingestion      — fetch, parse, chunk, embed, index
  2. Standards      — load coding rules from stage2_standards/rules/
  3. Review         — LLM reviews every selected chunk
  4. Comments       — group, budget, format, polish
  5. Report         — build HTML/JSON report, optionally post to GitHub

Each stage is wrapped in a try/except that writes state["error"] and stops
the pipeline on failure.  Successful stages write their own state keys.

Usage::

    pipeline = ReviewPipeline(llm_client=LLMClientFactory.create())
    final_state = pipeline.run(state)
    # final_state["report_path"] → path to the HTML report
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)


class ReviewPipeline:
    """
    Sequential 5-stage code-review pipeline.

    Args:
        llm_client:     UnifiedLLMClient from LLMClientFactory (sync).  Required
                        for Stage 3 (review) and Stage 4 (comment polish).
                        If None, those stages run in template-only mode.
        summary_llm_client: AsyncUnifiedLLMClient (from LLMClientFactory.create_async()
                        or create_for_role_async("summarizer")) for Step 1h's batch
                        summarisation, which runs on an asyncio event loop and needs
                        an awaitable client — never the sync one above. If None,
                        stage1_ingestion.summary_generator.SummaryGenerator falls
                        back to constructing its own async client lazily.
        embed_tool:     EmbeddingTool instance (created from config if None).
        qdrant_tool:    QdrantTool instance (created from config if None).
        github_token:   GitHub personal access token for posting PR comments.
        platform:       Comment platform — "github" | "gitlab" | "jira" | "text".
        skip_qdrant:    True = skip embedding + Qdrant upsert (dry-run mode).
        skip_summaries: True = skip LLM chunk summaries (Step 1h).
        skip_review:    True = skip Stage 3 LLM review (only pre-flagged issues).
        skip_posting:   True = skip posting comments to GitHub (report only).
        event_spine:    Optional EventSpine — when given, emits a "stage_start"/
                        "stage_end" event per stage (see tools/event_spine.py).
    """

    def __init__(
        self,
        llm_client            = None,
        summary_llm_client    = None,
        embed_tool            = None,
        qdrant_tool           = None,
        github_token: str     = "",
        platform:     str     = "github",
        skip_qdrant:  bool    = False,
        skip_summaries: bool  = False,
        skip_review:  bool    = False,
        skip_posting: bool    = False,
        event_spine           = None,
    ) -> None:
        self._llm         = llm_client
        self._summary_llm = summary_llm_client
        self._embed       = embed_tool
        self._qdrant      = qdrant_tool
        self._gh_token    = github_token
        self._platform    = platform
        self._skip_qdrant = skip_qdrant
        self._skip_sums   = skip_summaries
        self._skip_review = skip_review
        self._skip_post   = skip_posting
        self._events      = event_spine

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run all 5 stages in sequence, returning the final state.

        Stops immediately on the first stage failure (state["error"] is set).
        """
        stages: List[Tuple[str, Callable]] = [
            ("Stage 1 — Ingestion",  self._run_ingestion),
            ("Stage 2 — Standards",  self._run_standards),
            ("Stage 3 — Review",     self._run_review),
            ("Stage 4 — Comments",   self._run_comments),
            ("Stage 5 — Report",     self._run_report),
        ]

        t_pipeline = time.monotonic()
        for name, fn in stages:
            if "error" in state:
                logger.warning("[Pipeline] Skipping %s — upstream error", name)
                break
            t_stage = time.monotonic()
            logger.info("[Pipeline] ▶ %s", name)
            if self._events is not None:
                self._events.record("stage_start", stage=name)
            try:
                state = fn(state)
            except Exception as exc:
                logger.exception("[Pipeline] ✗ %s failed: %s", name, exc)
                state["error"] = f"{name}: {exc}"
                if self._events is not None:
                    self._events.record("stage_end", stage=name, ok=False, error=str(exc),
                                         duration_ms=(time.monotonic() - t_stage) * 1000)
                break
            duration_ms = (time.monotonic() - t_stage) * 1000
            logger.info(
                "[Pipeline] ✓ %s  (%.1fs)", name, time.monotonic() - t_stage
            )
            if self._events is not None:
                self._events.record("stage_end", stage=name, ok=True, duration_ms=duration_ms)

        logger.info(
            "[Pipeline] Finished in %.1fs — %s",
            time.monotonic() - t_pipeline,
            "ERROR: " + state["error"] if "error" in state else "OK",
        )
        return state

    # ── Stage runners ─────────────────────────────────────────────────────────

    def _run_ingestion(self, state: Dict) -> Dict:
        """
        Delegate to Stage 1 with the tools and skip flags configured on this
        pipeline. Step 1h needs an awaitable client — self._llm is sync, so
        self._summary_llm (or None, letting SummaryGenerator build its own
        async client) is passed here instead.
        """
        from stage1_ingestion.agent import run_ingestion
        return run_ingestion(
            state,
            embed_tool     = self._embed,
            qdrant_tool    = self._qdrant,
            llm_client     = self._summary_llm,
            skip_summaries = self._skip_sums,
            skip_qdrant    = self._skip_qdrant,
        )

    def _run_standards(self, state: Dict) -> Dict:
        from stage2_standards.agent import run_standards
        return run_standards(state)

    def _run_review(self, state: Dict) -> Dict:
        """
        Delegate to Stage 3, gated by the Stage 1 Ingestion Quality Judge
        decision: short-circuits to an empty issue list on ABORT (ingestion
        score too low to trust), logs a warning and proceeds normally on
        WARN, and runs unconditionally otherwise.
        """
        from stage3_review.agent import run_review

        quality = state.get("ingestion_quality")
        if quality is not None and quality.decision == "ABORT":
            logger.warning(
                "[Pipeline] Stage 3 ABORTED — ingestion quality score=%d (<50). "
                "Top fix: %s",
                quality.overall_score,
                quality.top_fix_hint,
            )
            state["issues"]       = []
            state["review_stats"] = {
                "aborted_by_quality_judge": True,
                "ingestion_score":          quality.overall_score,
            }
            return state

        if quality is not None and quality.decision == "WARN":
            logger.warning(
                "[Pipeline] Stage 3 proceeding with WARNING — ingestion score=%d. "
                "Top fix: %s",
                quality.overall_score,
                quality.top_fix_hint,
            )

        return run_review(
            state,
            llm_client = self._llm,
            skip_llm   = self._skip_review,
        )

    def _run_comments(self, state: Dict) -> Dict:
        """Delegate to Stage 4; polish is skipped automatically when no llm_client was configured."""
        from stage4_comments.agent import run_comments
        return run_comments(
            state,
            platform     = self._platform,
            llm_client   = self._llm,
            skip_polish  = (self._llm is None),
        )

    def _run_report(self, state: Dict) -> Dict:
        from stage5_report.agent import run_report
        return run_report(
            state,
            github_token = self._gh_token,
            skip_posting = self._skip_post,
        )
