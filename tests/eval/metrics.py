"""
Precision/recall/F1 for the groundedness eval suite (Tier 1).

Pure functions over three lists of {"rule_id": ..., "severity": ...} dicts:
  found:         what the pipeline actually reported for one golden case
  must_find:     required true positives (recall)
  must_not_find:  explicit false-positive traps (precision)

must_not_find is deliberately NOT "everything not in must_find" — a golden
case can't enumerate every conceivable wrong answer, so anything the pipeline
found that's neither an expected true positive nor an explicitly forbidden
false positive is reported separately as "unexpected", not silently folded
into the precision score in either direction. This keeps precision honest:
it only measures what the case author actually asserted, not an implicit
claim of completeness the case never made.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


def _matches(found_item: Dict, expected_item: Dict) -> bool:
    """rule_id must match; severity only checked if the expected item specifies one."""
    if found_item.get("rule_id") != expected_item.get("rule_id"):
        return False
    expected_severity = expected_item.get("severity")
    return expected_severity is None or found_item.get("severity") == expected_severity


@dataclass
class EvalResult:
    true_positives:  int
    false_negatives: int
    false_positives: int
    unexpected:      int   # found, but neither expected nor explicitly forbidden
    precision:       float
    recall:          float
    f1:              float

    def as_row(self, label: str) -> List[str]:
        """One row for a _table()-style summary print."""
        return [
            label,
            str(self.true_positives), str(self.false_negatives), str(self.false_positives),
            str(self.unexpected),
            f"{self.precision:.2f}", f"{self.recall:.2f}", f"{self.f1:.2f}",
        ]


def evaluate(found: List[Dict], must_find: List[Dict], must_not_find: List[Dict]) -> EvalResult:
    """Score one golden case's actual findings against its expected.json."""
    tp = sum(1 for mf in must_find if any(_matches(f, mf) for f in found))
    fn = len(must_find) - tp

    fp = sum(1 for f in found if any(_matches(f, mnf) for mnf in must_not_find))

    accounted = sum(
        1 for f in found
        if any(_matches(f, mf) for mf in must_find) or any(_matches(f, mnf) for mnf in must_not_find)
    )
    unexpected = len(found) - accounted

    if tp + fp == 0:
        # No true or false positives to judge — either nothing was found (a
        # correct "clean" verdict, precision trivially perfect) or everything
        # found was unaccounted-for (precision undefined by this case's own
        # assertions; reported via `unexpected` above, not folded in here).
        precision = 1.0
    else:
        precision = tp / (tp + fp)

    recall = tp / len(must_find) if must_find else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    return EvalResult(
        true_positives=tp, false_negatives=fn, false_positives=fp, unexpected=unexpected,
        precision=precision, recall=recall, f1=f1,
    )


def aggregate(results: List[EvalResult]) -> EvalResult:
    """Micro-average across cases: sum the counts, recompute the rates from the totals."""
    tp = sum(r.true_positives for r in results)
    fn = sum(r.false_negatives for r in results)
    fp = sum(r.false_positives for r in results)
    unexpected = sum(r.unexpected for r in results)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    return EvalResult(
        true_positives=tp, false_negatives=fn, false_positives=fp, unexpected=unexpected,
        precision=precision, recall=recall, f1=f1,
    )
