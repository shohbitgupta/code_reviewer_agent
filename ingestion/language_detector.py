"""
Step 1d — Language Detection

LanguageDetector assigns a language to every file in the raw inventory and
produces per-file flags (binary, generated, minified, vendor, test, config)
that drive skip and parse decisions in the downstream steps.

Detection priority (first match wins):
  1. Binary check      → null bytes in first 8 KB
  2. Vendor check      → path contains node_modules/, vendor/, etc.
  3. Generated check   → "DO NOT EDIT" header or *_pb2.py / *.generated.ts
  4. Minified check    → first non-empty line > 500 chars or *.min.js
  5. Extension map     → primary lookup table
  6. Content sniffing  → shebang on first line (fallback for unknown ext)

Usage:
    detector = LanguageDetector()
    result   = detector.detect(inventory)
    # result.language_map, result.language_stats, result.file_flags
"""

import logging
from pathlib import Path
from typing import Dict

from ingestion.models import FileFlag, FileInventory, LanguageDetectionResult

logger = logging.getLogger(__name__)

# ── Extension → language map ──────────────────────────────────────────────────
EXTENSION_MAP: Dict[str, str] = {
    ".py":   "python",
    ".js":   "javascript", ".jsx": "javascript",
    ".mjs":  "javascript", ".cjs": "javascript",
    ".ts":   "typescript", ".tsx": "typescript",
    ".java": "java",
    ".go":   "go",
    ".rb":   "ruby",
    ".rs":   "rust",
    ".cs":   "csharp",
    ".cpp":  "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".h":    "cpp", ".hpp": "cpp",          # refined by content sniffing
    ".c":    "c",
    ".kt":   "kotlin", ".kts": "kotlin",
    ".swift":"swift",
    ".dart": "dart",
    ".php":  "php",
    ".sh":   "shell", ".bash": "shell", ".zsh": "shell",
    ".sql":  "sql",
    ".yaml": "yaml", ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".md":   "markdown", ".mdx": "markdown",
    ".html": "html", ".htm": "html",
    ".css":  "css", ".scss": "css", ".sass": "css",
}

# Shebang → language (fallback for extensionless or ambiguous files)
SHEBANG_MAP: Dict[str, str] = {
    "python":     ("#!/usr/bin/env python", "#!/usr/bin/python"),
    "javascript": ("#!/usr/bin/env node",),
    "shell":      ("#!/bin/bash", "#!/bin/sh", "#!/usr/bin/env bash", "#!/usr/bin/env zsh"),
    "ruby":       ("#!/usr/bin/env ruby",),
}

# Path segments that indicate vendor / third-party code
VENDOR_PATH_SEGMENTS = {
    "node_modules", "vendor", "third_party", "third-party",
    ".venv", "venv", "env", "site-packages",
}

# Filename patterns that indicate generated code
GENERATED_FILENAME_SUFFIXES = (
    "_pb2.py", "_pb.go", ".generated.ts", ".auto.ts",
    ".generated.js", "_generated.go",
)

GENERATED_HEADER_MARKERS = (
    "DO NOT EDIT",
    "Code generated",
    "This file is auto-generated",
    "AUTO-GENERATED",
)

# Test file name patterns and directory names
TEST_FILE_PATTERNS = (
    "_test.py", "test_", ".test.ts", ".spec.ts",
    ".test.js", ".spec.js", "Test.java", "Spec.java",
)
TEST_DIR_NAMES = {"test", "tests", "spec", "__tests__", "e2e"}

# Config file extensions (retained in pipeline but flagged)
CONFIG_EXTENSIONS = {".yaml", ".yml", ".json", ".toml", ".ini", ".env", ".cfg"}

_READ_SIZE = 8_192  # bytes to read for binary detection


