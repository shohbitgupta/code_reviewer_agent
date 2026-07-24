# SKILL — Stage 1: Ingestion (v3)

## Purpose

Transform a remote Git repository URL into a fully indexed, searchable knowledge base:
- A flat list of `CodeChunk` objects in **Qdrant** (dual-vector semantic search)
- A directed **dependency graph** in **NetworkX** (structural + architectural analysis)
- A **file manifest** of `FileMeta` objects for navigation
- **LLM-extracted metadata summaries** per chunk enabling natural-language reranking

The Ingestion stage is a **sub-pipeline of 11 ordered steps** (1a → 1k).
Read each step's SKILL file before implementing it.

> ⚠️ Steps 1g, 1h, and 1k contain ingestion-time preparation required by the
> reranking and chunk expansion logic in Stage 3. Do NOT skip or simplify them.

---

## Sub-Pipeline Map

```
Remote Git URL  (state["repo_url"])
        │
        ▼
┌───────────────────────────────────────────────┐
│  Step 1a — Fetch Repo                         │  SKILL_fetch_repo.md
│  GitExecutor.clone_or_pull()                  │
│  Clone to: ./workspace/repos/{repo_name}      │
│  If already cloned → git pull (incremental)   │
│  → produces: local_repo_path, commit_sha      │
└────────────────────┬──────────────────────────┘
                     │ local_repo_path
                     ▼
┌───────────────────────────────────────────────┐
│  Step 1b — Store in Workspace                 │  SKILL_workspace.md
│  WorkspaceManager                             │
│  Creates run directory:                       │
│    workspace/runs/{run_id}/                   │
│      raw/       ← symlink to cloned repo      │
│      parsed/    ← AST outputs (Step 1f)       │
│      chunks/    ← chunk JSONL (Step 1g)       │
│      graphs/    ← dep graph JSON (Step 1j)    │
│      reports/   ← final outputs (Stage 5)     │
│  → produces: workspace layout, run_id         │
└────────────────────┬──────────────────────────┘
                     │ workspace paths
                     ▼
┌───────────────────────────────────────────────┐
│  Step 1c — Stored Repository Scanner          │  SKILL_repo_scanner.md
│  RepoScanner                                  │
│  Walks workspace/raw/ directory tree          │
│  Builds full inventory of all files           │
│  Records: size, last_modified, relative path  │
│  Detects: monorepo structure, sub-packages    │
│  → produces: raw_file_inventory: List[Path]   │
└────────────────────┬──────────────────────────┘
                     │ raw_file_inventory
                     ▼
┌───────────────────────────────────────────────┐
│  Step 1d — Language Detection                 │  SKILL_language_detection.md
│  LanguageDetector                             │
│  Per file: extension map + content sniffing   │
│  Groups files into language buckets           │
│  Flags: binary, generated, minified, vendor   │
│  → produces: language_map: Dict[Path, str]    │
│              language_stats: Dict[str, int]   │
└────────────────────┬──────────────────────────┘
                     │ language_map
                     ▼
┌───────────────────────────────────────────────┐
│  Step 1e — File Discovery                     │  SKILL_file_discovery.md
│  FileFilter                                   │
│  Applies skip rules: binaries, build dirs,    │
│    lock files, node_modules, generated code   │
│  Applies size thresholds: min 10B, max 500KB  │
│  Sets is_parseable flag per file              │
│  → produces: List[FileMeta]                   │
└────────────────────┬──────────────────────────┘
                     │ List[FileMeta]
                     ▼
┌───────────────────────────────────────────────┐
│  Step 1f — File Parser                        │  SKILL_file_parser.md
│  FileParser  (AST / Tree-sitter)              │
│  Dispatches to language-specific parser:      │
│    Python   → built-in ast  (full fidelity)   │
│    JS / TS  → Tree-sitter   (preferred)       │
│             → regex heuristics (fallback)     │
│    Java     → Tree-sitter   (preferred)       │
│             → regex heuristics (fallback)     │
│  Extracts: functions, classes, methods,       │
│            imports, calls, bases, decorators  │
│  → produces: List[ParsedFile]                 │
└────────────────────┬──────────────────────────┘
                     │ List[ParsedFile]
                     ▼
┌──────────────────────────────────────────────────────┐
│  Step 1g — Chunking  ★ RERANK PREP ★                │  SKILL_chunking.md
│  HierarchicalChunkBuilder                            │
│                                                      │
│  3-layer strategy:                                   │
│    Layer 1 → MODULE chunk  (always)                  │
│    Layer 2 → AST semantic  (fn / class / method)     │
│    Layer 3 → Sliding window fallback  (60L / 15L)    │
│                                                      │
│  ★ ALSO ASSIGNS (required for O(1) expansion):       │
│    parent_chunk_id  → class owner of a method        │
│    prev_chunk_id    → previous chunk in same file    │
│    next_chunk_id    → next chunk in same file        │
│    sentence_offsets → line boundary offsets          │
│                                                      │
│  → produces: List[CodeChunk] with nav pointers       │
└────────────────────┬─────────────────────────────────┘
                     │ List[CodeChunk]
                     ▼
┌──────────────────────────────────────────────────────┐
│  Step 1h — Metadata Extraction  ★ RERANK PREP ★     │  SKILL_metadata_extraction.md
│  SummaryGenerator                                    │
│                                                      │
│  For each FUNCTION / METHOD / CLASS chunk:           │
│    → LLM call (Claude Haiku)                         │
│    → 1-2 sentence natural language summary           │
│    → Embed summary → summary_vector                  │
│                                                      │
│  Batch: 20 chunks per LLM call (20x cost reduction)  │
│  Async: semaphore = 5 concurrent LLM calls           │
│  Skip: MODULE, IMPORT, BLOCK, CONSTANT chunks        │
│                                                      │
│  Stores on chunk:                                    │
│    chunk.summary           ← extracted text          │
│    chunk.summary_embedding ← float vector            │
│                                                      │
│  → produces: List[CodeChunk] with metadata           │
└────────────────────┬─────────────────────────────────┘
                     │ List[CodeChunk]
                     ▼
┌───────────────────────────────────────────────┐
│  Step 1i — Dependency Extraction              │  SKILL_dependency_extraction.md
│  DependencyExtractor                          │
│                                               │
│  Reads ParsedSymbol metadata per file         │
│  Extracts typed directed edges:               │
│    BELONGS_TO → method     → class_head       │
│    CALLS      → function   → function called  │
│    IMPORTS    → file       → imported file    │
│    INHERITS   → class      → base class       │
│                                               │
│  Resolves symbol names against chunk_map      │
│  Flags: is_cross_file, is_cross_domain        │
│  Drops: stdlib / third-party unresolved refs  │
│  → produces: List[DependencyEdge]             │
└────────────────────┬──────────────────────────┘
                     │ List[DependencyEdge]
                     ▼
┌───────────────────────────────────────────────┐
│  Step 1j — Dependency Graph Building          │  SKILL_graph_building.md
│  DependencyGraph  (NetworkX DiGraph)          │
│                                               │
│  Nodes  = chunk_id per CodeChunk              │
│  Edges  = typed DependencyEdge objects        │
│                                               │
│  Architectural analysis:                      │
│    find_cycles()           → ARCH001 issues   │
│    find_layer_violations() → ARCH002 issues   │
│    find_orphans()          → ARCH003 issues   │
│    find_high_coupling()    → ARCH004 issues   │
│                                               │
│  Persists to:                                 │
│    workspace/runs/{run_id}/graphs/            │
│    dependency_graph.json                      │
│                                               │
│  → produces: DependencyGraph (in-memory)      │
└────────────────────┬──────────────────────────┘
                     │ DependencyGraph + List[CodeChunk]
                     ▼
┌──────────────────────────────────────────────────────┐
│  Step 1k — Vector Upsert  ★ RERANK PREP ★           │  SKILL_vector_upsert.md
│  QdrantTool + EmbeddingTool                          │
│                                                      │
│  For each chunk:                                     │
│    Embed content → code_vector                       │
│                                                      │
│  Upserts ONE Qdrant point per chunk:                 │
│    id:      chunk.chunk_id  (UUID)                   │
│    vectors: {                                        │
│      "code_vector":    embed(content),  ← set here   │
│      "summary_vector": summary_embedding ← from 1h   │
│    }                                                 │
│    payload: ALL fields including nav pointers        │
│      parent_chunk_id, prev_chunk_id, next_chunk_id   │
│      summary text (searchable in payload)            │
│                                                      │
│  Collection: "repo_chunks"                           │
│  Batch: 100 chunks per request                       │
│  → produces: populated Qdrant collection             │
└──────────────────────────────────────────────────────┘
```

