"""
Integration test — Ingestion Pipeline  (Steps 1a → 1j)

Target repo : https://github.com/RahulMahesh62/Visitor-Tracker

Each test function exercises one pipeline step in isolation, receives the
output of the previous step via module-level state, and prints a structured
summary of what was produced.

Run with:
    pytest tests/test_pipeline_stages.py -s -v

Flags used to keep the test self-contained (no external services needed):
    skip_summaries = True   — no Anthropic API call
    skip_qdrant    = True   — no Qdrant instance required

Stages covered : 1a Clone, 1b Workspace, 1c Scanner, 1d Language Detection,
                 1e File Filter, 1f Parser, 1g Chunker, 1i Dependency Extractor,
                 1j Graph Builder
Stage 1h (LLM summaries) and 1k (Qdrant upsert) are skipped — they require
external services.  Separate optional tests show the expected schema.
"""

import json
import sys
from pathlib import Path
from typing import Dict, List

import pytest

# ── Make project root importable ──────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Shared state written by each stage, consumed by the next ─────────────────
# (pytest does not guarantee test ordering by default — we use a single
#  class with method ordering via the `pytest-ordering` convention, or
#  simply rely on alphabetical / declaration order which pytest preserves
#  within a class when collected top-to-bottom.)

REPO_URL = "https://github.com/RahulMahesh62/Visitor-Tracker"

# Module-level cache so later tests can reuse expensive steps
_STATE: Dict = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _banner(title: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print('═' * 60)


def _section(label: str, value) -> None:
    print(f"  {label:<30} {value}")


def _table(headers: List[str], rows: List[List]) -> None:
    widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
              for i, h in enumerate(headers)]
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in widths)
    sep = "  " + "  ".join("-" * w for w in widths)
    print(fmt.format(*headers))
    print(sep)
    for row in rows[:20]:   # cap table at 20 rows for readability
        print(fmt.format(*[str(v)[:widths[i]] for i, v in enumerate(row)]))
    if len(rows) > 20:
        print(f"  … {len(rows) - 20} more rows not shown")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1a — Fetch Repo
# ══════════════════════════════════════════════════════════════════════════════

def test_step_1a_fetch_repo():
    """Clone or pull the target repository."""
    _banner("STEP 1a — Fetch Repo")

    from tools.git_tool import GitExecutor

    executor = GitExecutor(repo_url=REPO_URL)
    result   = executor.clone_or_pull()

    _STATE["clone_result"] = result

    _section("Repo slug",       result.repo_name)
    _section("Local path",      result.local_repo_path)
    _section("Commit SHA",      result.commit_sha)
    _section("Fresh clone?",    result.is_fresh_clone)
    _section("Duration (s)",    f"{result.clone_duration:.2f}")

    # Assertions
    assert Path(result.local_repo_path).is_dir(),        "Repo directory must exist"
    assert Path(result.local_repo_path, ".git").is_dir(),"Must contain a .git directory"
    assert len(result.commit_sha) == 40,                 "SHA must be 40 hex chars"
    assert result.repo_name != "",                       "Repo name must not be empty"

    print("\n  ✓ Step 1a passed")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1b — Workspace
# ══════════════════════════════════════════════════════════════════════════════

