"""
Shared dataclasses used across all ingestion pipeline steps (1a–1k).

Import from here in all pipeline modules to avoid circular dependencies.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set


# ── Step 1a — Fetch Repo ──────────────────────────────────────────────────────

@dataclass
class CloneResult:
    """Output of GitExecutor.clone_or_pull()."""
    local_repo_path: str    # absolute path on disk
    repo_name:       str    # "owner__repo" slug
    commit_sha:      str    # 40-char HEAD SHA
    is_fresh_clone:  bool   # True = first clone, False = git pull
    clone_duration:  float  # wall-clock seconds


# ── Step 1b — Workspace ───────────────────────────────────────────────────────

@dataclass
class WorkspaceLayout:
    """Directory layout for a single pipeline run."""
    run_id:      str
    run_dir:     Path
    raw_dir:     Path    # symlink → cloned repo
    parsed_dir:  Path    # ParsedFile JSON cache (Step 1f)
    chunks_dir:  Path    # chunks.jsonl (Step 1g)
    graphs_dir:  Path    # dependency_graph.json (Step 1j)
    reports_dir: Path    # review outputs (Stage 5)


# ── Step 1c — Repo Scanner ────────────────────────────────────────────────────

@dataclass
class RawFileEntry:
    """Metadata for one file discovered during the repo scan."""
    path:          Path
    relative_path: str      # relative to repo root, no leading slash
    size_bytes:    int
    last_modified: datetime
    extension:     str      # ".py", ".ts", "" for extensionless files
    is_dir:        bool = False  # always False — scanner records files only


@dataclass
class StructureHints:
    """Heuristic hints about the repository's top-level layout."""
    is_monorepo:    bool
    sub_packages:   List[str]  # ["packages/core", "packages/api"]
    has_src_layout: bool       # True if a src/ directory exists at root
    root_languages: Set[str]   # rough language set {"python", "typescript"}


@dataclass
class FileInventory:
    """Full output of RepoScanner.scan()."""
    all_files:         List[RawFileEntry]
    dir_tree:          Dict[str, List[str]]  # dir path → [child file names]
    total_files:       int
    total_size_bytes:  int
    structure_hints:   StructureHints


# ── Step 1d — Language Detection ─────────────────────────────────────────────

@dataclass
class FileFlag:
    """Language and content-type flags for a single file."""
    language:     str   # "python" | "typescript" | ... | "unknown"
    is_binary:    bool
    is_generated: bool  # protobuf, "DO NOT EDIT" headers
    is_minified:  bool  # single long line, *.min.js
    is_vendor:    bool  # node_modules/, vendor/, third_party/
    is_test:      bool  # *_test.py, *.test.ts, spec/ dirs
    is_config:    bool  # .yaml, .json, .toml, .env


@dataclass
class LanguageDetectionResult:
    """Output of LanguageDetector.detect()."""
    language_map:   Dict[str, str]       # relative_path → language
    language_stats: Dict[str, int]       # language → file count
    file_flags:     Dict[str, FileFlag]  # relative_path → FileFlag


# ── Step 1e — File Filter ─────────────────────────────────────────────────────

@dataclass
class FileMeta:
    """A source file that passed filtering and is ready for parsing."""
    file_path:     str       # relative from repo root, e.g. "src/auth/login.py"
    absolute_path: str
    language:      str
    extension:     str
    size_bytes:    int
    line_count:    int
    last_modified: datetime
    is_parseable:  bool      # False → chunker uses sliding window only
    repo_name:     str


# ── Step 1f — File Parser ─────────────────────────────────────────────────────

@dataclass
class ParsedSymbol:
    """One logical code unit extracted by the file parser."""
    symbol_type: str            # "function"|"method"|"class_head"|"import"|"block"
    name:        str
    start_line:  int            # 1-indexed
    end_line:    int
    source:      str            # raw source text of this symbol
    parent_name: Optional[str]  # class name for methods; None for top-level
    decorators:  List[str] = field(default_factory=list)
    calls:       List[str] = field(default_factory=list)  # called symbol names
    imports:     List[str] = field(default_factory=list)  # import paths
    bases:       List[str] = field(default_factory=list)  # base class names


@dataclass
class ParsedFile:
    """Output of FileParser.parse() for one file."""
    file_meta:     FileMeta
    symbols:       List[ParsedSymbol]  # empty list (not None) when parse fails
    raw_lines:     List[str]           # always populated regardless of outcome
    parse_success: bool
    parse_error:   Optional[str] = None


# ── Step 1g — Chunker ─────────────────────────────────────────────────────────

class ChunkType(str, Enum):
    MODULE     = "module"      # Layer 1 — file-level, always present
    FUNCTION   = "function"    # Layer 2 — top-level function
    METHOD     = "method"      # Layer 2 — method inside a class
    CLASS_HEAD = "class_head"  # Layer 2 — class signature + docstring only
    CLASS      = "class"       # Layer 2 — small class < 50 lines
    IMPORT     = "import"      # Layer 2 — grouped import block
    INTERFACE  = "interface"   # Layer 2 — interface / abstract class
    CONSTANT   = "constant"    # Layer 2 — module-level constants
    BLOCK      = "block"       # Layer 3 — sliding window fallback


