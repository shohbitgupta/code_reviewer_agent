"""
Code Reviewer Agent — main entry point.

Runs the ingestion pipeline against a target GitHub repository and prints
a summary of what was indexed.

Usage:
    python main.py https://github.com/owner/repo

Dry-run (no Qdrant, no LLM summaries):
    python main.py https://github.com/owner/repo --dry-run

Environment variables:
    VOYAGE_API_KEY     or OPENAI_API_KEY   — for embeddings
    ANTHROPIC_API_KEY                      — for LLM summaries (Step 1h)
    QDRANT_URL                             — defaults to http://localhost:6333
    GITHUB_TOKEN                           — for private repos
"""

import argparse
import logging
import sys

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt= "%H:%M:%S",
)

from agents.ingestion_agent import run_ingestion


def main() -> None:
    parser = argparse.ArgumentParser(description="Code Reviewer Agent — Ingestion")
    parser.add_argument("repo_url", help="GitHub repository URL")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip embedding, LLM calls, and Qdrant upsert"
    )
    parser.add_argument(
        "--skip-summaries", action="store_true",
        help="Skip LLM summary generation (Step 1h) but still upsert to Qdrant"
    )
    args = parser.parse_args()

    state = {"repo_url": args.repo_url}

    state = run_ingestion(
        state,
        skip_summaries = args.skip_summaries or args.dry_run,
        skip_qdrant    = args.dry_run,
    )

    if "error" in state:
        print(f"\n[ERROR] Ingestion failed: {state['error']}", file=sys.stderr)
        sys.exit(1)

    stats = state.get("ingestion_stats", {})
    print("\n── Ingestion complete ──────────────────────────────")
    print(f"  Run ID:       {stats.get('run_id')}")
    print(f"  Repo:         {stats.get('repo_name')}  @ {stats.get('commit_sha', '')[:8]}")
    print(f"  Files:        {stats.get('files_parseable')} parseable / {stats.get('files_total')} total")
    print(f"  Chunks:       {stats.get('chunks_total')}  ({stats.get('summaries_generated')} summaries)")
    print(f"  Edges:        {stats.get('edges_total')}")
    print(f"  Cycles:       {stats.get('cycles_found')}")
    print(f"  Violations:   {stats.get('layer_violations')}")
    print(f"  Duration:     {stats.get('duration_seconds')}s")
    print("────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()
