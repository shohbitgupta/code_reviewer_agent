"""
Code Reviewer Agent — CLI entry point.

Modes
─────
  Ingestion only (default):
      python main.py https://github.com/owner/repo

  Full review — all 5 stages, HTML report opened in browser:
      python main.py https://github.com/owner/repo --review

  Review a specific PR (diff-aware + GitHub comment posting):
      python main.py https://github.com/owner/repo --review --pr 42

  Dry-run (no Qdrant, no LLM, no GitHub API):
      python main.py https://github.com/owner/repo --review --dry-run

Environment variables
─────────────────────
    MODEL_TYPE          — FREE | OPENAI | ANTHROPIC (default: ANTHROPIC)
    MODEL_NAME          — override model ID for the selected provider
    ZHIPUAI_API_KEY     — API key when MODEL_TYPE=FREE (ZhipuAI GLM)
    OPENAI_API_KEY      — API key when MODEL_TYPE=OPENAI
    ANTHROPIC_API_KEY   — API key when MODEL_TYPE=ANTHROPIC (default)
    LLM_API_KEY         — universal fallback key (checked after provider key)
    LLM_BASE_URL        — override base URL for OpenAI-compatible endpoints
    GITHUB_TOKEN        — required for private repos + PR comment posting
    VOYAGE_API_KEY      — for Voyage embeddings (default backend)
    QDRANT_URL          — defaults to http://localhost:6333
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt= "%H:%M:%S",
)
logger = logging.getLogger(__name__)

_SEV_COLORS = {
    "CRITICAL": "\033[91m",   # bright red
    "HIGH":     "\033[93m",   # bright yellow
    "MEDIUM":   "\033[94m",   # bright blue
    "LOW":      "\033[96m",   # cyan
    "INFO":     "\033[37m",   # gray
    "RESET":    "\033[0m",
    "BOLD":     "\033[1m",
    "GREEN":    "\033[92m",
    "DIM":      "\033[2m",
}


def _c(key: str, text: str) -> str:
    """Wrap text in ANSI colour if stdout is a terminal."""
    if not sys.stdout.isatty():
        return text
    return _SEV_COLORS.get(key, "") + text + _SEV_COLORS["RESET"]


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="code-reviewer",
        description="AI-powered code reviewer agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python main.py https://github.com/owner/repo
  python main.py https://github.com/owner/repo --review
  python main.py https://github.com/owner/repo --review --pr 42
  python main.py https://github.com/owner/repo --review --dry-run
        """,
    )
    p.add_argument("repo_url", help="GitHub repository URL")
    # Review flags
    p.add_argument("--review",      action="store_true", help="Run full 5-stage review pipeline")
    p.add_argument("--pr",          type=int, default=0, metavar="N", help="PR number to review and comment on")
    p.add_argument("--platform",    default="github",    help="Comment platform: github|gitlab|jira|text")
    p.add_argument("--open",        action="store_true", help="Open HTML report in browser when done")
    # Skip flags
    p.add_argument("--dry-run",       action="store_true", help="No Qdrant, no LLM, no GitHub API")
    p.add_argument("--skip-qdrant",   action="store_true", help="Skip Qdrant upsert (still parses + embeds)")
    p.add_argument("--skip-summaries",action="store_true", help="Skip LLM chunk summaries (Step 1h)")
    p.add_argument("--skip-review",   action="store_true", help="Skip LLM review (pre-flagged issues only)")
    p.add_argument("--skip-posting",  action="store_true", help="Skip GitHub PR comment posting")
    # Output
    p.add_argument("--verbose", "-v", action="store_true", help="DEBUG level logging")
    return p


def main() -> None:
    args = build_parser().parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    dry = args.dry_run

    # ── Ingestion-only mode ───────────────────────────────────────────────────
    if not args.review and not args.pr:
        _run_ingestion_only(args, dry)
        return

    # ── Full review pipeline ──────────────────────────────────────────────────
    _run_full_review(args, dry)


# ── Ingestion-only path ───────────────────────────────────────────────────────

def _run_ingestion_only(args, dry: bool) -> None:
    from stage1_ingestion.agent import run_ingestion

    state = {"repo_url": args.repo_url}
    state = run_ingestion(
        state,
        skip_summaries = args.skip_summaries or dry,
        skip_qdrant    = args.skip_qdrant    or dry,
    )

    if "error" in state:
        _die(f"Ingestion failed: {state['error']}")

    _print_ingestion_summary(state)
    print(_c("DIM", "\nTip: add --review to run the full 5-stage code review pipeline.\n"))


# ── Full pipeline path ────────────────────────────────────────────────────────

