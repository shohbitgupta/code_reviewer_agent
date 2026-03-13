"""
Step 1g — Chunking  ★ RERANK PREP ★

HierarchicalChunkBuilder converts each ParsedFile into a list of CodeChunk
objects using a 3-layer strategy:

  Layer 1  MODULE chunk (always, always first)
           Content: file header + first 10 lines
           Purpose: file-level navigation and repo-wide routing

  Layer 2  AST semantic chunks (parse_success=True only)
           One chunk per ParsedSymbol: FUNCTION, METHOD, CLASS_HEAD, IMPORT
           Large functions (> 150 lines) are split into overlapping BLOCK chunks

  Layer 3  Sliding window fallback (parse_success=False, or gap-fill)
           Window: 60 lines  |  Overlap: 15 lines (25%)

★ NAVIGATION POINTERS — assigned in a second pass after all chunks for a file
  are created (required for O(1) chunk expansion in Stage 3):
    parent_chunk_id  → CLASS_HEAD owning this METHOD (same file only)
    prev_chunk_id    → previous chunk in document order
    next_chunk_id    → next chunk in document order
    sentence_offsets → line-boundary offsets every 5 lines

Usage:
    builder = HierarchicalChunkBuilder(repo_name="owner__repo")
    chunks  = builder.chunk_file(parsed_file)
    # chunks written to workspace/runs/{run_id}/chunks/chunks.jsonl by pipeline
"""

import json
import logging
from pathlib import Path
from typing import List, Optional

from ingestion.models import (
    ChunkType,
    CodeChunk,
    FileMeta,
    ParsedFile,
    ParsedSymbol,
)

logger = logging.getLogger(__name__)

# ── Sizing constants ──────────────────────────────────────────────────────────
WINDOW_SIZE_LINES    = 60   # sliding window chunk size
WINDOW_OVERLAP_LINES = 15   # 25% overlap between adjacent windows
MAX_FUNCTION_LINES   = 150  # functions above this are split into BLOCK chunks

# Map from ParsedSymbol.symbol_type → ChunkType
_SYMBOL_TYPE_MAP = {
    "function":  ChunkType.FUNCTION,
    "method":    ChunkType.METHOD,
    "class_head":ChunkType.CLASS_HEAD,
    "class":     ChunkType.CLASS,
    "import":    ChunkType.IMPORT,
    "interface": ChunkType.INTERFACE,
    "constant":  ChunkType.CONSTANT,
    "block":     ChunkType.BLOCK,
}


