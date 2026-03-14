"""
Step 1f — File Parser (dispatcher)

FileParser is a thin orchestrator.  It:
  1. Reads raw lines from disk (always — needed even on parse failure)
  2. Looks up the correct language-specific parser from REGISTRY
  3. Delegates to that parser's parse() method
  4. Writes/reads a JSON cache to avoid re-parsing on pipeline re-runs

Adding a new language requires only:
  - Creating ingestion/parsers/{language}_parser.py  (subclass BaseParser)
  - Registering it in REGISTRY below

No changes to FileParser itself — open/closed principle.

Usage:
    parser      = FileParser()
    parsed_file = parser.parse(file_meta, cache_dir=layout.parsed_dir)
    results     = parser.parse_many(file_metas, cache_dir=layout.parsed_dir)
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Type

from ingestion.models import FileMeta, ParsedFile, ParsedSymbol
from ingestion.parsers.base import BaseParser
from ingestion.parsers.dart_parser import DartParser
from ingestion.parsers.kotlin_parser import KotlinParser
from ingestion.parsers.python_parser import PythonParser
from ingestion.parsers.rust_parser import RustParser
from ingestion.parsers.swift_parser import SwiftParser

logger = logging.getLogger(__name__)

# ── Language registry ─────────────────────────────────────────────────────────
# Maps language name (from LanguageDetector) → parser class
# Add new languages here — no other file needs to change.

# Try tree-sitter parsers; fall back to regex if grammar unavailable
try:
    from ingestion.parsers.kotlin_ts_parser import KotlinTSParser
    _kotlin_parser: Type[BaseParser] = KotlinTSParser
    if not KotlinTSParser._make_parser():
        from ingestion.parsers.kotlin_parser import KotlinParser as _kotlin_parser  # noqa: F811
except Exception:
    from ingestion.parsers.kotlin_parser import KotlinParser as _kotlin_parser  # noqa: F811

try:
    from ingestion.parsers.rust_ts_parser import RustTSParser
    _rust_parser: Type[BaseParser] = RustTSParser
    if not RustTSParser._make_parser():
        from ingestion.parsers.rust_parser import RustParser as _rust_parser  # noqa: F811
except Exception:
    from ingestion.parsers.rust_parser import RustParser as _rust_parser  # noqa: F811

REGISTRY: Dict[str, Type[BaseParser]] = {
    "python": PythonParser,
    "swift":  SwiftParser,
    "rust":   _rust_parser,
    "dart":   DartParser,
    "kotlin": _kotlin_parser,
}

# Instantiated parsers — one instance per language (stateless, safe to reuse)
_INSTANCES: Dict[str, BaseParser] = {
    lang: cls() for lang, cls in REGISTRY.items()
}


class FileParser:
    """
    Dispatches each FileMeta to the appropriate language-specific parser.

    This class owns only:
      - File reading (raw_lines)
      - Parser lookup (REGISTRY)
      - Parse-result caching (workspace/runs/{run_id}/parsed/)
      - Aggregate logging

    All language logic lives in ingestion/parsers/{language}_parser.py.
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def parse(self, file_meta: FileMeta, cache_dir: Optional[Path] = None) -> ParsedFile:
        """
        Parse one source file.  Returns a cached result if available.

        Files marked is_parseable=False are returned immediately (no parser
        called) — the chunker will apply sliding window.

        Args:
            file_meta:  FileMeta from Step 1e.
            cache_dir:  workspace/runs/{run_id}/parsed/ for caching.

        Returns:
            ParsedFile — raw_lines always populated; symbols=[] on failure.
        """
        raw_lines = _read_lines(file_meta.absolute_path)

        # Cache hit
        if cache_dir:
            cached = _load_cache(file_meta, cache_dir, raw_lines)
            if cached:
                return cached

        # Not parseable — skip parser, let chunker use sliding window
        if not file_meta.is_parseable:
            result = ParsedFile(
                file_meta     = file_meta,
                symbols       = [],
                raw_lines     = raw_lines,
                parse_success = False,
                parse_error   = "marked not parseable in Step 1e",
            )
            _save_cache(result, cache_dir)
            return result

        parser = _INSTANCES.get(file_meta.language)
        if parser is None:
            result = ParsedFile(
                file_meta     = file_meta,
                symbols       = [],
                raw_lines     = raw_lines,
                parse_success = False,
                parse_error   = f"no parser registered for language: {file_meta.language!r}",
            )
            _save_cache(result, cache_dir)
            return result

        try:
            source  = "\n".join(raw_lines)
            symbols = parser.parse(source=source, raw_lines=raw_lines)
            result  = ParsedFile(
                file_meta     = file_meta,
                symbols       = symbols,
                raw_lines     = raw_lines,
                parse_success = True,
            )
        except Exception as exc:
            logger.warning(
                "[FileParser] %s parse error in %s: %s",
                file_meta.language, file_meta.file_path, exc,
            )
            result = ParsedFile(
                file_meta     = file_meta,
                symbols       = [],
                raw_lines     = raw_lines,
                parse_success = False,
                parse_error   = str(exc),
            )

        _save_cache(result, cache_dir)
        return result

    def parse_many(
        self,
        file_metas: List[FileMeta],
        cache_dir:  Optional[Path] = None,
    ) -> List[ParsedFile]:
        """
        Parse a list of files and log aggregate stats.

        Returns:
            List[ParsedFile] in the same order as file_metas.
        """
        results   = [self.parse(fm, cache_dir) for fm in file_metas]
        succeeded = sum(1 for r in results if r.parse_success)
        total_sym = sum(len(r.symbols) for r in results)
        logger.info(
            "[FileParser] Parsed %d/%d files, %d symbols extracted",
            succeeded, len(file_metas), total_sym,
        )
        return results

    @staticmethod
    def supported_languages() -> List[str]:
        """Return the list of languages with a registered parser."""
        return sorted(REGISTRY.keys())


