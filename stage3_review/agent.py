"""
Reviewer Agent — Stage 3 of the code review pipeline.

Takes the ingested chunks and loaded standards from ReviewState, runs LLM
code review on every reviewable chunk in parallel, deduplicates the resulting
issues, and writes the final issue list back to state.

Key behaviours
──────────────
1. Chunk selection & prioritisation
   - If diff-aware mode is active (state["changed_chunk_ids"]), FUNCTION and
     METHOD chunks in changed files are reviewed first (priority queue).
   - MODULE chunks (file-level summaries) are skipped unless they carry a
     pre-flagged violation — reviewing a file summary rarely adds value.
   - Chunks < MIN_REVIEW_LINES lines are skipped (trivial getters, stubs).

2. Blast-radius skip
   - If a chunk has ≥ BLAST_RADIUS_THRESHOLD unique call edges (callee OR
     caller) in the dependency graph it is flagged in the prompt header but
     only a blast-radius note is added rather than a full LLM review.
     Rationale: high-fan-out infrastructure code is rarely where defects hide,
     but reviewing it naively drags in enormous context.

3. Pre-flagged conversion
   - chunk.pre_flagged_violations are converted to ReviewIssue objects before
     the LLM pass; they skip the API call entirely.

4. Parallel LLM review
   - WorkerPool (ThreadPoolExecutor) submits one task per chunk.
   - LLMReviewer.review() has its own BoundedSemaphore to cap concurrency.

5. Deduplication
   - IssueDeduplicator.deduplicate() removes duplicate (file, line, rule_id)
     triples, preferring pre-flagged issues and higher confidence.

State keys consumed
───────────────────
    state["chunks"]             List[CodeChunk]
    state["chunk_map"]          Dict[str, CodeChunk]
    state["dependency_graph"]   DependencyGraph
    state["standards"]          List[Rule]
    state["changed_chunk_ids"]  Set[str] | None
    state["bm25_index_path"]    str | None

State keys produced
───────────────────
    state["issues"]         List[ReviewIssue] — deduplicated, sorted by severity
    state["review_stats"]   Dict

Usage:
    from stage3_review.agent import run_review

    state = run_review(state, llm_client=LLMClientFactory.create())
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core import config
from stage2_standards.agent import build_review_prompt_rules, Rule
from core.models import CodeChunk, ChunkType, ReviewIssue, RuleViolation
from stage3_review.context_builder import ContextBuilder, format_context_for_prompt
from stage3_review.issue_deduplicator import IssueDeduplicator, pre_flagged_to_issues
from stage3_review.llm_reviewer import LLMReviewer
from stage3_review.prompt_builder import SYSTEM_PROMPT, PromptBuilder

logger = logging.getLogger(__name__)

# ── Tuning constants ──────────────────────────────────────────────────────────
MIN_REVIEW_LINES        = 3    # shorter chunks (trivial getters) are skipped
BLAST_RADIUS_THRESHOLD  = 50   # chunks with ≥ this many dep edges get a note, not a full review
_WORKER_THREADS         = 3    # GLM free tier: keep low to avoid throttling
_REVIEW_CACHE_DIR       = Path("./workspace/review_cache")

# ── O1: Security-sensitive path patterns → always use full model ──────────────
_SECURITY_PATHS = frozenset({
    "auth", "crypt", "token", "password", "secret", "jwt",
    "oauth", "signin", "login", "session", "credential", "key",
    "permission", "role", "access", "secure",
})

# Chunk types that carry real logic and benefit from LLM review
_REVIEWABLE_TYPES: Set[ChunkType] = {
    ChunkType.FUNCTION,
    ChunkType.METHOD,
    ChunkType.CLASS,
    ChunkType.BLOCK,
}


def run_review(
    state:      Dict[str, Any],
    llm_client          = None,
    qdrant_tool         = None,
    skip_llm:   bool    = False,
) -> Dict[str, Any]:
    """
    Stage 3 agent entry point.

    Args:
        state:       Shared ReviewState dict (see module docstring).
        llm_client:  UnifiedLLMClient from LLMClientFactory.  Created from env if None.
        qdrant_tool: Optional QdrantTool for dense-vector similarity search.
        skip_llm:    If True, only emit pre-flagged violations (no API calls).

    Returns:
        Updated state with "issues" and "review_stats" populated.
    """
    t_start = time.monotonic()

    chunks: List[CodeChunk]      = state.get("chunks", [])
    chunk_map: Dict[str, CodeChunk] = state.get("chunk_map", {})
    dep_graph                    = state.get("dependency_graph")
    standards: List[Rule]        = state.get("standards", [])
    changed_ids: Optional[Set]   = state.get("changed_chunk_ids")
    bm25_path: Optional[str]     = state.get("bm25_index_path")
    rule_lookup: Dict[str, Rule] = {r.rule_id: r for r in standards}

    if not chunks:
        logger.warning("[ReviewerAgent] No chunks in state — skipping review")
        state["issues"]       = []
        state["review_stats"] = {"skipped_reason": "no_chunks"}
        return state

    # ── 1. Convert pre-flagged violations ─────────────────────────────────────
    pre_flagged_issues = pre_flagged_to_issues(chunks, rule_lookup)
    logger.info(
        "[ReviewerAgent] %d pre-flagged issues converted from mechanical checker",
        len(pre_flagged_issues),
    )

    if skip_llm:
        dedup = IssueDeduplicator()
        state["issues"]       = dedup.deduplicate(pre_flagged_issues)
        state["review_stats"] = {"mode": "pre_flagged_only", "total": len(state["issues"])}
        return state

    # ── 2. Load BM25 index if available ───────────────────────────────────────
    bm25_index = None
    if bm25_path:
        try:
            from tools.bm25_tool import BM25Index
            bm25_index = BM25Index.load(Path(bm25_path))
            logger.info("[ReviewerAgent] BM25 index loaded from %s", bm25_path)
        except Exception as exc:
            logger.warning("[ReviewerAgent] BM25 load failed: %s", exc)

    # ── 3. Instantiate shared helpers ─────────────────────────────────────────
    if llm_client is None:
        from tools.llm_client import LLMClientFactory
        llm_client = LLMClientFactory.create()

    ctx_builder  = ContextBuilder(chunk_map, dep_graph, qdrant_tool, bm25_index)
    prompt_builder = PromptBuilder()
    reviewer     = LLMReviewer(
        llm_client    = llm_client,
        max_concurrency = _WORKER_THREADS,
        cache_dir     = _REVIEW_CACHE_DIR,
    )

    # ── 4. Select and prioritise chunks for review ────────────────────────────
    to_review = _select_chunks(chunks, changed_ids, dep_graph)
    logger.info(
        "[ReviewerAgent] %d/%d chunks selected for LLM review (diff-aware=%s)",
        len(to_review), len(chunks), changed_ids is not None,
    )

    # ── 5. Parallel LLM review ────────────────────────────────────────────────
    llm_issues: List[ReviewIssue] = []
    stats_reviewed   = 0
    stats_cache_hits = 0
    stats_errors     = 0
    stats_fast_model = 0   # O1: chunks reviewed with FAST_MODEL
    stats_full_model = 0   # O1: chunks reviewed with REVIEW_MODEL

    with ThreadPoolExecutor(max_workers=_WORKER_THREADS, thread_name_prefix="review") as pool:
        future_to_chunk = {
            pool.submit(
                _review_chunk,
                chunk, ctx_builder, prompt_builder, reviewer, standards,
            ): chunk
            for chunk in to_review
        }

        for future in as_completed(future_to_chunk):
            chunk = future_to_chunk[future]
            try:
                issues, was_cached = future.result()
                llm_issues.extend(issues)
                stats_reviewed += 1
                if was_cached:
                    stats_cache_hits += 1
                # O1: tally model tier usage
                if _select_model(chunk) == config.FAST_MODEL:
                    stats_fast_model += 1
                else:
                    stats_full_model += 1
            except Exception as exc:
                stats_errors += 1
                logger.error(
                    "[ReviewerAgent] Review failed for %s:%s — %s",
                    chunk.file_path, chunk.symbol_name, exc,
                )

    # ── 6. Merge pre-flagged + LLM issues and deduplicate ────────────────────
    all_issues = pre_flagged_issues + llm_issues
    dedup = IssueDeduplicator(min_confidence=0.5)
    final_issues = dedup.deduplicate(all_issues)

    # ── 7. Write state ────────────────────────────────────────────────────────
    elapsed = round(time.monotonic() - t_start, 2)
    stats = {
        "chunks_total":      len(chunks),
        "chunks_selected":   len(to_review),
        "chunks_capped":     max(0, len(chunks) - len(to_review)),
        "chunks_reviewed":   stats_reviewed,
        "cache_hits":        stats_cache_hits,
        "errors":            stats_errors,
        "pre_flagged":       len(pre_flagged_issues),
        "llm_found":         len(llm_issues),
        "issues_total":      len(final_issues),
        "model_fast_chunks": stats_fast_model,   # O1
        "model_full_chunks": stats_full_model,   # O1
        "elapsed_seconds":   elapsed,
    }

    critical_count = sum(1 for i in final_issues if i.severity == "CRITICAL")
    high_count     = sum(1 for i in final_issues if i.severity == "HIGH")
    logger.info(
        "[ReviewerAgent] Review complete in %.1fs — %d issues "
        "(CRITICAL=%d, HIGH=%d)",
        elapsed, len(final_issues), critical_count, high_count,
    )

    state["issues"]       = final_issues
    state["review_stats"] = stats
    return state


# ── Private helpers ───────────────────────────────────────────────────────────

def _select_chunks(
    chunks:      List[CodeChunk],
    changed_ids: Optional[Set[str]],
    dep_graph,
) -> List[CodeChunk]:
    """
    Filter chunks to those worth LLM review and sort by priority.

    Priority order (highest first):
      1. Changed function/method chunks
      2. Unchanged function/method/class chunks
      3. Block chunks (sliding-window overflow)

    Returns a flat list in priority order.
    """
    priority1: List[CodeChunk] = []
    priority2: List[CodeChunk] = []
    priority3: List[CodeChunk] = []

    for chunk in chunks:
        if chunk.chunk_type not in _REVIEWABLE_TYPES:
            continue

        line_count = chunk.end_line - chunk.start_line + 1
        if line_count < MIN_REVIEW_LINES:
            continue

        is_changed = changed_ids is None or chunk.chunk_id in changed_ids

        if chunk.chunk_type in (ChunkType.FUNCTION, ChunkType.METHOD):
            if is_changed:
                priority1.append(chunk)
            else:
                priority2.append(chunk)
        elif chunk.chunk_type == ChunkType.CLASS:
            priority2.append(chunk)
        else:
            priority3.append(chunk)

    # Within each priority group, sort by chunk complexity (line count desc)
    # so the most complex chunks survive when the cap kicks in.
    _complexity = lambda c: c.end_line - c.start_line + 1
    priority1.sort(key=_complexity, reverse=True)
    priority2.sort(key=_complexity, reverse=True)
    priority3.sort(key=_complexity, reverse=True)

    selected = priority1 + priority2 + priority3

    # O2: apply MAX_REVIEW_CHUNKS cap (0 = disabled)
    cap = config.MAX_REVIEW_CHUNKS
    if cap > 0 and len(selected) > cap:
        logger.info(
            "[ReviewerAgent] Chunk cap: reviewing %d/%d chunks "
            "(MAX_REVIEW_CHUNKS=%d, %d skipped by complexity rank)",
            cap, len(selected), cap, len(selected) - cap,
        )
        selected = selected[:cap]

    return selected


def _select_model(chunk: CodeChunk) -> str:
    """
    O1 — Pick the LLM model for this chunk.

    Full model (REVIEW_MODEL / Opus) when:
      - chunk has any pre-flagged violations (mechanical checker already saw risk)
      - chunk lives in a security-sensitive file path

    Fast model (FAST_MODEL / Haiku) for everything else.
    """
    if chunk.pre_flagged_violations:
        return config.REVIEW_MODEL

    lower_path = chunk.file_path.lower()
    if any(pat in lower_path for pat in _SECURITY_PATHS):
        return config.REVIEW_MODEL

    return config.FAST_MODEL


def _review_chunk(
    chunk:          CodeChunk,
    ctx_builder:    ContextBuilder,
    prompt_builder: PromptBuilder,
    reviewer:       LLMReviewer,
    standards:      List[Rule],
) -> tuple[List[ReviewIssue], bool]:
    """
    Review a single chunk.  Returns (issues, was_cached).
    Runs inside a ThreadPoolExecutor worker — must be thread-safe.
    """
    # Build context
    ctx = ctx_builder.build(chunk)
    context_section = format_context_for_prompt(ctx)

    # Build rules section for this chunk's language
    rules_section = build_review_prompt_rules(standards, chunk.language)
    relevant_rule_ids = [
        r.rule_id for r in standards if r.applies_to(chunk.language)
    ]

    # Build user prompt
    user_prompt = prompt_builder.build_user(
        chunk           = chunk,
        context_section = context_section,
        rules_section   = rules_section,
        pre_flagged     = chunk.pre_flagged_violations,
    )

    # O1: select fast vs full model based on chunk risk signals
    model = _select_model(chunk)

    # LLM call (may return cached result)
    raw_issues, was_cached = reviewer.review(
        system_prompt = SYSTEM_PROMPT,
        user_prompt   = user_prompt,
        chunk         = chunk,
        rule_ids      = relevant_rule_ids,
        model         = model,
    )

    # Convert raw dicts to ReviewIssue objects
    issues: List[ReviewIssue] = []
    for raw in raw_issues:
        line = raw.get("line", chunk.start_line)
        # Clamp to chunk boundaries (LLM sometimes hallucinates lines)
        line = max(chunk.start_line, min(line, chunk.end_line))
        issues.append(ReviewIssue.new(
            chunk_id    = chunk.chunk_id,
            rule_id     = raw.get("rule_id", "UNKNOWN"),
            severity    = raw.get("severity", "INFO"),
            title       = raw.get("title", ""),
            description = raw.get("description", ""),
            file_path   = chunk.file_path,
            start_line  = line,
            end_line    = line,
            suggestion  = raw.get("suggestion", ""),
            language    = chunk.language,
            category    = raw.get("category", "general"),
            confidence  = float(raw.get("confidence", 0.7)),
            layer       = chunk.layer,
        ))

    return issues, was_cached