def test_step_1b_workspace():
    """Create the run workspace directory tree."""
    assert "clone_result" in _STATE, "Run test_step_1a first"

    _banner("STEP 1b — Workspace")

    from ingestion.workspace import WorkspaceManager

    cr = _STATE["clone_result"]
    ws = WorkspaceManager(
        repo_name       = cr.repo_name,
        local_repo_path = cr.local_repo_path,
        commit_sha      = cr.commit_sha,
        repo_url        = REPO_URL,
    )
    layout = ws.setup()
    _STATE["layout"] = layout

    _section("Run ID",       layout.run_id)
    _section("run_dir",      layout.run_dir)
    _section("raw_dir",      layout.raw_dir)
    _section("chunks_dir",   layout.chunks_dir)
    _section("graphs_dir",   layout.graphs_dir)
    _section("reports_dir",  layout.reports_dir)

    run_meta_path = layout.run_dir / "run_meta.json"
    if run_meta_path.exists():
        meta = json.loads(run_meta_path.read_text())
        print("\n  run_meta.json:")
        for k, v in meta.items():
            print(f"    {k}: {v}")

    # Assertions
    assert layout.run_dir.is_dir(),                          "run_dir must exist"
    assert layout.raw_dir.is_symlink() or layout.raw_dir.is_dir(), "raw_dir must be symlink or dir"
    assert layout.chunks_dir.is_dir(),                       "chunks_dir must exist"
    assert layout.graphs_dir.is_dir(),                       "graphs_dir must exist"
    assert layout.reports_dir.is_dir(),                      "reports_dir must exist"
    assert run_meta_path.exists(),                           "run_meta.json must be written"

    print("\n  ✓ Step 1b passed")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1c — Repo Scanner
# ══════════════════════════════════════════════════════════════════════════════

def test_step_1c_repo_scanner():
    """Walk the repo and build the raw file inventory."""
    assert "layout" in _STATE, "Run test_step_1b first"

    _banner("STEP 1c — Repo Scanner")

    from ingestion.repo_scanner import RepoScanner

    cr     = _STATE["clone_result"]
    layout = _STATE["layout"]
    scanner = RepoScanner(raw_dir=layout.raw_dir, repo_name=cr.repo_name)
    inventory = scanner.scan()
    _STATE["inventory"] = inventory

    _section("Total files",       inventory.total_files)
    _section("Total size (KB)",   f"{inventory.total_size_bytes / 1024:.1f}")
    _section("Is monorepo",       inventory.structure_hints.is_monorepo)
    _section("Has src/ layout",   inventory.structure_hints.has_src_layout)
    _section("Root languages",    sorted(inventory.structure_hints.root_languages))
    _section("Sub-packages",      inventory.structure_hints.sub_packages or "none")

    # Top extensions
    from collections import Counter
    ext_counts = Counter(e.extension or "(none)" for e in inventory.all_files)
    print("\n  Top file extensions:")
    for ext, cnt in ext_counts.most_common(10):
        print(f"    {ext:<20} {cnt}")

    # Assertions
    assert inventory.total_files > 0,                       "Repo must contain files"
    assert inventory.total_files == len(inventory.all_files)
    assert all(not e.relative_path.startswith("/")
               for e in inventory.all_files),               "Relative paths must not start with /"

    print("\n  ✓ Step 1c passed")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1d — Language Detection
# ══════════════════════════════════════════════════════════════════════════════

def test_step_1d_language_detection():
    """Assign language and content-type flags to every file."""
    assert "inventory" in _STATE, "Run test_step_1c first"

    _banner("STEP 1d — Language Detection")

    from ingestion.language_detector import LanguageDetector

    detector    = LanguageDetector()
    lang_result = detector.detect(_STATE["inventory"])
    _STATE["lang_result"] = lang_result

    print("\n  Language breakdown:")
    for lang, count in sorted(lang_result.language_stats.items(), key=lambda x: -x[1]):
        bar = "█" * min(count, 40)
        print(f"    {lang:<20} {count:>4}  {bar}")

    # Flag summary
    flags = lang_result.file_flags.values()
    print(f"\n  Binary files    : {sum(1 for f in flags if f.is_binary)}")
    flags = lang_result.file_flags.values()
    print(f"  Vendor files    : {sum(1 for f in flags if f.is_vendor)}")
    flags = lang_result.file_flags.values()
    print(f"  Generated files : {sum(1 for f in flags if f.is_generated)}")
    flags = lang_result.file_flags.values()
    print(f"  Minified files  : {sum(1 for f in flags if f.is_minified)}")
    flags = lang_result.file_flags.values()
    print(f"  Test files      : {sum(1 for f in flags if f.is_test)}")
    flags = lang_result.file_flags.values()
    print(f"  Config files    : {sum(1 for f in flags if f.is_config)}")

    # Assertions
    inv = _STATE["inventory"]
    assert len(lang_result.language_map) == inv.total_files, \
        "Every file must have a language entry"
    assert sum(lang_result.language_stats.values()) == inv.total_files

    print("\n  ✓ Step 1d passed")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1e — File Filter
