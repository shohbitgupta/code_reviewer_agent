"""
Step 1b — Store in Workspace

WorkspaceManager creates an isolated, reproducible directory for each pipeline
run. Every run gets its own run_id directory so concurrent runs never collide
and past runs remain fully inspectable.

Directory layout produced by setup():

    workspace/
      repos/
        {repo_name}/              ← cloned repo (Step 1a — already exists)
      parse_cache/
        {repo_name}/              ← ParsedFile JSON cache (Step 1f) — repo-scoped,
                                     persists across runs (see file_parser.py)
      runs/
        {run_id}/
          raw/                    ← symlink → ../../repos/{repo_name}
          chunks/
            chunks.jsonl          ← one CodeChunk per line (Step 1g)
          graphs/
            dependency_graph.json ← NetworkX serialisation (Step 1j)
          reports/                ← review_report.md/.json (Stage 5)
          run_meta.json           ← run provenance metadata

Usage:
    ws     = WorkspaceManager(repo_name, local_repo_path, commit_sha)
    layout = ws.setup()
    # layout.run_id, layout.chunks_dir, layout.graphs_dir, ...
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from core.models import WorkspaceLayout
from core import config

logger = logging.getLogger(__name__)


class WorkspaceManager:
    """
    Creates and manages the workspace directory for a single pipeline run.

    Args:
        repo_name:        "owner__repo" slug (from Step 1a).
        local_repo_path:  Absolute path to the cloned repo (from Step 1a).
        commit_sha:       HEAD SHA (from Step 1a) — embedded in the run_id.
        run_id:           Optional override; auto-generated if None.
        repo_url:         Original remote URL — written to run_meta.json.
    """

    WORKSPACE_ROOT = Path(config.WORKSPACE_ROOT)

    def __init__(
        self,
        repo_name: str,
        local_repo_path: str,
        commit_sha: str,
        run_id: str | None = None,
        repo_url: str = "",
    ):
        self.repo_name       = repo_name
        self.local_repo_path = local_repo_path
        self.commit_sha      = commit_sha
        self.repo_url        = repo_url
        self._run_id         = run_id  # None → auto-generate in setup()

    # ── Public API ────────────────────────────────────────────────────────────

    def setup(self) -> WorkspaceLayout:
        """
        Create all run subdirectories, the raw/ symlink, and run_meta.json.

        Returns:
            WorkspaceLayout with paths to every subdirectory.
        """
        run_id  = self._run_id or self._generate_run_id()
        run_dir = self.WORKSPACE_ROOT / "runs" / run_id

        layout = WorkspaceLayout(
            run_id      = run_id,
            run_dir     = run_dir,
            raw_dir     = run_dir / "raw",
            chunks_dir  = run_dir / "chunks",
            graphs_dir  = run_dir / "graphs",
            reports_dir = run_dir / "reports",
        )

        # Create all subdirectories
        for d in (
            layout.chunks_dir,
            layout.graphs_dir,
            layout.reports_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        # raw/ → symlink to the cloned repo (saves disk — no copy)
        if not layout.raw_dir.exists() and not layout.raw_dir.is_symlink():
            layout.raw_dir.symlink_to(Path(self.local_repo_path).resolve())

        # Write run provenance
        self._write_run_meta(layout)

        logger.info("[WorkspaceManager] Run %s workspace ready", run_id)
        return layout

    # ── Private helpers ───────────────────────────────────────────────────────

    def _generate_run_id(self) -> str:
        """
        Build a human-readable, sortable run ID that embeds commit SHA.

        Format:
            {YYYYMMDD_HHMMSS}__{repo_slug_20chars}__{sha6}
        Example:
            20260312_153042__owner__repo__a3f9c1
        """
        ts    = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        short = self.commit_sha[:6]
        slug  = self.repo_name[:20]
        return f"{ts}__{slug}__{short}"

    def _write_run_meta(self, layout: WorkspaceLayout) -> None:
        """Write run_meta.json immediately so failed runs are still traceable."""
        meta = {
            "run_id":           layout.run_id,
            "repo_name":        self.repo_name,
            "repo_url":         self.repo_url,
            "commit_sha":       self.commit_sha,
            "started_at":       datetime.now(timezone.utc).isoformat(),
            "pipeline_version": config.PIPELINE_VERSION,
        }
        path = layout.run_dir / "run_meta.json"
        path.write_text(json.dumps(meta, indent=2))
