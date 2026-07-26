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
from typing import Dict, List, Optional, Set, Tuple


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
    # ── Tier 1 additions (populated by tree-sitter parsers) ───────────────
    param_types: List[str]       = field(default_factory=list)  # e.g. ["String", "BuildContext"]
    return_type: Optional[str]   = None                          # e.g. "Future<Widget>", "void"


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
    """The kind of code unit a CodeChunk represents, per the 3-layer chunking model."""
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

    # Architectural layer — assigned at Step 1f-LA (Language Analyzer)
    layer: str = "unknown"   # primary label; highest-priority from layers list
    # Multi-label layer assignments (Priority 5) — cross-cutting files carry multiple labels.
    # e.g. a mapper bridging data↔domain has layers=["data", "domain"]
    layers: List[str] = field(default_factory=list)

    # Embeddings — assigned at Step 1k
    embedding: Optional[List[float]] = None

    # Mechanical rule violations — assigned at Step 1g-RC (Rule Checker, Priority 2)
    pre_flagged_violations: List["RuleViolation"] = field(default_factory=list)

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
            "chunk_id":               self.chunk_id,
            "repo_name":              self.repo_name,
            "file_path":              self.file_path,
            "language":               self.language,
            "chunk_type":             self.chunk_type.value,
            "symbol_name":            self.symbol_name,
            "start_line":             self.start_line,
            "end_line":               self.end_line,
            "content":                self.content,
            "parent_symbol":          self.parent_symbol,
            "parent_chunk_id":        self.parent_chunk_id,
            "prev_chunk_id":          self.prev_chunk_id,
            "next_chunk_id":          self.next_chunk_id,
            "sentence_offsets":       self.sentence_offsets,
            "summary":                self.summary,
            "summary_embedding":      self.summary_embedding,
            "outgoing_edges":         self.outgoing_edges,
            "incoming_edges":         self.incoming_edges,
            "imports":                self.imports,
            "calls":                  self.calls,
            "dependencies":           self.dependencies,
            "layer":                  self.layer,
            "layers":                 self.layers,
            "embedding":              self.embedding,
            "pre_flagged_violations": [v.to_dict() for v in self.pre_flagged_violations],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CodeChunk":
        """Deserialise from a plain dict."""
        d = dict(d)
        d["chunk_type"] = ChunkType(d["chunk_type"])
        d.setdefault("layer", "unknown")   # backwards-compat for cached chunks
        d.setdefault("layers", [])
        raw_violations = d.pop("pre_flagged_violations", [])
        obj = cls(**d)
        obj.pre_flagged_violations = [RuleViolation(**v) for v in raw_violations]
        return obj

    @property
    def content_hash(self) -> str:
        """MD5 of raw source content — used by incremental Qdrant upsert (O4)."""
        import hashlib
        return hashlib.md5(self.content.encode()).hexdigest()

    def to_qdrant_payload(self) -> dict:
        """
        Returns the Qdrant point payload (no vector fields).

        Fields marked with ★ are required for O(1) chunk expansion in Stage 3.
        content_hash (★★) enables incremental upsert — skip re-embedding unchanged chunks.
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
            "layer":           self.layer,
            "layers":          self.layers,
            "violations":      [v.rule_id for v in self.pre_flagged_violations],
            "content_hash":    self.content_hash,     # ★★ incremental upsert
        }


# ── Step 1g-RC — Mechanical Rule Violations (Priority 2) ─────────────────────

@dataclass
class RuleViolation:
    """A coding-standard violation detected without an LLM (auto_check=True rules)."""
    rule_id:     str    # "GEN001", "SEC001", "PY001", etc.
    severity:    str    # "CRITICAL"|"HIGH"|"MEDIUM"|"LOW"
    title:       str    # Short rule title
    description: str    # Human-readable explanation for this specific violation
    line:        int    # 1-indexed line where the violation begins

    def to_dict(self) -> dict:
        """Serialise to a plain dict (used for JSON/JSONL persistence)."""
        return {
            "rule_id":     self.rule_id,
            "severity":    self.severity,
            "title":       self.title,
            "description": self.description,
            "line":        self.line,
        }


# ── Quality Metrics ───────────────────────────────────────────────────────────

@dataclass
class QualityMetrics:
    """Per-run quality indicators computed by the ingestion pipeline."""

    # Parsing
    parseable_files:        int   = 0
    parse_success_count:    int   = 0
    total_symbols_extracted: int  = 0
    parse_symbol_rate:      float = 0.0   # symbols / parseable_files

    # Chunking
    total_lines:            int   = 0
    lines_covered_by_chunks: int  = 0
    chunk_coverage_rate:    float = 0.0   # covered / total  (target ≥ 0.90)
    gap_fills_count:        int   = 0     # BLOCK chunks from gap-fill

    # Dependency resolution
    total_raw_calls:        int   = 0     # sum of ParsedSymbol.calls lengths
    resolved_calls:         int   = 0     # CALLS edges successfully emitted
    cross_file_resolved:    int   = 0     # of those, how many are cross-file
    dep_resolution_rate:    float = 0.0   # resolved / total_raw_calls  (target ≥ 0.80)
    cross_file_dep_rate:    float = 0.0   # cross_file / resolved
    low_confidence_edges:   int   = 0     # edges with confidence < 0.6


# ── Step 1i — Dependency Extraction ──────────────────────────────────────────

class EdgeType(str, Enum):
    """The relationship a DependencyEdge represents between two CodeChunk nodes."""
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
    # ── Tier 1 additions (resolution metadata) ────────────────────────────
    is_external:  bool  = False    # target is outside the project (SDK, stdlib)
    confidence:   float = 1.0      # 1.0 exact, 0.8 name-match, 0.5 heuristic
    resolved_via: str   = "exact"  # "exact"|"name_match"|"heuristic"


# ── Stage 3 — Code Review ─────────────────────────────────────────────────────

@dataclass
class ReviewIssue:
    """
    One coding-standard violation found by Stage 3 (or Stage 1g-RC).

    Consumed by Stage 4 (comment writer) and Stage 5 (report generator).
    The is_pre_flagged field distinguishes mechanical detections (no LLM cost)
    from LLM-reasoned findings.
    """
    issue_id:       str    # UUID — stable ID used by Stage 4 for GitHub comment
    chunk_id:       str    # CodeChunk.chunk_id that produced this issue
    rule_id:        str    # e.g. "SW005", "GEN001", "SEC001"
    severity:       str    # "CRITICAL"|"HIGH"|"MEDIUM"|"LOW"|"INFO"
    title:          str    # short rule title
    description:    str    # LLM or checker explanation specific to this occurrence
    file_path:      str    # relative from repo root (used for GitHub inline comment)
    start_line:     int    # 1-indexed line where the violation begins
    end_line:       int    # 1-indexed last line of the violation
    suggestion:     str    # concrete one-line or short fix written by LLM/checker
    language:       str    # "swift"|"kotlin"|"python"|...
    category:       str    # "security"|"architecture"|"complexity"|...
    is_pre_flagged: bool  = False   # True = MechanicalRuleChecker caught this (no LLM)
    confidence:     float = 1.0    # 0.0–1.0; LLM self-reported certainty
    layer:          str   = "unknown"  # chunk's architectural layer

    @staticmethod
    def new(
        chunk_id:       str,
        rule_id:        str,
        severity:       str,
        title:          str,
        description:    str,
        file_path:      str,
        start_line:     int,
        end_line:       int,
        suggestion:     str,
        language:       str,
        category:       str,
        is_pre_flagged: bool  = False,
        confidence:     float = 1.0,
        layer:          str   = "unknown",
    ) -> "ReviewIssue":
        """Factory: create a ReviewIssue with a fresh UUID for issue_id."""
        return ReviewIssue(
            issue_id       = str(uuid.uuid4()),
            chunk_id       = chunk_id,
            rule_id        = rule_id,
            severity       = severity,
            title          = title,
            description    = description,
            file_path      = file_path,
            start_line     = start_line,
            end_line       = end_line,
            suggestion     = suggestion,
            language       = language,
            category       = category,
            is_pre_flagged = is_pre_flagged,
            confidence     = confidence,
            layer          = layer,
        )

    def to_dict(self) -> dict:
        """Serialise to a plain dict (used for JSON/JSONL persistence)."""
        return {
            "issue_id":       self.issue_id,
            "chunk_id":       self.chunk_id,
            "rule_id":        self.rule_id,
            "severity":       self.severity,
            "title":          self.title,
            "description":    self.description,
            "file_path":      self.file_path,
            "start_line":     self.start_line,
            "end_line":       self.end_line,
            "suggestion":     self.suggestion,
            "language":       self.language,
            "category":       self.category,
            "is_pre_flagged": self.is_pre_flagged,
            "confidence":     self.confidence,
            "layer":          self.layer,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewIssue":
        """Deserialise from a plain dict produced by to_dict()."""
        return cls(**d)


# ── Stage 4 — Comment Writing ─────────────────────────────────────────────────

@dataclass
class ReviewComment:
    """
    One formatted, platform-ready comment produced by Stage 4.

    A single ReviewComment may cover multiple ReviewIssue objects when those
    issues are clustered on nearby lines (proximity grouping).  The issue_ids
    list tracks which ReviewIssue.issue_id values are represented.

    comment_type:
        "inline"     — attached to a specific line range in a diff view
        "file_level" — attached to the file as a whole (no line anchor)
        "summary"    — catch-all comment at the PR/review level for MEDIUM/LOW
                       issues that were evicted from the inline budget

    platform:
        "github" | "gitlab" | "jira" | "text"
    """
    comment_id:   str
    file_path:    str           # relative from repo root
    line:         int           # anchor line (1-indexed); 0 = file/PR level
    end_line:     int           # last line of the highlighted range
    body:         str           # fully formatted markdown / wiki text
    severity:     str           # highest severity among covered issues
    comment_type: str           # "inline" | "file_level" | "summary"
    platform:     str           # "github" | "gitlab" | "jira" | "text"
    issue_ids:    List[str]     # ReviewIssue.issue_id values covered
    language:     str = ""      # source language (for syntax highlighting hints)
    polished:     bool = False  # True = LLM polish pass was applied

    @staticmethod
    def new(
        file_path:    str,
        line:         int,
        end_line:     int,
        body:         str,
        severity:     str,
        comment_type: str,
        platform:     str,
        issue_ids:    List[str],
        language:     str  = "",
        polished:     bool = False,
    ) -> "ReviewComment":
        """Factory: create a ReviewComment with a fresh UUID for comment_id."""
        return ReviewComment(
            comment_id   = str(uuid.uuid4()),
            file_path    = file_path,
            line         = line,
            end_line     = end_line,
            body         = body,
            severity     = severity,
            comment_type = comment_type,
            platform     = platform,
            issue_ids    = issue_ids,
            language     = language,
            polished     = polished,
        )

    def to_dict(self) -> dict:
        """Serialise to a plain dict (used for JSON/JSONL persistence)."""
        return {
            "comment_id":   self.comment_id,
            "file_path":    self.file_path,
            "line":         self.line,
            "end_line":     self.end_line,
            "body":         self.body,
            "severity":     self.severity,
            "comment_type": self.comment_type,
            "platform":     self.platform,
            "issue_ids":    self.issue_ids,
            "language":     self.language,
            "polished":     self.polished,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewComment":
        """Deserialise from a plain dict produced by to_dict()."""
        return cls(**d)