---

## Entry Point

```python
# agents/ingestion_agent.py
from ingestion.pipeline import IngestionPipeline

pipeline = IngestionPipeline(
    repo_url=state["repo_url"],
    repo_name=state["repo_name"],
    run_id=state["run_id"],
)
result = pipeline.run(
    qdrant_tool=qdrant_tool,
    llm_tool=llm_tool,
    embed_tool=embed_tool,
)

return {
    **state,
    "local_repo_path":  result.local_repo_path,
    "file_manifest":    result.file_metas,
    "chunks":           result.chunks,
    "chunk_map":        result.chunk_map,
    "dependency_graph": result.dependency_graph,
    "ingestion_stats":  result.stats,
}
```

---

## Key Files

| File | Step | Responsibility |
|---|---|---|
| `ingestion/pipeline.py` | orchestrator | Runs all 11 steps in sequence |
| `tools/git_tool.py` | 1a | Clone / pull remote repo |
| `ingestion/workspace.py` | 1b | Create and manage run workspace |
| `ingestion/repo_scanner.py` | 1c | Walk directory, build file inventory |
| `ingestion/language_detector.py` | 1d | Detect language per file |
| `ingestion/file_filter.py` | 1e | Apply skip rules, set is_parseable |
| `ingestion/file_parser.py` | 1f | AST / Tree-sitter dispatch |
| `ingestion/chunker.py` | 1g | Hierarchical chunking + nav pointer assignment |
| `ingestion/summary_generator.py` | 1h | LLM metadata extraction + summary_vector |
| `ingestion/dependency_extractor.py` | 1i | Typed edge extraction |
| `ingestion/graph_builder.py` | 1j | NetworkX graph build + analysis |
| `tools/qdrant_tool.py` | 1k | Dual-vector upsert + search interface |
| `tools/embedding_tool.py` | 1k | Encode chunk content → code_vector |

