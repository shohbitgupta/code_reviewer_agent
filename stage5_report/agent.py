"""
Report Agent — Stage 5 of the code review pipeline.

Closes the pipeline loop:
  1. Builds a structured report dict + HTML + JSON from all prior stage outputs
  2. Optionally posts inline review comments to GitHub (if pr_number > 0)

State keys consumed
───────────────────
    state["issues"]          List[ReviewIssue]
    state["comments"]        List[ReviewComment]
    state["ingestion_stats"] Dict
    state["review_stats"]    Dict
    state["comment_stats"]   Dict
    state["repo_url"]        str
    state["pr_number"]       int   (0 = no GitHub posting)
    state["head_sha"]        str   (needed for GitHub review API)
    state["run_id"]          str

State keys produced
───────────────────
    state["report"]           Dict  — structured, JSON-serialisable summary
    state["report_path"]      str   — absolute path to the HTML file
    state["posted_to_github"] bool

Usage:
    from stage5_report.agent import run_report
    state = run_report(state)
    print(state["report_path"])   # open this in a browser
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from stage5_report.github_poster import GitHubPoster
from stage5_report.report_builder import ReportBuilder

logger = logging.getLogger(__name__)


def run_report(
    state:        Dict[str, Any],
    github_token: str  = "",
    skip_posting: bool = False,
    dry_run:      bool = False,
) -> Dict[str, Any]:
    """
    Stage 5 agent entry point.

    Args:
        state:        Shared ReviewState dict.
        github_token: GitHub PAT for posting inline PR comments.
        skip_posting: If True, never call the GitHub API (report-only mode).
        dry_run:      If True, log what would be posted but don't call the API.

    Returns:
        Updated state with "report", "report_path", "posted_to_github".
    """
    t_start = time.monotonic()

    repo_url:  str = state.get("repo_url", "")
    pr_number: int = state.get("pr_number", 0)
    head_sha:  str = state.get("head_sha", "")
    run_id:    str = state.get("run_id", "unknown")

    repo_name = _slug(repo_url)

    # ── 1. Build report ───────────────────────────────────────────────────────
    builder = ReportBuilder(run_id=run_id, repo_name=repo_name, pr_number=pr_number)
    report  = builder.build(state)
    state["report"]      = report
    state["report_path"] = report["html_path"]

    logger.info(
        "[ReportAgent] Report built in %.1fs — %d issues across %d files",
        time.monotonic() - t_start,
        report["summary"].get("total_issues", 0),
        report["summary"].get("files_with_issues", 0),
    )

    # ── 2. Post to GitHub ─────────────────────────────────────────────────────
    posted = False
    if pr_number and not skip_posting:
        poster = GitHubPoster(token=github_token, dry_run=dry_run)
        posted = poster.post(
            repo_url  = repo_url,
            pr_number = pr_number,
            head_sha  = head_sha,
            state     = state,
        )

    state["posted_to_github"] = posted
    return state


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slug(repo_url: str) -> str:
    """owner/repo from a GitHub URL, or the URL itself as fallback."""
    try:
        from tools.github_tool import GitHubTool
        owner, repo = GitHubTool.parse_repo_url(repo_url)
        return f"{owner}/{repo}"
    except Exception:
        return repo_url
