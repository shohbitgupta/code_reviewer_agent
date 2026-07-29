"""
Tier 3 — LLM-as-judge factual audit.

Independent of the reviewer's own rule_id/framing: the judge sees only the
real code chunk and a finding's free-text claim, and rates whether that claim
is actually true of the code — blind to which rule the original reviewer
thought it was enforcing. This is what catches a coherently grounded
hallucination that Evidence Validation (stage3_review/evidence_validator.py)
structurally cannot: a real cited line and a real symbol name, wrapped around
a conclusion that's simply false.

Own thin LLM-calling wrapper, matching the established pattern of each
purpose-built caller having its own wrapper (see
stage3_review/reflection_agent.py's docstring) rather than sharing
stage3_review/llm_reviewer.py's per-chunk content-hash-cached LLMReviewer —
a judge verdict must never be cached against the same key a reviewer finding
used, or the "independent" check stops being independent.
"""

from __future__ import annotations

from typing import Any, Dict

_MAX_TOKENS = 500

JUDGE_TOOL: Dict[str, Any] = {
    "name": "rate_finding",
    "description": "Rate whether a code-review finding's claim is factually true of the shown code.",
    "input_schema": {
        "type": "object",
        "required": ["verdict", "reason"],
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["TRUE", "PARTIALLY_TRUE", "FALSE"],
            },
            "reason": {"type": "string"},
        },
    },
}

_JUDGE_SYSTEM_PROMPT = """\
You are an independent fact-checker auditing a single code-review finding.

You will be shown a code chunk and one finding's claim about it. Your only \
job is to judge whether the claim is factually TRUE of the code shown — not \
whether it's well-written, not whether you'd word it the same way, and you \
are not told which rule this finding was meant to enforce.

  - TRUE: the claim accurately describes something that is really happening \
in the code shown.
  - PARTIALLY_TRUE: the claim is directionally correct but overstates, \
misattributes a detail, or is only true with a caveat.
  - FALSE: the claim describes something that is not actually happening in \
this code — even if it sounds plausible or is well-written.

Always call rate_finding — never respond with free text.\
"""


def _build_prompt(chunk_code: str, finding_description: str) -> str:
    return (
        f"CODE:\n```\n{chunk_code}\n```\n\n"
        f"FINDING TO AUDIT:\n{finding_description}\n\n"
        "Call rate_finding with your verdict."
    )


def judge_finding(
    chunk_code: str,
    finding_description: str,
    llm_client,
    model: str,
) -> Dict[str, str]:
    """
    Ask an independent judge pass whether *finding_description* is factually
    true of *chunk_code*. Returns {"verdict": ..., "reason": ...}.

    Raises on API failure rather than failing open — unlike
    reflection_agent.reflect() (where a broken call must never cause a real
    finding to silently vanish), a broken judge call must never be silently
    treated as a TRUE verdict, so the caller decides how to handle it.
    """
    response = llm_client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        system=_JUDGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_prompt(chunk_code, finding_description)}],
        tools=[JUDGE_TOOL],
        tool_choice={"type": "any"},
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "rate_finding":
            return {
                "verdict": block.input.get("verdict", "FALSE"),
                "reason": block.input.get("reason", ""),
            }
    return {"verdict": "FALSE", "reason": "no tool_use block in judge response"}
