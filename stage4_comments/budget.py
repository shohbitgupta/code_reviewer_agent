"""
Stage 4 — Comment Budget

Enforces two caps so a review never floods a PR:

  max_inline_per_file  (default 5)  — inline comments per file
  max_total            (default 20) — inline comments across all files

Eviction policy
───────────────
Groups are sorted by priority score = (severity_rank, -confidence_avg).
When a cap would be exceeded, the lowest-priority groups are demoted to
"summary" rather than dropped — nothing is silently lost.

Demoted groups are returned in a separate list so the comment agent can
bundle them into a single catch-all summary comment at the PR level.

Usage::

    budget = CommentBudget(max_inline_per_file=5, max_total=20)
    inline_groups, summary_groups = budget.apply(all_groups)
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from stage4_comments.grouper import CommentGroup

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}


class CommentBudget:
    """
    Splits CommentGroup objects into inline-approved and summary-demoted sets.

    Args:
        max_inline_per_file: Max inline comments per file (CRITICAL exempt).
        max_total:           Max total inline comments (CRITICAL exempt).
    """

    def __init__(
        self,
        max_inline_per_file: int = 5,
        max_total:           int = 20,
    ) -> None:
        self._max_per_file = max_inline_per_file
        self._max_total    = max_total

    def apply(
        self,
        groups: List[CommentGroup],
    ) -> Tuple[List[CommentGroup], List[CommentGroup]]:
        """
        Partition groups into (inline, summary).

        CRITICAL groups are never demoted — they always go inline.
        All others compete for the remaining budget slots.

        Returns:
            (inline_groups, summary_groups)
        """
        critical:     List[CommentGroup] = []
        non_critical: List[CommentGroup] = []

        for g in groups:
            if g.severity == "CRITICAL":
                critical.append(g)
            else:
                non_critical.append(g)

        # Sort non-critical by priority (best first)
        non_critical.sort(key=_priority_key)

        inline:  List[CommentGroup] = list(critical)
        summary: List[CommentGroup] = []

        per_file_counts: Dict[str, int] = {}
        for g in critical:
            per_file_counts[g.file_path] = per_file_counts.get(g.file_path, 0) + 1

        total_used = len(critical)

        for g in non_critical:
            file_count = per_file_counts.get(g.file_path, 0)
            if total_used >= self._max_total or file_count >= self._max_per_file:
                summary.append(g)
            else:
                inline.append(g)
                per_file_counts[g.file_path] = file_count + 1
                total_used += 1

        if summary:
            logger.info(
                "[CommentBudget] %d groups demoted to summary "
                "(inline=%d, budget: per_file=%d, total=%d)",
                len(summary), len(inline),
                self._max_per_file, self._max_total,
            )

        return inline, summary


def _priority_key(g: CommentGroup):
    """Lower value = higher priority = kept inline."""
    severity_score = _SEVERITY_RANK.get(g.severity, 99)
    avg_confidence = (
        sum(i.confidence for i in g.issues) / len(g.issues)
        if g.issues else 0.0
    )
    return (severity_score, -avg_confidence)
