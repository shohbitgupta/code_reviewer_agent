"""
Change Impact Analysis — deterministic, no-LLM risk signals for a chunk.

Single source of truth for "how risky is this chunk" so the signal is
computed once and consumed by more than one thing: today, the blast-radius
skip inside Stage 3's dispatch loop; later, a Phase-B specialist router.
Nothing here calls an LLM — it's pure graph/lookup arithmetic, same as
stage3_review/context_builder.py's arch-issue lookups (which this module
reuses rather than duplicates).

Note: `BLAST_RADIUS_THRESHOLD` was previously declared in stage3_review/agent.py
and described in its module docstring as gating a "blast-radius skip," but no
such computation was actually wired up — the constant was unused. This module
is the real implementation of that documented-but-missing behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Set

from core.models import CodeChunk, ReviewIssue

# Chunks with >= this many unique direct callers+callees get a blast-radius
# note instead of a full LLM review — high fan-out infrastructure code is
# rarely where defects hide, but reviewing it naively drags in enormous context.
BLAST_RADIUS_THRESHOLD = 50

# Path substrings that mark a chunk as security-sensitive regardless of its
# other risk signals. Single source of truth — stage3_review/agent.py's
# _select_model() imports this rather than keeping its own copy.
SECURITY_PATHS = frozenset({
    "auth", "crypt", "token", "password", "secret", "jwt",
    "oauth", "signin", "login", "session", "credential", "key",
    "permission", "role", "access", "secure",
})

# Rule categories shown to every chunk regardless of risk signals. Security stays
# here (not gated behind a routing condition) because today every dispatched chunk
# is already checked against all SEC* rules regardless of file path — only SEC001
# has a mechanical equivalent, SEC002-004 rely entirely on this LLM pass, so gating
# security behind e.g. is_security_path would be a real coverage regression.
BASELINE_RULE_CATEGORIES = frozenset({"security", "style", "complexity", "error_handling"})

# Independent from BLAST_RADIUS_THRESHOLD (50, above which a chunk skips the LLM
# entirely) — these gate which *extra* rule categories join the baseline for
# chunks that DO reach the LLM. Reusing 50 here would be dead code, since nothing
# at or above it ever reaches this check.
PERFORMANCE_BLAST_RADIUS_THRESHOLD = 10
TESTING_BLAST_RADIUS_THRESHOLD = 8


@dataclass
class RiskSignals:
    """Deterministic risk profile for one chunk, computed from the dependency graph."""
    blast_radius:        int   = 0     # unique direct callers + callees (depth=1)
    has_cycle:           bool  = False  # ARCH001 — chunk sits in a circular call chain
    has_layer_violation: bool  = False  # ARCH002 — this file makes a cross-domain call
    is_orphan:           bool  = False  # ARCH003 — zero incoming edges
    is_high_coupling:    bool  = False  # ARCH004 — out-degree over threshold
    is_security_path:    bool  = False  # file path matches a security-sensitive pattern
    is_pre_flagged:      bool  = False  # mechanical rule checker already found a violation
    is_changed:          bool  = False  # chunk is known to be part of the current diff


def is_security_path(file_path: str) -> bool:
    """True if *file_path* matches any known security-sensitive path pattern."""
    lower = file_path.lower()
    return any(pattern in lower for pattern in SECURITY_PATHS)


def blast_radius(chunk: CodeChunk, dep_graph) -> int:
    """Unique union of this chunk's direct callees and callers (depth=1), excluding itself."""
    if dep_graph is None:
        return 0
    try:
        deps = dep_graph.get_dependencies(chunk.chunk_id, depth=1)
        dependents = dep_graph.get_dependents(chunk.chunk_id, depth=1)
    except Exception:
        return 0
    ids = {
        meta.get("chunk_id")
        for meta in (deps + dependents)
        if meta.get("chunk_id") and meta.get("chunk_id") != chunk.chunk_id
    }
    return len(ids)