class HierarchicalChunkBuilder:
    """
    Converts a single ParsedFile into a list of CodeChunk objects.

    Args:
        repo_name: "owner__repo" slug — stored on every chunk.
    """

    def __init__(self, repo_name: str):
        self.repo_name = repo_name

    # ── Public API ────────────────────────────────────────────────────────────

    def chunk_file(self, parsed_file: ParsedFile) -> List[CodeChunk]:
        """
        Chunk one file.  Always returns at least one chunk (the MODULE chunk).

        Navigation pointers are assigned in a second pass so that all chunk_ids
        are known before prev/next/parent references are set.

        Returns:
            List[CodeChunk] sorted by start_line, with nav pointers assigned.
        """
        meta  = parsed_file.file_meta
        lines = parsed_file.raw_lines

        chunks: List[CodeChunk] = []

        # Layer 1: file-level MODULE chunk — always first
        chunks.append(self._module_chunk(meta, lines))

        if parsed_file.parse_success and parsed_file.symbols:
            # Layer 2: AST semantic chunks
            ast_chunks = self._ast_chunks(parsed_file)
            chunks.extend(ast_chunks)

            # Layer 3: gap-fill with sliding window for uncovered line ranges
            chunks.extend(self._gap_fill(lines, ast_chunks, meta))
        else:
            # Layer 3: full sliding window fallback
            chunks.extend(self._sliding_window(lines, meta, start=1))

        # Second pass: assign nav pointers
        self._assign_nav_pointers(chunks)
        return chunks

    def chunk_many(
        self,
        parsed_files: List[ParsedFile],
        jsonl_path: Optional[Path] = None,
    ) -> List[CodeChunk]:
        """
        Chunk all files and optionally persist to a JSONL file.

        Returns:
            Flat list of all CodeChunk objects across all files.
        """
        all_chunks: List[CodeChunk] = []
        for pf in parsed_files:
            all_chunks.extend(self.chunk_file(pf))

        if jsonl_path:
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with jsonl_path.open("w") as fh:
                for chunk in all_chunks:
                    fh.write(json.dumps(chunk.to_dict()) + "\n")

        logger.info(
            "[HierarchicalChunkBuilder] %d chunks from %d files",
            len(all_chunks), len(parsed_files),
        )
        return all_chunks

    # ── Layer 1 ───────────────────────────────────────────────────────────────

    def _module_chunk(self, meta: FileMeta, lines: List[str]) -> CodeChunk:
        """FILE-LEVEL chunk: header + first 10 source lines."""
        preview = lines[:10]
        content = (
            f"# File: {meta.file_path}\n"
            f"# Language: {meta.language}\n"
            f"# Lines: {meta.line_count}\n\n"
            + "\n".join(preview)
        )
        return CodeChunk.new(
            repo_name   = self.repo_name,
            file_path   = meta.file_path,
            language    = meta.language,
            chunk_type  = ChunkType.MODULE,
            symbol_name = meta.file_path,
            start_line  = 1,
            end_line    = max(1, min(10, len(lines))),
            content     = content,
        )

    # ── Layer 2 ───────────────────────────────────────────────────────────────

    def _ast_chunks(self, parsed_file: ParsedFile) -> List[CodeChunk]:
        """One CodeChunk per ParsedSymbol, splitting large functions."""
        meta   = parsed_file.file_meta
        lines  = parsed_file.raw_lines
        chunks: List[CodeChunk] = []

        for sym in parsed_file.symbols:
            chunk_type = _SYMBOL_TYPE_MAP.get(sym.symbol_type, ChunkType.BLOCK)
            sym_len    = sym.end_line - sym.start_line + 1

            # Split functions that exceed the size threshold
            if (
                sym_len > MAX_FUNCTION_LINES
                and chunk_type in (ChunkType.FUNCTION, ChunkType.METHOD)
            ):
                chunks.extend(self._split_large_symbol(sym, meta, lines))
                continue

            chunk = CodeChunk.new(
                repo_name   = self.repo_name,
                file_path   = meta.file_path,
                language    = meta.language,
                chunk_type  = chunk_type,
                symbol_name = sym.name,
                start_line  = sym.start_line,
                end_line    = sym.end_line,
                content     = sym.source,
                parent_symbol = sym.parent_name,
            )
            # Copy call and import lists from the parser
            chunk.calls   = list(sym.calls)
            chunk.imports = list(sym.imports)
            chunks.append(chunk)

        return chunks

    def _split_large_symbol(
        self,
        sym: ParsedSymbol,
        meta: FileMeta,
        lines: List[str],
    ) -> List[CodeChunk]:
        """Split a function > MAX_FUNCTION_LINES into overlapping BLOCK chunks."""
        chunks: List[CodeChunk] = []
        i   = sym.start_line - 1   # 0-indexed start (inclusive)
        end = sym.end_line          # 0-indexed end (exclusive, for slicing)

        while i < end:
            chunk_start = i + 1                          # back to 1-indexed
            chunk_end   = min(i + MAX_FUNCTION_LINES, end)
            content     = "\n".join(lines[i:chunk_end])

            chunks.append(CodeChunk.new(
                repo_name     = self.repo_name,
                file_path     = meta.file_path,
                language      = meta.language,
                chunk_type    = ChunkType.BLOCK,
                symbol_name   = f"{sym.name}_block_{chunk_start}",
                start_line    = chunk_start,
                end_line      = chunk_end,
                content       = content,
                parent_symbol = sym.parent_name,
            ))
            i += MAX_FUNCTION_LINES - WINDOW_OVERLAP_LINES

        return chunks

    # ── Layer 3 ───────────────────────────────────────────────────────────────

    def _sliding_window(
        self,
        lines: List[str],
        meta: FileMeta,
        start: int = 1,
        end: Optional[int] = None,
    ) -> List[CodeChunk]:
        """Sliding window over a range of lines with WINDOW_OVERLAP_LINES overlap."""
        chunks: List[CodeChunk] = []
        total  = end if end is not None else len(lines)
        i      = start - 1   # 0-indexed

        while i < total:
            chunk_start = i + 1
            chunk_end   = min(i + WINDOW_SIZE_LINES, total)
            content     = "\n".join(lines[i:chunk_end])

            if content.strip():
                chunks.append(CodeChunk.new(
                    repo_name   = self.repo_name,
                    file_path   = meta.file_path,
                    language    = meta.language,
                    chunk_type  = ChunkType.BLOCK,
                    symbol_name = f"block_{chunk_start}_{chunk_end}",
                    start_line  = chunk_start,
                    end_line    = chunk_end,
                    content     = content,
                ))
            i += WINDOW_SIZE_LINES - WINDOW_OVERLAP_LINES

        return chunks

    def _gap_fill(
        self,
        lines: List[str],
        ast_chunks: List[CodeChunk],
        meta: FileMeta,
    ) -> List[CodeChunk]:
        """Apply sliding window to line ranges > 60 lines not covered by AST chunks."""
        if not ast_chunks:
            return self._sliding_window(lines, meta, start=1)

        covered: set[int] = set()
        for c in ast_chunks:
            covered.update(range(c.start_line, c.end_line + 1))

        total       = len(lines)
        gap_chunks: List[CodeChunk] = []
        gap_start:  Optional[int]   = None

        for line in range(1, total + 1):
            if line not in covered:
                if gap_start is None:
                    gap_start = line
            else:
                if gap_start is not None:
                    gap_len = line - gap_start
                    if gap_len > WINDOW_SIZE_LINES:
                        gap_chunks.extend(
                            self._sliding_window(lines, meta, start=gap_start, end=line - 1)
                        )
                    gap_start = None

        # Trailing gap
        if gap_start is not None:
            gap_len = total + 1 - gap_start
            if gap_len > WINDOW_SIZE_LINES:
                gap_chunks.extend(
                    self._sliding_window(lines, meta, start=gap_start, end=total)
                )

        return gap_chunks

    # ── Second pass: nav pointer assignment ──────────────────────────────────

    def _assign_nav_pointers(self, chunks: List[CodeChunk]) -> None:
        """
        Assign prev_chunk_id, next_chunk_id, parent_chunk_id, and
        sentence_offsets for every chunk in the list.

        Must run after all chunks are created so that chunk_ids are stable.
        Parent lookup is scoped to the same file_path to prevent cross-file
        false matches.
        """
        ordered = sorted(chunks, key=lambda c: c.start_line)

        for i, chunk in enumerate(ordered):
            chunk.prev_chunk_id = ordered[i - 1].chunk_id if i > 0 else None
            chunk.next_chunk_id = (
                ordered[i + 1].chunk_id if i < len(ordered) - 1 else None
            )

            # Parent class pointer — METHOD → CLASS_HEAD (same file only)
            if chunk.chunk_type == ChunkType.METHOD and chunk.parent_symbol:
                parent = next(
                    (
                        c for c in ordered
                        if c.symbol_name == chunk.parent_symbol
                        and c.chunk_type == ChunkType.CLASS_HEAD
                        and c.file_path  == chunk.file_path   # scope guard
                    ),
                    None,
                )
                chunk.parent_chunk_id = parent.chunk_id if parent else None

            # Sentence offsets: boundary every 5 lines, always include end
            total   = chunk.end_line - chunk.start_line + 1
            offsets = list(range(0, total, 5))
            if not offsets or offsets[-1] != total:
                offsets.append(total)
            chunk.sentence_offsets = offsets
