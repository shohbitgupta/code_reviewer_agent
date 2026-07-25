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


_MAX_PARAMS = 5

class LongParameterListRule(MechanicalRule):
    """CP013 — Functions must not have more than 5 parameters."""

    rule_id  = "CP013"
    severity = "MEDIUM"
    title    = "Long parameter list (> 5 parameters)"

    def check(self, chunk: CodeChunk) -> List[RuleViolation]:
        if chunk.chunk_type not in (ChunkType.FUNCTION, ChunkType.METHOD):
            return []

        # Scan the first 15 lines — where the signature lives even for multi-line sigs
        sig_text = "\n".join(chunk.content.splitlines()[:15])
        count = self._count_top_level_params(sig_text)

        # Python methods: exclude `self` / `cls` from the caller-visible count
        if chunk.language == "python" and chunk.chunk_type == ChunkType.METHOD:
            count = max(0, count - 1)

        if count <= _MAX_PARAMS:
            return []

        return [RuleViolation(
            rule_id     = self.rule_id,
            severity    = self.severity,
            title       = self.title,
            description = (
                f"'{chunk.symbol_name}' has {count} parameters (limit {_MAX_PARAMS}). "
                "Group related parameters into a data object or split the function "
                "by responsibility."
            ),
            line = chunk.start_line,
        )]

    @staticmethod
    def _count_top_level_params(text: str) -> int:
        """
        Count parameters by tracking parenthesis depth and tallying top-level commas.
        Correctly handles nested generics, lambdas, and default-value expressions.
        """
        depth = 0
        top_commas = 0
        in_params = False
        for ch in text:
            if ch == "(":
                depth += 1
                if depth == 1:
                    in_params = True
            elif ch == ")":
                if depth == 1 and in_params:
                    # Check whether anything was between the parens
                    break
                depth -= 1
            elif ch == "," and depth == 1:
                top_commas += 1
        if not in_params:
            return 0
        # 0 commas at depth=1 means either 0 or 1 params;
        # we can't tell without inspecting content, so treat it as 1 (safe)
        return top_commas + 1


_NETWORK_CALL_RE = re.compile(
    r"""(?x)
    URLSession\s*\.          # URLSession.shared / URLSession(configuration:)
  | URLRequest\s*\(          # URLRequest(url:)
  | \.dataTask\s*\(          # .dataTask(with:)
  | \.uploadTask\s*\(        # .uploadTask(with:)
  | \.downloadTask\s*\(      # .downloadTask(with:)
  | Alamofire\s*\.           # Alamofire.request / Alamofire.upload
  | \bAF\s*\.request\s*\(    # AF.request(
  | \bAF\s*\.upload\s*\(     # AF.upload(
  | \bAF\s*\.download\s*\(   # AF.download(
    """,
)

class CleanArchNetworkRule(MechanicalRule):
    """SW008 — ViewControllers must not call URLSession or Alamofire directly."""

    rule_id  = "SW008"
    severity = "HIGH"
    title    = "No direct network calls from ViewController (Clean Architecture)"

    def check(self, chunk: CodeChunk) -> List[RuleViolation]:
        if chunk.language != "swift":
            return []
        if chunk.chunk_type not in (ChunkType.FUNCTION, ChunkType.METHOD):
            return []

        # Determine whether this chunk lives inside a ViewController class.
        in_viewcontroller = (
            ("ViewController" in (chunk.parent_symbol or ""))
            or ("ViewController" in chunk.file_path)
        )
        if not in_viewcontroller:
            return []

        violations = []
        for i, line in enumerate(chunk.content.splitlines(), start=chunk.start_line):
            stripped = line.strip()
            if stripped.startswith("//"):
                continue
            if _NETWORK_CALL_RE.search(line):
                violations.append(RuleViolation(
                    rule_id     = self.rule_id,
                    severity    = self.severity,
                    title       = self.title,
                    description = (
                        f"Direct network call on line {i} inside a ViewController. "
                        "Move this call to a dedicated service or repository class "
                        "and inject it via a protocol — this keeps the UI layer "
                        "decoupled from the network layer and testable without a live connection."
                    ),
                    line = i,
                ))
        return violations[:3]  # cap at 3 per chunk to avoid noise


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

    _GOD_CLASS_MAX_METHODS = 10

    def __init__(self) -> None:
        self._rules: List[MechanicalRule] = [
            FunctionLengthRule(),
            HardcodedSecretRule(),
            MagicNumberRule(),
            MissingTypeHintRule(),
            LongParameterListRule(),
            CleanArchNetworkRule(),
        ]

    def check(self, chunk: CodeChunk) -> List[RuleViolation]:
        """Run all per-chunk rules against *chunk* and return the combined violation list."""
        violations: List[RuleViolation] = []
        for rule in self._rules:
            try:
                violations.extend(rule.check(chunk))
            except Exception:
                pass   # a broken rule must never crash the pipeline
        return violations

    def check_many(self, chunks: List[CodeChunk]) -> int:
        """
        In-place: set pre_flagged_violations on every chunk.

        Two passes:
          1. Per-chunk rules (all MechanicalRule subclasses above).
          2. Class-level GodClass rule (CP012) — requires cross-chunk context.

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

        flagged += self._check_god_classes(chunks)
        return flagged

    def _check_god_classes(self, chunks: List[CodeChunk]) -> int:
        """
        CP012 — God Class detector.

        Groups METHOD chunks by parent_symbol, flags any class whose public
        method count exceeds _GOD_CLASS_MAX_METHODS.  The violation is stamped
        on the CLASS_HEAD chunk (or the first method chunk as a fallback).
        """
        from collections import defaultdict

        # Index class-head chunks and count methods per class
        class_heads: dict = {}
        method_counts: dict = defaultdict(list)

        for chunk in chunks:
            if chunk.chunk_type == ChunkType.CLASS_HEAD:
                class_heads[chunk.symbol_name] = chunk
            elif chunk.chunk_type == ChunkType.METHOD and chunk.parent_symbol:
                method_counts[chunk.parent_symbol].append(chunk)

        flagged = 0
        for class_name, methods in method_counts.items():
            if len(methods) <= self._GOD_CLASS_MAX_METHODS:
                continue

            target = class_heads.get(class_name, methods[0])
            target.pre_flagged_violations.append(RuleViolation(
                rule_id     = "CP012",
                severity    = "HIGH",
                title       = "God class: too many responsibilities",
                description = (
                    f"'{class_name}' has {len(methods)} methods "
                    f"(limit {self._GOD_CLASS_MAX_METHODS}). "
                    "Extract cohesive groups of methods into separate, focused classes."
                ),
                line = target.start_line,
            ))
            flagged += 1

        return flagged