def compute_risk_signals(
    chunk: CodeChunk,
    dep_graph,
    changed_ids: Optional[Set[str]] = None,
) -> RiskSignals:
    """
    Compute the full RiskSignals profile for *chunk* — no LLM calls, safe to call per-chunk.

    is_changed is deliberately strict: True only when changed_ids is given AND
    contains this chunk. Unlike stage3_review/agent.py's _select_chunks(), which
    treats "no diff info" as "everything is changed" for review-priority *ordering*,
    this answers "do we actually know this chunk is part of a diff" — that should be
    False, not True, when there's no diff info at all.
    """
    has_cycle = False
    has_layer_violation = False
    is_orphan = False
    is_high_coupling = False
    if dep_graph is not None:
        try:
            has_cycle = any(
                chunk.chunk_id in cycle for cycle in getattr(dep_graph, "_cycles", [])[:10]
            )
            has_layer_violation = any(
                v.get("from_file") == chunk.file_path
                for v in getattr(dep_graph, "_violations", [])
            )
            is_orphan = any(
                o.get("chunk_id") == chunk.chunk_id for o in getattr(dep_graph, "_orphans", [])
            )
            is_high_coupling = any(
                c.get("chunk_id") == chunk.chunk_id
                for c in getattr(dep_graph, "_high_coupling", [])
            )
        except Exception:
            pass

    return RiskSignals(
        blast_radius=blast_radius(chunk, dep_graph),
        has_cycle=has_cycle,
        has_layer_violation=has_layer_violation,
        is_orphan=is_orphan,
        is_high_coupling=is_high_coupling,
        is_security_path=is_security_path(chunk.file_path),
        is_pre_flagged=bool(chunk.pre_flagged_violations),
        is_changed=changed_ids is not None and chunk.chunk_id in changed_ids,
    )


def select_rule_categories(risk: RiskSignals, chunk_layer: str) -> Set[str]:
    """
    Decide which rule categories apply to one chunk's single review call.

    This is NOT a specialist router — it doesn't change how many LLM calls happen,
    only which rules are visible in the one call every chunk already gets (matches
    Alibaba open-code-review's "fine-grained rule matching... keeps the model's
    attention sharply focused" — narrowing what's checked, not multiplying calls).
    """
    categories = set(BASELINE_RULE_CATEGORIES)

    if risk.has_cycle or risk.has_layer_violation or risk.is_high_coupling:
        categories.add("architecture")
    # Layer-aware nudge: Clean Architecture / layer-boundary rules matter most for
    # domain and data-layer code, regardless of whether a graph signal already fired.
    if chunk_layer in ("domain", "data"):
        categories.add("architecture")

    if risk.blast_radius >= PERFORMANCE_BLAST_RADIUS_THRESHOLD:
        categories.add("performance")

    if risk.is_changed and risk.blast_radius >= TESTING_BLAST_RADIUS_THRESHOLD:
        categories.add("testing")

    return categories


def blast_radius_note(chunk: CodeChunk, radius: int) -> ReviewIssue:
    """Build the ARCH005 INFO issue emitted instead of a full LLM review for high-blast-radius chunks."""
    return ReviewIssue.new(
        chunk_id=chunk.chunk_id,
        rule_id="ARCH005",
        severity="INFO",
        title="High blast-radius chunk — reviewed at reduced depth",
        description=(
            f"{chunk.symbol_name} has {radius} direct callers/callees, at or above the "
            f"blast-radius threshold ({BLAST_RADIUS_THRESHOLD}). This is a deliberate "
            "cost/attention decision, not a detection failure — high fan-out "
            "infrastructure code is rarely where defects hide, but reviewing it "
            "naively drags in enormous context."
        ),
        file_path=chunk.file_path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        suggestion="Consider a targeted manual review given this chunk's wide blast radius.",
        language=chunk.language,
        category="architecture",
        confidence=1.0,
        layer=chunk.layer,
    )