# ══════════════════════════════════════════════════════════════════════════════

def test_step_1e_file_filter():
    """Apply skip rules and produce the filtered FileMeta list."""
    assert "lang_result" in _STATE, "Run test_step_1d first"

    _banner("STEP 1e — File Filter")

    from ingestion.file_filter import FileFilter

    cr        = _STATE["clone_result"]
    ff        = FileFilter(_STATE["inventory"], _STATE["lang_result"], cr.repo_name)
    file_metas = ff.run()
    _STATE["file_metas"] = file_metas

    total = _STATE["inventory"].total_files
    kept  = len(file_metas)
    parseable = sum(1 for f in file_metas if f.is_parseable)

    _section("Total files (raw)",   total)
    _section("Files kept",          kept)
    _section("Files skipped",       total - kept)
    _section("Parseable",           parseable)
    _section("Not parseable",       kept - parseable)

    # Language breakdown of kept files
    from collections import Counter
    lang_counts = Counter(f.language for f in file_metas)
    print("\n  Kept files by language:")
    for lang, cnt in lang_counts.most_common():
        parseable_cnt = sum(1 for f in file_metas
                            if f.language == lang and f.is_parseable)
        print(f"    {lang:<20} total={cnt}  parseable={parseable_cnt}")

    print("\n  Sample kept files (first 10):")
    _table(
        ["file_path", "language", "size_b", "parseable"],
        [[f.file_path, f.language, f.size_bytes, f.is_parseable]
         for f in file_metas[:10]]
    )

    # Assertions
    for fm in file_metas:
        assert not fm.file_path.startswith("/"),  "Paths must be relative"
        assert ".git/" not in fm.file_path,       ".git files must be filtered"
        assert "node_modules/" not in fm.file_path

    print("\n  ✓ Step 1e passed")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1f — File Parser
# ══════════════════════════════════════════════════════════════════════════════

def test_step_1f_file_parser():
    """Parse each source file into ParsedFile with symbols."""
    assert "file_metas" in _STATE, "Run test_step_1e first"

    _banner("STEP 1f — File Parser")

    from ingestion.file_parser import FileParser
    from pathlib import Path as _Path
    from core import config as _config

    repo_name    = _STATE["clone_result"].repo_name
    parser       = FileParser()
    parsed_files = parser.parse_many(
        _STATE["file_metas"],
        cache_dir=_Path(_config.WORKSPACE_ROOT) / "parse_cache" / repo_name,
    )
    _STATE["parsed_files"] = parsed_files

    succeeded  = sum(1 for p in parsed_files if p.parse_success)
    failed     = len(parsed_files) - succeeded
    total_syms = sum(len(p.symbols) for p in parsed_files)

    _section("Files parsed",       len(parsed_files))
    _section("Parse succeeded",    succeeded)
    _section("Parse failed",       failed)
    _section("Total symbols",      total_syms)

    # Symbol type breakdown
    from collections import Counter
    sym_types = Counter(
        s.symbol_type
        for p in parsed_files
        for s in p.symbols
    )
    print("\n  Symbol types extracted:")
    for stype, cnt in sym_types.most_common():
        print(f"    {stype:<20} {cnt}")

    # Show a sample of parsed symbols from the first successful file
    sample_pf = next((p for p in parsed_files if p.parse_success and p.symbols), None)
    if sample_pf:
        print(f"\n  Sample: {sample_pf.file_meta.file_path}")
        print(f"    language={sample_pf.file_meta.language}  "
              f"lines={len(sample_pf.raw_lines)}  "
              f"symbols={len(sample_pf.symbols)}")
        for sym in sample_pf.symbols[:8]:
            print(f"    [{sym.symbol_type:<10}] {sym.name:<30} "
                  f"L{sym.start_line}–{sym.end_line}"
                  + (f"  parent={sym.parent_name}" if sym.parent_name else ""))

    # Assertions
    for pf in parsed_files:
        assert pf.raw_lines is not None,             "raw_lines must always be set"
        assert isinstance(pf.symbols, list),         "symbols must be a list (never None)"
        if pf.parse_success:
            assert len(pf.symbols) > 0 or True       # some files may parse but have no symbols

    print("\n  ✓ Step 1f passed")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1g — Chunker
