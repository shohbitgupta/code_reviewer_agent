# SKILL — Step 1k: Vector Upsert

## Purpose

Embed each `CodeChunk` and upsert into **Qdrant** as the final step of ingestion.
This step makes chunks searchable via two independent named vectors:

| Vector | Embeds | Used For |
|---|---|---|
| `code_vector` | Raw source code (with path/type header) | Structural similarity search |
| `summary_vector` | LLM-extracted summary text (from Step 1h) | Natural language query matching |

Both vectors must be stored per point. The payload must include all navigation
pointer fields (`parent_chunk_id`, `prev_chunk_id`, `next_chunk_id`) as these are
the keys used for O(1) chunk expansion in Stage 3.

---

## Implemented In

`tools/qdrant_tool.py`    → class `QdrantTool`
`tools/embedding_tool.py` → class `EmbeddingTool`

---

## Flow

```
List[CodeChunk]  (with summary_embedding set from Step 1h)
      │
      ▼
For each chunk:
  EmbeddingTool.encode(
    f"# File: {chunk.file_path}\n# Type: {chunk.chunk_type}\n\n{chunk.content}"
  )
  → code_vector: List[float]  (1536-dim)
  → chunk.embedding = code_vector
      │
      ▼
QdrantTool.upsert_chunks(chunks, batch_size=100)
      │
      │   One Qdrant point per chunk:
      │   {
      │     id:      chunk.chunk_id,                   ← UUID string
      │     vectors: {
      │       "code_vector":    chunk.embedding,        ← set here
      │       "summary_vector": chunk.summary_embedding ← from Step 1h
      │     },
      │     payload: chunk.to_qdrant_payload()          ← all fields + nav ptrs
      │   }
      ▼
Qdrant collection: "repo_chunks"
```

---

## Dual Named Vector Configuration

Named vectors must be declared at **collection creation time** — cannot be changed later.
Drop and recreate the collection if dimensions change.

```python
from qdrant_client.models import VectorParams, Distance

client.recreate_collection(
    collection_name="repo_chunks",
    vectors_config={
        "code_vector": VectorParams(
            size=1536,
            distance=Distance.COSINE
        ),
        "summary_vector": VectorParams(
            size=1536,
            distance=Distance.COSINE
        ),
    }
)
```

> If Step 1h was deferred (large repo), upsert `summary_vector=None`.
> Qdrant accepts partial named vectors. Patch later via `update_vectors()`.

---

## Embedding Strategy

Prepend structural context to `code_vector` input — improves retrieval precision:

```python
text_to_embed = (
    f"# File: {chunk.file_path}\n"
    f"# Type: {chunk.chunk_type.value} — {chunk.symbol_name}\n\n"
    f"{chunk.content}"
)
code_vector = embed_tool.encode(text_to_embed)
```

For `summary_vector`, embed `chunk.summary` directly (already done in Step 1h).

---

## Embedding Models

| Environment | Model | Dimensions | Notes |
|---|---|---|---|
| POC (OpenAI) | `text-embedding-3-small` | 1536 | Best cost/quality for code |
| POC (local) | `all-MiniLM-L6-v2` | 384 | Free, lower quality |
| Production | `text-embedding-3-large` | 3072 | Higher recall, 2× cost |

```python
# config.py
EMBEDDING_MODEL      = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
```

---

## Qdrant Payload Schema

All fields below must be stored in the payload.
Fields marked ★ are required for Stage 3 chunk expansion.

```
chunk_id         : keyword   ★  O(1) point lookup
repo_name        : keyword
file_path        : keyword      filter by file
language         : keyword      filter before reranking
chunk_type       : keyword      skip MODULE/IMPORT in review queries
symbol_name      : keyword
start_line       : integer   ★  used by commenter_agent for inline comments
end_line         : integer
parent_symbol    : keyword
parent_chunk_id  : keyword   ★  O(1) parent class expansion
prev_chunk_id    : keyword   ★  O(1) prev sibling expansion
next_chunk_id    : keyword   ★  O(1) next sibling expansion
summary          : text         updated by Step 1h; LLM description
content          : text         raw source (stored for reranker document use)
```

