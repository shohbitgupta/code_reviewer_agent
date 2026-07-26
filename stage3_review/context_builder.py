"""
Stage 3 — Context Builder

Assembles everything the LLM reviewer needs to understand a chunk before
making a judgment:

  1. Nav pointer expansion   — parent class, prev/next sibling (O(1), chunk_map)
  2. Dependency context      — direct callees from the dep graph (NetworkX, in-memory)
  3. Hybrid retrieval        — similar chunks via RRF(BM25 + Qdrant dense)
  4. Pre-computed arch issues— cycles / layer violations that touch this chunk

Context is deliberately bounded so prompt size stays predictable:
  - Parent class head : max 10 lines
  - Prev/next sibling : max 5 lines each (just the signature)
  - Dependencies      : top 3, max 8 lines each
  - Similar chunks    : top 2, max 10 lines each

No LLM calls here — this is pure retrieval and lookup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.models import CodeChunk, ChunkType

logger = logging.getLogger(__name__)

# ── Token-budget limits ───────────────────────────────────────────────────────
_MAX_PARENT_LINES  = 10
_MAX_SIBLING_LINES =  5
_MAX_DEP_LINES     =  8
_MAX_SIMILAR_LINES = 10
_MAX_DEPS          =  3
_MAX_SIMILAR       =  2


@dataclass
class ReviewContext:
    """All context assembled for one chunk review call."""
    chunk:        CodeChunk
    parent:       Optional[CodeChunk]        = None  # owning class head
    prev_sibling: Optional[CodeChunk]        = None  # preceding chunk in file
    next_sibling: Optional[CodeChunk]        = None  # following chunk in file
    dependencies: List[CodeChunk]            = field(default_factory=list)  # callees
    similar:      List[CodeChunk]            = field(default_factory=list)  # retrieved
    arch_issues:  List[Dict]                 = field(default_factory=list)  # ARCH violations


def _head_lines(chunk: CodeChunk, max_lines: int) -> str:
    """Return the first *max_lines* lines of chunk content."""
    lines = chunk.content.splitlines()[:max_lines]
    if len(chunk.content.splitlines()) > max_lines:
        lines.append(f"    ... ({chunk.end_line - chunk.start_line + 1} lines total)")
    return "\n".join(lines)


class ContextBuilder:
    """
    Builds a ReviewContext for a single CodeChunk.

    Designed to be instantiated once per Stage 3 run and called once per
    chunk being reviewed.  All lookups are O(1) or bounded-depth graph
    traversals — no expensive operations happen per call.

    Args:
        chunk_map:   Dict[chunk_id, CodeChunk] — the in-memory chunk index.
        dep_graph:   DependencyGraph — for callee lookup and arch issue retrieval.
        qdrant_tool: QdrantTool | None — for dense-vector similarity search.
                     If None, only BM25 retrieval is used.
        bm25_index:  BM25Index | None — for sparse retrieval.
                     If None, only Qdrant search is used.
    """

    def __init__(
        self,
        chunk_map,
        dep_graph,
        qdrant_tool=None,
        bm25_index=None,
    ) -> None:
        self._chunk_map   = chunk_map
        self._dep_graph   = dep_graph
        self._qdrant      = qdrant_tool
        self._bm25        = bm25_index

    # ── Public ────────────────────────────────────────────────────────────────

    def build(self, chunk: CodeChunk) -> ReviewContext:
        """
        Assemble the full ReviewContext for *chunk*: nav-pointer expansion,
        dependency callees, hybrid-retrieval similar chunks, and any
        pre-computed architectural issues touching this chunk.

        Returns:
            A populated ReviewContext ready for format_context_for_prompt().
        """
        ctx = ReviewContext(chunk=chunk)

        # 1. Navigation pointer expansion (purely in-memory)
        ctx.parent       = self._resolve(chunk.parent_chunk_id)
        ctx.prev_sibling = self._resolve(chunk.prev_chunk_id)
        ctx.next_sibling = self._resolve(chunk.next_chunk_id)

        # 2. Dependency context (callee chunks this chunk calls)
        ctx.dependencies = self._build_deps(chunk)

        # 3. Hybrid retrieval — similar patterns elsewhere in the repo
        ctx.similar = self._build_similar(chunk)

        # 4. Pre-computed architectural issues that involve this chunk
        ctx.arch_issues = self._build_arch_issues(chunk)

        return ctx

    # ── Private helpers ───────────────────────────────────────────────────────

    def _resolve(self, chunk_id: Optional[str]) -> Optional[CodeChunk]:
        if not chunk_id:
            return None
        return self._chunk_map.get(chunk_id)

    def _build_deps(self, chunk: CodeChunk) -> List[CodeChunk]:
        """Return up to _MAX_DEPS callee CodeChunk objects via the dep graph."""
        try:
            dep_metas = self._dep_graph.get_dependencies(chunk.chunk_id, depth=1)
        except Exception:
            return []
        deps: List[CodeChunk] = []
        for meta in dep_metas:
            cid = meta.get("chunk_id")
            if cid and cid != chunk.chunk_id and cid in self._chunk_map:
                deps.append(self._chunk_map[cid])
            if len(deps) >= _MAX_DEPS:
                break
        return deps

    def _build_similar(self, chunk: CodeChunk) -> List[CodeChunk]:
        """Hybrid BM25 + dense retrieval fused with RRF."""
        query = chunk.summary or chunk.symbol_name

        bm25_ids: List[str] = []
        if self._bm25 is not None:
            try:
                bm25_ids = self._bm25.search(chunk.symbol_name, top_k=15)
            except Exception:
                pass

        qdrant_ids: List[str] = []
        if self._qdrant is not None:
            try:
                results = self._qdrant.search(
                    query,
                    vector_name="summary_vector",
                    limit=15,
                    filters={"language": chunk.language},
                )
                qdrant_ids = [r.get("chunk_id", "") for r in results if r.get("chunk_id")]
            except Exception:
                pass

        # RRF fusion — import lazily to keep this module testable without tools/
        try:
            from tools.bm25_tool import BM25Index
            fused = BM25Index.rrf_fuse(bm25_ids, qdrant_ids, top_k=_MAX_SIMILAR + 5)
        except Exception:
            fused = (bm25_ids + qdrant_ids)[:_MAX_SIMILAR + 5]

        similar: List[CodeChunk] = []
        for cid in fused:
            if cid == chunk.chunk_id:
                continue
            if cid in self._chunk_map:
                similar.append(self._chunk_map[cid])
            if len(similar) >= _MAX_SIMILAR:
                break
        return similar

    def _build_arch_issues(self, chunk: CodeChunk) -> List[Dict]:
        """Return pre-computed ARCH violations that involve this chunk's file."""
        issues: List[Dict] = []
        try:
            # Layer violations where this file is the caller
            for v in self._dep_graph._violations:
                if v.get("from_file") == chunk.file_path:
                    issues.append(v)
            # Cycles that contain this chunk_id
            for cycle in self._dep_graph._cycles[:10]:  # cap to avoid huge prompts
                if chunk.chunk_id in cycle:
                    issues.append({"type": "ARCH001_cycle", "cycle": cycle})
        except Exception:
            pass
        return issues


