"""
Eval harness — golden-dataset regression gate for review quality.

Deliberately scoped to be fully deterministic and free to run: no LLM calls,
no API key, no network. Two kinds of cases:

  1. Mechanical-rule recall/precision — small hand-written snippets under
     tests/golden/<case>/input.py + expected.json, checked against the
     (pure-Python, deterministic) MechanicalRuleChecker. This is where new
     specialist-recall cases get added once Phase B/C exist — at that point
     gated behind a skip-if-no-API-key, not before.
  2. Evidence Validation / Consensus unit cases — synthetic CodeChunk /
     ReviewContext / ReviewIssue objects exercising the new Phase D/E logic
     directly, since those are algorithms over constructed inputs rather
     than "known source, known finding" snippets.

Run with:
    pytest tests/test_eval_golden.py -s -v
"""

import sys
from pathlib import Path

import pytest

# ── Make project root importable ──────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import ChunkType, CodeChunk, ReviewIssue
from stage1_ingestion.rule_checker import MechanicalRuleChecker
from stage3_review.context_builder import ReviewContext
from stage3_review.evidence_validator import validate
from stage3_review.issue_deduplicator import IssueDeduplicator
from tests.eval.dataset import load_case


# ── Helpers ───────────────────────────────────────────────────────────────────

def _banner(title: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print("═" * 60)


def _make_chunk(**overrides) -> CodeChunk:
    """A minimal FUNCTION CodeChunk for the synthetic Evidence/Consensus cases below."""
    defaults = dict(
        chunk_id="c1",
        repo_name="synthetic",
        file_path="app/services/cart_repository.py",
        language="python",
        chunk_type=ChunkType.FUNCTION,
        symbol_name="do_work",
        start_line=10,
        end_line=20,
        content="def do_work():\n    return True\n",
    )
    defaults.update(overrides)
    return CodeChunk(**defaults)


# ══════════════════════════════════════════════════════════════════════════════
#  Mechanical-rule recall/precision (golden snippets)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("case_name", ["hardcoded_secret", "magic_number", "clean_function"])
def test_golden_mechanical_case(case_name: str):
    """Every golden snippet's mechanical findings must match its expected.json exactly."""
    _banner(f"GOLDEN — {case_name}")
    case = load_case(None, case_name)

    violations = MechanicalRuleChecker().check(case.chunk)
    found = [{"rule_id": v.rule_id, "severity": v.severity} for v in violations]

    print(f"  Expected : {case.must_find}")
    print(f"  Found    : {found}")

    for must in case.must_find:
        assert any(
            f["rule_id"] == must["rule_id"] and f["severity"] == must["severity"]
            for f in found
        ), f"{case_name}: expected to find {must}, got {found}"

    if not case.must_find:
        assert not found, f"{case_name}: expected zero violations, got {found}"

    print(f"  ✓ {case_name} passed")


# ══════════════════════════════════════════════════════════════════════════════
#  Evidence Validation (Phase D)
# ══════════════════════════════════════════════════════════════════════════════

def test_evidence_validation_accepts_self_contained_finding():
    """A finding with no evidence claims and an in-bounds line must pass untouched."""
    chunk = _make_chunk()
    ctx = ReviewContext(chunk=chunk)
    raw = {"line": 12, "rule_id": "GEN002", "evidence": []}

    is_valid, reason = validate(raw, chunk, ctx)

    assert is_valid, reason


def test_evidence_validation_accepts_grounded_evidence():
    """Evidence naming a symbol that was actually shown in context must pass."""
    chunk = _make_chunk()
    dep = _make_chunk(chunk_id="c2", symbol_name="save_to_disk", file_path="app/db/writer.py")
    ctx = ReviewContext(chunk=chunk, dependencies=[dep])
    raw = {"line": 12, "rule_id": "SEC001", "evidence": ["save_to_disk"]}

    is_valid, reason = validate(raw, chunk, ctx)

    assert is_valid, reason


def test_evidence_validation_rejects_out_of_bounds_line():
    """A cited line outside the chunk's own range must be rejected, not clamped."""
    chunk = _make_chunk(start_line=10, end_line=20)
    ctx = ReviewContext(chunk=chunk)
    raw = {"line": 9999, "rule_id": "GEN001", "evidence": []}

    is_valid, reason = validate(raw, chunk, ctx)

    assert not is_valid
    assert "outside this chunk's range" in reason


def test_evidence_validation_rejects_unfounded_evidence_claim():
    """
    The narrower, filesystem-independent half of Design 1's walkthrough: a
    specialist cites a symbol/dependency that was never shown in this call's
    context at all, and the finding must be rejected before it ever reaches
    Consensus/publish. (A real "does a companion test file exist on disk"
    check — the exact scenario in Design 1's walkthrough — needs chunk_map/
    file_manifest wiring that's deferred to the Testing specialist work;
    this validator only checks claims against what was actually retrieved.)
    """
    chunk = _make_chunk(symbol_name="cart_repository", file_path="app/data/cart_repository.py")
    ctx = ReviewContext(chunk=chunk)  # nothing retrieved — no dependencies/similar/arch_issues
    raw = {
        "line": 12,
        "rule_id": "PERF001",
        "evidence": ["external_payment_gateway_v3"],
    }

    is_valid, reason = validate(raw, chunk, ctx)

    assert not is_valid
    assert "does not match anything shown" in reason


# ══════════════════════════════════════════════════════════════════════════════
#  Consensus / cross-specialist agreement (Phase E scaffolding)
# ══════════════════════════════════════════════════════════════════════════════

def _make_issue(**overrides) -> ReviewIssue:
    defaults = dict(
        chunk_id="c1",
        rule_id="SEC010",
        severity="MEDIUM",
        title="t",
        description="d",
        file_path="app/services/cart_repository.py",
        start_line=42,
        end_line=42,
        suggestion="s",
        language="python",
        category="security",
        confidence=0.7,
        agent_type="general",
    )
    defaults.update(overrides)
    return ReviewIssue.new(**defaults)


def test_consensus_merges_cross_specialist_agreement():
    """Two distinct agent_types flagging the same area must merge into one corroborated finding."""
    a = _make_issue(rule_id="SEC010", agent_type="security", severity="HIGH", confidence=0.8)
    b = _make_issue(rule_id="ARCH002", agent_type="architecture", severity="MEDIUM",
                     confidence=0.7, start_line=43)

    result = IssueDeduplicator(min_confidence=0.5).deduplicate([a, b])

    assert len(result) == 1, f"expected the pair to merge into one finding, got {len(result)}"
    survivor = result[0]
    assert survivor.agent_type == "security"        # HIGH beats MEDIUM as primary
    assert survivor.corroborated_by == ["architecture"]
    assert survivor.confidence == pytest.approx(0.95)  # 0.8 + 0.15 boost


def test_consensus_leaves_single_specialist_findings_untouched():
    """Today's single agent_type ('general') must never trigger a merge — this stays inert."""
    a = _make_issue(rule_id="GEN001", start_line=10, agent_type="general")
    b = _make_issue(rule_id="GEN002", start_line=11, agent_type="general")

    result = IssueDeduplicator(min_confidence=0.5).deduplicate([a, b])

    assert len(result) == 2
    assert all(i.corroborated_by == [] for i in result)