@dataclass
class CodeChunk:
    """
    The atomic unit stored in Qdrant and reviewed by the Code Review Agent.

    Nav pointer fields (parent_chunk_id, prev_chunk_id, next_chunk_id,
    sentence_offsets) are assigned by HierarchicalChunkBuilder in a second
    pass after all chunks for a file are created — required for O(1)
    chunk expansion in Stage 3.
    """
    # Core identity
    chunk_id:    str
    repo_name:   str
    file_path:   str        # relative from repo root
    language:    str
    chunk_type:  ChunkType
    symbol_name: str
    start_line:  int        # 1-indexed
    end_line:    int
    content:     str        # raw source — never transform before storing

    # Ownership
    parent_symbol: Optional[str] = None

    # Navigation pointers — assigned at Step 1g
    parent_chunk_id:  Optional[str] = None  # CLASS_HEAD owning this METHOD
    prev_chunk_id:    Optional[str] = None  # previous chunk in same file
    next_chunk_id:    Optional[str] = None  # next chunk in same file
    sentence_offsets: List[int]     = field(default_factory=list)

    # Metadata — assigned at Step 1h
    summary:           Optional[str]        = None
    summary_embedding: Optional[List[float]] = None

    # Dependency graph refs — assigned at Step 1i
    outgoing_edges: List[str] = field(default_factory=list)
    incoming_edges: List[str] = field(default_factory=list)
    imports:        List[str] = field(default_factory=list)
    calls:          List[str] = field(default_factory=list)
    dependencies:   List[str] = field(default_factory=list)

    # Embeddings — assigned at Step 1k
    embedding: Optional[List[float]] = None

    @staticmethod
    def new(
        repo_name: str,
        file_path: str,
        language: str,
        chunk_type: ChunkType,
        symbol_name: str,
        start_line: int,
        end_line: int,
        content: str,
        parent_symbol: Optional[str] = None,
    ) -> "CodeChunk":
        """Factory: create a CodeChunk with a fresh UUID."""
        return CodeChunk(
            chunk_id=str(uuid.uuid4()),
            repo_name=repo_name,
            file_path=file_path,
            language=language,
            chunk_type=chunk_type,
            symbol_name=symbol_name,
            start_line=start_line,
            end_line=end_line,
            content=content,
            parent_symbol=parent_symbol,
        )

    def to_dict(self) -> dict:
        """Serialise to a plain dict (used for JSONL persistence)."""
        return {
            "chunk_id":          self.chunk_id,
            "repo_name":         self.repo_name,
            "file_path":         self.file_path,
            "language":          self.language,
            "chunk_type":        self.chunk_type.value,
            "symbol_name":       self.symbol_name,
            "start_line":        self.start_line,
            "end_line":          self.end_line,
            "content":           self.content,
            "parent_symbol":     self.parent_symbol,
            "parent_chunk_id":   self.parent_chunk_id,
            "prev_chunk_id":     self.prev_chunk_id,
            "next_chunk_id":     self.next_chunk_id,
            "sentence_offsets":  self.sentence_offsets,
            "summary":           self.summary,
            "summary_embedding": self.summary_embedding,
            "outgoing_edges":    self.outgoing_edges,
            "incoming_edges":    self.incoming_edges,
            "imports":           self.imports,
            "calls":             self.calls,
            "dependencies":      self.dependencies,
            "embedding":         self.embedding,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CodeChunk":
        """Deserialise from a plain dict."""
        d = dict(d)
        d["chunk_type"] = ChunkType(d["chunk_type"])
        return cls(**d)

    def to_qdrant_payload(self) -> dict:
        """
        Returns the Qdrant point payload (no vector fields).

        Fields marked with ★ are required for O(1) chunk expansion in Stage 3.
        """
        return {
            "chunk_id":        self.chunk_id,        # ★
            "repo_name":       self.repo_name,
            "file_path":       self.file_path,
            "language":        self.language,
            "chunk_type":      self.chunk_type.value,
            "symbol_name":     self.symbol_name,
            "start_line":      self.start_line,       # ★ inline comment positioning
            "end_line":        self.end_line,
            "parent_symbol":   self.parent_symbol,
            "parent_chunk_id": self.parent_chunk_id,  # ★
            "prev_chunk_id":   self.prev_chunk_id,    # ★
            "next_chunk_id":   self.next_chunk_id,    # ★
            "summary":         self.summary or "",
            "content":         self.content,
        }


# ── Step 1i — Dependency Extraction ──────────────────────────────────────────

class EdgeType(str, Enum):
    BELONGS_TO = "BELONGS_TO"  # method → class_head
    CALLS      = "CALLS"       # function/method → called function/method
    IMPORTS    = "IMPORTS"     # file import chunk → MODULE chunk of imported file
    INHERITS   = "INHERITS"    # class → base class


@dataclass
class DependencyEdge:
    """A typed directed edge between two CodeChunk nodes."""
    from_chunk_id:   str
    to_chunk_id:     str
    edge_type:       EdgeType
    from_symbol:     str
    to_symbol:       str
    from_file:       str
    to_file:         str
    is_cross_file:   bool           # from_file != to_file
    is_cross_domain: bool           # top-level directories differ
    raw_import:      Optional[str] = None  # original import statement
    weight:          float          = 1.0
