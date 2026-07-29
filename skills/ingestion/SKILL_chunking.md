# SKILL — Step 1g: Chunking

## Purpose

Convert each `ParsedFile` into a list of `CodeChunk` objects using the
**Hierarchical Multi-Level Chunking** strategy. Each chunk is the atomic unit
stored in Qdrant and reviewed by the Code Review Agent.

> ★ This step also assigns navigation pointer fields (`parent_chunk_id`,
> `prev_chunk_id`, `next_chunk_id`, `sentence_offsets`) required for O(1)
> chunk expansion in Stage 3. Do not skip this assignment.

---

## Implemented In

`ingestion/chunker.py` → class `HierarchicalChunkBuilder`

---

## Input / Output

```python
builder = HierarchicalChunkBuilder(repo_name="my_repo")
chunks: List[CodeChunk] = builder.chunk_file(parsed_file: ParsedFile)
# Nav pointers assigned internally via second pass before return
```

---

## The 3-Layer Strategy

```
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 1 — File-Level MODULE Chunk  (always, always first)          │
│  chunk_type = MODULE                                                 │
│  Content: file path + language + line count + first 10 lines        │
│  Purpose: file-level navigation, repo-wide routing                  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ always
             ┌─────────────────┴──────────────────────┐
             │ parse_success = True                   │ parse_success = False
             ▼                                        ▼
┌──────────────────────────┐        ┌──────────────────────────────────┐
│  LAYER 2 — AST Semantic  │        │  LAYER 3 — Sliding Window        │
│                          │        │  Fallback                        │
│  One chunk per symbol:   │        │  Window:  60 lines               │
│    FUNCTION              │        │  Overlap: 15 lines (25%)         │
│    METHOD                │        │  chunk_type = BLOCK              │
│    CLASS_HEAD            │        │                                  │
│    IMPORT (grouped)      │        │  Used for: config, YAML, shell,  │
│                          │        │  minified JS, broken code        │
│  fn > 150 lines →        │        └──────────────────────────────────┘
│  split into overlapping  │
│  BLOCK sub-chunks        │
└──────────────────────────┘
             │
             │ (if line gaps > 60 lines remain after AST chunking)
             ▼
    LAYER 3 gap-fill sliding window on uncovered ranges
```

---

## ChunkType Reference

| ChunkType | Layer | Description |
|---|---|---|
| `MODULE` | 1 | File-level chunk — always first |
| `FUNCTION` | 2 | Top-level function |
| `METHOD` | 2 | Method inside a class |
| `CLASS_HEAD` | 2 | Class signature + docstring only (not body) |
| `CLASS` | 2 | Entire small class (< 50 lines) |
| `IMPORT` | 2 | Grouped import block |
| `INTERFACE` | 2 | Interface or abstract class |
| `CONSTANT` | 2 | Module-level constants group |
| `BLOCK` | 3 | Sliding window fallback |

---

## CodeChunk Schema (v3)

```python
@dataclass
class CodeChunk:
    chunk_id:       str
    repo_name:      str
    file_path:      str          # relative from repo root
    language:       str
    chunk_type:     ChunkType
    symbol_name:    str
    start_line:     int          # 1-indexed
    end_line:       int
    content:        str          # raw source — never transform before storing
    parent_symbol:  Optional[str]

    # Navigation pointers — set in second pass — required for O(1) expansion
    parent_chunk_id:   Optional[str]   # chunk_id of CLASS_HEAD owning this METHOD
    prev_chunk_id:     Optional[str]   # previous chunk in same file
    next_chunk_id:     Optional[str]   # next chunk in same file
    sentence_offsets:  List[int]       # line offsets for sentence window expansion

    # Filled by later steps
    outgoing_edges:    List[str]        # Step 1i
    incoming_edges:    List[str]        # Step 1i
    imports:           List[str]        # Step 1i
    calls:             List[str]        # Step 1i
    dependencies:      List[str]        # Step 1i
    summary:           Optional[str]    # Step 1h
    summary_embedding: Optional[List[float]]  # Step 1h
    embedding:         Optional[List[float]]  # Step 1k
```

---

## Navigation Pointer Assignment (Second Pass)

