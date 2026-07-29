"""
Static $/token pricing table used by the budget guard and event spine to
estimate the cost of an LLM call.

There is no live pricing API to query, so these figures are approximate
snapshots and need periodic manual updates as providers change pricing.
Unknown models fall back to a $0 estimate (logged once) rather than raising,
since an estimation failure should never block a review.
"""

from __future__ import annotations

import logging
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

# model name (or prefix) -> (usd per 1K prompt tokens, usd per 1K completion tokens)
_PRICE_PER_1K_TOKENS: Dict[str, Tuple[float, float]] = {
    # FREE tier — no cost regardless of volume.
    "z-ai/glm-5.2-free": (0.0, 0.0),
    # Anthropic (approximate, as of this writing).
    "claude-opus-4-8":   (15.0 / 1000, 75.0 / 1000),
    "claude-sonnet-5":   (3.0 / 1000, 15.0 / 1000),
    "claude-haiku-4-5":  (0.8 / 1000, 4.0 / 1000),
    # OpenAI (approximate, as of this writing).
    "gpt-4o":            (2.5 / 1000, 10.0 / 1000),
    "gpt-4o-mini":       (0.15 / 1000, 0.6 / 1000),
}

_warned_unknown_models: set = set()


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate the USD cost of one LLM call from its token usage.

    Matches `model` by exact name first, then by longest known prefix, so a
    versioned/dated model id (e.g. "gpt-4o-2024-08-06") still resolves.
    Returns 0.0 for unrecognized models — logged once per model name so a
    missing price entry is visible without spamming the log.
    """
    rates = _PRICE_PER_1K_TOKENS.get(model)
    if rates is None:
        match = next(
            (name for name in _PRICE_PER_1K_TOKENS if model.startswith(name)),
            None,
        )
        rates = _PRICE_PER_1K_TOKENS.get(match) if match else None

    if rates is None:
        if model not in _warned_unknown_models:
            _warned_unknown_models.add(model)
            logger.warning(
                "[pricing] No price entry for model=%r — estimating $0.00 "
                "(add it to tools/pricing.py._PRICE_PER_1K_TOKENS)",
                model,
            )
        return 0.0

    prompt_rate, completion_rate = rates
    return (prompt_tokens / 1000) * prompt_rate + (completion_tokens / 1000) * completion_rate
