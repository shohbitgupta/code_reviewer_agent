"""
Golden dataset loader — tests/golden/<category>/<case>/{input.<ext>, expected.json}
or the legacy flat tests/golden/<case>/ layout (category=None) that
tests/test_eval_golden.py's original 3 mechanical cases already use.

Generalizes what was test_eval_golden.py's own private _load_golden_case() so
both the mechanical-rule tests and the new groundedness eval tiers share one
loader instead of two copies drifting apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from core.models import ChunkType, CodeChunk

GOLDEN_DIR = Path(__file__).parent.parent / "golden"

_EXT_BY_LANGUAGE = {
    "python": "py", "dart": "dart", "kotlin": "kt", "swift": "swift", "rust": "rs",
}


@dataclass
class GoldenCase:
    """One golden case: a real CodeChunk plus its expected findings."""
    name:                    str
    category:                str            # "misc" for the legacy flat layout
    chunk:                   CodeChunk
    must_find:               List[Dict] = field(default_factory=list)
    must_not_find:           List[Dict] = field(default_factory=list)
    expected_evidence_terms: List[str]  = field(default_factory=list)
    description:             str        = ""


def _case_dir(category: Optional[str], name: str) -> Path:
    return (GOLDEN_DIR / category / name) if category else (GOLDEN_DIR / name)


def load_case(category: Optional[str], name: str) -> GoldenCase:
    """Load one golden case. category=None reads the legacy flat layout."""
    case_dir = _case_dir(category, name)
    expected = json.loads((case_dir / "expected.json").read_text())

    ext = _EXT_BY_LANGUAGE.get(expected["language"], "py")
    input_path = case_dir / f"input.{ext}"
    if not input_path.exists():
        input_path = case_dir / "input.py"   # legacy cases are always .py
    source = input_path.read_text()
    lines = source.splitlines()

    # Deliberately NOT placed under a literal tests/... on-disk path — that
    # would match stage1_ingestion/rule_checker.py's _TEST_PATH_RE (tests/
    # specs/mocks/fixtures routinely hold placeholder credentials by design)
    # and silently suppress SEC001. A realistic app/ path exercises rules the
    # way they actually run against real application code.
    subdir = f"{category}/" if category else ""
    chunk = CodeChunk(
        chunk_id=f"golden-{category}-{name}" if category else f"golden-{name}",
        repo_name="golden-dataset",
        file_path=f"app/golden/{subdir}{name}.{ext}",
        language=expected["language"],
        chunk_type=ChunkType(expected["chunk_type"]),
        symbol_name=name,
        start_line=1,
        end_line=len(lines),
        content=source,
        layer=expected.get("layer", "unknown"),
    )

    return GoldenCase(
        name=name,
        category=category or "misc",
        chunk=chunk,
        must_find=expected.get("must_find", []),
        must_not_find=expected.get("must_not_find", []),
        expected_evidence_terms=expected.get("expected_evidence_terms", []),
        description=expected.get("description", ""),
    )


def list_cases(category: str) -> List[str]:
    """Case names under tests/golden/<category>/ (empty if the directory doesn't exist)."""
    cat_dir = GOLDEN_DIR / category
    if not cat_dir.is_dir():
        return []
    return sorted(p.name for p in cat_dir.iterdir() if p.is_dir())