Nav pointers are assigned **after all chunks for a file are created**.
Running during first pass is impossible — chunk N+1's `chunk_id` doesn't
exist yet when chunk N is being created.

```python
def _assign_nav_pointers(self, chunks: List[CodeChunk]) -> None:
    ordered = sorted(chunks, key=lambda c: c.start_line)

    for i, chunk in enumerate(ordered):
        # Prev / next sibling in document order
        chunk.prev_chunk_id = ordered[i - 1].chunk_id if i > 0 else None
        chunk.next_chunk_id = ordered[i + 1].chunk_id if i < len(ordered) - 1 else None

        # Parent class pointer — MUST scope to same file
        if chunk.chunk_type == ChunkType.METHOD and chunk.parent_symbol:
            parent = next(
                (c for c in ordered
                 if c.symbol_name == chunk.parent_symbol
                 and c.chunk_type == ChunkType.CLASS_HEAD
                 and c.file_path == chunk.file_path),  # scope guard
                None
            )
            chunk.parent_chunk_id = parent.chunk_id if parent else None

        # Sentence offsets — one boundary every 5 lines
        total = chunk.end_line - chunk.start_line + 1
        chunk.sentence_offsets = list(range(0, total, 5))
```

---

## Sizing Rules

```
Function < 50 lines    → 1 FUNCTION / METHOD chunk
Function 50–150 lines  → 1 chunk (fits LLM context window)
Function > 150 lines   → split into overlapping BLOCK sub-chunks

WINDOW_SIZE_LINES    = 60    # lines per sliding window chunk
WINDOW_OVERLAP_LINES = 15    # 25% overlap between adjacent windows
MAX_FUNCTION_LINES   = 150   # split threshold for large functions
```

---

## Chunk Output Persistence

```python
# Write to workspace JSONL for re-use without re-chunking:
output = f"workspace/runs/{run_id}/chunks/chunks.jsonl"
with open(output, "a") as f:
    for chunk in chunks:
        f.write(json.dumps(chunk.to_dict()) + "\n")
```

---

## Common Mistakes to Avoid

| Mistake | Correct Approach |
|---|---|
| Single CLASS chunk containing all methods | `CLASS_HEAD` + one chunk per method |
| `start_line = 0` | Lines are 1-indexed — first line = 1 |
| Assigning nav pointers during first pass | Always run second pass after all chunks created |
| Parent lookup without file scope | Filter `c.file_path == chunk.file_path` always |
| Skipping nav pointers on BLOCK / IMPORT chunks | ALL chunk types need `prev_chunk_id` / `next_chunk_id` |
| Missing overlap offset in gap-fill window | Gap fills need `offset = gap_start - 1` |
| Reusing `chunk_id` values across DIFFERENT chunks | `CodeChunk.new()` derives a deterministic `uuid.uuid5()` from `(repo_name, file_path, chunk_type, symbol_name, start_line)` — unique within a repo, but stable across re-runs of the same repo (unlike a fresh `uuid.uuid4()` per chunk) |
| One IMPORT symbol per statement | Group all contiguous imports into ONE symbol |
| Large function → one huge chunk | Split at 150 lines with 15-line overlap |

---

## Validation Checklist

- [ ] Every file produces at least 1 chunk (the MODULE chunk)
- [ ] `chunk.start_line >= 1` for all chunks
- [ ] `chunk.end_line >= chunk.start_line` for all chunks
- [ ] `MODULE` chunk exists and is first in every file's chunk list
- [ ] Methods have both `parent_symbol` AND `parent_chunk_id` set
- [ ] `prev_chunk_id` is `None` for first chunk in each file
- [ ] `next_chunk_id` is `None` for last chunk in each file
- [ ] `parent_chunk_id` scoped to same `file_path`
- [ ] `sentence_offsets` non-empty for all reviewable chunks
- [ ] Functions > 150 lines split into overlapping BLOCK sub-chunks
- [ ] `chunk.content` non-empty for every chunk
- [ ] No two chunks share the same `chunk_id`
- [ ] Chunks written to `workspace/runs/{run_id}/chunks/chunks.jsonl`
- [ ] `[HierarchicalChunkBuilder] {n} chunks from {m} files` logged
