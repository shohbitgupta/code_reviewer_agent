"""
Step 1g-RC — Mechanical Rule Checker

Runs deterministic coding-standard rules against every CodeChunk immediately
after chunking (Step 1g), before any LLM token is spent.  Violations are
stored on ``CodeChunk.pre_flagged_violations`` so that Stage 3 can surface
them even when the LLM endpoint is unavailable, and avoid redundant analysis
for chunks that already have a clear mechanical finding.

Rules implemented
-----------------
GEN001  Function/method length > 50 non-blank lines        (MEDIUM)
GEN002  Magic numbers in comparisons/loop bounds             (LOW)
SEC001  Hardcoded secrets (password=, api_key=, …)         (CRITICAL)
PY001   Missing type hints on public Python functions       (LOW)
CP013   Long parameter list (> 5 parameters)               (MEDIUM)
SW008   Direct network call from ViewController            (HIGH)
DA008   Dart: context used after await without mounted check (HIGH)
CP012   God class (> 10 methods on one class) [class-level] (HIGH)

Per-chunk vs. class-level rules
--------------------------------
Most rules implement ``check(chunk)`` and are called once per chunk.
CP012 (God Class) requires cross-chunk context — it is applied as a
post-pass in ``MechanicalRuleChecker.check_many()`` after all per-chunk
checks complete.  Its violation is stamped on the CLASS_HEAD chunk.

Adding a new rule
-----------------
1. For a per-chunk rule: subclass ``MechanicalRule``, implement ``check()``,
   and append an instance to ``_rules`` in ``MechanicalRuleChecker.__init__``.
2. For a class-level rule: add a ``_check_<name>`` method to
   ``MechanicalRuleChecker`` and call it from ``check_many()``.
No other file needs to change.

Usage::

    checker = MechanicalRuleChecker()
    checker.check_many(chunks)   # mutates chunk.pre_flagged_violations in-place
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

_COMMENT_ONLY_RE = re.compile(r"^\s*(?:#|//)")

class FunctionLengthRule(MechanicalRule):
    """GEN001 — Functions/methods must not exceed 50 non-blank, non-comment lines."""

    rule_id  = "GEN001"
    severity = "MEDIUM"
    title    = "Function length must not exceed 50 lines"
    _MAX     = 50

    def check(self, chunk: CodeChunk) -> List[RuleViolation]:
        if chunk.chunk_type not in (ChunkType.FUNCTION, ChunkType.METHOD):
            return []
        # GEN001's own description promises comment-only lines are excluded
        # alongside blank ones — only blanks were, letting a heavily
        # commented short function get flagged as if it were all code.
        # Covers '#' and '//' single-line markers (Python/Rust/Swift/
        # Kotlin/Dart); block comments (/* */, """ """) aren't tracked.
        non_blank = sum(
            1 for ln in chunk.content.splitlines()
            if ln.strip() and not _COMMENT_ONLY_RE.match(ln)
        )
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
       private[_-]?key|client[_-]?secret|bearer)\s*=\s*['"]([^'"]{4,})['"]
    """,
)
# Values that are obviously placeholders, not real leaked credentials.
_PLACEHOLDER_VALUE_RE = re.compile(
    r"^(?:dummy\w*|changeme|x{3,}|placeholder\w*|fake\w*|example\w*|test\w*|"
    r"your[_-].*here|\.{3,}|<[^>]*>)$",
    re.IGNORECASE,
)
# Test/mock/fixture files routinely hold placeholder credentials by design.
_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|specs?|mocks?|fixtures?|__tests__|__mocks__)(?:/|$)",
    re.IGNORECASE,
)

class HardcodedSecretRule(MechanicalRule):
    """SEC001 — No API keys, passwords, or tokens hardcoded in source."""

    rule_id  = "SEC001"
    severity = "CRITICAL"
    title    = "No hardcoded secrets"

    def check(self, chunk: CodeChunk) -> List[RuleViolation]:
        if _TEST_PATH_RE.search(chunk.file_path):
            return []
        violations = []
        for i, line in enumerate(chunk.content.splitlines(), start=chunk.start_line):
            m = _SECRET_RE.search(line)
            if m and not _PLACEHOLDER_VALUE_RE.match(m.group(1).strip()):
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
# GEN002's own documented example is a comparison ("if (retryCount > 3)") —
# an unexplained threshold or loop bound. That risk is concentrated in
# comparisons/branches/loop constructs, not in constructor or function-call
# arguments (`EdgeInsets.all(16)`, `SizedBox(height: 8)`, `Duration(seconds: 2)`),
# which make up most numeric literals in UI-heavy Dart/Swift/Kotlin code and
# are already self-documenting via the surrounding call. Gating on this
# context keeps the check aligned with its intent instead of flagging every
# layout constant in a widget tree.
_RISK_CONTEXT_RE = re.compile(r"<=|>=|==|!=|<|>|\bfor\b|\bwhile\b|\brange\(")

