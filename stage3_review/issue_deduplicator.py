"""
Stage 3 — Issue Deduplicator

Removes duplicate ReviewIssue objects that arise when:
  1. The same violation appears in multiple overlapping chunks
     (e.g. a BLOCK chunk from sliding-window and its parent FUNCTION chunk
     both trigger GEN001 at the same line).
  2. The LLM flags a violation the MechanicalRuleChecker already caught —
     is_pre_flagged=True entries always win over LLM detections.
  3. The same LLM call produces duplicate tool_use entries for the same line.

Deduplication key: (file_path, start_line, rule_id)

When two issues share the same key:
  - Keep the one with is_pre_flagged=True (deterministic, no LLM cost)
  - Among LLM issues, keep the one with higher confidence
  - Break ties by severity order: CRITICAL > HIGH > MEDIUM > LOW > INFO

Output order: CRITICAL → HIGH → MEDIUM → LOW → INFO, then by file_path + line.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from core.models import ReviewIssue

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}


class IssueDeduplicator:
    """
    Deduplicates and sorts a flat list of ReviewIssue objects.

    Usage::

        dedup = IssueDeduplicator(min_confidence=0.5)
        final = dedup.deduplicate(all_issues)
    """

    def __init__(self, min_confidence: float = 0.5) -> None:
        self._min_confidence = min_confidence

    # ── Public ────────────────────────────────────────────────────────────────

    def deduplicate(self, issues: List[ReviewIssue]) -> List[ReviewIssue]:
        """
        Filter, deduplicate, and sort issues.

        Steps:
          1. Drop issues below min_confidence threshold.
          2. Deduplicate by (file_path, start_line, rule_id).
          3. Sort by severity then file_path + line for stable output.
        """
        # Step 1: confidence filter
        filtered = [i for i in issues if i.confidence >= self._min_confidence]
        dropped  = len(issues) - len(filtered)
        if dropped:
            logger.debug(
                "[Deduplicator] Dropped %d low-confidence issues (threshold=%.1f)",
                dropped, self._min_confidence,
            )

        # Step 2: dedup by key
        best: Dict[Tuple, ReviewIssue] = {}
        for issue in filtered:
            key = (issue.file_path, issue.start_line, issue.rule_id)
            if key not in best:
                best[key] = issue
            else:
                best[key] = self._pick_winner(best[key], issue)

        deduped = len(filtered) - len(best)
        if deduped:
            logger.debug("[Deduplicator] Removed %d duplicate issues", deduped)

        # Step 3: sort
        result = sorted(
            best.values(),
            key=lambda i: (
                _SEVERITY_ORDER.get(i.severity, 99),
                i.file_path,
                i.start_line,
            ),
        )

        logger.info(
            "[Deduplicator] %d total → %d after dedup/filter",
            len(issues), len(result),
        )
        return result

    # ── Private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _pick_winner(a: ReviewIssue, b: ReviewIssue) -> ReviewIssue:
        """
        Given two issues with the same dedup key, return the better one.

        Priority:
          1. is_pre_flagged=True wins unconditionally (mechanical certainty)
          2. Higher confidence wins
          3. Higher severity wins (as a secondary tiebreak)
        """
        if a.is_pre_flagged and not b.is_pre_flagged:
            return a
        if b.is_pre_flagged and not a.is_pre_flagged:
            return b

        if a.confidence > b.confidence:
            return a
        if b.confidence > a.confidence:
            return b

        # Equal confidence — prefer higher severity
        if _SEVERITY_ORDER.get(a.severity, 99) <= _SEVERITY_ORDER.get(b.severity, 99):
            return a
        return b


def pre_flagged_to_issues(chunks, rule_lookup: Dict) -> List[ReviewIssue]:
    """
    Convert chunk.pre_flagged_violations (RuleViolation) → ReviewIssue objects.

    Called before the LLM pass so these issues are in state["issues"] regardless
    of whether the chunk is sent for LLM review.

    Args:
        chunks:       All CodeChunk objects (only those with pre_flagged_violations
                      will produce output).
        rule_lookup:  {rule_id: Rule} — used to fill category from the Rule object.
    """
    issues: List[ReviewIssue] = []
    for chunk in chunks:
        for v in chunk.pre_flagged_violations:
            rule    = rule_lookup.get(v.rule_id)
            category = rule.category if rule else "general"
            issues.append(ReviewIssue.new(
                chunk_id       = chunk.chunk_id,
                rule_id        = v.rule_id,
                severity       = v.severity,
                title          = v.title,
                description    = v.description,
                file_path      = chunk.file_path,
                start_line     = v.line,
                end_line       = v.line,
                suggestion     = (
                    f"See rule {v.rule_id} in standards/{chunk.language}.md "
                    "for the required fix."
                ),
                language       = chunk.language,
                category       = category,
                is_pre_flagged = True,
                confidence     = 1.0,
                layer          = chunk.layer,
            ))
    return issues
