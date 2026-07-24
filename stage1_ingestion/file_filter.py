"""
Step 1e — File Discovery

FileFilter applies skip rules to the full FileInventory and produces a
filtered List[FileMeta] containing only files worth parsing and chunking.

Skip rules (first match wins):
  1. Hard-skip directories  (.git/, node_modules/, dist/, build/, etc.)
  2. Vendor files           (is_vendor=True from Step 1d)
  3. Binary files           (is_binary=True from Step 1d)
  4. Lock files             (package-lock.json, yarn.lock, etc.)
  5. Generated files        (is_generated=True from Step 1d)
  6. Size < 10 B            (empty / near-empty)
  Size > 500 KB             → kept, but is_parseable=False
  Minified files            → kept, but is_parseable=False
  language = "unknown"      → kept, but is_parseable=False

Usage:
    ff         = FileFilter(inventory, language_map, file_flags, repo_name)
    file_metas = ff.run()
"""

import fnmatch
import logging
from pathlib import Path
from typing import List

from core.models import (
    FileFlag,
    FileMeta,
    FileInventory,
    LanguageDetectionResult,
    RawFileEntry,
)

logger = logging.getLogger(__name__)

# ── Skip-rule constants ───────────────────────────────────────────────────────

# Directory path segments that always cause a file to be skipped
SKIP_DIR_SEGMENTS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", ".idea", ".vscode", "coverage",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".eggs",
}

# Glob patterns matched against individual path segments (fnmatch)
SKIP_DIR_GLOBS = ["*.egg-info", "*.dist-info"]

# Exact file names that are always skipped (lock files)
LOCK_FILE_NAMES = {
    "package-lock.json", "yarn.lock", "poetry.lock",
    "Pipfile.lock", "composer.lock", "Gemfile.lock",
    "pnpm-lock.yaml", "bun.lockb",
}

# Binary extensions that are always skipped
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz",
    ".whl", ".egg", ".pyc", ".pyo",
    ".class", ".jar", ".war",
    ".so", ".dylib", ".dll", ".exe", ".o", ".a",
    ".wasm", ".bin", ".dat",
}

MIN_FILE_SIZE    = 10          # bytes — skip files smaller than this
MAX_PARSEABLE    = 500_000     # bytes — above this, is_parseable=False


class FileFilter:
    """
    Filters the raw file inventory down to a list of FileMeta objects.

    Args:
        inventory:   Full FileInventory from RepoScanner (Step 1c).
        lang_result: LanguageDetectionResult from LanguageDetector (Step 1d).
        repo_name:   "owner__repo" slug — stored on each FileMeta.
    """

    def __init__(
        self,
        inventory:   FileInventory,
        lang_result: LanguageDetectionResult,
        repo_name:   str,
    ):
        self.inventory   = inventory
        self.lang_result = lang_result
        self.repo_name   = repo_name

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> List[FileMeta]:
        """
        Apply all filter rules and return the list of files to process.

        Returns:
            List[FileMeta] — files that passed filtering, each with an
            is_parseable flag indicating whether AST parsing should be
            attempted.
        """
        kept:    List[FileMeta] = []
        skipped: int = 0

        for entry in self.inventory.all_files:
            flag = self.lang_result.file_flags.get(entry.relative_path)
            if flag is None:
                skipped += 1
                continue

            skip_reason = self._should_skip(entry, flag)
            if skip_reason:
                skipped += 1
                continue

            is_parseable = self._compute_parseable(entry, flag)
            if not is_parseable:
                logger.debug(
                    "[FileFilter] is_parseable=False: %s (size=%d, minified=%s, lang=%s)",
                    entry.relative_path, entry.size_bytes,
                    flag.is_minified, flag.language,
                )

            line_count = self._count_lines(entry)
            kept.append(
                FileMeta(
                    file_path     = entry.relative_path,
                    absolute_path = str(entry.path),
                    language      = flag.language,
                    extension     = entry.extension,
                    size_bytes    = entry.size_bytes,
                    line_count    = line_count,
                    last_modified = entry.last_modified,
                    is_parseable  = is_parseable,
                    repo_name     = self.repo_name,
                )
            )

        logger.info(
            "[FileFilter] %d files kept, %d skipped", len(kept), skipped
        )
        return kept

    # ── Private helpers ───────────────────────────────────────────────────────

    def _should_skip(self, entry: RawFileEntry, flag: FileFlag) -> str | None:
        """
        Return a reason string if the file should be skipped, else None.
        """
        # 1. Hard-skip directories
        parts = list(Path(entry.relative_path).parts)
        parts_set = set(parts)
        # Exact segment match
        if parts_set & SKIP_DIR_SEGMENTS:
            return "skip-dir"
        # Glob pattern match (e.g. *.egg-info)
        if any(fnmatch.fnmatch(p, pat) for p in parts for pat in SKIP_DIR_GLOBS):
            return "skip-dir-glob"
        # Also skip dirs that start with "." (hidden)
        if any(p.startswith(".") for p in parts):
            return "hidden-dir"

        # 2. Vendor
        if flag.is_vendor:
            return "vendor"

        # 3. Binary (by flag or extension)
        if flag.is_binary or entry.extension in BINARY_EXTENSIONS:
            return "binary"

        # 4. Lock files
        if Path(entry.relative_path).name in LOCK_FILE_NAMES:
            return "lock-file"

        # 5. Generated
        if flag.is_generated:
            return "generated"

        # 6. Too small
        if entry.size_bytes < MIN_FILE_SIZE:
            return "too-small"

        return None

    @staticmethod
    def _compute_parseable(entry: RawFileEntry, flag: FileFlag) -> bool:
        """Determine whether AST parsing should be attempted."""
        if entry.size_bytes > MAX_PARSEABLE:
            logger.warning(
                "[FileFilter] Large file (%.1f KB) set to is_parseable=False: %s",
                entry.size_bytes / 1024,
                entry.relative_path,
            )
            return False
        if flag.is_minified:
            return False
        if flag.language == "unknown":
            return False
        return True

    @staticmethod
    def _count_lines(entry: RawFileEntry) -> int:
        """Count lines in the file; return 0 on read error."""
        try:
            return entry.path.read_text(errors="ignore").count("\n") + 1
        except OSError:
            return 0