def format_context_for_prompt(ctx: ReviewContext) -> str:
    """
    Render a ReviewContext into the multi-section string embedded in the LLM prompt.

    Sections are only emitted when they contain content — empty sections are skipped.
    """
    parts: List[str] = []

    # ── Parent class head ─────────────────────────────────────────────────────
    if ctx.parent and ctx.parent.chunk_type.value in ("class_head", "class"):
        src = _head_lines(ctx.parent, _MAX_PARENT_LINES)
        parts.append(
            f"PARENT CLASS ({ctx.parent.symbol_name} — "
            f"lines {ctx.parent.start_line}–{ctx.parent.end_line}):\n"
            f"```{ctx.chunk.language}\n{src}\n```"
        )

    # ── Prev / next sibling (signature only) ─────────────────────────────────
    if ctx.prev_sibling:
        sig = _head_lines(ctx.prev_sibling, _MAX_SIBLING_LINES)
        parts.append(
            f"PRECEDING SYMBOL ({ctx.prev_sibling.symbol_name}):\n"
            f"```{ctx.chunk.language}\n{sig}\n```"
        )

    # ── Pre-computed architectural issues ────────────────────────────────────
    if ctx.arch_issues:
        arch_lines = []
        for issue in ctx.arch_issues:
            if issue.get("type") == "ARCH001_cycle":
                arch_lines.append(f"  ARCH001 (cycle): {' → '.join(issue.get('cycle', []))}")
            else:
                arch_lines.append(f"  ARCH002 (layer): {issue.get('description', issue)}")
        parts.append("KNOWN ARCHITECTURAL ISSUES (pre-computed — confirm and elaborate):\n"
                     + "\n".join(arch_lines))

    # ── Dependency context ────────────────────────────────────────────────────
    if ctx.dependencies:
        dep_parts = []
        for dep in ctx.dependencies:
            src = _head_lines(dep, _MAX_DEP_LINES)
            dep_parts.append(
                f"  → {dep.symbol_name}  [{dep.layer}]  "
                f"({dep.file_path}:{dep.start_line})\n"
                f"```{dep.language}\n{src}\n```"
            )
        parts.append("DEPENDENCIES CALLED BY THIS CHUNK:\n" + "\n".join(dep_parts))

    # ── Similar patterns in the repo ─────────────────────────────────────────
    if ctx.similar:
        sim_parts = []
        for sim in ctx.similar:
            src = _head_lines(sim, _MAX_SIMILAR_LINES)
            sim_parts.append(
                f"  ~ {sim.symbol_name}  ({sim.file_path}:{sim.start_line})\n"
                f"```{sim.language}\n{src}\n```"
            )
        parts.append("SIMILAR PATTERNS IN THIS REPO:\n" + "\n".join(sim_parts))

    return "\n\n".join(parts)