---

## Upsert Implementation

```python
def upsert_chunks(self, chunks: List[CodeChunk], batch_size: int = 100):
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        points = [
            PointStruct(
                id=chunk.chunk_id,
                vector={
                    "code_vector":    chunk.embedding,
                    "summary_vector": chunk.summary_embedding,  # None if deferred
                },
                payload=chunk.to_qdrant_payload()
            )
            for chunk in batch
        ]
        self.client.upsert(collection_name="repo_chunks", points=points)
        logger.info(f"[QdrantTool] Upserted batch {i // batch_size + 1}")
```

---

## Patching summary_vector After Deferred Step 1h

```python
# Run this after background summary generation completes:
from qdrant_client.models import PointVectors

# 1. Patch payload text
client.set_payload(
    collection_name="repo_chunks",
    payload={"summary": chunk.summary},
    points=[chunk.chunk_id]
)

# 2. Set summary_vector
client.update_vectors(
    collection_name="repo_chunks",
    points=[
        PointVectors(
            id=chunk.chunk_id,
            vector={"summary_vector": chunk.summary_embedding}
        )
    ]
)
```

---

## QdrantTool Search Interface (used by Stage 3)

```python
# Initial wide retrieval — high recall (K=30 before reranking)
results = qdrant_tool.search(
    query_text="function that validates JWT token",
    vector_name="code_vector",
    limit=30,
    filters={"language": "python"}
)

# Summary-based retrieval (natural language reviewer query)
results = qdrant_tool.search(
    query_text="missing error handling in authentication",
    vector_name="summary_vector",
    limit=30,
)

# O(1) point lookup — for chunk expansion by nav pointer
chunk_payload = qdrant_tool.get_by_id(chunk_id="uuid-here")

# File-scoped retrieval — get all chunks in one file
results = qdrant_tool.search_by_file(file_path="auth/service.py", limit=100)
```

---

## Docker Setup (POC)

```bash
docker run -p 6333:6333 -v $(pwd)/qdrant_storage:/qdrant/storage qdrant/qdrant
```

```python
# config.py
QDRANT_URL     = "http://localhost:6333"          # POC
# QDRANT_URL   = "https://cluster.qdrant.tech"   # Production
# QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")   # Production
```

---

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---|---|
| Single `vectors_config` (not dict) | Always use named vectors dict — cannot migrate later |
| Setting `summary_vector = []` | Use `None` for absent named vectors — Qdrant accepts partial |
| Missing nav pointer fields in payload | `parent_chunk_id`, `prev_chunk_id`, `next_chunk_id` MUST be in payload |
| Integer index as point ID | Always use `chunk.chunk_id` (UUID string) |
| Re-upserting full collection every run | Check count first — skip if already indexed at same commit SHA |
| Vector dimension mismatch | `EMBEDDING_DIMENSIONS` in config must match model output exactly |
| Batch size > 500 | Use `batch_size=100` — avoids Qdrant gRPC timeout |

---

## Validation Checklist

- [ ] Collection `"repo_chunks"` has two named vector configs: `code_vector`, `summary_vector`
- [ ] `client.count("repo_chunks").count` equals `len(chunks)`
- [ ] `search(..., vector_name="code_vector")` returns results
- [ ] `search(..., vector_name="summary_vector")` returns results (after Step 1h)
- [ ] `get_by_id(chunk_id)` payload includes `parent_chunk_id`, `prev_chunk_id`, `next_chunk_id`
- [ ] Filtering by `language="python"` returns only Python chunks
- [ ] `summary_vector` is `None` for all points if Step 1h was deferred
- [ ] No chunks with empty `content` are upserted
- [ ] `[QdrantTool] Upserted {n} points to "repo_chunks"` logged
