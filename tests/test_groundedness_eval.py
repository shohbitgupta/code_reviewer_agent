"""
Groundedness eval suite — Tiers 1-4.

Measures whether the review pipeline's findings are not just *grounded*
(Evidence Validation already checks citations are real — see
tests/test_eval_golden.py) but *factually correct*. A hallucination can be
coherently grounded: a real line, a real symbol lifted from real context,
wrapped around a conclusion that's simply false. Evidence Validation cannot
catch that structurally; these tiers exist to measure it.

Tier 1 — Precision/Recall/F1 against the expanded golden dataset
          (tests/golden/{security,architecture,performance,testing,style}/),
          run through the actual production prompt/rules/LLM path.
Tier 2 — Grounding-integrity audit (free, deterministic): re-examines Tier 1's
          raw findings for weak/coincidental evidence matches.
Tier 3 — LLM-as-judge factual audit, blind to the target rule, with a planted
          negative-control case to verify the judge itself works.
Tier 4 — Adversarial cases: false-positive traps (tests/golden/adversarial/),
          a bundle chunk_index attribution stress test, and a reflection audit.

Tiers 1, 3, and the reflection half of Tier 4 need a real LLM call and are
skipped (not failed) without an API key. Tier 2 and the bundling half of
Tier 4 are fully deterministic and always run.

Run with:
    pytest tests/test_groundedness_eval.py -s -v
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import config
from core.models import ChunkType, CodeChunk, ReviewIssue
from stage2_standards.agent import Rule, StandardsLoader, build_review_prompt_rules
from stage3_review.bundler import bundle_chunks
from stage3_review.change_impact import BASELINE_RULE_CATEGORIES
from stage3_review.context_builder import ContextBuilder, ReviewContext, format_context_for_prompt
from stage3_review.evidence_validator import validate as validate_evidence
from stage3_review.llm_reviewer import LLMReviewer
from stage3_review.prompt_builder import SYSTEM_PROMPT, PromptBuilder
from stage3_review.reflection_agent import reflect
from tests.eval import metrics
from tests.eval.dataset import GoldenCase, list_cases, load_case
from tests.eval.grounding_audit import audit_terms_specificity
from tests.eval.judge import judge_finding

_HAS_API_KEY = bool(
    os.getenv("ZHIPUAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    or os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_API_KEY")
)
_needs_llm = pytest.mark.skipif(not _HAS_API_KEY, reason="no LLM API key configured")

_CATEGORIES = ["security", "architecture", "performance", "testing", "style"]

# Intentionally lenient starting thresholds for a small, hand-authored golden
# set — the point is to catch a systemic regression (a prompt/rule change
# that tanks quality), not to demand perfection from a single sampled run
# against a nondeterministic model. Tighten once a real baseline exists.
_MIN_AGGREGATE_RECALL = 0.6
_MIN_AGGREGATE_PRECISION = 0.6


def _banner(title: str) -> None:
    print(f"\n{'═' * 70}")
    print(f"  {title}")
    print("═" * 70)


def _table(headers: List[str], rows: List[List[str]]) -> None:
    widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
              for i, h in enumerate(headers)]
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("  " + "  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*[str(v)[:widths[i]] for i, v in enumerate(row)]))


def _all_standards() -> List[Rule]:
    """The real standards set, loaded the same way Stage 2 loads it in production."""
    return StandardsLoader().load()


class _FakeToolBlock:
    """Mimics one tool_use content block from an Anthropic-shaped response."""

    def __init__(self, name: str, input_data: Dict) -> None:
        self.type = "tool_use"
        self.name = name
        self.input = input_data


class _FakeResponse:
    def __init__(self, content: List) -> None:
        self.content = content


class _FakeMessages:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def create(self, **kwargs) -> _FakeResponse:
        return self._response


class _FakeClient:
    """A scripted stand-in for LLMClientFactory's client — no network, deterministic output."""

    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessages(response)


def _review_golden_case(case: GoldenCase, standards: List[Rule], llm_client, model: str) -> List[Dict]:
    """
    Run the real production prompt/rules/LLM path against one isolated golden
    chunk. Bypasses risk-signal routing (already unit-tested in
    tests/test_fine_grained_review.py) and directly requests the category
    this case is meant to exercise, plus the baseline — this tier measures
    rule-application quality, not the routing decision.
    """
    ctx_builder = ContextBuilder(chunk_map={case.chunk.chunk_id: case.chunk}, dep_graph=None)
    ctx = ctx_builder.build(case.chunk)
    context_section = format_context_for_prompt(ctx)

    categories = set(BASELINE_RULE_CATEGORIES) | {case.category}
    rules_section = build_review_prompt_rules(standards, case.chunk.language, categories=categories)
    relevant_rule_ids = [
        r.rule_id for r in standards
        if r.applies_to(case.chunk.language) and r.category in categories
    ]

    prompt_builder = PromptBuilder()
    user_prompt = prompt_builder.build_user(
        chunk=case.chunk, context_section=context_section,
        rules_section=rules_section, pre_flagged=[],
    )

    # No cache — eval runs want a fresh judgment every time, not a stale
    # cached result from a previous prompt/rule iteration.
    reviewer = LLMReviewer(llm_client=llm_client, cache_dir=None)
    raw_issues, _ = reviewer.review(
        system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt, chunk=case.chunk,
        rule_ids=relevant_rule_ids, model=model,
    )
    return raw_issues