class MagicNumberRule(MechanicalRule):
    """GEN002 — No unexplained numeric literals in comparisons or loop bounds."""

    rule_id  = "GEN002"
    severity = "LOW"
    title    = "No magic numbers in comparisons or loop bounds"

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
            if not _RISK_CONTEXT_RE.search(stripped):
                continue   # no comparison/loop on this line — not a magic-threshold risk
            if _MAGIC_NUMBER_RE.search(stripped) and i not in seen_lines:
                seen_lines.add(i)
                violations.append(RuleViolation(
                    rule_id     = self.rule_id,
                    severity    = self.severity,
                    title       = self.title,
                    description = (
                        f"Line {i} compares against or loops using an unexplained "
                        "numeric literal. Extract it to a named constant so the "
                        "threshold's meaning doesn't have to be re-derived."
                    ),
                    line = i,
                ))
        return violations[:3]   # cap at 3 per chunk to avoid noise


_MAX_PARAMS = 5
_RECEIVER_TOKENS = {"self", "cls", "&self", "&mut self", "mut self"}

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

        # A receiver parameter (self/cls in Python, &self/&mut self in Rust)
        # isn't caller-visible — exclude it regardless of language.
        first_param = self._first_top_level_param(sig_text)
        if first_param in _RECEIVER_TOKENS:
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

        Also tracks angle-bracket depth (within the parameter list only) so a
        comma inside a generic type argument (`Map<String, Int>`, `Result<T, E>`)
        isn't mistaken for a parameter separator, and likewise tracks square-
        bracket depth for Python's subscript-style generics (`Dict[str, int]`,
        `List[int]`) — without it, a well-typed Python signature using these
        was miscounted a parameter too high. Correctly handles nested
        generics, lambdas, and default-value expressions.
        """
        paren_depth = 0
        angle_depth = 0
        bracket_depth = 0
        top_commas = 0
        in_params = False
        for ch in text:
            if ch == "(":
                paren_depth += 1
                if paren_depth == 1:
                    in_params = True
            elif ch == ")":
                if paren_depth == 1 and in_params:
                    # Check whether anything was between the parens
                    break
                paren_depth -= 1
            elif ch == "<" and paren_depth == 1:
                angle_depth += 1
            elif ch == ">" and paren_depth == 1 and angle_depth > 0:
                angle_depth -= 1
            elif ch == "[" and paren_depth == 1:
                bracket_depth += 1
            elif ch == "]" and paren_depth == 1 and bracket_depth > 0:
                bracket_depth -= 1
            elif ch == "," and paren_depth == 1 and angle_depth == 0 and bracket_depth == 0:
                top_commas += 1
        if not in_params:
            return 0
        # 0 commas at depth=1 means either 0 or 1 params;
        # we can't tell without inspecting content, so treat it as 1 (safe)
        return top_commas + 1

    @staticmethod
    def _first_top_level_param(text: str) -> str:
        """Return the first depth-1 parameter's text (whitespace-collapsed), or ''."""
        depth = 0
        in_params = False
        buf: List[str] = []
        for ch in text:
            if ch == "(":
                depth += 1
                if depth == 1:
                    in_params = True
                    continue
            elif ch == ")":
                if depth == 1:
                    break
                depth -= 1
            elif ch == "," and depth == 1:
                break
            if in_params:
                buf.append(ch)
        return re.sub(r"\s+", " ", "".join(buf)).strip()


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


_AWAIT_RE           = re.compile(r"\bawait\b")
_MOUNTED_CHECK_RE   = re.compile(r"\bmounted\b")
_CONTEXT_USE_RE     = re.compile(r"\bcontext\b")

