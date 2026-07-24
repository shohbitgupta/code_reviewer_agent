# SKILL — Step 1h: Metadata Extraction

## Purpose

For every reviewable `CodeChunk`, call the LLM to extract a concise 1–2 sentence
natural language summary describing what the code does. Store this as:

1. `chunk.summary` — plain text on the chunk (human-readable, stored in Qdrant payload)
2. `chunk.summary_embedding` — float vector stored as `summary_vector` in Qdrant

This is the ingestion-time preparation that makes retrieval-time reranking accurate.
Without it, reviewer queries like *"missing error handling in auth"* cannot match code
that only contains token-level names like `decode`, `exp`, `raise AuthError`.

> The LLM acts as a **metadata extractor**: code in → natural language description out.
> The description is embedded separately so it lives in its own searchable vector space.
> See `SKILL_vector_upsert.md` (Step 1k) for how `summary_vector` is stored alongside `code_vector`.

---

## Implemented In

`ingestion/summary_generator.py` → class `SummaryGenerator`

---

## Input / Output

```python
generator = SummaryGenerator(llm_tool=llm_tool, embed_tool=embed_tool)
chunks = generator.run(chunks)
# Mutates each eligible chunk in-place:
#   chunk.summary           = "Validates JWT token and checks expiry..."
#   chunk.summary_embedding = [0.87, 0.21, -0.43, ...]
# Ineligible chunks:
#   chunk.summary           = ""     (empty string, not None)
#   chunk.summary_embedding = None
```

---

## Which Chunks Get Summaries

| ChunkType | Summarise? | Reason |
|---|---|---|
| `FUNCTION` | Yes | Core review target |
| `METHOD` | Yes | Core review target |
| `CLASS_HEAD` | Yes | Describes class responsibility |
| `CLASS` | Yes | Describes class responsibility |
| `INTERFACE` | Yes | Describes contract |
| `BLOCK` > 30 lines | Optional | Large blocks may benefit |
| `IMPORT` | No | Content is self-describing |
| `MODULE` | No | File path is sufficient |
| `CONSTANT` | No | Name + value is self-describing |
| `BLOCK` ≤ 30 lines | No | Too small — cost not justified |

---

## LLM Prompt Template

```
System:
  You are a code documentation assistant. Your summaries must be:
  - Exactly 1-2 sentences
  - Plain English (not code)
  - Describe WHAT the code does and WHY it exists
  - Mention patterns, risks, or key dependencies if notable
  - Never start the first sentence with the function/class name as subject

User:
  Summarise this {language} {chunk_type} named `{symbol_name}`:

  ```{language}
  {chunk.content}
  ```

  Parent class: {parent_symbol or "none"}
  File: {file_path}

  Summary (1-2 sentences, no preamble):
```

### Good vs Bad Examples

| Code | Bad | Good |
|---|---|---|
| `def validate_token(token)` | "Validates a token." | "Checks JWT signature and expiry against Redis, raising AuthError if invalid or expired." |
| `class PaymentProcessor` | "Handles payments." | "Orchestrates Stripe charge flow including card validation, idempotency key management, and retry logic." |
| `def get_user(id)` | "Gets a user." | "Fetches a User record by primary key, returning None rather than raising if not found." |

Vague summaries produce the same retrieval quality as no summaries. **Specificity is the entire point.**

---

## Batching Strategy (20× cost reduction)

```python
SUMMARY_BATCH_SIZE = 20

def _build_batch_prompt(self, batch: List[CodeChunk]) -> str:
    items = "\n\n".join(
        f"--- Chunk {i+1}: {c.symbol_name} ({c.chunk_type.value}) ---\n"
        f"```{c.language}\n{c.content[:800]}\n```"
        for i, c in enumerate(batch)
    )
    return (
        f"Summarise each of the {len(batch)} code chunks below.\n"
        f"Return a JSON array of exactly {len(batch)} strings, one per chunk, in order.\n"
        f"Each: 1-2 sentences, plain English, no preamble.\n\n"
        f"{items}\n\n"
        f"Return JSON array only:"
    )

def _parse_response(self, response: str, batch_size: int) -> List[str]:
    clean = response.strip().lstrip("```json").rstrip("```").strip()
    summaries = json.loads(clean)
    if len(summaries) != batch_size:
        raise ValueError(f"Expected {batch_size}, got {len(summaries)}")
    return summaries
```

---

## Async Execution

```python
async def run_async(self, chunks: List[CodeChunk]) -> List[CodeChunk]:
    eligible  = [c for c in chunks if self._should_summarise(c)]
    batches   = [eligible[i:i+20] for i in range(0, len(eligible), 20)]
    semaphore = asyncio.Semaphore(5)

    async def process_batch(batch: List[CodeChunk]):
        async with semaphore:
            try:
                summaries = await self._call_llm_async(batch)
                for chunk, summary in zip(batch, summaries):
                    chunk.summary           = summary
                    chunk.summary_embedding = await self._embed_async(summary)
            except Exception as e:
                logger.warning(f"[SummaryGenerator] Batch failed: {e}")
                for chunk in batch:
                    chunk.summary = ""   # non-blocking failure

    await asyncio.gather(*[process_batch(b) for b in batches])
    return chunks
```

---

## Model Recommendation

| Step | Model | Reason |
|---|---|---|
| Step 1h (summaries) | `claude-haiku-4-5-20251001` | Fast, cheap — 1-2 sentences |
| Stage 3 (review) | `claude-sonnet-4-6` | Full reasoning for issue detection |

```python
# config.py
SUMMARY_MODEL = "claude-haiku-4-5-20251001"
REVIEW_MODEL  = "claude-sonnet-4-6"
```

---

## Cost Estimation

```
Typical function (~30 lines) input: ~200 tokens
Batch of 20 → output: ~800 tokens

Cost per chunk (Claude Haiku): ~$0.00003

500 reviewable chunks:  ~$0.015   (negligible)
2000 chunks:            ~$0.06    (negligible)
5000 chunks:            ~$0.15    (acceptable)
```

---

## Deferred Execution for Large Repos

| Mode | Threshold | Fallback in Stage 3 |
|---|---|---|
| Inline | < 500 chunks | N/A |
| Background | 500–2000 chunks | Stage 3 uses `code_vector` only until done |
| Deferred | > 2000 chunks | Schedule overnight job; patch Qdrant when complete |

Stage 3 must handle `chunk.summary = None` gracefully — fall back to `code_vector`
search only when `summary_vector` is absent in Qdrant.

---

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---|---|
| Summarising IMPORT / MODULE / CONSTANT chunks | Skip — no retrieval benefit |
| Storing full LLM response as `chunk.summary` | Parse out 1-2 sentences; strip preamble |
| One LLM call per chunk | Always batch 20/call |
| Summary failure crashing pipeline | Log warning, set `chunk.summary = ""`, continue |
| Embedding `chunk.content` for `summary_embedding` | Embed `chunk.summary`, not raw code |
| Not validating batch response length | Assert `len(summaries) == len(batch)` before zip |

---

## Validation Checklist

- [ ] All FUNCTION / METHOD / CLASS_HEAD chunks have non-empty `chunk.summary`
- [ ] All summarised chunks have `chunk.summary_embedding` as `List[float]`
- [ ] IMPORT / MODULE chunks have `chunk.summary = ""` (empty string, not `None`)
- [ ] Individual LLM failures caught and logged — no pipeline crash
- [ ] Batch size is 20 (not 1 per chunk)
- [ ] Semaphore limits concurrent LLM calls to ≤ 5
- [ ] `[SummaryGenerator] Generated {n} summaries in {x:.1f}s` logged
