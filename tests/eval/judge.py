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

Falls back to GLM 5.2 (ZhipuAI free tier) if the primary judge model's call
fails — a different backend from whatever configs/model_config.json's
"judge" role configures, so one provider's outage doesn't take Tier 3 down
entirely. Only raises if both the primary and the fallback fail.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

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


def _call_judge(llm_client, model: str, chunk_code: str, finding_description: str) -> Dict[str, str]:
    """One judge tool call against a specific client/model. Raises on API failure."""
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
                "model_used": model,
            }
    return {"verdict": "FALSE", "reason": "no tool_use block in judge response", "model_used": model}


def judge_finding(
    chunk_code: str,
    finding_description: str,
    llm_client,
    model: str,
) -> Dict[str, str]:
    """
    Ask an independent judge pass whether *finding_description* is factually
    true of *chunk_code*. Returns {"verdict": ..., "reason": ..., "model_used": ...}.

    If the primary call fails, falls back once to GLM 5.2 (ZhipuAI free tier,
    via LLMClientFactory.create_provider("FREE")) — a different backend from
    the configured judge model, so a single provider's outage doesn't take
    Tier 3 down entirely. "model_used" reports whichever model actually
    produced the verdict, so a fallback firing is never silent.

    Raises only if BOTH the primary and the fallback fail — a broken judge
    call must never be silently treated as a TRUE verdict, so the caller
    decides how to handle a total failure.
    """
    try:
        return _call_judge(llm_client, model, chunk_code, finding_description)
    except Exception as primary_exc:
        logger.warning(
            "[Judge] Primary call failed (model=%s): %s — falling back to GLM 5.2",
            model, primary_exc,
        )
        from tools.llm_client import LLMClientFactory
        fallback_client = LLMClientFactory.create_provider("FREE")
        try:
            return _call_judge(fallback_client, fallback_client.model_name, chunk_code, finding_description)
        except Exception as fallback_exc:
            raise RuntimeError(
                f"Judge failed on both the primary model ({model}: {primary_exc}) "
                f"and the GLM 5.2 fallback ({fallback_exc})"
            ) from fallback_exc
