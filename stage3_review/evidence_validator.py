"""
Evidence Validation — Design 1's Phase D.

Checks each raw finding from the LLM against the actual chunk and context it
was given, and rejects (rather than silently relocating) anything that
doesn't hold up:

  1. The cited `line` must fall inside the chunk's own line range.
  2. Every string in `evidence` (when the model provided any) must resolve to
     something that was actually rendered into that call's prompt — a symbol
     name or file path drawn from the parent/siblings/dependencies/similar
     chunks or the pre-computed architectural issues (see
     stage3_review/context_builder.py's format_context_for_prompt()).

This replaces the previous behaviour in stage3_review/agent.py of silently
clamping an out-of-range line number into the chunk's bounds, which masked
LLM line-hallucination instead of surfacing it.

No LLM calls here — pure string/range checks, safe to call per-issue.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from core.models import CodeChunk
from stage3_review.context_builder import ReviewContext

_MIN_MATCH_LEN = 3  # below this length, require an exact (not substring) match


def known_context_terms(ctx: ReviewContext) -> List[str]:
    """Every symbol name / file path / arch-issue reference actually shown to the LLM for this call."""
    terms: List[str] = [ctx.chunk.symbol_name, ctx.chunk.file_path]

    for related in (ctx.parent, ctx.prev_sibling, ctx.next_sibling):
        if related is not None:
            terms.append(related.symbol_name)
            terms.append(related.file_path)

    for dep in ctx.dependencies:
        terms.append(dep.symbol_name)
        terms.append(dep.file_path)

    for sim in ctx.similar:
        terms.append(sim.symbol_name)
        terms.append(sim.file_path)

    for issue in ctx.arch_issues:
        if issue.get("type") == "ARCH001_cycle":
            terms.extend(issue.get("cycle", []))
        else:
            for key in ("from_symbol", "to_symbol", "from_file", "to_file", "symbol_name"):
                if issue.get(key):
                    terms.append(issue[key])

    return [t for t in terms if t]


def _terms_match(evidence_term: str, known_term: str) -> bool:
    e = evidence_term.lower().strip()
    k = known_term.lower().strip()
    if not e or not k:
        return False
    if len(e) < _MIN_MATCH_LEN or len(k) < _MIN_MATCH_LEN:
        return e == k
    return e in k or k in e


def validate(raw_issue: Dict[str, Any], chunk: CodeChunk, ctx: ReviewContext) -> Tuple[bool, str]:
    """
    Check *raw_issue* (a dict from the LLM's report_issues tool call) against
    *chunk* and the context it was actually shown.

    Returns (is_valid, reason) — reason is "" when valid, otherwise a short
    human-readable explanation suitable for logging.
    """
    line = raw_issue.get("line")
    if not isinstance(line, int) or not (chunk.start_line <= line <= chunk.end_line):
        return False, (
            f"cited line {line!r} is outside this chunk's range "
            f"[{chunk.start_line}, {chunk.end_line}]"
        )

    evidence = raw_issue.get("evidence") or []
    if evidence:
        known = known_context_terms(ctx)
        for item in evidence:
            item_str = str(item).strip()
            if not item_str:
                continue
            if not any(_terms_match(item_str, known_term) for known_term in known):
                return False, (
                    f"evidence {item_str!r} does not match anything shown "
                    "in this call's context"
                )

    return True, ""