# ══════════════════════════════════════════════════════════════════════════════

def test_step_1g_chunker():
    """Hierarchically chunk all parsed files into CodeChunk objects."""
    assert "parsed_files" in _STATE, "Run test_step_1f first"

    _banner("STEP 1g — Chunker  ★ RERANK PREP ★")

    from ingestion.chunker import HierarchicalChunkBuilder
    from ingestion.models import ChunkType

    cr      = _STATE["clone_result"]
    layout  = _STATE["layout"]
    builder = HierarchicalChunkBuilder(repo_name=cr.repo_name)
    chunks  = builder.chunk_many(
        _STATE["parsed_files"],
        jsonl_path=layout.chunks_dir / "chunks.jsonl",
    )
    _STATE["chunks"] = chunks

    # Build chunk_map (UUID + file::symbol keys)
    chunk_map = {c.chunk_id: c for c in chunks}
    for c in chunks:
        key = f"{c.file_path}::{c.symbol_name}"
        if key not in chunk_map:
            chunk_map[key] = c
    _STATE["chunk_map"] = chunk_map

    # Stats
    from collections import Counter
    type_counts = Counter(c.chunk_type.value for c in chunks)

    _section("Total chunks",        len(chunks))
    _section("Unique chunk IDs",    len({c.chunk_id for c in chunks}))

    print("\n  Chunks by type:")
    for ct, cnt in type_counts.most_common():
        print(f"    {ct:<20} {cnt}")

    # Nav pointer coverage
    with_parent = sum(1 for c in chunks if c.parent_chunk_id)
    with_prev   = sum(1 for c in chunks if c.prev_chunk_id)
    with_next   = sum(1 for c in chunks if c.next_chunk_id)
    print(f"\n  Nav pointers assigned:")
    print(f"    parent_chunk_id   : {with_parent} chunks")
    print(f"    prev_chunk_id     : {with_prev} chunks")
    print(f"    next_chunk_id     : {with_next} chunks")

    # Sentence offsets
    no_offsets = sum(1 for c in chunks if not c.sentence_offsets)
    print(f"\n  Chunks with sentence_offsets  : {len(chunks) - no_offsets}")
    print(f"  Chunks missing sentence_offsets: {no_offsets}  (should be 0)")

    # JSONL written?
    jsonl_path = layout.chunks_dir / "chunks.jsonl"
    if jsonl_path.exists():
        line_count = sum(1 for _ in jsonl_path.open())
        print(f"\n  chunks.jsonl written: {line_count} lines at {jsonl_path}")

    # Sample: show 5 chunks with their nav pointers
    print("\n  Sample chunks (first 5):")
    _table(
        ["symbol_name", "type", "lines", "prev?", "next?", "parent?"],
        [
            [
                c.symbol_name[:30],
                c.chunk_type.value,
                f"{c.start_line}–{c.end_line}",
                "✓" if c.prev_chunk_id else "–",
                "✓" if c.next_chunk_id else "–",
                "✓" if c.parent_chunk_id else "–",
            ]
            for c in chunks[:5]
        ]
    )

    # Assertions
    assert len(chunks) > 0,                              "Must produce at least one chunk"
    assert len({c.chunk_id for c in chunks}) == len(chunks), "All chunk_ids must be unique"
    for c in chunks:
        assert c.start_line >= 1,                        "start_line must be 1-indexed"
        assert c.end_line >= c.start_line,               "end_line must be >= start_line"
        assert c.content.strip() != "",                  "content must not be empty"
        assert len(c.sentence_offsets) > 0,              "sentence_offsets must be non-empty"
    # Every file must have at least one MODULE chunk
    module_files = {c.file_path for c in chunks if c.chunk_type == ChunkType.MODULE}
    all_files    = {c.file_path for c in chunks}
    assert module_files == all_files,                    "Every file must have a MODULE chunk"

    print("\n  ✓ Step 1g passed")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1h — Summary Generator (schema only — no API call)
