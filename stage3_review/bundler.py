"""
File-bundling — group small, low-risk chunks from the same file into one
LLM call instead of N separate calls.

This is the genuine time/throughput lever borrowed from Alibaba
open-code-review: their sub-agents exist for concurrency/scale (process many
review units at once), and the actual throughput lever under a fixed external
rate limit (GLM free tier: 45-50 req/min including retries — not a
worker-count limit) is fewer total calls, not more concurrent workers.
Bundling reduces call count for the "boring majority" of chunks in a typical
PR regardless of tier.

Deliberately narrow scope:
  - Only chunks whose rule categories are EXACTLY the baseline set (see
    change_impact.BASELINE_RULE_CATEGORIES) are eligible — no
    architecture/performance/testing signal fired. This also sidesteps any
    "wrong rules applied to a bundle member" risk by construction, since
    every member needs the same rule set anyway.
  - Only chunks from the SAME FILE are bundled together (same file implies
    same language, so a bundle's rules_section can be built once).
  - Bundle size and combined line count are capped to keep prompts bounded.
  - Caching is deliberately out of scope for bundles this pass — see
    stage3_review/llm_reviewer.py's review_bundle().
"""

from __future__ import annotations

from typing import Dict, List, Set, Tuple

from core.models import CodeChunk
from stage3_review.change_impact import BASELINE_RULE_CATEGORIES

MAX_BUNDLE_SIZE = 5
MAX_BUNDLE_LINES = 200


def is_bundleable(categories: Set[str]) -> bool:
    """True if *categories* is exactly the baseline set — no extra risk signal fired."""
    return categories == set(BASELINE_RULE_CATEGORIES)


def bundle_chunks(
    chunks_with_categories: List[Tuple[CodeChunk, Set[str]]],
) -> Tuple[List[List[CodeChunk]], List[Tuple[CodeChunk, Set[str]]]]:
    """
    Partition (chunk, categories) pairs into same-file bundles (baseline-only,
    size/line-capped) and a leftover list that must be reviewed individually.

    Args:
        chunks_with_categories: (chunk, categories) pairs, already computed by
            change_impact.select_rule_categories() for each chunk.

    Returns:
        (bundles, individual) — bundles is a list of chunk-lists (each with
        2+ members); individual keeps its (chunk, categories) pairs so the
        caller can route them through the normal single-chunk review path
        (a would-be "bundle" of exactly one leftover chunk is returned here,
        not as a size-1 bundle — no bundling benefit, and it keeps that
        chunk's content-hash cache eligibility).
    """
    bundleable_by_file: Dict[str, List[CodeChunk]] = {}
    individual: List[Tuple[CodeChunk, Set[str]]] = []

    for chunk, categories in chunks_with_categories:
        if is_bundleable(categories):
            bundleable_by_file.setdefault(chunk.file_path, []).append(chunk)
        else:
            individual.append((chunk, categories))

    bundles: List[List[CodeChunk]] = []
    for file_chunks in bundleable_by_file.values():
        current: List[CodeChunk] = []
        current_lines = 0
        for chunk in file_chunks:
            chunk_lines = chunk.end_line - chunk.start_line + 1
            if current and (
                len(current) >= MAX_BUNDLE_SIZE
                or current_lines + chunk_lines > MAX_BUNDLE_LINES
            ):
                bundles.append(current)
                current = []
                current_lines = 0
            current.append(chunk)
            current_lines += chunk_lines
        if len(current) == 1:
            individual.append((current[0], set(BASELINE_RULE_CATEGORIES)))
        elif current:
            bundles.append(current)

    return bundles, individual
