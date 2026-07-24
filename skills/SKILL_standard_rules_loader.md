# SKILL — Stage 2: Standards Loader

## Purpose

Read the `coding_standards.md` file, parse it into structured `Rule` objects,
and store them in the LangGraph shared state so the Review Agent can query
specific rules by language, severity, or category.

---

## Implemented In

`agents/standards_agent.py` → `standards_agent(state)`

---

## Input

```
coding_standards.md   (file in project root)
```

The standards file is human-authored markdown. It defines the rules the
LLM reviewer will check against. Format described below.

---

## Output: Rule Objects (written to ReviewState)

```python
@dataclass
class Rule:
    rule_id:      str       # "PY001", "GEN003", "SEC001"
    language:     str       # "python" | "javascript" | "java" | "all"
    category:     str       # "naming" | "complexity" | "security" | "style" | "architecture"
    severity:     Severity  # CRITICAL | HIGH | MEDIUM | LOW | INFO
    title:        str       # "Function length must not exceed 50 lines"
    description:  str       # Full explanation of the rule
    bad_example:  str       # Optional: example of violating code
    good_example: str       # Optional: example of compliant code
    auto_check:   bool      # True = can be checked programmatically (line count etc.)
```

---

## Coding Standards File Format

The standards file uses structured markdown headings so the parser can
reliably extract rules. Follow this format exactly:

```markdown
# Coding Standards

## General Rules

### GEN001 — Function Length
- **Severity**: MEDIUM
- **Language**: all
- **Category**: complexity
- Functions must not exceed 50 lines of code (excluding blank lines and comments).

### GEN002 — Magic Numbers
- **Severity**: LOW
- **Language**: all
- **Category**: style
- No magic numbers in code. Extract to named constants.

## Python Rules

### PY001 — Type Hints
- **Severity**: LOW
- **Language**: python
- **Category**: style
- All public functions must have type hints on parameters and return type.

### PY002 — Exception Handling
- **Severity**: HIGH
- **Language**: python
- **Category**: security
- Never use bare `except:`. Always catch specific exception types.
- **Bad:** `except:`
- **Good:** `except ValueError as e:`

## Security Rules

### SEC001 — No Hardcoded Secrets
- **Severity**: CRITICAL
- **Language**: all
- **Category**: security
- No API keys, passwords, or tokens hardcoded in source files.
- Patterns: `password =`, `api_key =`, `secret =`, `token =`
```

---

## Parsing Logic

The standards agent parses the markdown using heading levels:

```
## Section Heading    → category grouping
### RULE_ID — Title   → one Rule object
- **Severity**: X     → Rule.severity
- **Language**: X     → Rule.language
- Body text           → Rule.description
- **Bad:** ...        → Rule.bad_example
- **Good:** ...       → Rule.good_example
```

---

## Rule Categories

| Category | Description |
|---|---|
| `naming` | Variable/function/class naming conventions |
| `complexity` | Cyclomatic complexity, function length, nesting depth |
| `security` | Hardcoded secrets, SQL injection, unsafe deserialization |
| `style` | Code formatting, comments, type hints |
| `architecture` | Layer violations, circular dependencies, module cohesion |
| `error_handling` | Exception handling, null checks, return value handling |
| `testing` | Test coverage requirements, test naming |

---

## LangGraph State Output

```python
# Written by standards_agent to ReviewState:
state["standards"] = List[Rule]

# Example retrieval in reviewer_agent:
python_rules = [r for r in state["standards"] if r.language in ("python", "all")]
security_rules = [r for r in state["standards"] if r.category == "security"]
```

---

## Reviewer Prompt Integration

The reviewer_agent injects relevant rules into the LLM prompt:

```python
def build_review_prompt(chunk: CodeChunk, standards: List[Rule]) -> str:
    relevant_rules = [
        r for r in standards
        if r.language in (chunk.language, "all")
    ]
    rules_text = "\n".join(
        f"- [{r.rule_id}] {r.title} (Severity: {r.severity})"
        for r in relevant_rules
    )
    return f"""
You are a code reviewer. Review the following {chunk.language} code.

CODING STANDARDS TO ENFORCE:
{rules_text}

CODE TO REVIEW:
File: {chunk.file_path} (lines {chunk.start_line}–{chunk.end_line})
{chunk.content}

List any violations as JSON array...
"""
```

---

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---|---|
| Passing all rules to LLM regardless of language | Filter rules by `rule.language in (chunk.language, "all")` |
| Re-parsing standards file on every chunk | Parse once in `standards_agent`, store in state |
| Free-form standards file format | Use the structured `### RULE_ID — Title` format for reliable parsing |
| Missing `rule_id` | Every rule needs a unique ID for issue tracking and reporting |
| No bad/good examples | Examples dramatically improve LLM review accuracy |

---

## Validation Checklist

Before marking Stage 2 complete, verify:

- [ ] `state["standards"]` is a non-empty `List[Rule]`
- [ ] Every Rule has `rule_id`, `severity`, `language`, `category`
- [ ] At least one `CRITICAL` severity rule exists (e.g. hardcoded secrets)
- [ ] Rules with `language="all"` are included when filtering for any language
- [ ] Standards agent does not crash if `coding_standards.md` has extra blank lines
- [ ] Rule count printed to console: `[StandardsAgent] Loaded N rules`
