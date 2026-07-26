# Contributing

Thank you for considering a contribution to this project. This document covers how to set up a development environment, the conventions we follow, and how to submit changes.

---

## Development setup

**Prerequisites**: Python 3.11+, Git.

```bash
# 1. Clone the repo
git clone https://github.com/<your-fork>/code_reviewer_agent.git
cd code_reviewer_agent

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure credentials
cp .env.example .env
# Edit .env — fill in at least MODEL_TYPE and the matching API key
```

**Optional services** (needed for full pipeline runs):

| Service | Purpose | How to start |
|---|---|---|
| Qdrant | Vector store for chunk embeddings | `docker run -p 6333:6333 qdrant/qdrant` |
| GLM free tier | Default LLM backend | No local setup — API key only |

To run without Qdrant (BM25 retrieval only):

```bash
python main.py https://github.com/owner/repo --review --skip-qdrant
```

---

## Running tests

```bash
pytest tests/test_pipeline_stages.py -s -v
```

The test suite covers Stage 1 ingestion (steps 1a–1j). It clones a small public repository on first run, so a network connection is required. Subsequent runs reuse the cloned repo from `workspace/`.

For a quick syntax check before committing:

```bash
python -m py_compile <changed_file.py>
```

---

## Code conventions

### Style
- **Python 3.11+**, PEP 8, 4-space indentation.
- **Type hints** on every public function and method signature.
- **Module-level docstring** on every non-empty `.py` file explaining its role.
- No inline comments that restate what the code does — only explain *why* when the reason is non-obvious.
- Maximum function length: 50 non-blank lines (GEN001). Extract sub-functions when exceeded.

### Architecture rules (non-negotiable)
- **All shared dataclasses in `core/models.py`** — `CodeChunk`, `RuleViolation`, `ParsedFile`, etc. Never define them inline in a module.
- **Stages are append-only** — each stage writes its own `ReviewState` keys and never mutates keys from a prior stage.
- **Tools are stateless** — `tools/` modules must have no instance state or side effects between calls.
- **`stage1_ingestion/parsers/` must not import from `stage1_ingestion/analyzers/`** — parsers are lower-level.

### Adding a new coding rule
1. Add the rule to the appropriate `stage2_standards/rules/<language>.md` file following the existing `### RULE_ID — Title` format.
2. If the rule can be detected without an LLM, add a `MechanicalRule` subclass in `stage1_ingestion/rule_checker.py` and register it in `MechanicalRuleChecker.__init__`.

### Adding a new language
See the "Adding a new language" section in `CLAUDE.md` for the full 7-step checklist.

---

## Submitting a pull request

1. **Create a feature branch** from `main`:
   ```bash
   git checkout -b feat/your-feature-name
   ```

2. **Make focused changes** — one logical change per PR. Avoid mixing refactors with feature additions.

3. **Run the test suite** and confirm it passes:
   ```bash
   pytest tests/test_pipeline_stages.py -s -v
   ```

4. **Open a pull request** against `main`. Use a clear title and describe:
   - What changed and why.
   - Any design trade-offs made.
   - How to test the change manually if the automated suite doesn't cover it.

5. **Respond to review feedback** — maintainers may request changes before merging.

---

## Reporting bugs

Open a GitHub Issue with:
- A minimal reproduction command (`python main.py <url> --review ...`).
- The full error message and relevant log lines.
- Your Python version (`python --version`) and OS.

---

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