class AsyncContextWithoutMountedCheckRule(MechanicalRule):
    """DA008 — Dart: no `context` use after an `await` without a `mounted` guard."""

    rule_id  = "DA008"
    severity = "HIGH"
    title    = "BuildContext used after async gap without a mounted check"

    def check(self, chunk: CodeChunk) -> List[RuleViolation]:
        if chunk.language != "dart":
            return []
        if chunk.chunk_type not in (ChunkType.FUNCTION, ChunkType.METHOD):
            return []

        violations = []
        awaited = False
        mounted_checked = False
        for i, line in enumerate(chunk.content.splitlines(), start=chunk.start_line):
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            if _AWAIT_RE.search(stripped):
                awaited = True
                mounted_checked = False
                continue
            if not awaited:
                continue
            if _MOUNTED_CHECK_RE.search(stripped):
                mounted_checked = True
                continue
            if _CONTEXT_USE_RE.search(stripped) and not mounted_checked:
                violations.append(RuleViolation(
                    rule_id     = self.rule_id,
                    severity    = self.severity,
                    title       = self.title,
                    description = (
                        f"Line {i} uses `context` after an `await` with no preceding "
                        "`mounted` check. If the widget was disposed while awaiting, "
                        "this throws or silently misbehaves — guard with "
                        "`if (!mounted) return;` before touching context again."
                    ),
                    line = i,
                ))
                awaited = False   # only report the first use per async gap
        return violations[:3]   # cap at 3 per chunk to avoid noise


_TYPE_HINT_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+\w+\s*\(([^)]*)\)\s*(?:->|:)",
    re.MULTILINE,   # decorated functions push `def` past the first line
)

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

        first_lines = "\n".join(chunk.content.splitlines()[:8])
        m = _TYPE_HINT_RE.search(first_lines)
        if m is None:
            return []   # can't determine — no def found in the scanned window

        params_str = m.group(1)
        # Strip self/cls — they don't need type hints
        params = [p.strip() for p in params_str.split(",") if p.strip() not in ("self", "cls", "")]
        # A param counts as typed only if IT carries a ':' before its own
        # default value — one typed param no longer masks the rest as fine.
        untyped_params = [p for p in params if ":" not in p.split("=")[0]]
        has_return = "->" in first_lines

        if not untyped_params and has_return:
            return []   # note: no params besides self/cls still requires has_return

        missing = []
        if untyped_params:
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


_OVERRIDE_RE        = re.compile(r"^\s*(?:@Override\s*\n\s*)?override\s+", re.MULTILINE | re.IGNORECASE)
_PRIVATE_KEYWORD_RE = re.compile(r"^\s*(?:private|fileprivate)\s+", re.MULTILINE)

def _is_public_own_method(chunk: CodeChunk) -> bool:
    """
    Heuristic for CP012: is this the class's own public API surface?

    Excludes framework-mandated overrides (`override func`/`@Override`) and
    language-specific private methods (Python/Dart leading underscore, Swift/
    Kotlin `private`/`fileprivate`, Rust methods lacking `pub`) — these are
    either forced by a base class or already hidden from callers, so they
    shouldn't count toward "too many responsibilities."
    """
    head = "\n".join(chunk.content.splitlines()[:3])
    if _OVERRIDE_RE.search(head):
        return False
    if _PRIVATE_KEYWORD_RE.search(head):
        return False
    if chunk.language in ("python", "dart") and chunk.symbol_name.startswith("_"):
        return False
    if chunk.language == "rust" and "pub " not in head and "pub(" not in head:
        return False
    return True


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
            AsyncContextWithoutMountedCheckRule(),
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

        Groups METHOD chunks by parent_symbol, counting only public,
        non-override methods — private helpers and framework-mandated
        overrides (`override func`, `@Override`) are implementation details
        forced by the base class, not evidence the class itself has too many
        responsibilities. Test classes (many small `test_x` methods by
        design) are exempt. Flags any class whose qualifying method count
        exceeds _GOD_CLASS_MAX_METHODS; the violation is stamped on the
        CLASS_HEAD chunk (or the first method chunk as a fallback).
        """
        from collections import defaultdict

        # Index class-head chunks and count methods per class
        class_heads: dict = {}
        method_counts: dict = defaultdict(list)

        for chunk in chunks:
            if chunk.chunk_type == ChunkType.CLASS_HEAD:
                class_heads[chunk.symbol_name] = chunk
            elif chunk.chunk_type == ChunkType.METHOD and chunk.parent_symbol:
                if "test" in chunk.parent_symbol.lower():
                    continue
                if _is_public_own_method(chunk):
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