class LanguageDetector:
    """
    Assigns a language and content-type flags to every file in a FileInventory.

    The detector is stateless — a single instance can be reused across runs.
    """

    def detect(self, inventory: FileInventory) -> LanguageDetectionResult:
        """
        Process every file in the inventory.

        Returns:
            LanguageDetectionResult with language_map, language_stats,
            and per-file FileFlag objects.
        """
        language_map:   Dict[str, str]       = {}
        language_stats: Dict[str, int]       = {}
        file_flags:     Dict[str, FileFlag]  = {}

        for entry in inventory.all_files:
            flag = self._classify(entry.path, entry.relative_path, entry.extension)
            language_map[entry.relative_path]  = flag.language
            file_flags[entry.relative_path]    = flag
            language_stats[flag.language] = language_stats.get(flag.language, 0) + 1

        stats_str = " ".join(
            f"{lang}:{count}" for lang, count in
            sorted(language_stats.items(), key=lambda x: -x[1])
        )
        logger.info("[LanguageDetector] %s", stats_str)

        return LanguageDetectionResult(
            language_map   = language_map,
            language_stats = language_stats,
            file_flags     = file_flags,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _classify(self, path: Path, relative_path: str, ext: str) -> FileFlag:
        """Apply detection rules in priority order and return a FileFlag."""
        # 1. Binary
        if self._is_binary(path):
            return FileFlag(
                language="binary", is_binary=True, is_generated=False,
                is_minified=False, is_vendor=False, is_test=False, is_config=False,
            )

        # 2. Vendor
        is_vendor = self._is_vendor(relative_path)

        # 3. Generated
        is_generated = self._is_generated(path, relative_path)

        # 4. Minified
        is_minified = self._is_minified(path, relative_path)

        # 5. Extension map
        language = EXTENSION_MAP.get(ext)

        # 6. Content sniffing (fallback for unknown / ambiguous extensions)
        if language is None:
            language = self._sniff_language(path)

        # 7. Flags
        is_test   = self._is_test(path, relative_path)
        is_config = ext in CONFIG_EXTENSIONS

        return FileFlag(
            language     = language or "unknown",
            is_binary    = False,
            is_generated = is_generated,
            is_minified  = is_minified,
            is_vendor    = is_vendor,
            is_test      = is_test,
            is_config    = is_config,
        )

    @staticmethod
    def _is_binary(path: Path) -> bool:
        """Return True if the file contains null bytes in the first 8 KB."""
        try:
            chunk = path.read_bytes()[:_READ_SIZE]
            return b"\x00" in chunk
        except OSError:
            return False

    @staticmethod
    def _is_vendor(relative_path: str) -> bool:
        """Return True if any path segment indicates vendor / third-party code."""
        parts = set(Path(relative_path).parts)
        return bool(parts & VENDOR_PATH_SEGMENTS)

    @staticmethod
    def _is_generated(path: Path, relative_path: str) -> bool:
        """Return True for protobuf or 'DO NOT EDIT' generated files."""
        # Filename suffix check
        fname = Path(relative_path).name
        if any(fname.endswith(suf) for suf in GENERATED_FILENAME_SUFFIXES):
            return True
        # Header check (read first 5 lines)
        try:
            head = path.read_text(errors="ignore").split("\n", 5)[:5]
            header = "\n".join(head)
            return any(m in header for m in GENERATED_HEADER_MARKERS)
        except OSError:
            return False

    @staticmethod
    def _is_minified(path: Path, relative_path: str) -> bool:
        """Return True for *.min.js/.min.css or files with a very long first line."""
        fname = Path(relative_path).name
        if ".min." in fname:
            return True
        try:
            for line in path.read_text(errors="ignore").splitlines():
                if line.strip():
                    return len(line) > 500
        except OSError:
            pass
        return False

    @staticmethod
    def _is_test(path: Path, relative_path: str) -> bool:
        """Return True if the file is a test file by name or directory."""
        parts = Path(relative_path).parts
        # Test directory
        if any(p in TEST_DIR_NAMES for p in parts[:-1]):
            return True
        fname = parts[-1]
        return any(pat in fname for pat in TEST_FILE_PATTERNS)

    @staticmethod
    def _sniff_language(path: Path) -> str | None:
        """Read the first line and check for a shebang."""
        try:
            first_line = path.read_text(errors="ignore").split("\n", 1)[0]
            for lang, shebangs in SHEBANG_MAP.items():
                if any(first_line.startswith(s) for s in shebangs):
                    return lang
        except OSError:
            pass
        return None