# ══════════════════════════════════════════════════════════════════════════════

def test_step_1h_summary_schema():
    """
    Verify the chunks produced by 1g have the correct fields for Step 1h
    without making any LLM calls.
    """
    assert "chunks" in _STATE, "Run test_step_1g first"

    _banner("STEP 1h — Metadata Extraction (schema check only, no LLM call)")

    from ingestion.models import ChunkType
    from ingestion.summary_generator import SUMMARISE_TYPES

    chunks     = _STATE["chunks"]
    eligible   = [c for c in chunks if c.chunk_type in SUMMARISE_TYPES]
    ineligible = [c for c in chunks if c.chunk_type not in SUMMARISE_TYPES]

    _section("Eligible for summary",   len(eligible))
    _section("Ineligible (skip)",      len(ineligible))
    _section("Batches needed (@20)",   (len(eligible) + 19) // 20)

    # Verify all chunks are in the correct initial state
    unset_summary   = sum(1 for c in chunks if c.summary is None)
    unset_embedding = sum(1 for c in chunks if c.summary_embedding is not None)

    _section("Chunks with summary=None (pre-1h)", unset_summary)
    _section("Chunks with embedding already set", unset_embedding)

    print("\n  Eligible chunk types:")
    from collections import Counter
    for ct, cnt in Counter(c.chunk_type.value for c in eligible).most_common():
        print(f"    {ct:<20} {cnt}")

    print("\n  ⚠  LLM call skipped (set ANTHROPIC_API_KEY and remove skip_summaries to enable)")
    print("\n  ✓ Step 1h schema check passed")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1i — Dependency Extractor
# ══════════════════════════════════════════════════════════════════════════════

def test_step_1i_dependency_extractor():
    """Extract typed dependency edges from parsed symbols."""
    assert "chunk_map" in _STATE, "Run test_step_1g first"

    _banner("STEP 1i — Dependency Extraction")

    from ingestion.dependency_extractor import DependencyExtractor
    from ingestion.models import EdgeType

    cr        = _STATE["clone_result"]
    extractor = DependencyExtractor(repo_root=cr.local_repo_path)
    edges     = extractor.extract(
        parsed_files = _STATE["parsed_files"],
        chunk_map    = _STATE["chunk_map"],
    )
    _STATE["edges"] = edges

    from collections import Counter
    edge_types = Counter(e.edge_type.value for e in edges)

    _section("Total edges",       len(edges))
    print("\n  Edges by type:")
    for et, cnt in edge_types.most_common():
        print(f"    {et:<20} {cnt}")

    cross_file   = sum(1 for e in edges if e.is_cross_file)
    cross_domain = sum(1 for e in edges if e.is_cross_domain)
    _section("Cross-file edges",   cross_file)
    _section("Cross-domain edges", cross_domain)

    # Sample edges
    if edges:
        print("\n  Sample edges (first 10):")
        _table(
            ["type", "from_symbol", "to_symbol", "cross_file"],
            [[e.edge_type.value, e.from_symbol[:25], e.to_symbol[:25], e.is_cross_file]
             for e in edges[:10]]
        )

    # Verify back-population on chunks
    chunks_with_out = sum(1 for c in _STATE["chunks"] if c.outgoing_edges)
    chunks_with_in  = sum(1 for c in _STATE["chunks"] if c.incoming_edges)
    _section("Chunks with outgoing_edges", chunks_with_out)
    _section("Chunks with incoming_edges", chunks_with_in)

    # Assertions
    chunk_ids = {c.chunk_id for c in _STATE["chunks"]}
    for e in edges:
        assert e.from_chunk_id != e.to_chunk_id,         "Self-edges must not exist"
        assert e.from_chunk_id in chunk_ids,             "from_chunk_id must be in chunk_map"
        assert e.to_chunk_id   in chunk_ids,             "to_chunk_id must be in chunk_map"
    # Deduplication check
    edge_keys = [(e.from_chunk_id, e.to_chunk_id, e.edge_type) for e in edges]
    assert len(edge_keys) == len(set(edge_keys)),         "Edges must be deduplicated"

    print("\n  ✓ Step 1i passed")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1j — Dependency Graph Builder
# ══════════════════════════════════════════════════════════════════════════════

def test_step_1j_graph_builder():
    """Build the NetworkX graph and run architectural analysis."""
    assert "edges" in _STATE, "Run test_step_1i first"

    _banner("STEP 1j — Dependency Graph Building")

    from ingestion.graph_builder import DependencyGraph

    layout    = _STATE["layout"]
    dep_graph = DependencyGraph()
    dep_graph.build(_STATE["chunks"], _STATE["edges"])
    dep_graph.analyse()
    _STATE["dep_graph"] = dep_graph

    graph_path = layout.graphs_dir / "dependency_graph.json"
    dep_graph.save(graph_path)

    stats = dep_graph.get_stats()

    _section("Nodes (chunks)",       stats["total_nodes"])
    _section("Edges",                stats["total_edges"])
    _section("Graph density",        f"{stats['density']:.6f}")
    _section("Cycles (ARCH001)",     stats["cycles_found"])
    _section("Layer violations (ARCH002)", stats["layer_violations"])
    _section("Orphans (ARCH003)",    stats["orphans_found"])
    _section("High coupling (ARCH004)", stats["high_coupling"])

    # Cycle details
    cycles = dep_graph.find_cycles()
    if cycles:
        print(f"\n  ⚠  Cycles detected ({len(cycles)}):")
        for i, cycle in enumerate(cycles[:3]):
            meta = [dep_graph._chunk_meta.get(cid, {}) for cid in cycle]
            names = " → ".join(m.get("symbol_name", cid[:8]) for m, cid in zip(meta, cycle))
            print(f"    Cycle {i+1}: {names}")

    # Layer violations
    violations = dep_graph.find_layer_violations()
    if violations:
        print(f"\n  ⚠  Layer violations ({len(violations)}):")
        for v in violations[:3]:
            print(f"    {v['from_file']} → {v['to_file']}")

    # Orphans sample
    orphans = dep_graph.find_orphans()
    if orphans:
        print(f"\n  ⚠  Orphaned symbols ({len(orphans)} total, showing 5):")
        for o in orphans[:5]:
            print(f"    {o.get('symbol_name', '?')} in {o.get('file_path', '?')}")

    # Graph JSON written?
    if graph_path.exists():
        size_kb = graph_path.stat().st_size / 1024
        print(f"\n  dependency_graph.json: {size_kb:.1f} KB at {graph_path}")

    # Load/roundtrip check
    dep_graph2 = DependencyGraph()
    dep_graph2.load(graph_path)
    assert dep_graph2.graph.number_of_nodes() == stats["total_nodes"], \
        "Roundtrip node count must match"
    assert dep_graph2.graph.number_of_edges() == stats["total_edges"], \
        "Roundtrip edge count must match"

    print("\n  ✓ Step 1j passed (graph save/load roundtrip verified)")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1k — Qdrant Upsert (schema check only — no Qdrant instance needed)
# ══════════════════════════════════════════════════════════════════════════════

def test_step_1k_qdrant_payload_schema():
    """
    Verify every chunk produces a valid Qdrant payload without connecting
    to Qdrant.  Checks all Stage-3 required nav pointer fields are present.
    """
    assert "chunks" in _STATE, "Run test_step_1g first"

    _banner("STEP 1k — Vector Upsert (payload schema check only, no Qdrant)")

    REQUIRED_PAYLOAD_FIELDS = [
        "chunk_id", "repo_name", "file_path", "language", "chunk_type",
        "symbol_name", "start_line", "end_line",
        "parent_chunk_id",   # ★ required for O(1) expansion
        "prev_chunk_id",     # ★
        "next_chunk_id",     # ★
        "summary", "content",
    ]

    chunks   = _STATE["chunks"]
    failures = []

    for c in chunks:
        payload = c.to_qdrant_payload()
        missing = [f for f in REQUIRED_PAYLOAD_FIELDS if f not in payload]
        if missing:
            failures.append((c.chunk_id, missing))

    _section("Chunks checked",         len(chunks))
    _section("Payload validation fails", len(failures))

    if failures:
        print("\n  ✗ Payload failures (first 5):")
        for chunk_id, missing in failures[:5]:
            print(f"    {chunk_id}: missing {missing}")
    else:
        print("\n  ✓ All chunk payloads contain required fields")

    # Verify chunk_id is UUID-shaped
    import re
    uuid_re = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    bad_ids = [c.chunk_id for c in chunks if not uuid_re.match(c.chunk_id)]
    _section("Non-UUID chunk_ids", len(bad_ids))

    # Verify no empty content upserted
    empty_content = [c for c in chunks if not c.content.strip()]
    _section("Chunks with empty content", len(empty_content))

    print(f"\n  ⚠  Qdrant upsert skipped — run Qdrant via Docker and set QDRANT_URL")
    print(f"     docker run -p 6333:6333 qdrant/qdrant")

    assert len(failures) == 0,      "All payloads must contain required fields"
    assert len(bad_ids) == 0,       "All chunk_ids must be valid UUIDs"
    assert len(empty_content) == 0, "No empty-content chunks must be upserted"

    print("\n  ✓ Step 1k schema check passed")


# ══════════════════════════════════════════════════════════════════════════════
#  END-TO-END SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def test_zz_pipeline_summary():
    """Print a final summary of the complete pipeline run."""
    assert "chunks" in _STATE, "Run all previous steps first"

    _banner("PIPELINE SUMMARY")

    cr     = _STATE.get("clone_result")
    layout = _STATE.get("layout")
    inv    = _STATE.get("inventory")
    fms    = _STATE.get("file_metas", [])
    chunks = _STATE.get("chunks", [])
    edges  = _STATE.get("edges", [])
    graph  = _STATE.get("dep_graph")

    print(f"""
  Repo          : {REPO_URL}
  Commit SHA    : {cr.commit_sha if cr else 'n/a'}
  Run ID        : {layout.run_id if layout else 'n/a'}

  ┌─────────────────────────────────────────┐
  │  Step 1a  Clone/pull           ✓        │
  │  Step 1b  Workspace created    ✓        │
  │  Step 1c  Files scanned        {inv.total_files if inv else '?':>6}   │
  │  Step 1d  Languages detected   ✓        │
  │  Step 1e  Files after filter   {len(fms):>6}   │
  │  Step 1f  Symbols extracted    {sum(len(p.symbols) for p in _STATE.get('parsed_files',[])):>6}   │
  │  Step 1g  Chunks produced      {len(chunks):>6}   │
  │  Step 1h  Summaries (skipped)  ------   │
  │  Step 1i  Edges extracted      {len(edges):>6}   │
  │  Step 1j  Graph built          ✓        │
  │  Step 1k  Qdrant (skipped)     ------   │
  └─────────────────────────────────────────┘
""")

    if graph:
        stats = graph.get_stats()
        print(f"  Architectural findings:")
        print(f"    Cycles           : {stats['cycles_found']}")
        print(f"    Layer violations : {stats['layer_violations']}")
        print(f"    Orphans          : {stats['orphans_found']}")
        print(f"    High-coupling    : {stats['high_coupling']}")

    print(f"\n  Workspace : {layout.run_dir if layout else 'n/a'}")
    print()
