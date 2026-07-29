"""
Fine-grained rule matching + file-bundling + reflection — regression gate.

Covers the "pivot to Alibaba's model" work: one LLM call per chunk (or per
bundle of small chunks), rule categories narrowed by risk signal instead of
a per-concern specialist fan-out, plus a reflection pass over the final
issue list. Deterministic and free to run: no LLM/network except through
fake clients constructed in-test.

Run with:
    pytest tests/test_fine_grained_review.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import ChunkType, CodeChunk, ReviewIssue
from stage2_standards.agent import Rule, Severity, build_review_prompt_rules
from stage1_ingestion.graph_builder import DependencyGraph
from stage3_review.agent import run_review
from stage3_review.bundler import MAX_BUNDLE_SIZE, bundle_chunks, is_bundleable
from stage3_review.change_impact import BASELINE_RULE_CATEGORIES, RiskSignals, select_rule_categories
from stage3_review.reflection_agent import _should_reflect, reflect


def _chunk(cid, file_path="a.py", symbol="foo", start=1, end=5, layer="presentation", content="x"):
    return CodeChunk(
        chunk_id=cid, repo_name="r", file_path=file_path, language="python",
        chunk_type=ChunkType.FUNCTION, symbol_name=symbol, start_line=start, end_line=end,
        content=content, layer=layer,
    )


def _issue(rule_id, severity, line=1, file_path="a.py"):
    return ReviewIssue.new(
        chunk_id="c", rule_id=rule_id, severity=severity, title="t", description="d",
        file_path=file_path, start_line=line, end_line=line, suggestion="s",
        language="python", category="style",
    )


# ── select_rule_categories ────────────────────────────────────────────────────

def test_boring_chunk_gets_baseline_only():
    assert select_rule_categories(RiskSignals(), "presentation") == set(BASELINE_RULE_CATEGORIES)


def test_architecture_triggers_on_cycle_or_layer_violation_or_high_coupling():
    for kwargs in ({"has_cycle": True}, {"has_layer_violation": True}, {"is_high_coupling": True}):
        assert "architecture" in select_rule_categories(RiskSignals(**kwargs), "presentation")


def test_architecture_triggers_on_domain_or_data_layer_regardless_of_signals():
    for layer in ("domain", "data"):
        assert "architecture" in select_rule_categories(RiskSignals(), layer)
    assert "architecture" not in select_rule_categories(RiskSignals(), "presentation")


def test_performance_triggers_on_blast_radius_independent_of_the_50_skip_threshold():
    assert "performance" not in select_rule_categories(RiskSignals(blast_radius=5), "presentation")
    assert "performance" in select_rule_categories(RiskSignals(blast_radius=10), "presentation")


def test_testing_requires_both_changed_and_blast_radius():
    assert "testing" not in select_rule_categories(
        RiskSignals(blast_radius=9, is_changed=False), "presentation"
    )
    assert "testing" in select_rule_categories(
        RiskSignals(blast_radius=9, is_changed=True), "presentation"
    )


def test_security_always_included_no_regression_vs_todays_behavior():
    """Every chunk must still be checked against security rules regardless of path."""
    assert "security" in select_rule_categories(RiskSignals(), "presentation")
    assert "security" in select_rule_categories(RiskSignals(is_security_path=False), "infrastructure")


# ── build_review_prompt_rules categories filter ───────────────────────────────

def test_build_review_prompt_rules_filters_by_category():
    standards = [
        Rule(rule_id="SEC001", language="all", category="security", severity=Severity.CRITICAL,
             title="t", description="d"),
        Rule(rule_id="PERF001", language="all", category="performance", severity=Severity.HIGH,
             title="t", description="d"),
    ]
    security_only = build_review_prompt_rules(standards, "python", categories={"security"})
    assert "SEC001" in security_only
    assert "PERF001" not in security_only

    unfiltered = build_review_prompt_rules(standards, "python")
    assert "PERF001" in unfiltered


# ── bundler ────────────────────────────────────────────────────────────────────

def test_bundleable_only_for_exact_baseline_set():
    assert is_bundleable(set(BASELINE_RULE_CATEGORIES))
    assert not is_bundleable(set(BASELINE_RULE_CATEGORIES) | {"architecture"})


def test_bundle_chunks_groups_same_file_baseline_only():
    baseline = set(BASELINE_RULE_CATEGORIES)
    chunks = [
        (_chunk("a", "f.py", start=1, end=5), baseline),
        (_chunk("b", "f.py", start=6, end=10), baseline),
        (_chunk("c", "f.py", start=11, end=15), baseline),
    ]
    bundles, individual = bundle_chunks(chunks)
    assert len(bundles) == 1 and len(bundles[0]) == 3
    assert not individual


def test_bundle_chunks_excludes_non_baseline_and_singleton_bundles():
    baseline = set(BASELINE_RULE_CATEGORIES)
    arch = baseline | {"architecture"}
    chunks = [
        (_chunk("a", "f.py"), baseline),      # alone in its file -> falls back to individual
        (_chunk("b", "g.py"), arch),          # needs architecture -> never bundled
    ]
    bundles, individual = bundle_chunks(chunks)
    assert not bundles
    assert {c.chunk_id for c, _ in individual} == {"a", "b"}


def test_bundle_chunks_respects_size_cap():
    baseline = set(BASELINE_RULE_CATEGORIES)
    chunks = [(_chunk(f"c{i}", "h.py", start=i, end=i), baseline) for i in range(12)]
    bundles, individual = bundle_chunks(chunks)
    assert not individual
    assert sum(len(b) for b in bundles) == 12
    assert all(len(b) <= MAX_BUNDLE_SIZE for b in bundles)


# ── reflection agent ───────────────────────────────────────────────────────────

def test_should_reflect_cost_gate():
    assert not _should_reflect([_issue("GEN002", "LOW")])
    assert _should_reflect([_issue("SEC001", "HIGH")])
    assert _should_reflect([_issue("GEN002", "LOW"), _issue("GEN001", "LOW", line=2)])


def test_reflect_fails_open_on_error():
    class _ExplodingClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("simulated failure")

    issues = [_issue("GEN002", "LOW"), _issue("GEN001", "LOW", line=2)]
    refined, stats = reflect(issues, _ExplodingClient(), "model")
    assert len(refined) == 2
    assert stats["reflection_calls"] == 0


def test_reflect_drops_and_revises_per_decision():
    class _FakeMessages:
        def create(self, **kwargs):
            class Block:
                type = "tool_use"
                name = "refine_findings"
                input = {"decisions": [
                    {"index": 0, "keep": True, "revised_description": "revised"},
                    {"index": 1, "keep": False, "reason_if_dropped": "false positive"},
                ]}
            class Resp:
                content = [Block()]
            return Resp()

    class _FakeClient:
        messages = _FakeMessages()

    issues = [_issue("GEN002", "LOW"), _issue("GEN001", "LOW", line=2)]
    refined, stats = reflect(issues, _FakeClient(), "model")
    assert len(refined) == 1
    assert refined[0].description == "revised"
    assert stats == {"reflection_calls": 1, "reflection_dropped": 1, "reflection_revised": 1}


# ── end-to-end smoke tests through run_review (fake client) ──────────────────

def test_run_review_bundles_boring_chunks_and_routes_by_category():
    c1 = _chunk("c1", "app/ui/widgets.py", "foo", 1, 3)
    c2 = _chunk("c2", "app/ui/widgets.py", "bar", 5, 7)
    c3 = _chunk("c3", "app/ui/widgets.py", "baz", 9, 11)
    c4 = _chunk("c4", "app/domain/order.py", "checkout", 1, 5, layer="domain")
    chunks = [c1, c2, c3, c4]
    chunk_map = {c.chunk_id: c for c in chunks}
    dep_graph = DependencyGraph()
    dep_graph.build(chunks=chunks, edges=[])
    dep_graph.analyse()

    standards = [
        Rule(rule_id="SEC001", language="all", category="security", severity=Severity.CRITICAL,
             title="t", description="d"),
        Rule(rule_id="CP006", language="all", category="architecture", severity=Severity.HIGH,
             title="t", description="d"),
    ]
    state = {"chunks": chunks, "chunk_map": chunk_map, "dependency_graph": dep_graph,
             "standards": standards, "changed_chunk_ids": None, "bm25_index_path": None}

    calls = []

    class _FakeMessages:
        def create(self, **kwargs):
            calls.append(kwargs["messages"][0]["content"])
            is_bundle = "CHUNK 0 UNDER REVIEW" in calls[-1]
            class Block:
                type = "tool_use"
                name = "report_issues"
                if is_bundle:
                    input = {"issues": [{"rule_id": "SEC001", "severity": "LOW", "title": "t",
                                          "description": "d", "line": 1, "suggestion": "s",
                                          "confidence": 0.9, "chunk_index": 0}]}
                else:
                    # LOW severity + single finding for this file deliberately keeps
                    # reflection's cost gate from firing — this test is scoped to
                    # bundling behavior only (reflection has its own dedicated tests).
                    input = {"issues": [{"rule_id": "CP006", "severity": "LOW", "title": "t",
                                          "description": "d", "line": 1, "suggestion": "s",
                                          "confidence": 0.9}]}
            class Resp:
                content = [Block()]
                usage = None
            return Resp()

    class _FakeClient:
        messages = _FakeMessages()
        provider = "fake"
        model_name = "fake-model"

    out = run_review(dict(state), llm_client=_FakeClient(), skip_llm=False)

    assert len(calls) == 2, "expected exactly 2 LLM calls: 1 bundle + 1 individual"
    assert out["review_stats"]["bundles_used"] == 1
    assert out["review_stats"]["chunks_bundled"] == 3
    assert out["review_stats"]["chunks_reviewed"] == 4
    bundle_issue = next(i for i in out["issues"] if i.rule_id == "SEC001")
    assert bundle_issue.chunk_id == "c1"  # chunk_index 0 resolved to the right bundle member