def _write_eval_report(report: Dict) -> Path:
    out_dir = Path(config.WORKSPACE_ROOT) / "eval_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{int(time.time())}.json"
    path.write_text(json.dumps(report, indent=2))
    return path


# ══════════════════════════════════════════════════════════════════════════════
#  Tier 1 — Precision / Recall / F1 against the expanded golden dataset
# ══════════════════════════════════════════════════════════════════════════════

@_needs_llm
def test_tier1_precision_recall_f1():
    """Run every categorized golden case through the real prompt/rules/LLM path."""
    _banner("TIER 1 — Precision / Recall / F1")

    from tools.llm_client import LLMClientFactory
    llm_client = LLMClientFactory.create()
    standards = _all_standards()

    rows = []
    per_case_results = {}
    all_results = []
    for category in _CATEGORIES:
        for name in list_cases(category):
            case = load_case(category, name)
            raw_issues = _review_golden_case(case, standards, llm_client, config.REVIEW_MODEL)
            found = [{"rule_id": r.get("rule_id"), "severity": r.get("severity")} for r in raw_issues]
            result = metrics.evaluate(found, case.must_find, case.must_not_find)
            per_case_results[f"{category}/{name}"] = {
                "found": found, "result": result.__dict__,
            }
            all_results.append(result)
            rows.append(result.as_row(f"{category}/{name}"))

    _table(["case", "TP", "FN", "FP", "unexpected", "precision", "recall", "f1"], rows)

    aggregate = metrics.aggregate(all_results)
    print()
    _table(["AGGREGATE", "TP", "FN", "FP", "unexpected", "precision", "recall", "f1"],
           [aggregate.as_row("all cases")])

    report = {
        "tier": 1, "timestamp": time.time(), "model": config.REVIEW_MODEL,
        "cases": per_case_results, "aggregate": aggregate.__dict__,
    }
    report_path = _write_eval_report(report)
    print(f"\n  Report written: {report_path}")

    assert aggregate.recall >= _MIN_AGGREGATE_RECALL, (
        f"aggregate recall {aggregate.recall:.2f} below floor {_MIN_AGGREGATE_RECALL} "
        "— the pipeline is missing known violations it used to catch"
    )
    assert aggregate.precision >= _MIN_AGGREGATE_PRECISION, (
        f"aggregate precision {aggregate.precision:.2f} below floor {_MIN_AGGREGATE_PRECISION} "
        "— the pipeline is over-triggering on explicitly-clean code"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Tier 2 — Grounding-integrity audit (free, deterministic)
# ══════════════════════════════════════════════════════════════════════════════

def test_tier2_grounding_audit_catches_generic_evidence_evidence_validator_accepts():
    """
    Pins the known soft spot in evidence_validator._terms_match(): a short,
    generic evidence term (e.g. "data") substring-matches a real dependency
    name shown in context (e.g. "get_user_data") and passes validate() as
    "grounded" — even though it demonstrates no specific knowledge of that
    dependency. audit_terms_specificity() exists precisely to catch what
    validate() alone cannot: not "is this term real" but "is this term
    specific enough to trust."
    """
    _banner("TIER 2 — Grounding-integrity audit")

    chunk = CodeChunk(
        chunk_id="c1", repo_name="synthetic", file_path="app/services/profile.py",
        language="python", chunk_type=ChunkType.FUNCTION, symbol_name="render_profile",
        start_line=10, end_line=20, content="def render_profile(uid):\n    return uid\n",
    )
    dep = CodeChunk(
        chunk_id="c2", repo_name="synthetic", file_path="app/services/user_service.py",
        language="python", chunk_type=ChunkType.FUNCTION, symbol_name="get_user_data",
        start_line=1, end_line=5, content="def get_user_data(uid): ...\n",
    )
    ctx = ReviewContext(chunk=chunk, dependencies=[dep])

    generic_evidence = {"line": 12, "rule_id": "GEN002", "evidence": ["data"]}
    is_valid, reason = validate_evidence(generic_evidence, chunk, ctx)
    assert is_valid, (
        f"expected evidence_validator to accept the coincidental substring match "
        f"('data' in 'get_user_data') this test pins — got rejected: {reason}"
    )

    audit = audit_terms_specificity(["data"])
    assert audit["weak"] == ["data"], (
        "audit_terms_specificity must flag 'data' as weak evidence even though "
        "evidence_validator.validate() accepts it as grounded"
    )

    specific_evidence = {"line": 12, "rule_id": "GEN002", "evidence": ["get_user_data"]}
    is_valid, reason = validate_evidence(specific_evidence, chunk, ctx)
    assert is_valid, reason
    audit = audit_terms_specificity(["get_user_data"])
    assert audit["specific"] == ["get_user_data"], (
        "a real, specific symbol name must not be flagged as weak"
    )

    print("  ✓ generic term ('data'): accepted by evidence_validator, correctly flagged weak by audit")
    print("  ✓ specific term ('get_user_data'): accepted by both, correctly not flagged")


# ══════════════════════════════════════════════════════════════════════════════
#  Tier 3 — LLM-as-judge factual audit
# ══════════════════════════════════════════════════════════════════════════════

@_needs_llm
def test_tier3_llm_judge_factual_audit():
    """
    An independent judge rates a real finding and a fabricated one, blind to
    which rule either was meant to enforce. The fabricated finding is the
    meta-check the plan calls for: an eval harness that never fails its own
    negative control isn't verifying anything — if the judge can't catch an
    obviously false claim about a chunk it's looking straight at, none of its
    other verdicts (used in Tier 4's reflection audit) can be trusted either.
    """
    _banner("TIER 3 — LLM-as-judge factual audit")

    from tools.llm_client import LLMClientFactory
    llm_client = LLMClientFactory.create()

    sql_case = load_case("security", "sql_injection")

    true_finding = (
        "This function builds a SQL query by directly concatenating a raw, "
        "user-supplied value into the query string, instead of using a "
        "parameterized query placeholder — allowing SQL injection."
    )
    # Negative control: plausible-sounding, but describes something that is
    # simply not in this code (there is no logging call anywhere in it).
    false_finding = (
        "This function logs the constructed SQL query, including the raw "
        "user-supplied value, to the application logs on every call — "
        "leaking user data into log storage."
    )

    rows = []
    verdicts = {}
    for label, finding_text in [
        ("true_finding", true_finding),
        ("false_finding_negative_control", false_finding),
    ]:
        verdict = judge_finding(sql_case.chunk.content, finding_text, llm_client, config.REVIEW_MODEL)
        verdicts[label] = verdict
        rows.append([label, verdict.get("verdict", ""), verdict.get("reason", "")[:60]])

    _table(["case", "verdict", "reason"], rows)

    assert verdicts["true_finding"]["verdict"] in ("TRUE", "PARTIALLY_TRUE"), (
        f"judge rated a real, factually-correct finding as "
        f"{verdicts['true_finding']['verdict']} — judge prompt is too strict or broken"
    )
    assert verdicts["false_finding_negative_control"]["verdict"] == "FALSE", (
        "judge failed its own negative control (rated a fabricated finding as "
        f"{verdicts['false_finding_negative_control']['verdict']}) — the judge is "
        "not trustworthy until this passes; do not trust any other judge verdict "
        "produced by this suite until it's fixed"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Tier 4 — Adversarial / stress cases
# ══════════════════════════════════════════════════════════════════════════════

@_needs_llm
def test_tier4_adversarial_false_positive_traps():
    """
    Runs tests/golden/adversarial/ cases (a placeholder credential, a
    well-named constant comparison) through the real prompt/rules/LLM path.
    These are deliberately designed to look like violations without being
    one — a stricter check than Tier 1's aggregate threshold: zero tolerance
    per case, since each one mirrors an exemption the mechanical checker
    (stage1_ingestion/rule_checker.py) already gets right.
    """
    _banner("TIER 4 — Adversarial false-positive traps")

    from tools.llm_client import LLMClientFactory
    llm_client = LLMClientFactory.create()
    standards = _all_standards()

    rows = []
    failures = []
    for name in list_cases("adversarial"):
        case = load_case("adversarial", name)
        raw_issues = _review_golden_case(case, standards, llm_client, config.REVIEW_MODEL)
        found = [{"rule_id": r.get("rule_id"), "severity": r.get("severity")} for r in raw_issues]
        result = metrics.evaluate(found, case.must_find, case.must_not_find)
        rows.append(result.as_row(f"adversarial/{name}"))
        if result.false_positives > 0:
            failures.append((name, found))

    _table(["case", "TP", "FN", "FP", "unexpected", "precision", "recall", "f1"], rows)

    assert not failures, (
        f"adversarial false-positive trap(s) triggered: {failures} — the LLM pass "
        "is more trigger-happy than the mechanical checker it's layered on top of"
    )


def test_tier4_bundle_chunk_index_attribution_stress_test():
    """
    5 near-identical small chunks from the same file, bundled into one call
    (see stage3_review/bundler.py) — a planted violation in member 3 must
    attribute back to exactly chunk_index 3, not bleed into a neighbor as
    the members get harder to tell apart. Deterministic (fake client) — this
    tests attribution plumbing, not LLM judgment.
    """
    _banner("TIER 4 — Bundle chunk_index attribution stress test")

    chunks = [
        CodeChunk(
            chunk_id=f"bundle-{i}", repo_name="synthetic", file_path="app/utils/helpers.py",
            language="python", chunk_type=ChunkType.FUNCTION, symbol_name=f"helper_{i}",
            start_line=i * 10 + 1, end_line=i * 10 + 5,
            content=f"def helper_{i}(x):\n    return x + {i}\n",
        )
        for i in range(5)
    ]

    categories = set(BASELINE_RULE_CATEGORIES)
    bundles, individual = bundle_chunks([(c, categories) for c in chunks])
    assert not individual, f"expected all 5 near-identical chunks to bundle together, got {len(individual)} leftover"
    assert len(bundles) == 1 and len(bundles[0]) == 5

    planted_index = 3
    fake_response = _FakeResponse([
        _FakeToolBlock("report_issues", {
            "issues": [{
                "rule_id": "GEN002", "severity": "LOW", "title": "Magic number",
                "description": f"helper_{planted_index} adds a bare literal",
                "line": chunks[planted_index].start_line + 1,
                "suggestion": "Use a named constant.", "confidence": 0.8,
                "chunk_index": planted_index,
            }],
        })
    ])
    reviewer = LLMReviewer(llm_client=_FakeClient(fake_response), cache_dir=None)
    raw_issues = reviewer.review_bundle(
        system_prompt=SYSTEM_PROMPT, user_prompt="(irrelevant — fake client ignores it)",
        label="app/utils/helpers.py (bundle of 5)", model=config.REVIEW_MODEL,
    )

    assert len(raw_issues) == 1
    assert raw_issues[0]["chunk_index"] == planted_index, (
        f"planted violation in member {planted_index} attributed to chunk_index "
        f"{raw_issues[0].get('chunk_index')} instead — attribution bled across "
        "near-identical bundle members"
    )
    print(f"  ✓ violation planted in member {planted_index} attributed correctly")


@_needs_llm
def test_tier4_reflection_audit_drops_semantically_false_finding():
    """
    Feeds reflection (stage3_review/reflection_agent.py) one real finding and
    one finding describing something that simply isn't in the file (no
    logging call exists anywhere in this chunk) — not a literal duplicate of
    anything else in the list, so a dedup-style check wouldn't catch it.
    Confirms reflection's semantic second look drops it on its own judgment.
    """
    _banner("TIER 4 — Reflection audit")

    from tools.llm_client import LLMClientFactory
    llm_client = LLMClientFactory.create()

    sql_case = load_case("security", "sql_injection")
    common = dict(
        chunk_id=sql_case.chunk.chunk_id, file_path=sql_case.chunk.file_path,
        start_line=sql_case.chunk.start_line, end_line=sql_case.chunk.end_line,
        language="python", category="security", confidence=0.9, agent_type="general",
    )
    true_issue = ReviewIssue.new(
        rule_id="SEC002", severity="HIGH",
        title="SQL injection via string concatenation",
        description=(
            "The query string is built by directly concatenating a raw "
            "user-supplied value instead of using a parameterized placeholder."
        ),
        suggestion="Use a parameterized query.", **common,
    )
    false_issue = ReviewIssue.new(
        rule_id="SEC003", severity="HIGH",
        title="Credential logged in plaintext",
        description="This function logs the constructed query and the user's email to the application logs.",
        suggestion="Never log credentials or PII.", **common,
    )

    refined, stats = reflect([true_issue, false_issue], llm_client, config.REVIEW_MODEL)
    kept_rule_ids = {i.rule_id for i in refined}

    print(f"  reflection stats: {stats}")
    print(f"  kept rule_ids: {sorted(kept_rule_ids)}")

    assert "SEC002" in kept_rule_ids, "reflection dropped the real finding — over-aggressive"
    assert "SEC003" not in kept_rule_ids, (
        "reflection kept a finding describing something not actually in the file "
        "(there is no logging call anywhere in this chunk) — reflection's second "
        "look is only catching literal duplicates, not semantically false findings"
    )
