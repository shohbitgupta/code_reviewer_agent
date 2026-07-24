# SKILL — Stage 1: Ingestion

## Purpose

Transform a raw cloned Git repository into a fully indexed, searchable knowledge base:
- A flat list of `CodeChunk` objects stored in **Qdrant** (vector search)
- A directed **dependency graph** stored in **NetworkX** (structural search)
- A **file manifest** of `FileMeta` objects for navigation

The Ingestion stage is itself a **sub-pipeline** of 6 ordered steps.
Each step has its own SKILL file (see below). Read the sub-step SKILL before
implementing that sub-step.

---

## Sub-Pipeline Map

```
Git Repo (local clone)
        │
        ▼
┌───────────────────┐
│  Step 1a          │  SKILL_file_discovery.md
│  File Discovery   │  FileFilter walks the repo tree
│  (FileFilter)     │  → filters binaries, build dirs, lock files
│                   │  → detects language per file
│                   │  → produces: List[FileMeta]
└────────┬──────────┘
         │ List[FileMeta]
         ▼
┌───────────────────┐
│  Step 1b          │  SKILL_ast_parsing.md
│  AST Parsing      │  ASTParser dispatches to language-specific parsers
│  (ASTParser)      │  Python → built-in ast module (full fidelity)
│                   │  JS/TS/Java → regex heuristics (tree-sitter ready)
│                   │  → produces: List[ParsedFile]
└────────┬──────────┘
         │ List[ParsedFile]
         ▼
┌───────────────────┐
│  Step 1c          │  SKILL_chunking.md
│  Chunking         │  Hierarchical 3-layer strategy:
│  (Chunker)        │  Layer 1 → MODULE chunk (always)
│                   │  Layer 2 → AST semantic chunks (functions/classes)
│                   │  Layer 3 → Sliding window fallback
│                   │  → produces: List[CodeChunk]
└────────┬──────────┘
         │ List[CodeChunk]
         ▼
┌───────────────────┐
│  Step 1d          │  SKILL_dependency_extraction.md
│  Dependency       │  DependencyExtractor reads ParsedSymbol metadata
│  Extraction       │  Extracts: CALLS, BELONGS_TO, IMPORTS, INHERITS edges
│  (DepExtractor)   │  → produces: List[DependencyEdge]
└────────┬──────────┘
         │ List[DependencyEdge]
         ▼
┌───────────────────┐
│  Step 1e          │  SKILL_graph_building.md
│  Graph Building   │  DependencyGraph builds NetworkX DiGraph
│  (DepGraph)       │  Runs analysis: cycles, orphans, layer violations
│                   │  Saves graph to workspace/dependency_graph.json
│                   │  → produces: DependencyGraph (in-memory + persisted)
└────────┬──────────┘
         │ DependencyGraph + List[CodeChunk]
         ▼
┌───────────────────┐
│  Step 1f          │  SKILL_vector_upsert.md
│  Vector Upsert    │  EmbeddingTool encodes each chunk's content
│  (QdrantTool +    │  QdrantTool upserts: vector + payload to Qdrant
│   EmbeddingTool)  │  Collection: "repo_chunks"
│                   │  → produces: populated Qdrant collection
└───────────────────┘
```

---

## Entry Point

```python
# agents/chunking_agent.py calls:
from ingestion.indexer import IngestionPipeline

pipeline = IngestionPipeline(
    repo_root=state["local_repo_path"],
    repo_name=state["repo_name"]
)
result = pipeline.run(qdrant_tool=qdrant_tool)

# Write back to LangGraph state:
return {
    **state,
    "file_manifest":    result.file_metas,
    "chunks":           result.chunks,
    "chunk_map":        result.chunk_map,
    "dependency_graph": result.dependency_graph,
    "ingestion_stats":  result.stats,
}
```

---

## Key Files

| File | Responsibility |
|---|---|
| `ingestion/indexer.py` | Sub-pipeline orchestrator — runs all 6 steps in order |
| `ingestion/file_filter.py` | Step 1a — file discovery and language detection |
| `ingestion/ast_parser.py` | Step 1b — language-aware AST parsing |
| `ingestion/chunker.py` | Step 1c — hierarchical chunking logic |
| `ingestion/dependency_extractor.py` | Step 1d — dependency edge extraction |
| `ingestion/graph_builder.py` | Step 1e — NetworkX graph construction and queries |
| `tools/qdrant_tool.py` | Step 1f — vector DB upsert (called by indexer) |
| `tools/embedding_tool.py` | Step 1f — chunk embedding (called by qdrant_tool) |

---

## Output Contract (written to ReviewState)

```python
state["file_manifest"]     # List[FileMeta] — every file found in repo
state["chunks"]            # List[CodeChunk] — all chunks, all files
state["chunk_map"]         # Dict[str, CodeChunk] — keyed by chunk_id AND "file::symbol"
state["dependency_graph"]  # DependencyGraph — queryable graph object
state["ingestion_stats"]   # Dict — totals: files, chunks, edges, cycles, orphans
```

---

## Failure Modes & Handling

| Failure | Behaviour |
|---|---|
| Git clone fails | GitExecutor raises GitCommandError → caught in scanner_agent, written to state["error"] |
| File unreadable | FileFilter skips with `is_parseable=False` |
| AST parse error | ASTParser falls back to sliding window (no crash) |
| Qdrant unreachable | QdrantTool raises — caught in chunking_agent, written to state["error"] |
| Empty repo | Pipeline completes with 0 chunks, downstream agents skip gracefully |

---

## Performance Notes

- FileFilter skips `node_modules`, `.git`, `__pycache__` and all binary extensions
- ASTParser uses Python built-in `ast` — no subprocess overhead
- Chunker processes files sequentially; parallelise with `concurrent.futures` for repos > 500 files
- Qdrant upsert uses batch mode (100 chunks per request) — see `tools/qdrant_tool.py`
- Dependency extraction is O(symbols × calls) — fast for typical repos

---

## Sub-Step SKILL Files

- `skills/ingestion/SKILL_file_discovery.md`
- `skills/ingestion/SKILL_ast_parsing.md`
- `skills/ingestion/SKILL_chunking.md`
- `skills/ingestion/SKILL_dependency_extraction.md`
- `skills/ingestion/SKILL_graph_building.md`
- `skills/ingestion/SKILL_vector_upsert.md`
