# Copilot instructions for this repository

## Project context

This is a **5-stage AI code-review pipeline** that takes a GitHub repository URL and produces an HTML report with structured, rule-backed findings and inline PR comments.

Stages: `Stage 1 Ingestion → Stage 2 Standards → Stage 3 Review → Stage 4 Comments → Stage 5 Report`

All stages share a single `ReviewState` TypedDict (`orchestration/state.py`). Stages are **append-only** — each stage writes its own keys and never mutates keys from a prior stage.

## Directory conventions

| Layer | Location | Role |
|---|---|---|
| Shared models | `core/models.py` | All dataclasses — never define inline |
| Configuration | `core/config.py` | MODEL_TYPE routing, env var defaults |
| Stage 1 pipeline | `stage1_ingestion/pipeline.py` + sub-modules | 11-step ingestion (1a–1k) |
| Stage 1 parsers | `stage1_ingestion/parsers/` | One per language (regex + tree-sitter) |
| Stage 1 analyzers | `stage1_ingestion/analyzers/` | Layer classifier + call resolver per language |
| Stage 2 rules | `stage2_standards/rules/*.md` | Coding standards loaded at runtime |
| Stage 3 review | `stage3_review/` | LLM chunk review with content-hash cache |
| Stage 4 comments | `stage4_comments/` | Group → budget → format → polish |
| Stage 5 report | `stage5_report/` | HTML report + GitHub PR posting |
| Tools | `tools/` | Stateless wrappers: git, LLM, embedding, Qdrant, BM25 |
| Design docs | `skills/` | Read the relevant SKILL file before modifying a stage |

## Coding expectations

- **Small, targeted changes** — prefer modifying one module over touching many.
- **Type hints and docstrings** — all public functions and classes.
- **No new dependencies** unless the project already uses them or the task clearly requires them.
- **All shared dataclasses in `core/models.py`** — never define `CodeChunk`, `RuleViolation`, or `ParsedFile` elsewhere.
- **New coding rules** → add a `.md` rule to `stage2_standards/rules/`; if mechanically detectable, also add a `MechanicalRule` subclass in `stage1_ingestion/rule_checker.py`.
- **`tools/` are stateless** — no side effects or instance state between calls.

## Testing and verification

```bash
# Full integration test suite (Stage 1 only; requires network for clone)
pytest tests/test_pipeline_stages.py -s -v

# Syntax check before committing
python -m py_compile <file>
```

For a quick end-to-end smoke test:

```bash
python main.py https://github.com/owner/repo --review --skip-summaries --skip-qdrant
```

## Repo-specific conventions

- `main.py` is the user-facing CLI entry point — keep its interface stable.
- `workspace/` is gitignored and holds all runtime artifacts (repos, caches, reports).
- Do not commit `.env` — copy `.env.example` and fill in local keys.
- `skills/PIPELINE.md` is the canonical reference for `ReviewState` key names.
