"""
Comment Agent — Stage 4 of the code review pipeline.

Takes the deduplicated ReviewIssue list from Stage 3 and produces
ReviewComment objects that are ready to post to a pull-request platform.

Pipeline within this stage
───────────────────────────
  1. Group    — CommentGrouper clusters nearby issues (≤5 lines apart,
                same file, same severity bucket) into CommentGroup objects.
  2. Budget   — CommentBudget caps per-file (5) and total (20) inline
                comments.  Over-budget groups are demoted to summary.
  3. Format   — CommentFormatter renders every group (inline + summary) into
                a platform-specific markdown body.
  4. Polish   — CommentWriter optionally runs an LLM pass on CRITICAL/HIGH
                groups to add "why this matters" context and a concrete
                before/after code fix.  MEDIUM/LOW use the template body.
  5. Assemble — Inline ReviewComment objects are built; if any groups were
                demoted to summary a single catch-all summary comment is
                appended at the PR level (line=0).

State keys consumed
───────────────────
    state["issues"]     List[ReviewIssue]
    state["chunk_map"]  Dict[str, CodeChunk]   (optional, improves polish)

State keys produced
───────────────────
    state["comments"]       List[ReviewComment]  — sorted by file + line
    state["comment_stats"]  Dict

Usage:
    from stage4_comments.agent import run_comments

    state = run_comments(state, platform="github")
    state = run_comments(state, platform="github", llm_client=anthropic.Anthropic())
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from stage4_comments.budget import CommentBudget
from stage4_comments.formatter import CommentFormatter
from stage4_comments.grouper import CommentGroup, CommentGrouper
from stage4_comments.writer import CommentWriter
from core.models import ReviewComment, ReviewIssue

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}


def run_comments(
    state:       Dict[str, Any],
    platform:    str   = "github",
    llm_client          = None,
    skip_polish: bool   = False,
    max_inline_per_file: int = 5,
    max_total:           int = 20,
) -> Dict[str, Any]:
    """
    Stage 4 agent entry point.

    Args:
        state:               Shared ReviewState dict.
        platform:            "github" | "gitlab" | "jira" | "text".
        llm_client:          anthropic.Anthropic() sync client.
                             If None, the LLM polish step is skipped for all
                             groups (formatter output used directly).
        skip_polish:         Force-skip LLM polish even if llm_client provided.
        max_inline_per_file: Cap for inline comments per file.
        max_total:           Cap for total inline comments.

    Returns:
        Updated state with "comments" and "comment_stats" populated.
    """
    t_start = time.monotonic()

    issues:    List[ReviewIssue] = state.get("issues", [])
    chunk_map: Dict              = state.get("chunk_map", {})

    if not issues:
        logger.warning("[CommentAgent] No issues in state — producing empty comment list")
        state["comments"]      = []
        state["comment_stats"] = {"skipped_reason": "no_issues"}
        return state

    # ── Step 1: Group ─────────────────────────────────────────────────────────
    grouper = CommentGrouper()
    all_groups = grouper.group(issues)
    logger.info(
        "[CommentAgent] %d issues → %d groups (file_count=%d)",
        len(issues),
        len(all_groups),
        len({g.file_path for g in all_groups}),
    )

    # ── Step 2: Budget ────────────────────────────────────────────────────────
    budget = CommentBudget(
        max_inline_per_file=max_inline_per_file,
        max_total=max_total,
    )
    inline_groups, summary_groups = budget.apply(all_groups)

    # ── Step 3: Format ────────────────────────────────────────────────────────
    formatter = CommentFormatter(platform=platform)

    formatted_bodies: Dict[str, str] = {}
    for group in inline_groups:
        formatted_bodies[group.group_id] = formatter.render(group)

    # ── Step 4: Polish (LLM, CRITICAL/HIGH only) ──────────────────────────────
    effective_client = None if skip_polish else llm_client
    writer = CommentWriter(
        llm_client = effective_client,
        platform   = platform,
    )
    inline_comments = writer.polish(
        groups          = inline_groups,
        formatted_bodies = formatted_bodies,
        chunk_map       = chunk_map,
    )
    polished_count = sum(1 for c in inline_comments if c.polished)

    # ── Step 5: Summary comment for demoted groups ────────────────────────────
    final_comments: List[ReviewComment] = list(inline_comments)
    if summary_groups:
        summary_body = formatter.render_summary(
            summary_groups,
            inline_count=len(inline_comments),
        )
        if summary_body:
            final_comments.append(ReviewComment.new(
                file_path    = "",          # PR-level comment has no file anchor
                line         = 0,
                end_line     = 0,
                body         = summary_body,
                severity     = _dominant_severity(summary_groups),
                comment_type = "summary",
                platform     = platform,
                issue_ids    = [iid for g in summary_groups for iid in g.issue_ids],
                language     = "",
                polished     = False,
            ))

    # Sort: inline first by file+line, summary last
    final_comments.sort(
        key=lambda c: (
            0 if c.comment_type == "inline" else 1,
            c.file_path,
            c.line,
        )
    )

    # ── Stats ─────────────────────────────────────────────────────────────────
    elapsed = round(time.monotonic() - t_start, 2)
    stats = {
        "issues_in":          len(issues),
        "groups_total":       len(all_groups),
        "groups_inline":      len(inline_groups),
        "groups_summary":     len(summary_groups),
        "comments_inline":    len(inline_comments),
        "comments_total":     len(final_comments),
        "polished_by_llm":    polished_count,
        "platform":           platform,
        "elapsed_seconds":    elapsed,
    }

    sev_counts = {}
    for c in final_comments:
        sev_counts[c.severity] = sev_counts.get(c.severity, 0) + 1
    stats["by_severity"] = sev_counts

    logger.info(
        "[CommentAgent] %d comments produced in %.1fs "
        "(inline=%d, summary=%d, polished=%d) platform=%s",
        len(final_comments), elapsed,
        len(inline_comments), len(summary_groups),
        polished_count, platform,
    )

    state["comments"]      = final_comments
    state["comment_stats"] = stats
    return state


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dominant_severity(groups: List[CommentGroup]) -> str:
    """Return the highest severity string among all issues in all groups."""
    best = "INFO"
    for g in groups:
        if _SEVERITY_RANK.get(g.severity, 99) < _SEVERITY_RANK.get(best, 99):
            best = g.severity
    return best
