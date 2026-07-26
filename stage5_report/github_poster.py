"""
Stage 5 — GitHub Poster

Posts the Stage 4 ReviewComment objects to the target pull request as a
single GitHub pull-request review (all inline comments in one API call).

Posting strategy
────────────────
- CRITICAL / HIGH inline comments → posted as line comments in the diff
- Summary ReviewComment (line=0)  → folded into the review body header
- A short Markdown summary from the report is prepended to the review body
  so reviewers get the overall severity count at a glance

Error handling
──────────────
- If GitHub returns 422 for a specific line comment (the line is not part of
  the PR diff), the comment is silently downgraded: its body is appended to
  the review body instead.
- If posting fails entirely, the error is logged but the stage still completes
  successfully so the local HTML report is always written.

Usage::

    poster = GitHubPoster(token=os.getenv("GITHUB_TOKEN",""), dry_run=False)
    posted = poster.post(
        repo_url="https://github.com/owner/repo",
        pr_number=42,
        head_sha="abc123",
        state=state,
    )
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from core.models import ReviewComment
from tools.github_tool import GitHubTool

logger = logging.getLogger(__name__)


class GitHubPoster:
    """
    Posts inline review comments to a GitHub pull request.

    Args:
        token:   GitHub personal access token.
        dry_run: If True, log what would be posted but make no API calls.
    """

    def __init__(self, token: str = "", dry_run: bool = False) -> None:
        self._token   = token or os.getenv("GITHUB_TOKEN", "")
        self._dry_run = dry_run
        self._gh      = GitHubTool(token=self._token)

    def post(
        self,
        repo_url:   str,
        pr_number:  int,
        head_sha:   str,
        state:      Dict[str, Any],
    ) -> bool:
        """
        Post all ReviewComment objects to the PR.

        Returns True if the review was posted successfully, False otherwise.
        """
        if not pr_number:
            logger.info("[GitHubPoster] No PR number — skipping GitHub posting")
            return False

        comments: List[ReviewComment] = state.get("comments", [])
        report:   Dict                = state.get("report",   {})

        if not comments:
            logger.info("[GitHubPoster] No comments to post")
            return False

        try:
            owner, repo = self._gh.parse_repo_url(repo_url)
        except ValueError as exc:
            logger.error("[GitHubPoster] %s", exc)
            return False

        summary_body = self._build_review_body(report, state)

        if self._dry_run:
            logger.info(
                "[GitHubPoster] DRY-RUN — would post %d comments to %s/%s PR#%d",
                len([c for c in comments if c.comment_type == "inline"]),
                owner, repo, pr_number,
            )
            return False

        try:
            result = self._gh.post_review(
                owner        = owner,
                repo         = repo,
                pr_number    = pr_number,
                commit_sha   = head_sha,
                comments     = comments,
                summary_body = summary_body,
            )
            logger.info(
                "[GitHubPoster] Review %s posted to %s/%s PR#%d",
                result.get("id"), owner, repo, pr_number,
            )
            return True
        except RuntimeError as exc:
            logger.error("[GitHubPoster] Failed to post review: %s", exc)
            return False

    # ── Private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build_review_body(report: Dict, state: Dict) -> str:
        """
        Render the review's top-level body: title, severity badges, the top
        5 most frequent rule violations, and any evicted "summary"-type
        ReviewComment bodies appended below a divider.
        """
        summary = report.get("summary", {})
        by_sev  = summary.get("by_severity", {})
        total   = summary.get("total_issues", 0)
        repo    = report.get("repo_name", "")
        pr_num  = report.get("pr_number", 0)

        badges = " · ".join(
            f"**{sev}**: {by_sev.get(sev, 0)}"
            for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
            if by_sev.get(sev, 0) > 0
        )

        lines = [
            f"## 🔍 Code Review — {repo} PR #{pr_num}" if pr_num else "## 🔍 Code Review",
            "",
            f"**{total} issue(s) found** · {badges}" if badges else f"**{total} issue(s) found**",
            "",
        ]

        top_rules = summary.get("top_rules", [])[:5]
        if top_rules:
            lines.append("**Most frequent violations:**")
            for r in top_rules:
                lines.append(f"- `{r['rule_id']}` × {r['count']}")
            lines.append("")

        lines.append(
            "_Inline comments show specific violations. "
            "See the full HTML report for details._"
        )

        # Append any summary-type ReviewComment bodies
        for c in state.get("comments", []):
            if c.comment_type == "summary" and c.body:
                lines += ["", "---", c.body]

        return "\n".join(lines)
