"""
Step 1g-RC — Mechanical Rule Checker (Priority 2)

Runs auto_check=True coding-standard rules against every CodeChunk immediately
after chunking (Step 1g), before any LLM is involved.  Violations are stored
on CodeChunk.pre_flagged_violations so Stage 3 can skip the LLM for chunks
that only have mechanical violations.

Rules implemented
-----------------
GEN001  Function/method length > 50 lines              (MEDIUM)
GEN002  Magic numbers in non-constant code              (LOW)
SEC001  Hardcoded secrets (password=, api_key=, etc.)  (CRITICAL)
PY001   Missing type hints on public Python functions   (LOW)

Adding a new rule: subclass MechanicalRule and append to _RULES in
MechanicalRuleChecker.__init__.  No other file needs to change.

Usage:
    checker    = MechanicalRuleChecker()
    violations = checker.check(chunk)
    chunk.pre_flagged_violations = violations
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import List

from core.models import ChunkType, CodeChunk, RuleViolation


# ── Abstract rule interface ───────────────────────────────────────────────────

class MechanicalRule(ABC):
    rule_id:  str
    severity: str
    title:    str

    @abstractmethod
    def check(self, chunk: CodeChunk) -> List[RuleViolation]:
        """Return zero or more violations found in *chunk*."""
        ...


# ── Concrete rules ────────────────────────────────────────────────────────────

class FunctionLengthRule(MechanicalRule):
    """GEN001 — Functions/methods must not exceed 50 non-blank lines."""

    rule_id  = "GEN001"
    severity = "MEDIUM"
    title    = "Function length must not exceed 50 lines"
    _MAX     = 50

    def check(self, chunk: CodeChunk) -> List[RuleViolation]:
        if chunk.chunk_type not in (ChunkType.FUNCTION, ChunkType.METHOD):
            return []
        non_blank = sum(1 for ln in chunk.content.splitlines() if ln.strip())
        if non_blank <= self._MAX:
            return []
        return [RuleViolation(
            rule_id     = self.rule_id,
            severity    = self.severity,
            title       = self.title,
            description = (
                f"'{chunk.symbol_name}' is {non_blank} non-blank lines "
                f"(limit {self._MAX}). Extract sub-functions to reduce length."
            ),
            line        = chunk.start_line,
        )]


_SECRET_RE = re.compile(
    r"""(?ix)
    (?:password|passwd|secret|api[_-]?key|auth[_-]?token|access[_-]?token|
       private[_-]?key|client[_-]?secret|bearer)\s*=\s*['"][^'"]{4,}['"]
    """,
)

class HardcodedSecretRule(MechanicalRule):
    """SEC001 — No API keys, passwords, or tokens hardcoded in source."""

    rule_id  = "SEC001"
    severity = "CRITICAL"
    title    = "No hardcoded secrets"

    def check(self, chunk: CodeChunk) -> List[RuleViolation]:
        violations = []
        for i, line in enumerate(chunk.content.splitlines(), start=chunk.start_line):
            if _SECRET_RE.search(line):
                violations.append(RuleViolation(
                    rule_id     = self.rule_id,
                    severity    = self.severity,
                    title       = self.title,
                    description = (
                        f"Possible hardcoded secret on line {i}. "
                        "Move credentials to environment variables or a secrets manager."
                    ),
                    line = i,
                ))
        return violations


_MAGIC_NUMBER_RE = re.compile(
    r"""(?<!\w)         # not preceded by a word char (avoids version strings)
    (?<![\.\-])         # not a float continuation or negative
    (?:
        [2-9]\d{2,}     # 200+
      | [1-9]\d{1,}     # 10–99
      | [3-9]           # 3–9 (skip 0,1,2 which are almost always valid)
    )
    (?!\w)              # not followed by a word char
    """,
    re.VERBOSE,
)
_MAGIC_SKIP_PATTERNS = re.compile(
    r"(?:def |class |import |from |#|\"\"\"|\'\'\'"
    r"|const |CONSTANT|_MAX|_MIN|_DEFAULT|_SIZE|_LIMIT"
    r"|=\s*\d+\s*$)"   # bare constant assignments are fine
)

class MagicNumberRule(MechanicalRule):
    """GEN002 — No magic numbers in non-constant, non-trivial contexts."""

    rule_id  = "GEN002"
    severity = "LOW"
    title    = "No magic numbers"

    def check(self, chunk: CodeChunk) -> List[RuleViolation]:
        if chunk.chunk_type not in (ChunkType.FUNCTION, ChunkType.METHOD):
            return []
        violations = []
        seen_lines: set = set()
        for i, line in enumerate(chunk.content.splitlines(), start=chunk.start_line):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if _MAGIC_SKIP_PATTERNS.search(stripped):
                continue
            if _MAGIC_NUMBER_RE.search(stripped) and i not in seen_lines:
                seen_lines.add(i)
                violations.append(RuleViolation(
                    rule_id     = self.rule_id,
                    severity    = self.severity,
                    title       = self.title,
                    description = (
                        f"Line {i} contains a magic number. "
                        "Extract to a named constant for readability."
                    ),
                    line = i,
                ))
        return violations[:3]   # cap at 3 per chunk to avoid noise


_TYPE_HINT_RE = re.compile(
    r"^\s*def\s+\w+\s*\(([^)]*)\)\s*(?:->|:)",
)
_PARAM_TYPED_RE = re.compile(r"\w+\s*:")  # at least one param has a type hint

class MissingTypeHintRule(MechanicalRule):
    """PY001 — All public Python functions must have type hints."""

    rule_id  = "PY001"
    severity = "LOW"
    title    = "Missing type hints on public function"

    def check(self, chunk: CodeChunk) -> List[RuleViolation]:
        if chunk.language != "python":
            return []
        if chunk.chunk_type not in (ChunkType.FUNCTION, ChunkType.METHOD):
            return []
        if chunk.symbol_name.startswith("_"):
            return []   # private — not required

        first_lines = "\n".join(chunk.content.splitlines()[:5])
        m = _TYPE_HINT_RE.search(first_lines)
        if m is None:
            return []   # can't determine — no def found in first 5 lines

        params_str = m.group(1)
        # Strip self/cls — they don't need type hints
        params = [p.strip() for p in params_str.split(",") if p.strip() not in ("self", "cls", "")]
        if not params:
            return []   # no parameters — return type hint would need separate check

        has_hints = _PARAM_TYPED_RE.search(params_str)
        has_return = "->" in first_lines
        if has_hints and has_return:
            return []

        missing = []
        if not has_hints:
            missing.append("parameter types")
        if not has_return:
            missing.append("return type")

        return [RuleViolation(
            rule_id     = self.rule_id,
            severity    = self.severity,
            title       = self.title,
            description = (
                f"'{chunk.symbol_name}' is missing {' and '.join(missing)}. "
                "Add type hints to all public functions."
            ),
            line = chunk.start_line,
        )]


# ── Orchestrator ──────────────────────────────────────────────────────────────

class MechanicalRuleChecker:
    """
    Runs all auto_check=True rules against a CodeChunk.

    Usage::

        checker = MechanicalRuleChecker()
        for chunk in chunks:
            chunk.pre_flagged_violations = checker.check(chunk)
    """

    def __init__(self) -> None:
        self._rules: List[MechanicalRule] = [
            FunctionLengthRule(),
            HardcodedSecretRule(),
            MagicNumberRule(),
            MissingTypeHintRule(),
        ]

    def check(self, chunk: CodeChunk) -> List[RuleViolation]:
        """Run all rules against *chunk* and return the combined violation list."""
        violations: List[RuleViolation] = []
        for rule in self._rules:
            try:
                violations.extend(rule.check(chunk))
            except Exception:
                pass   # a broken rule must never crash the pipeline
        return violations

    def check_many(self, chunks: List[CodeChunk]) -> None:
        """
        In-place: set pre_flagged_violations on every chunk.

        Skips MODULE and IMPORT chunks — they are structural, not behavioral.
        """
        skip = {ChunkType.MODULE, ChunkType.IMPORT}
        flagged = 0
        for chunk in chunks:
            if chunk.chunk_type in skip:
                continue
            chunk.pre_flagged_violations = self.check(chunk)
            if chunk.pre_flagged_violations:
                flagged += 1
        return flagged