---

## Data Models

### FileMeta
```python
@dataclass
class FileMeta:
    file_path:      str        # relative from repo root: "src/auth/login.py"
    absolute_path:  str
    language:       str        # "python" | "javascript" | "typescript" | "java"
    extension:      str
    size_bytes:     int
    line_count:     int
    last_modified:  datetime
    is_parseable:   bool       # False → chunker uses sliding window only
    repo_name:      str
```

### CodeChunk (v3 — complete schema)
```python
@dataclass
class CodeChunk:
    # Core identity
    chunk_id:       str          # uuid4 — Qdrant point ID
    repo_name:      str
    file_path:      str          # relative from repo root
    language:       str
    chunk_type:     ChunkType    # MODULE | FUNCTION | METHOD | CLASS_HEAD | ...
    symbol_name:    str
    start_line:     int          # 1-indexed
    end_line:       int
    content:        str          # raw source — never transform before storing

    # Ownership
    parent_symbol:  Optional[str]

    # Navigation pointers — set at Step 1g — required for O(1) expansion
    parent_chunk_id:   Optional[str]   # CLASS_HEAD owning this METHOD
    prev_chunk_id:     Optional[str]   # previous chunk in same file
    next_chunk_id:     Optional[str]   # next chunk in same file
    sentence_offsets:  List[int]       # line offsets for ±N sentence window

    # Metadata — set at Step 1h — required for summary_vector reranking
    summary:           Optional[str]           # "Validates JWT, raises on expiry"
    summary_embedding: Optional[List[float]]   # summary_vector float array

    # Dependency graph refs — set at Step 1i
    outgoing_edges:    List[str]
    incoming_edges:    List[str]
    imports:           List[str]
    calls:             List[str]
    dependencies:      List[str]

    # Embeddings — set at Step 1k
    embedding:         Optional[List[float]]   # code_vector float array
```

---

## Output Contract (written to ReviewState)

```python
state["local_repo_path"]  # str
state["file_manifest"]    # List[FileMeta]
state["chunks"]           # List[CodeChunk] — with nav ptrs, summaries, vectors
state["chunk_map"]        # Dict[str, CodeChunk] — chunk_id → CodeChunk
state["dependency_graph"] # DependencyGraph
state["ingestion_stats"]  # Dict (see below)
```

### ingestion_stats keys
```python
{
    "run_id":                str,
    "repo_name":             str,
    "commit_sha":            str,
    "files_total":           int,
    "files_parseable":       int,
    "files_skipped":         int,
    "language_breakdown":    Dict[str, int],  # {"python": 42, "typescript": 18}
    "chunks_total":          int,
    "chunks_by_type":        Dict[str, int],  # {"method": 120, "function": 40}
    "summaries_generated":   int,
    "summaries_skipped":     int,
    "edges_total":           int,
    "edges_by_type":         Dict[str, int],  # {"CALLS": 80, "BELONGS_TO": 60}
    "cycles_found":          int,
    "layer_violations":      int,
    "orphans_found":         int,
    "qdrant_points_upserted":int,
    "duration_seconds":      float,
}
```

---

## Step Ordering & Dependencies

