"""
Shared ReviewState — the single dict that flows through all 5 pipeline stages.

Every stage consumes a subset of keys and writes new keys back.  Stages must
never mutate keys they did not produce — the append-only contract keeps the
pipeline re-runnable from any checkpoint.

Key categories
──────────────
  Input keys   — set by the caller before the first stage runs
  Stage N keys — written exclusively by stage N

Adding a key: document it here, use it in exactly one stage's agent, and
read it in any downstream stage that needs it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from core.models import (
        CodeChunk, FileMeta, ReviewIssue, ReviewComment, WorkspaceLayout,
    )


# ── Full state schema (used as documentation + runtime dict) ─────────────────
#
# Python TypedDict cannot be used at runtime with Optional + forward refs on
# 3.9, so we keep this as a plain annotation block and use plain dict at runtime.
#
# class ReviewState(TypedDict, total=False):
#
# ── Input keys (set by caller) ────────────────────────────────────────────────
#   repo_url:         str      — GitHub URL  e.g. "https://github.com/owner/repo"
#   pr_number:        int      — PR to post comments on  (0 = no posting)
#   base_sha:         str      — base commit for diff-aware ingestion
#   head_sha:         str      — head commit for diff-aware ingestion
#   platform:         str      — "github" | "gitlab" | "jira" | "text"
#   run_id:           str      — set by pipeline if not provided
#
# ── Stage 1 — Ingestion ───────────────────────────────────────────────────────
#   local_repo_path:  str
#   file_manifest:    List[FileMeta]
#   chunks:           List[CodeChunk]
#   chunk_map:        Dict[str, CodeChunk]
#   dependency_graph: DependencyGraph
#   workspace_layout: WorkspaceLayout
#   ingestion_stats:  Dict
#   changed_chunk_ids:Optional[Set[str]]   — diff-aware mode only
#   bm25_index_path:  Optional[str]
#
# ── Stage 2 — Standards ───────────────────────────────────────────────────────
#   standards:        List[Rule]
#
# ── Stage 3 — Review ─────────────────────────────────────────────────────────
#   issues:           List[ReviewIssue]
#   review_stats:     Dict
#
# ── Stage 4 — Comments ───────────────────────────────────────────────────────
#   comments:         List[ReviewComment]
#   comment_stats:    Dict
#
# ── Stage 5 — Report ─────────────────────────────────────────────────────────
#   report:           Dict        — structured JSON-serialisable report
#   report_path:      str         — absolute path to the HTML report file
#   posted_to_github: bool        — True if inline comments were posted
#
# ── Error handling ────────────────────────────────────────────────────────────
#   error:            str         — set on any unrecoverable stage failure


def make_state(
    repo_url:  str,
    pr_number: int           = 0,
    base_sha:  Optional[str] = None,
    head_sha:  Optional[str] = None,
    platform:  str           = "github",
    run_id:    Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build an initial ReviewState dict with all input keys set.

    All downstream stage keys are absent until their stage writes them.
    """
    import uuid
    return {
        "repo_url":   repo_url,
        "pr_number":  pr_number,
        "base_sha":   base_sha,
        "head_sha":   head_sha,
        "platform":   platform,
        "run_id":     run_id or str(uuid.uuid4())[:8],
    }