def _run_full_review(args, dry: bool) -> None:
    from orchestration.graph import ReviewPipeline
    from orchestration.state import make_state

    llm_client = None
    if not dry:
        try:
            from tools.llm_client import LLMClientFactory
            llm_client = LLMClientFactory.create()
            logger.info(
                "LLM client: provider=%s  model=%s",
                llm_client.provider, llm_client.model_name,
            )
        except Exception as exc:
            logger.warning("Could not create LLM client (%s) — LLM steps skipped", exc)

    # If a PR number is given, fetch base/head SHA from GitHub before ingestion
    base_sha = head_sha = None
    if args.pr:
        base_sha, head_sha = _fetch_pr_shas(args.repo_url, args.pr)

    state = make_state(
        repo_url  = args.repo_url,
        pr_number = args.pr,
        base_sha  = base_sha,
        head_sha  = head_sha,
        platform  = args.platform,
    )

    pipeline = ReviewPipeline(
        llm_client     = llm_client,
        github_token   = os.getenv("GITHUB_TOKEN", ""),
        platform       = args.platform,
        skip_qdrant    = args.skip_qdrant    or dry,
        skip_summaries = args.skip_summaries or dry,
        skip_review    = args.skip_review    or dry,
        skip_posting   = args.skip_posting   or not args.pr or dry,
    )

    t0 = time.monotonic()
    state = pipeline.run(state)
    elapsed = round(time.monotonic() - t0, 1)

    if "error" in state:
        _die(f"Pipeline failed: {state['error']}")

    _print_review_summary(state, elapsed)

    html_path = state.get("report_path", "")
    if html_path:
        print(_c("GREEN", f"\n  📄 Report: {html_path}"))
        if args.open or _should_auto_open():
            _open_in_browser(html_path)
    print()


# ── PR SHA helper ─────────────────────────────────────────────────────────────

def _fetch_pr_shas(repo_url: str, pr_number: int):
    """Fetch base + head SHA from GitHub for diff-aware ingestion."""
    try:
        from tools.github_tool import GitHubTool
        gh    = GitHubTool()
        owner, repo = GitHubTool.parse_repo_url(repo_url)
        info  = gh.get_pr_info(owner, repo, pr_number)
        logger.info(
            "PR #%d: %s → %s  (%d changed files)",
            pr_number, info["base_ref"], info["head_ref"], info["changed_files"],
        )
        return info["base_sha"], info["head_sha"]
    except Exception as exc:
        logger.warning("Could not fetch PR info (%s) — running full ingestion", exc)
        return None, None


# ── Terminal output ───────────────────────────────────────────────────────────

def _print_ingestion_summary(state: dict) -> None:
    s = state.get("ingestion_stats", {})
    print()
    print(_c("BOLD", "── Ingestion complete " + "─" * 40))
    print(f"  Run ID   : {s.get('run_id')}")
    print(f"  Repo     : {s.get('repo_name')}  @ {str(s.get('commit_sha',''))[:8]}")
    print(f"  Files    : {s.get('files_parseable')} parseable / {s.get('files_total')} total")
    print(f"  Chunks   : {s.get('chunks_total')}  ({s.get('summaries_generated')} summaries)")
    print(f"  Edges    : {s.get('edges_total')}  |  Cycles: {s.get('cycles_found')}")
    print(f"  Duration : {s.get('duration_seconds')}s")
    print(_c("BOLD", "─" * 62))


def _print_review_summary(state: dict, elapsed: float) -> None:
    i_stats = state.get("ingestion_stats", {})
    r_stats = state.get("review_stats",    {})
    c_stats = state.get("comment_stats",   {})
    report  = state.get("report",          {})
    summary = report.get("summary",        {})
    by_sev  = summary.get("by_severity",   {})
    total   = summary.get("total_issues",   0)

    print()
    print(_c("BOLD", "═" * 62))
    print(_c("BOLD", "  Code Review Complete"))
    print(_c("BOLD", "═" * 62))

    # Repo / run info
    print(f"  Run     : {state.get('run_id')}  ({elapsed}s total)")
    print(f"  Repo    : {i_stats.get('repo_name','—')}  @ {str(i_stats.get('commit_sha',''))[:8]}")
    if state.get("pr_number"):
        posted = "✓ posted" if state.get("posted_to_github") else "✗ not posted"
        print(f"  PR      : #{state['pr_number']}  ({posted})")

    print()

    # Severity breakdown
    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
        n = by_sev.get(sev, 0)
        if n == 0:
            continue
        bar_w = min(30, n * 2)
        bar   = "█" * bar_w
        print(f"  {_c(sev, f'{sev:<9}')} {bar:<30}  {n:>4} issue(s)")

    if total == 0:
        print(_c("GREEN", "  ✅  No issues found — clean code!"))

    print()
    print(f"  Chunks reviewed : {r_stats.get('chunks_reviewed','—')} / {r_stats.get('chunks_selected','—')} selected")
    print(f"  LLM cache hits  : {r_stats.get('cache_hits','—')}")
    print(f"  Comments        : {c_stats.get('comments_inline','—')} inline"
          f"  +  {c_stats.get('comments_total',0) - c_stats.get('comments_inline',0)} summary")
    print(_c("BOLD", "─" * 62))


def _should_auto_open() -> bool:
    """Auto-open if running on macOS and not in CI."""
    return sys.platform == "darwin" and not os.getenv("CI")


def _open_in_browser(path: str) -> None:
    try:
        subprocess.Popen(["open", path])
        print(_c("DIM", "  Opening report in browser…"))
    except Exception:
        pass


def _die(msg: str) -> None:
    print(_c("CRITICAL", f"\n[ERROR] {msg}"), file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
