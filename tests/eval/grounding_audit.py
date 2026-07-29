"""
Tier 2 — grounding-integrity audit.

stage3_review/evidence_validator.py accepts an evidence term if it's a
substring match (or an exact match, below its own _MIN_MATCH_LEN=3) against
anything actually shown in context. That's deliberately lenient — real
symbol names get prefixed/suffixed and still need to match — but it means a
short, generic token ("self", "data", "get") can coincidentally substring-match
a real symbol name without the finding actually demonstrating any specific
knowledge of it. evidence_validator.validate() cannot see this: by the time
it runs, "is this term real" and "is this term meaningfully specific" have
been conflated into one substring check.

This module answers the second question, deterministically, so a caller can
flag a technically-valid-but-weak citation instead of trusting it at face
value. Free — no LLM calls.
"""

from __future__ import annotations

from typing import Dict, List

# Tokens common enough in real code that a substring match against them
# proves nothing specific — the same class of term evidence_validator's
# substring rule lets through for anything >= _MIN_MATCH_LEN (3 chars).
GENERIC_TERMS = {
    "self", "data", "item", "items", "list", "dict", "value", "values",
    "result", "results", "index", "name", "path", "file", "line", "true",
    "false", "none", "def", "return", "class", "the", "and", "for", "get",
    "set", "obj", "arg", "args", "kwargs", "temp", "tmp",
}

MIN_SPECIFIC_LEN = 4


def audit_terms_specificity(terms: List[str]) -> Dict[str, List[str]]:
    """
    Split *terms* into "specific" (a real citation worth trusting) and "weak"
    (too short/generic to count as meaningful evidence, even if
    evidence_validator would technically accept it as a substring match).
    """
    specific: List[str] = []
    weak: List[str] = []
    for term in terms:
        cleaned = term.strip().lower()
        if len(cleaned) < MIN_SPECIFIC_LEN or cleaned in GENERIC_TERMS:
            weak.append(term)
        else:
            specific.append(term)
    return {"specific": specific, "weak": weak}
