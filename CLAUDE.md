# CLAUDE.md

Guidance for Claude Code when working in this repository. Read this before touching any file.

---

## What this project is

A **5-stage AI code-review pipeline** that accepts a GitHub URL and produces an HTML report plus inline PR comments with structured, rule-backed findings.

```
GitHub URL
  → Stage 1  Ingestion     (parse, chunk, embed, build dependency graph)
  → Stage 2  Standards     (load coding rules from stage2_standards/rules/)
  → Stage 3  Review        (LLM review per chunk, content-hash cache)
  → Stage 4  Comments      (group, budget, format, polish inline comments)
  → Stage 5  Report        (HTML report + optional GitHub PR post)
```

All stages share a single `ReviewState` TypedDict defined in `orchestration/state.py`. Stages are **append-only** — a stage writes its own keys and never mutates keys written by a prior stage.

---

## Directory map

```
core/
  config.py          — MODEL_TYPE routing (FREE / OPENAI / ANTHROPIC / LITELLM), env var refs
  models.py          — ALL shared dataclasses (CodeChunk, RuleViolation, ParsedFile, …)

orchestration/
  graph.py           — sequential pipeline runner (stages 1-5 in order)
  state.py           — ReviewState TypedDict; append-only contract documented here

stage1_ingestion/
  agent.py           — Stage 1 entry point; wraps IngestionPipeline
  pipeline.py        — 11-step sub-pipeline: 1a fetch → 1b workspace → 1c scan →
                       1d detect → 1e filter → 1f parse → 1g chunk → 1h metadata →
                       1i deps → 1j graph → 1k upsert
  parsers/           — one parser per language (regex fallback + tree-sitter preferred)
  analyzers/         — language-specific layer classifier + call resolver per language
  rule_checker.py    — mechanical rule checker (GEN001, GEN002, SEC001, PY001,
                       CP012, CP013, SW008, DA008); runs before any LLM call
  quality_judge.py   — 7 IQ metrics; PROCEED / WARN / ABORT decision

stage2_standards/
  agent.py           — parses all rules/*.md files into Rule objects; writes state["standards"]
  rules/
    general.md       — GEN001-GEN005 (complexity/style), SEC001-SEC004 (security)
    principles.md    — CP001-CP015 (SOLID, Clean Architecture, Design Pattern anti-patterns)
    swift.md         — SW001-SW008
    kotlin.md        — KT001-KT008
    python.md        — PY001-PY007
    dart.md          — DA001-DA010
    rust.md          — RS001-RS007

stage3_review/
  agent.py           — chunk selector, cache check, parallelised LLM review dispatch
  llm_reviewer.py    — LLMReviewer: structured tool-call output, content-hash cache,
                       3-attempt backoff, max_tokens=1500 (GLM free tier floor)
  context_builder.py — assembles 4 context sources (callers, callees, siblings, imports)
  prompt_builder.py  — injects rules + context into the review prompt
  issue_deduplicator.py — dedup by (file, line, rule_id) with severity-wins policy

stage4_comments/
  agent.py           — inner pipeline: group → budget → format → polish
  grouper.py         — 3-level grouping: file → severity bucket → line proximity
  budget.py          — per-file comment cap; demote rather than drop
  formatter.py       — platform-specific markdown: github / gitlab / bitbucket / plain
  writer.py          — LLM polish for CRITICAL/HIGH; template-based for MEDIUM/LOW/INFO

stage5_report/
  agent.py           — Stage 5 entry point
  report_builder.py  — self-contained HTML report (no CDN deps); 3 artifacts
  github_poster.py   — posts inline comments + summary to GitHub PR via REST API

tools/
  git_tool.py        — GitExecutor: shallow clone + pull, result schema
  llm_client.py      — LLMClientFactory: FREE (GLM) / OPENAI / ANTHROPIC / LITELLM backends;
                       create_for_role() / create_for_role_async() for the reviewer/
                       summarizer/judge roles in configs/model_config.json, each with
                       its own model + api_key_env; create_summary_async() picks the
                       "summarizer" role for Step 1h automatically under MODEL_TYPE=LITELLM
  embedding_tool.py  — EmbeddingTool: Voyage AI or OpenAI backends, batch encode
  qdrant_tool.py     — QdrantTool: dual named vectors (code_vector + summary_vector)
  bm25_tool.py       — BM25Index: hybrid BM25 + vector RRF re-rank
  github_tool.py     — GitHubTool: PR metadata fetch + review post
  embedding_tool.py  — EmbeddingTool: Voyage AI primary, OpenAI fallback

configs/
  code_file_type_config.py — extension→language map used by the file filter
  model_config.json        — "reviewer"/"summarizer"/"judge" role model configs
                             (model, base_url, api_key_env per role) for the
                             LITELLM MODEL_TYPE and LLMClientFactory.create_for_role()

skills/              — living design docs; read the relevant SKILL file before
                       modifying a stage (e.g. skills/ingestion/SKILL_chunking.md)
```

---

## Running

```bash
# Full review (requires Qdrant running, MODEL_TYPE=LITELLM by default)
python main.py https://github.com/owner/repo --review

# Skip LLM summaries (faster first run)
python main.py https://github.com/owner/repo --review --skip-summaries

# No Qdrant needed (vector search disabled, BM25 only)
python main.py https://github.com/owner/repo --review --skip-qdrant

# Open report in browser after completion
python main.py https://github.com/owner/repo --review --open
```

Tests:
```bash
pytest tests/test_pipeline_stages.py -s -v
```

