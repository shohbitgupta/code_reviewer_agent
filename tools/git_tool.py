"""
Step 1a — Fetch Repo

GitExecutor clones a remote Git repository to local disk using a shallow clone,
or pulls the latest changes if it has already been cloned.

Usage:
    executor = GitExecutor(repo_url="https://github.com/owner/repo")
    result   = executor.clone_or_pull()
    # result.local_repo_path, result.commit_sha, result.is_fresh_clone
"""

import logging
import os
import subprocess
import time
from pathlib import Path

from ingestion.models import CloneResult

logger = logging.getLogger(__name__)


class GitExecutor:
    """
    Clones or pulls a remote Git repository to ./workspace/repos/{owner}__repo.

    Auth:
        Private repos: set GITHUB_TOKEN env var.  The token is injected into
        the clone URL at runtime and is never logged.

    Attributes:
        LOCAL_WORKSPACE: Root directory for all cloned repos.
        repo_url:        The original (un-authed) remote URL.
        repo_name:       "owner__repo" slug derived from the URL.
        local_path:      Absolute path to the cloned repo directory.
    """

    LOCAL_WORKSPACE = "./workspace/repos"

    def __init__(self, repo_url: str):
        self.repo_url   = repo_url.rstrip("/")
        self.repo_name  = self._slug_from_url(self.repo_url)
        self.local_path = str(Path(self.LOCAL_WORKSPACE) / self.repo_name)

    # ── Public API ────────────────────────────────────────────────────────────

    def clone_or_pull(self) -> CloneResult:
        """
        Clone the repo if it does not exist locally; otherwise git pull.

        Returns:
            CloneResult with local path, repo name, HEAD SHA, and timing.

        Raises:
            RuntimeError: On git command failure (network, auth, disk).
        """
        start = time.monotonic()
        if not Path(self.local_path).exists():
            result = self._clone()
        else:
            result = self._pull()
        result.clone_duration = time.monotonic() - start
        logger.info(
            "[GitExecutor] %s %s @ %s (%.1fs)",
            "Cloned" if result.is_fresh_clone else "Pulled",
            self.repo_name,
            result.commit_sha[:8],
            result.clone_duration,
        )
        return result

    # ── Private helpers ───────────────────────────────────────────────────────

    def _clone(self) -> CloneResult:
        """Shallow clone (--depth=1) the repository."""
        Path(self.local_path).parent.mkdir(parents=True, exist_ok=True)
        auth_url = self._inject_token(self.repo_url)
        self._run(
            ["git", "clone", "--depth=1", auth_url, self.local_path],
            log_cmd=["git", "clone", "--depth=1", "<url>", self.local_path],
        )
        return CloneResult(
            local_repo_path=str(Path(self.local_path).resolve()),
            repo_name=self.repo_name,
            commit_sha=self._get_commit_sha(),
            is_fresh_clone=True,
            clone_duration=0.0,
        )

    def _pull(self) -> CloneResult:
        """
        Fast-forward pull.  If the pull fails due to local divergence,
        reset hard to origin/HEAD before retrying.
        """
        try:
            self._run(["git", "-C", self.local_path, "pull", "--ff-only"])
        except RuntimeError:
            # Local workspace diverged — reset to origin
            logger.warning(
                "[GitExecutor] FF pull failed — resetting %s to origin/HEAD",
                self.repo_name,
            )
            self._run(["git", "-C", self.local_path, "fetch", "origin"])
            self._run(
                ["git", "-C", self.local_path, "reset", "--hard", "origin/HEAD"]
            )
        return CloneResult(
            local_repo_path=str(Path(self.local_path).resolve()),
            repo_name=self.repo_name,
            commit_sha=self._get_commit_sha(),
            is_fresh_clone=False,
            clone_duration=0.0,
        )

    def _get_commit_sha(self) -> str:
        """Return the 40-char HEAD commit SHA."""
        result = self._run(
            ["git", "-C", self.local_path, "rev-parse", "HEAD"],
            capture=True,
        )
        return result.stdout.strip()

    def _inject_token(self, url: str) -> str:
        """
        Prepend GITHUB_TOKEN to https:// URLs for private repo access.
        The authed URL must never be logged.
        """
        token = os.getenv("GITHUB_TOKEN")
        if token and url.startswith("https://"):
            return url.replace("https://", f"https://{token}@", 1)
        return url

    @staticmethod
    def _slug_from_url(url: str) -> str:
        """
        Convert a GitHub URL to a filesystem-safe slug.

        Example:
            https://github.com/owner/repo.git  →  "owner__repo"
        """
        clean = url.rstrip("/").removesuffix(".git")
        parts = clean.split("/")
        if len(parts) >= 2:
            return f"{parts[-2]}__{parts[-1]}"
        return parts[-1]

    @staticmethod
    def _run(
        cmd: list,
        log_cmd: list | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess:
        """
        Run a git subprocess with a 120 s timeout.

        Args:
            cmd:     The actual command (may contain auth token).
            log_cmd: A sanitised version safe to log (token replaced).
            capture: If True, return stdout.

        Raises:
            RuntimeError: If the command exits non-zero.
        """
        display = log_cmd or cmd
        logger.debug("[GitExecutor] Running: %s", " ".join(display))
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git command failed (exit {proc.returncode}): "
                f"{proc.stderr.strip()}"
            )
        return proc