Steps must run strictly in sequence. No step may begin before its predecessor completes.

| Step | Hard Depends On | Reason |
|---|---|---|
| 1a | — | Entry point |
| 1b | 1a | Needs `local_repo_path` to set up workspace |
| 1c | 1b | Scans `workspace/raw/` directory |
| 1d | 1c | Needs file inventory to detect per-file language |
| 1e | 1d | FileFilter uses `language_map` for skip decisions |
| 1f | 1e | Parser runs only on `is_parseable=True` files |
| 1g | 1f | Chunker needs `ParsedFile.symbols` + `raw_lines` |
| 1h | 1g | SummaryGenerator needs finalised chunk content |
| 1i | 1f + 1g | Needs `ParsedSymbol.calls` (1f) and `chunk_map` (1g) |
| 1j | 1i | Graph built from `DependencyEdge` list |
| 1k | 1g + 1h + 1j | Upserts `code_vector` + `summary_vector` + dep fields |

---

## Failure Modes

| Step | Failure | Behaviour |
|---|---|---|
| 1a | Git clone fails | Raises `GitCommandError` → `state["error"]` |
| 1a | Auth failure (private repo) | Raises → suggest adding `GITHUB_TOKEN` to env |
| 1b | Disk full | Raises `OSError` → `state["error"]` |
| 1c | Empty repo | Returns empty inventory → pipeline completes with 0 chunks |
| 1d | Unknown file extension | Defaults to `language="unknown"` — still processed |
| 1e | File unreadable | Sets `is_parseable=False`, logs warning, continues |
| 1f | AST syntax error | Returns `parse_success=False`, chunker uses sliding window |
| 1g | File > 500KB | Uses Layer 3 sliding window only |
| 1h | LLM call fails | Sets `chunk.summary=""`, logs warning, continues (non-blocking) |
| 1h | LLM rate limit | Retry with exponential backoff (max 3 retries) |
| 1i | Symbol unresolvable | Drops the edge, logs count |
| 1j | Cycle detection OOM | Caps at 1000 cycles, logs truncation warning |
| 1k | Qdrant unreachable | Raises → `state["error"]` |

---

## Performance Notes

| Step | Concern | Mitigation |
|---|---|---|
| 1a | Large repos (> 1GB) | `git clone --depth=1` shallow clone |
| 1f | Tree-sitter | Pool workers via `concurrent.futures` for > 200 files |
| 1g | Large repos | `concurrent.futures` parallelism for > 500 files |
| 1h | LLM cost | Batch 20/call + `asyncio.Semaphore(5)` — 20× cost reduction |
| 1h | > 2000 reviewable chunks | Run Step 1h as background job; reviewer falls back to `code_vector` |
| 1k | Qdrant upsert | Batch 100 points/request |

---

## Workspace Layout (produced by Step 1b)

```
workspace/
  repos/
    {repo_name}/                  ← cloned git repo (Step 1a)
  runs/
    {run_id}/
      raw/                        ← symlink → repos/{repo_name}
      parsed/
        {file_hash}.json          ← ParsedFile output (Step 1f)
      chunks/
        chunks.jsonl              ← one CodeChunk per line (Step 1g)
      graphs/
        dependency_graph.json     ← serialised graph (Step 1j)
      reports/                    ← populated by Stage 5
        review_report.md
        review_report.json
```

---

## Sub-Step SKILL Files (read in order)

| # | File | Step | Class |
|---|---|---|---|
| 1 | `skills/ingestion/SKILL_fetch_repo.md` | 1a | GitExecutor |
| 2 | `skills/ingestion/SKILL_workspace.md` | 1b | WorkspaceManager |
| 3 | `skills/ingestion/SKILL_repo_scanner.md` | 1c | RepoScanner |
| 4 | `skills/ingestion/SKILL_language_detection.md` | 1d | LanguageDetector |
| 5 | `skills/ingestion/SKILL_file_discovery.md` | 1e | FileFilter |
| 6 | `skills/ingestion/SKILL_file_parser.md` | 1f | FileParser |
| 7 | `skills/ingestion/SKILL_chunking.md` | 1g | HierarchicalChunkBuilder ★ |
| 8 | `skills/ingestion/SKILL_metadata_extraction.md` | 1h | SummaryGenerator ★ |
| 9 | `skills/ingestion/SKILL_dependency_extraction.md` | 1i | DependencyExtractor |
| 10 | `skills/ingestion/SKILL_graph_building.md` | 1j | DependencyGraph |
| 11 | `skills/ingestion/SKILL_vector_upsert.md` | 1k | QdrantTool ★ |
