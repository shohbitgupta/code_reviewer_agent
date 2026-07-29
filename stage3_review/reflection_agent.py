"""
Reflection — a second LLM pass over the final, deduplicated issue list.

Matches Alibaba open-code-review's "Comment Reflection" step: an independent
verification pass that removes false positives, flags near-duplicates, and
tightens wording. This is distinct from what this repo already has —
Evidence Validation (stage3_review/evidence_validator.py) checks line-bounds
and context-grounding, and issue_deduplicator.py's consensus merge is a
proximity-based dedup — neither is a semantic "does this finding actually
make sense" second look, which is what reflection adds.

Deliberately gated, not unconditional: only files with enough signal (2+
findings, or any CRITICAL/HIGH) get a reflection call — a single LOW/INFO
finding isn't worth a second call. Fail-open: any error in the reflection
call itself falls back to the original, unrefined findings for that file —
reflection must never cause a real finding to silently disappear because the
reflection call itself broke.

Uses its own thin LLM-calling wrapper (not stage3_review/llm_reviewer.py's
LLMReviewer, which is content_hash-keyed per-chunk and doesn't fit a
per-file list-of-findings access pattern) — matches the existing pattern of
stage1_ingestion/summary_generator.py and stage4_comments/writer.py each
having their own purpose-built LLM caller. Goes through whatever llm_client
instance the caller passes in, so budget-guard/event-spine instrumentation
(wired at the client level in tools/llm_client.py) covers reflection calls
automatically — no separate wiring needed here.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from core.models import ReviewIssue

logger = logging.getLogger(__name__)

_HIGH_SEVERITY = {"CRITICAL", "HIGH"}
_MAX_TOKENS = 1500

REFINE_FINDINGS_TOOL: Dict[str, Any] = {
    "name": "refine_findings",
    "description": (
        "Decide which of the listed findings to keep, and optionally improve "
        "their wording. Call once with a decision for every finding listed."
    ),
    "input_schema": {
        "type": "object",
        "required": ["decisions"],
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["index", "keep"],
                    "properties": {
                        "index": {"type": "integer", "minimum": 0},
                        "keep":  {"type": "boolean"},
                        "reason_if_dropped":   {"type": "string"},
                        "revised_description": {"type": "string"},
                        "revised_suggestion":  {"type": "string"},
                    },
                },
            }
        },
    },
}


def _should_reflect(issues: List[ReviewIssue]) -> bool:
    """A file's findings are worth a second look if there are 2+, or any CRITICAL/HIGH."""
    if len(issues) >= 2:
        return True
    return any(i.severity in _HIGH_SEVERITY for i in issues)


def _build_prompt(file_path: str, issues: List[ReviewIssue]) -> str:
    lines = [
        f"File: {file_path}",
        f"{len(issues)} finding(s) from an earlier automated review pass, listed below.",
        "For EACH finding (by index), decide whether to keep it — drop anything that "
        "looks like a false positive on reflection, or is a near-duplicate of another "
        "finding in this list. You may tighten the description/suggestion wording, but "
        "do not change what a kept finding is actually about.",
        "",
    ]
    for i, issue in enumerate(issues):
        lines.append(
            f"[{i}] {issue.rule_id} ({issue.severity}) line {issue.start_line}: "
            f"{issue.title}\n    {issue.description}\n    Suggestion: {issue.suggestion}"
        )
    lines.append("")
    lines.append("Call refine_findings with a decision for every index listed above.")
    return "\n".join(lines)


def _call_reflection(llm_client, model: str, file_path: str, issues: List[ReviewIssue]) -> List[Dict]:
    """One forced tool call asking the model to refine this file's findings."""
    prompt = _build_prompt(file_path, issues)
    response = llm_client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        system=(
            "You are a senior reviewer double-checking another reviewer's findings "
            "before they're posted. Be skeptical of anything that looks like a false "
            "positive or a near-duplicate. Always call refine_findings — even to keep "
            "everything unchanged."
        ),
        messages=[{"role": "user", "content": prompt}],
        tools=[REFINE_FINDINGS_TOOL],
        tool_choice={"type": "any"},
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "refine_findings":
            return block.input.get("decisions", [])
    return []


def reflect(
    issues: List[ReviewIssue],
    llm_client,
    model: str,
) -> Tuple[List[ReviewIssue], Dict[str, int]]:
    """
    Run the reflection pass over *issues*, grouped by file.

    Returns (refined_issues, stats) where stats has reflection_calls,
    reflection_dropped, reflection_revised. Fails open per-file: any error
    leaves that file's findings unchanged rather than dropping them.
    """
    by_file: Dict[str, List[ReviewIssue]] = {}
    for issue in issues:
        by_file.setdefault(issue.file_path, []).append(issue)

    refined: List[ReviewIssue] = []
    stats = {"reflection_calls": 0, "reflection_dropped": 0, "reflection_revised": 0}

    for file_path, file_issues in by_file.items():
        if not _should_reflect(file_issues):
            refined.extend(file_issues)
            continue

        try:
            decisions = _call_reflection(llm_client, model, file_path, file_issues)
            stats["reflection_calls"] += 1
        except Exception as exc:
            logger.warning(
                "[Reflection] Call failed for %s (%d findings) — keeping unrefined: %s",
                file_path, len(file_issues), exc,
            )
            refined.extend(file_issues)
            continue

        by_index = {d.get("index"): d for d in decisions if isinstance(d.get("index"), int)}
        for i, issue in enumerate(file_issues):
            decision = by_index.get(i)
            if decision is None:
                # No decision for this index — fail open, keep the original finding.
                refined.append(issue)
                continue
            if not decision.get("keep", True):
                stats["reflection_dropped"] += 1
                logger.debug(
                    "[Reflection] Dropped %s:%s line %d — %s",
                    file_path, issue.rule_id, issue.start_line,
                    decision.get("reason_if_dropped", "no reason given"),
                )
                continue
            revised_description = decision.get("revised_description")
            revised_suggestion = decision.get("revised_suggestion")
            if revised_description or revised_suggestion:
                stats["reflection_revised"] += 1
                issue.description = revised_description or issue.description
                issue.suggestion = revised_suggestion or issue.suggestion
            refined.append(issue)

    return refined, stats
