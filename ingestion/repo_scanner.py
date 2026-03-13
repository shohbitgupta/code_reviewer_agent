"""
Step 1c — Stored Repository Scanner

RepoScanner walks the cloned repository tree and builds a complete, unfiltered
file inventory.  No skip rules are applied here — that is Step 1e's job.
Recording everything (including .git/ entries) gives downstream steps accurate
total-file and total-size counts for stats reporting.

Usage:
    scanner   = RepoScanner(raw_dir=layout.raw_dir, repo_name="owner__repo")
    inventory = scanner.scan()
    # inventory.all_files, inventory.total_files, inventory.structure_hints
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set

from ingestion.models import FileInventory, RawFileEntry, StructureHints

logger = logging.getLogger(__name__)

# Monorepo indicator files at the repo root
MONOREPO_MARKER_FILES = {
    "pnpm-workspace.yaml",
    "lerna.json",
    "nx.json",
    "rush.json",
    "turbo.json",
}

# Directories whose presence at the root suggests a monorepo
MONOREPO_ROOT_DIRS = {"packages", "apps", "services", "modules", "libs"}

# Rough extension → language mapping used for structure hint detection only
_EXT_LANG = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rb": "ruby",
    ".rs": "rust",
}


class RepoScanner:
    """
    Walks workspace/raw/ and returns a complete FileInventory.

    The scanner intentionally records *every* file (including .git/ and
    node_modules/) so that stats like total_files reflect the real repo size.
    FileFilter (Step 1e) decides what to skip.

    Args:
        raw_dir:   Path to the symlinked repo root (WorkspaceLayout.raw_dir).
        repo_name: "owner__repo" slug used for logging.
    """

    def __init__(self, raw_dir: Path, repo_name: str):
        self.raw_dir   = Path(raw_dir)
        self.repo_name = repo_name

    # ── Public API ────────────────────────────────────────────────────────────

    def scan(self) -> FileInventory:
        """
        Walk the directory tree and build the file inventory.

        Returns:
            FileInventory with all_files, dir_tree, total counts, and
            structure hints.
        """
        all_files:  List[RawFileEntry] = []
        dir_tree:   Dict[str, List[str]] = {}
        total_size: int = 0

        for root, dirs, files in os.walk(self.raw_dir, followlinks=False):
            root_path    = Path(root)
            rel_root_str = str(root_path.relative_to(self.raw_dir))
            if rel_root_str == ".":
                rel_root_str = ""

            dir_tree[rel_root_str] = list(files)

            for fname in files:
                abs_path = root_path / fname
                rel_path = (
                    f"{rel_root_str}/{fname}".lstrip("/")
                    if rel_root_str
                    else fname
                )

                try:
                    stat      = abs_path.stat()
                    size      = stat.st_size
                    mtime     = datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    )
                    extension = abs_path.suffix.lower()

                    all_files.append(
                        RawFileEntry(
                            path          = abs_path,
                            relative_path = rel_path,
                            size_bytes    = size,
                            last_modified = mtime,
                            extension     = extension,
                        )
                    )
                    total_size += size

                except OSError as exc:
                    logger.warning(
                        "[RepoScanner] Cannot stat %s: %s", abs_path, exc
                    )

            if len(all_files) % 10_000 == 0 and all_files:
                logger.debug(
                    "[RepoScanner] Progress: %d files scanned…", len(all_files)
                )

        hints = self._detect_structure(dir_tree, all_files)
        inventory = FileInventory(
            all_files        = all_files,
            dir_tree         = dir_tree,
            total_files      = len(all_files),
            total_size_bytes = total_size,
            structure_hints  = hints,
        )

        logger.info(
            "[RepoScanner] Found %d files (%.1f MB)",
            inventory.total_files,
            inventory.total_size_bytes / 1e6,
        )
        return inventory

    # ── Private helpers ───────────────────────────────────────────────────────

    def _detect_structure(
        self,
        dir_tree: Dict[str, List[str]],
        all_files: List[RawFileEntry],
    ) -> StructureHints:
        """
        Infer monorepo layout and dominant languages from the root directory.
        """
        root_files = set(dir_tree.get("", []))
        root_dirs  = {
            Path(d).parts[0]
            for d in dir_tree
            if d and "/" not in d
        }

        # Monorepo check 1: marker files
        has_marker = bool(root_files & MONOREPO_MARKER_FILES)

        # Monorepo check 2: canonical monorepo top-level directories
        has_mono_dir = bool(root_dirs & MONOREPO_ROOT_DIRS)

        is_monorepo = has_marker or has_mono_dir

        # Sub-packages: immediate subdirs that have their own manifest
        sub_packages: List[str] = []
        if is_monorepo:
            for d in root_dirs & MONOREPO_ROOT_DIRS:
                children = dir_tree.get(d, [])
                # Each child dir of packages/ etc. is a sub-package
                sub_packages.extend(
                    f"{d}/{c}" for c in children
                    if (self.raw_dir / d / c).is_dir()
                )

        has_src_layout = "src" in root_dirs

        # Rough language detection from extensions (top-500 files for speed)
        root_langs: Set[str] = set()
        for entry in all_files[:500]:
            lang = _EXT_LANG.get(entry.extension)
            if lang:
                root_langs.add(lang)

        return StructureHints(
            is_monorepo    = is_monorepo,
            sub_packages   = sub_packages,
            has_src_layout = has_src_layout,
            root_languages = root_langs,
        )
