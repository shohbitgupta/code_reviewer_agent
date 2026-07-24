# Code Reviewer — Master Pipeline SKILL

## Overview

This document is the top-level reference for the entire Code Reviewer multi-agent pipeline.
Each stage has its own `SKILL.md` in its subdirectory. Always read the stage-specific
SKILL.md before implementing or modifying that stage.

---

## Pipeline Architecture

```
GitHub Repo URL
      │
      ▼
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 1 — INGESTION                                            │
│  skills/ingestion/SKILL_v1.md                                      │
│                                                                 │
│  Sub-pipeline:                                                  │
│    1a. Fetch Repo                                               │
│    1b. Store in Workspace                                       │
│    1c. Stored Repository Scanner                                │
│    1d. Language Detection                                       │
│    1e. File Discovery   (FileFilter)                            │
│    1f. File Parser (AST / Tree-sitter)                          │
│    1g. Chunking         (Hierarchical Chunk Builder )           │
│    1h. Metadata Extraction (SummaryGenerator)                   │
│    1h. Dep Extraction   (DependencyExtractor)                   │
│    1j. Dep Graph Building   (DependencyGraph / NetworkX)        │
│    1k. Vector Upsert    (QdrantTool)                            │
└────────────────────────┬────────────────────────────────────────┘
                         │  Outputs: chunks[], dep_graph, file_manifest
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 2 — STANDARDS LOADING                                    │
│  skills/standards_loader/SKILL.md                               │
│                                                                 │
│  Reads coding_standards.md → parses into structured Rule[]     │
│  Stores rules in LangGraph shared state                         │
└────────────────────────┬────────────────────────────────────────┘
                         │  Outputs: standards[] (Rule objects)
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 3 — CODE REVIEW                                          │
│  skills/code_review/SKILL.md                                    │
│                                                                 │
│  For each chunk:                                                │
│    - Expand context via dependency graph                        │
│    - Retrieve similar chunks from Qdrant                        │
│    - Call LLM with chunk + context + standards                  │
│    - Parse issues from LLM response                             │
└────────────────────────┬────────────────────────────────────────┘
                         │  Outputs: issues[] (ReviewIssue objects)
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 4 — COMMENT WRITING                                      │
│  skills/comment_writer/SKILL.md                                 │
│                                                                 │
│  Posts inline comments to GitHub repo via GitHub API            │
│  One comment per ReviewIssue, at the correct file + line        │
└────────────────────────┬────────────────────────────────────────┘
                         │  Outputs: comments_posted[] (GitHub comment IDs)
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 5 — REPORT GENERATION                                    │
│  skills/report_generator/SKILL.md                               │
│                                                                 │
│  Aggregates all issues → FinalReport                            │
│  Lists every reviewed file with issues + severity breakdown     │
└─────────────────────────────────────────────────────────────────┘
                         │  Outputs: FinalReport (markdown + JSON)
                         ▼
                   Console + File Output
```

---

## LangGraph Shared State

All stages communicate through a single `ReviewState` TypedDict.
Never pass data between agents directly — always read/write state.

```python
class ReviewState(TypedDict):
    # Set by caller
    repo_url:           str
    repo_name:          str
    local_repo_path:    str

    # Stage 1 outputs
    file_manifest:      List[FileMeta]
    chunks:             List[CodeChunk]
    chunk_map:          Dict[str, CodeChunk]   # chunk_id → CodeChunk
    dependency_graph:   DependencyGraph
    ingestion_stats:    Dict

    # Stage 2 outputs
    standards:          List[Rule]

    # Stage 3 outputs
    issues:             List[ReviewIssue]

    # Stage 4 outputs
    comments_posted:    List[str]              # GitHub comment IDs

    # Stage 5 outputs
    report:             FinalReport

    # Control flags
    error:              Optional[str]
    current_stage:      str
```

---

## Execution Order & Routing

```
clone_repo → ingestion → standards_loader → reviewer → commenter → reporter
                                                 ↓
                                    (if issues == 0 → skip commenter)
```

Implemented in `workflow/router.py` as LangGraph conditional edges.

---

## Skill File Locations

| Stage | Skill File |
|---|---|
| Full Pipeline (this file) | `skills/PIPELINE.md` |
| Stage 1: Ingestion | `skills/ingestion/SKILL.md` |
| Stage 1a: File Discovery | `skills/ingestion/SKILL_file_discovery.md` |
| Stage 1b: AST Parsing | `skills/ingestion/SKILL_ast_parsing.md` |
| Stage 1c: Chunking | `skills/ingestion/SKILL_chunking.md` |
| Stage 1d: Dependency Extraction | `skills/ingestion/SKILL_dependency_extraction.md` |
| Stage 1e: Graph Building | `skills/ingestion/SKILL_graph_building.md` |
| Stage 1f: Vector Upsert | `skills/ingestion/SKILL_vector_upsert.md` |
| Stage 2: Standards Loader | `skills/standards_loader/SKILL.md` |
| Stage 3: Code Review | `skills/code_review/SKILL.md` |
| Stage 4: Comment Writer | `skills/comment_writer/SKILL.md` |
| Stage 5: Report Generator | `skills/report_generator/SKILL.md` |

---

## Global Rules (apply to ALL stages)

1. **Never modify shared state keys that belong to a previous stage** — stages are append-only.
2. **All exceptions must be caught** and written to `state["error"]` — never crash the graph.
3. **Log every stage start/end** with `[StageName]` prefix for traceability.
4. **All data models** live in `models/` — never define schemas inline inside agents.
5. **Tools are stateless** — agents hold state, tools do not.
6. **Idempotency** — every stage must be safely re-runnable if restarted.
