"""
Standards Agent — Stage 2 of the code review pipeline.

Reads coding rules from the `standards/` directory (one file per language)
and writes them into the shared ReviewState so the reviewer agent can query
rules by language, severity, or category.

Directory layout (preferred):
    standards/
        general.md   ← GEN + SEC rules, language = "all"
        python.md    ← PY rules
        kotlin.md    ← KT rules
        rust.md      ← RS rules
        swift.md     ← SW rules
        dart.md      ← DA rules

Fallback: if `standards/` does not exist, falls back to `coding_standards.md`
in the project root (single-file legacy format).

Adding a new language: drop a new `standards/<language>.md` file — no code
changes required.

Usage:
    from stage2_standards.agent import run_standards

    state = run_standards(state)
    # state["standards"] → List[Rule]

    # Retrieve rules for a specific file:
    python_rules = [r for r in state["standards"] if r.language in ("python", "all")]
    critical     = [r for r in state["standards"] if r.severity == Severity.CRITICAL]
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Default paths ────────────────────────────────────────────────────────────
_STAGE_ROOT              = Path(__file__).parent
_DEFAULT_STANDARDS_DIR   = _STAGE_ROOT / "rules"                   # stage2_standards/rules/
_DEFAULT_STANDARDS_FILE  = _STAGE_ROOT.parent / "coding_standards.md"  # legacy fallback


# ── Domain models ─────────────────────────────────────────────────────────────

class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"
    LOW      = "LOW"
    INFO     = "INFO"

    @classmethod
    def _missing_(cls, value):
        """Graceful fallback for unexpected severity strings."""
        return cls.INFO


@dataclass
class Rule:
    """One coding standard rule."""
    rule_id:     str           # "PY001", "GEN003", "SEC001"
    language:    str           # "python" | "kotlin" | "rust" | "swift" | "dart" | "all"
    category:    str           # "naming" | "complexity" | "security" | "style" | "architecture" | ...
    severity:    Severity
    title:       str
    description: str
    bad_example:  str = ""
    good_example: str = ""
    auto_check:  bool = False  # True = rule can be verified programmatically

    def applies_to(self, language: str) -> bool:
        """Return True if this rule applies to *language* (or is universal)."""
        return self.language in (language.lower(), "all")

    def to_prompt_line(self) -> str:
        """One-line representation for injection into an LLM prompt."""
        return f"[{self.rule_id}] ({self.severity.value}) {self.title}"

    def to_dict(self) -> dict:
        """Serialise to a plain dict (JSON-friendly; Severity enum → its string value)."""
        return {
            "rule_id":     self.rule_id,
            "language":    self.language,
            "category":    self.category,
            "severity":    self.severity.value,
            "title":       self.title,
            "description": self.description,
            "bad_example":  self.bad_example,
            "good_example": self.good_example,
            "auto_check":  self.auto_check,
        }


# ── Parser ────────────────────────────────────────────────────────────────────

class StandardsParser:
    """
    Parses a coding_standards.md file into a list of Rule objects.

    Markdown format expected:
        ## Section Name          → category grouping (lower-cased, spaces→_)
        ### RULE_ID — Title      → one Rule
        - **Severity**: X
        - **Language**: X
        - **Category**: X        (optional override; falls back to section)
        - Body text              → description
        - **Bad:** ...           → bad_example  (may span multiple lines until **Good:**)
        - **Good:** ...          → good_example (may span to next ### or EOF)
    """

    # Matches "### PY001 — Title" or "### PY001 - Title"
    _RULE_HEADING = re.compile(r"^###\s+([A-Z]{2,6}\d{3,4})\s+[—\-]+\s+(.+)$")
    _SECTION_HEADING = re.compile(r"^##\s+(.+)$")
    _META_LINE = re.compile(r"^\s*-\s+\*\*(\w[\w\s]*?)\*\*:\s*(.+)$")

    def parse(self, text: str) -> List[Rule]:
        """
        Parse a standards markdown document into a list of Rule objects.

        Walks the text line by line, tracking the current section (category
        fallback) and the currently-open rule block; each "### RULE_ID —
        Title" heading starts a new Rule, which is flushed to *rules* when
        the next heading (or EOF) is reached.

        Args:
            text: Full contents of one standards *.md file.

        Returns:
            One Rule per "### RULE_ID — Title" heading found.
        """
        rules: List[Rule] = []
        lines = text.splitlines()

        current_section_category = "general"
        current_rule_id: Optional[str] = None
        current_title   = ""
        current_language = "all"
        current_severity = Severity.INFO
        current_category = ""
        body_lines: List[str] = []

        def _flush():
            nonlocal current_rule_id
            if current_rule_id is None:
                return
            description, bad, good = _split_body(body_lines)
            rules.append(Rule(
                rule_id     = current_rule_id,
                language    = current_language.lower().strip(),
                category    = (current_category or current_section_category).lower().strip(),
                severity    = current_severity,
                title       = current_title.strip(),
                description = description.strip(),
                bad_example  = bad.strip(),
                good_example = good.strip(),
            ))
            current_rule_id = None

        for line in lines:
            # Section heading (## …)
            m_sec = self._SECTION_HEADING.match(line)
            if m_sec and not line.startswith("###"):
                _flush()
                raw_section = m_sec.group(1).strip()
                current_section_category = _normalise_category(raw_section)
                continue

            # Rule heading (### RULE_ID — Title)
            m_rule = self._RULE_HEADING.match(line)
            if m_rule:
                _flush()
                current_rule_id  = m_rule.group(1)
                current_title    = m_rule.group(2)
                current_language = "all"
                current_severity = Severity.INFO
                current_category = ""
                body_lines       = []
                continue

            if current_rule_id is None:
                continue  # outside a rule block — skip

            # Meta lines (- **Severity**: HIGH)
            m_meta = self._META_LINE.match(line)
            if m_meta:
                key   = m_meta.group(1).strip().lower()
                value = m_meta.group(2).strip()
                if key == "severity":
                    current_severity = Severity(value.upper())
                elif key == "language":
                    current_language = value
                elif key == "category":
                    current_category = value
                else:
                    body_lines.append(line)
                continue

            body_lines.append(line)

        _flush()
        return rules


def _split_body(lines: List[str]):
    """
    Split body_lines into (description, bad_example, good_example).

    We look for "- **Bad:**" and "- **Good:**" markers (case-insensitive).
    Everything before the first marker is the description.
    Code fences (```) are included verbatim.
    """
    BAD_RE  = re.compile(r"^\s*-\s+\*\*Bad[:\*]*\*\*:?\s*(.*)", re.IGNORECASE)
    GOOD_RE = re.compile(r"^\s*-\s+\*\*Good[:\*]*\*\*:?\s*(.*)", re.IGNORECASE)

    desc_lines: List[str] = []
    bad_lines:  List[str] = []
    good_lines: List[str] = []
    mode = "desc"

    for line in lines:
        if mode == "desc":
            mb = BAD_RE.match(line)
            mg = GOOD_RE.match(line)
            if mb:
                rest = mb.group(1)
                if rest:
                    bad_lines.append(rest)
                mode = "bad"
            elif mg:
                rest = mg.group(1)
                if rest:
                    good_lines.append(rest)
                mode = "good"
            else:
                desc_lines.append(line)
        elif mode == "bad":
            mg = GOOD_RE.match(line)
            if mg:
                rest = mg.group(1)
                if rest:
                    good_lines.append(rest)
                mode = "good"
            else:
                bad_lines.append(line)
        else:  # good
            good_lines.append(line)

    return (
        "\n".join(desc_lines),
        "\n".join(bad_lines),
        "\n".join(good_lines),
    )


def _normalise_category(raw: str) -> str:
    """'Python Rules' → 'python', 'Security Rules' → 'security'"""
    lower = raw.lower().strip()
    # Strip trailing " rules"
    lower = re.sub(r"\s+rules?$", "", lower).strip()
    # Replace spaces/slashes with underscore
    return re.sub(r"[\s/]+", "_", lower)


# ── Standards loader ──────────────────────────────────────────────────────────

class StandardsLoader:
    """
    Loads Rule objects from a `standards/` directory or a single fallback file.

    Resolution order
    ----------------
    1. If *standards_dir* is given and exists as a directory → load all *.md files.
    2. Else if the default `standards/` directory exists    → load all *.md files.
    3. Else if *standards_path* (single file) is given      → load that file.
    4. Else load the legacy `coding_standards.md` fallback.

    Adding a new language requires only dropping a new `standards/<lang>.md`
    file — no code changes needed.

    Args:
        standards_dir:  Optional override for the rules directory (takes
                         precedence over the default `stage2_standards/rules/`).
        standards_path: Optional override for the legacy single-file fallback.
    """

    def __init__(
        self,
        standards_dir:  Optional[Path] = None,
        standards_path: Optional[Path] = None,
    ) -> None:
        self._dir  = standards_dir
        self._file = standards_path

    def load(self) -> List[Rule]:
        """
        Load and parse Rule objects using the resolution order documented
        on this class.

        Returns:
            All parsed rules, or an empty list if no standards source was found.
        """
        parser = StandardsParser()
        rules:  List[Rule] = []

        # ── Resolve source ────────────────────────────────────────────────────
        src_dir = self._resolve_dir()
        if src_dir is not None:
            return self._load_directory(src_dir, parser)

        src_file = self._resolve_file()
        if src_file is not None:
            return self._load_file(src_file, parser)

        logger.error("[StandardsLoader] No standards directory or file found")
        return rules

    # ── Resolution helpers ────────────────────────────────────────────────────

    def _resolve_dir(self) -> Optional[Path]:
        candidate = Path(self._dir) if self._dir else _DEFAULT_STANDARDS_DIR
        if candidate.is_dir():
            return candidate
        return None

    def _resolve_file(self) -> Optional[Path]:
        candidate = Path(self._file) if self._file else _DEFAULT_STANDARDS_FILE
        if candidate.is_file():
            return candidate
        return None

    # ── Loaders ───────────────────────────────────────────────────────────────

    def _load_directory(self, directory: Path, parser: StandardsParser) -> List[Rule]:
        """Load every *.md file in *directory*, sorted by name for determinism."""
        md_files = sorted(directory.glob("*.md"))
        if not md_files:
            logger.warning("[StandardsLoader] No .md files found in %s", directory)
            return []

        all_rules: List[Rule] = []
        for md_file in md_files:
            rules = self._load_file(md_file, parser)
            all_rules.extend(rules)

        logger.info(
            "[StandardsLoader] Loaded %d rules from %d files in %s/",
            len(all_rules), len(md_files), directory.name,
        )
        return all_rules

    @staticmethod
    def _load_file(path: Path, parser: StandardsParser) -> List[Rule]:
        """Parse *path*; returns [] (and logs the error) if it cannot be read."""
        try:
            text  = path.read_text(encoding="utf-8")
            rules = parser.parse(text)
            logger.debug("[StandardsLoader] %s → %d rules", path.name, len(rules))
            return rules
        except OSError as exc:
            logger.error("[StandardsLoader] Cannot read %s: %s", path, exc)
            return []

    # ── Convenience query helpers ─────────────────────────────────────────────

    @staticmethod
    def for_language(rules: List[Rule], language: str) -> List[Rule]:
        """Return rules that apply to *language* (including 'all' rules)."""
        return [r for r in rules if r.applies_to(language)]

    @staticmethod
    def by_severity(rules: List[Rule], severity: Severity) -> List[Rule]:
        """Return rules with exactly this severity."""
        return [r for r in rules if r.severity == severity]

    @staticmethod
    def by_category(rules: List[Rule], category: str) -> List[Rule]:
        """Return rules in the given category (case-insensitive)."""
        return [r for r in rules if r.category == category.lower()]

    @staticmethod
    def critical_and_high(rules: List[Rule]) -> List[Rule]:
        """Return only CRITICAL and HIGH severity rules."""
        return [r for r in rules if r.severity in (Severity.CRITICAL, Severity.HIGH)]


# ── Agent entry point ─────────────────────────────────────────────────────────

def run_standards(
    state: Dict[str, Any],
    standards_dir:  Optional[Path] = None,
    standards_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Stage 2 agent entry point.

    Loads rules from `standards/` directory (or fallback file) and writes
    `state["standards"]`.

    Args:
        state:          Shared ReviewState dict.
        standards_dir:  Override path to the standards directory.
        standards_path: Override path to a single standards file (legacy).

    Returns:
        Updated state dict with "standards" key populated.
    """
    loader = StandardsLoader(standards_dir=standards_dir, standards_path=standards_path)
    rules  = loader.load()

    if not rules:
        logger.warning(
            "[StandardsAgent] No rules loaded — review quality will be degraded"
        )

    # Validate at least one CRITICAL rule exists
    critical_count = sum(1 for r in rules if r.severity == Severity.CRITICAL)
    if critical_count == 0:
        logger.warning(
            "[StandardsAgent] No CRITICAL severity rules found in standards file"
        )

    # Log language breakdown
    lang_counts: Dict[str, int] = {}
    for r in rules:
        lang_counts[r.language] = lang_counts.get(r.language, 0) + 1
    logger.info("[StandardsAgent] Rules by language: %s", lang_counts)

    state["standards"] = rules
    return state


# ── Build-prompt helper (used by reviewer_agent) ──────────────────────────────

def build_review_prompt_rules(
    standards: List[Rule],
    language: str,
    max_rules: int = 30,
) -> str:
    """
    Return a formatted rules block for injection into an LLM review prompt.

    Applies language filtering and severity ordering (CRITICAL first).
    Includes bad/good examples for HIGH and CRITICAL rules.

    Args:
        standards: Full list of Rule objects from state["standards"].
        language:  Language of the chunk being reviewed.
        max_rules: Cap the number of rules injected (keeps prompt size bounded).

    Returns:
        Multi-line string ready to embed in a prompt.
    """
    relevant = StandardsLoader.for_language(standards, language)

    # Sort: CRITICAL → HIGH → MEDIUM → LOW → INFO
    _order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2,
              Severity.LOW: 3, Severity.INFO: 4}
    relevant.sort(key=lambda r: _order.get(r.severity, 99))
    relevant = relevant[:max_rules]

    if not relevant:
        return "(no rules defined for this language)"

    sections: List[str] = []
    for r in relevant:
        line = f"[{r.rule_id}] [{r.severity.value}] {r.title}"
        if r.description:
            line += f"\n  → {r.description[:200]}"
        if r.severity in (Severity.CRITICAL, Severity.HIGH):
            if r.bad_example:
                # Show only first 3 lines of the example to keep prompt tight
                bad_snippet = "\n".join(r.bad_example.strip().splitlines()[:3])
                line += f"\n  BAD:  {bad_snippet}"
            if r.good_example:
                good_snippet = "\n".join(r.good_example.strip().splitlines()[:3])
                line += f"\n  GOOD: {good_snippet}"
        sections.append(line)

    header = f"CODING STANDARDS ({len(relevant)} rules, language={language}):\n"
    return header + "\n".join(sections)