# ── Module-level cache helpers (no class state needed) ────────────────────────

def _cache_path(file_meta: FileMeta, cache_dir: Path) -> Path:
    # Use MD5 (not hash()) — hash() is randomised per-process in Python 3.3+
    import hashlib
    digest = hashlib.md5(file_meta.absolute_path.encode()).hexdigest()[:16]
    return cache_dir / f"{digest}.json"


def _load_cache(
    file_meta: FileMeta,
    cache_dir: Path,
    raw_lines: List[str],
) -> Optional[ParsedFile]:
    path = _cache_path(file_meta, cache_dir)
    if not path.exists():
        return None
    try:
        data    = json.loads(path.read_text())
        symbols = [ParsedSymbol(**s) for s in data["symbols"]]
        return ParsedFile(
            file_meta     = file_meta,
            symbols       = symbols,
            raw_lines     = raw_lines,
            parse_success = data["parse_success"],
            parse_error   = data.get("parse_error"),
        )
    except Exception:
        return None


def _save_cache(result: ParsedFile, cache_dir: Optional[Path]) -> None:
    if cache_dir is None:
        return
    path = _cache_path(result.file_meta, cache_dir)
    try:
        payload = {
            "parse_success": result.parse_success,
            "parse_error":   result.parse_error,
            "symbols": [
                {
                    "symbol_type": s.symbol_type,
                    "name":        s.name,
                    "start_line":  s.start_line,
                    "end_line":    s.end_line,
                    "source":      s.source,
                    "parent_name": s.parent_name,
                    "decorators":  s.decorators,
                    "calls":       s.calls,
                    "imports":     s.imports,
                    "bases":       s.bases,
                }
                for s in result.symbols
            ],
        }
        path.write_text(json.dumps(payload, indent=2))
    except OSError:
        pass


def _read_lines(absolute_path: str) -> List[str]:
    try:
        return Path(absolute_path).read_text(errors="ignore").splitlines()
    except OSError:
        return []
