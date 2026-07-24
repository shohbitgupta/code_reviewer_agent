# Copilot instructions for this repository

## Project context
- This repository is an early-stage Python code-review agent that fetches GitHub repositories, analyzes their structure, and builds an ingestion/dependency pipeline.
- Keep changes aligned with the existing ingestion workflow under `ingestion/`, the reusable tool layer under `tools/`, and the CLI entry point in `main.py`.

## Coding expectations
- Prefer small, modular changes over large rewrites.
- Keep new logic in the current abstraction layer: repo operations in `tools/`, parser/graphing logic in `ingestion/`, and orchestration in `agents/`.
- Preserve existing naming and data-flow patterns already used in the pipeline.
- Use Python type hints and clear docstrings for public functions/classes when you add or update code.
- Avoid introducing new dependencies unless the project already uses them or the change clearly requires them.

## Testing and verification
- When you change behavior, verify it with the relevant tests before claiming completion.
- The main validation path for this repo is `pytest tests/test_pipeline_stages.py -s -v`.
- If a fix touches only a narrow module, run the closest targeted test or a quick import/syntax check first.

## Repo-specific conventions
- Treat `main.py` as the user-facing entry point and keep CLI behavior stable.
- Preserve the current pipeline stages and metadata flow used across the repository.
- Keep generated workspace outputs under `workspace/` and avoid committing large runtime artifacts unless the task explicitly requires it.