---

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `MODEL_TYPE` | `FREE` (GLM), `OPENAI`, `ANTHROPIC`, or `LITELLM` | `LITELLM` |
| `ZHIPUAI_API_KEY` | GLM free tier API key | — |
| `VOYAGE_API_KEY` | Voyage AI embedding key | — |
| `OPENAI_API_KEY` | OpenAI key (if MODEL_TYPE=OPENAI); also the auth key for the `configs/model_config.json` "judge" role | — |
| `DEEPSEEK_API_KEY` | Auth key for the `configs/model_config.json` "reviewer" and "summarizer" roles | — |
| `ANTHROPIC_API_KEY` | Anthropic key (if MODEL_TYPE=ANTHROPIC) | — |

`MODEL_TYPE=LITELLM` drives Stage 3 review (`configs/model_config.json`'s `"reviewer"` entry → `core.config.REVIEW_MODEL`) and Stage 1h summaries (`"summarizer"` entry → `core.config.SUMMARY_MODEL`, fetched via `LLMClientFactory.create_summary_async()`) — each role has its own model and `api_key_env`, so they can run on entirely different backends. The `"judge"` entry is independent of `MODEL_TYPE`: `LLMClientFactory.create_for_role("judge")` always reads it directly, so the eval suite's Tier 3 judge (`tests/eval/judge.py`) can use a model different from whatever the pipeline under test is running — see `core.config.JUDGE_MODEL`. If the judge's primary call fails, `judge_finding()` falls back once to GLM 5.2 (`LLMClientFactory.create_provider("FREE")`, `ZHIPUAI_API_KEY`) before raising — the returned dict's `model_used` reports whichever model actually produced the verdict, so a fallback firing is never silent.

Step 1h's summary generation runs on an asyncio event loop and needs a genuinely awaitable client — `ReviewPipeline` threads a separate `summary_llm_client` (async) through to it, distinct from the sync `llm_client` Stage 3/4 use. Never pass the sync client into `SummaryGenerator`; its `_call_llm()` does `await client.messages.create(...)`, which only fails loudly once the network call itself would otherwise succeed (a real, previously-latent bug — see `orchestration/graph.py::_run_ingestion`).

Copy `.env.example` to `.env` and fill in your keys. Never commit `.env`.

---

## Invariants — do not break these

1. **All shared dataclasses live in `core/models.py`** — never define `CodeChunk`, `RuleViolation`, `ParsedFile`, or `ReviewState` inline in a module.
2. **Stages are append-only** — each stage writes its own `state["key"]` and never overwrites another stage's output.
3. **`tools/` are stateless** — no instance state between calls; safe to call from parallel threads.
4. **`stage1_ingestion/agent.py` is the only caller of `IngestionPipeline.run()`** — never call the pipeline from stage 2+.
5. **`max_tokens=1500` in `llm_reviewer.py`** — GLM free tier needs ≥1500 to complete a tool call; 1024 produces empty `[]` results that get cached and poison the cache.
6. **Content-hash cache key** — `md5(content) + md5(rules) + model_tag`. Changing rule text correctly invalidates the cache. Never bypass the cache without a reason.
7. **Parser → Analyzer dependency direction** — parsers in `stage1_ingestion/parsers/` must not import from `stage1_ingestion/analyzers/`. Analyzers are higher-level.
8. **New coding rules** — add to a `.md` file under `stage2_standards/rules/`. If the rule can be detected without an LLM, also add a `MechanicalRule` subclass in `stage1_ingestion/rule_checker.py`.
9. **`CodeChunk.chunk_id` must stay deterministic** — `CodeChunk.new()` derives it via `uuid.uuid5()` from `(repo_name, file_path, chunk_type, symbol_name, start_line)`, never from content or `uuid.uuid4()`. It's the join key for Qdrant's incremental upsert, the parse cache, and the summary cache — reverting to a random ID silently breaks all three across separate runs of the same repo. Never change the hash inputs or the fixed namespace constant without a migration plan (it would reassign every chunk_id in the Qdrant collection at once).

---

## Adding a new language

1. Add a regex parser in `stage1_ingestion/parsers/<lang>_parser.py` (extend `base.py`).
2. Optionally add a tree-sitter parser in `stage1_ingestion/parsers/<lang>_ts_parser.py`.
3. Add a language analyzer in `stage1_ingestion/analyzers/<lang>_analyzer.py` (extend `base.py`).
4. Register the parser in `stage1_ingestion/file_parser.py`.
5. Register the analyzer in `stage1_ingestion/language_analyzer.py`.
6. Add a rules file `stage2_standards/rules/<lang>.md`.
7. Extend the extension map in `stage1_ingestion/language_detector.py`.

No other files need changing.

---

## Common mistakes

- **Importing `ReviewState` from `orchestration/state.py` for type hints is fine**; importing the pipeline from a tool is not.
- **Do not set `_WORKER_THREADS` above 3** for GLM free tier — rate limit is 50 req/min including retries.
- **Clearing the review cache** (`workspace/review_cache/`) is necessary after changing `max_tokens` or the rules text, as old empty-`[]` entries from a too-low `max_tokens` run will be served as hits.
- **Clearing `workspace/parse_cache/`** is necessary after changing any parser or chunker logic (new tree-sitter grammar, an `SBR`/`SymbolBoundaryResolver` fix, a changed size threshold, etc.) — the cache's content-hash check only catches "same path, changed content," not "same content, changed parsing logic."
- **Recreating the `repo_chunks` Qdrant collection** (`QdrantTool(...).ensure_collection(recreate=True)`) is necessary after changing `stage1_ingestion/rule_checker.py` — `CodeChunk.content_hash` is `md5(content)` only, so an unchanged chunk's stored `violations` payload won't refresh on its own after a rule-checker change.
- **`workspace/` is gitignored** — cloned repos, reports, Qdrant storage, and the `parse_cache/`/`summary_cache/`/`review_cache/`/`comment_cache/` caches all live there; never commit them.
