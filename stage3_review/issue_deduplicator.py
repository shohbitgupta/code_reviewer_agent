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

Consensus (Design 1 Phase E scaffolding): after per-key dedup, issues from
2+ distinct agent_types within a small line-proximity window in the same
file are merged into a single corroborated finding — the most severe
survivor's confidence is boosted and the other agreeing agent_types are
recorded in its corroborated_by field, rather than posting near-duplicate
comments from each specialist. Inert today: every issue currently carries
agent_type="general" (the only reviewing role that exists yet), so no
bucket ever contains 2+ distinct values — this activates once more than
one specialist role exists.

Output order: CRITICAL → HIGH → MEDIUM → LOW → INFO, then by file_path + line.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from core.models import ReviewIssue

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}

# Lines within this distance of each other are treated as "the same area" for
# cross-specialist agreement — independent findings this close together are
# very likely about the same piece of code even if their exact line differs.
_AGREEMENT_LINE_PROXIMITY = 3

# Confidence boost applied to a finding independently corroborated by another
# specialist, capped at 1.0.
_AGREEMENT_CONFIDENCE_BOOST = 0.15


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
          3. Merge cross-specialist agreement (see module docstring).
          4. Sort by severity then file_path + line for stable output.
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

        # Step 3: cross-specialist agreement merge
        merged = self._merge_agreement(list(best.values()))
        if len(merged) < len(best):
            logger.debug(
                "[Deduplicator] Merged %d issue(s) into corroborated findings",
                len(best) - len(merged),
            )

        # Step 4: sort
        result = sorted(
            merged,
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

    @staticmethod
    def _merge_agreement(issues: List[ReviewIssue]) -> List[ReviewIssue]:
        """
        Merge cross-specialist agreement within each file (see module docstring).

        Issues in the same file whose start_line falls within
        _AGREEMENT_LINE_PROXIMITY of each other, and which carry 2+ distinct
        agent_type values, are collapsed into their most-severe member: that
        survivor's confidence is boosted and the other agreeing agent_types
        are recorded in its corroborated_by field. A cluster with only one
        agent_type represented (including today's single "general" role) is
        left untouched — this is a no-op until more than one specialist exists.
        """
        by_file: Dict[str, List[ReviewIssue]] = {}
        for issue in issues:
            by_file.setdefault(issue.file_path, []).append(issue)

        kept: List[ReviewIssue] = []
        for file_issues in by_file.values():
            file_issues.sort(key=lambda i: i.start_line)
            consumed: set = set()
            for issue in file_issues:
                if id(issue) in consumed:
                    continue
                cluster = [
                    other for other in file_issues
                    if id(other) not in consumed
                    and abs(other.start_line - issue.start_line) <= _AGREEMENT_LINE_PROXIMITY
                ]
                agent_types = {c.agent_type for c in cluster}
                if len(cluster) > 1 and len(agent_types) >= 2:
                    primary = min(cluster, key=lambda c: _SEVERITY_ORDER.get(c.severity, 99))
                    primary.corroborated_by = sorted(
                        {c.agent_type for c in cluster if c is not primary}
                    )
                    primary.confidence = min(1.0, primary.confidence + _AGREEMENT_CONFIDENCE_BOOST)
                    kept.append(primary)
                    consumed.update(id(c) for c in cluster)
                else:
                    kept.append(issue)
                    consumed.add(id(issue))
        return kept


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
