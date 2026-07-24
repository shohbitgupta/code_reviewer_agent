"""
Stage 4 — Comment Writer

Produces the final body string for each CommentGroup using a two-path strategy:

  CRITICAL / HIGH groups  →  LLM polish pass
      The LLM rewrites the body to add "why this matters" context, converts
      the raw suggestion into an actual before/after code snippet, and keeps
      the total under 250 words.  Uses anthropic tool_use (write_comment tool)
      so output is validated at the API layer — no string parsing.

  MEDIUM / LOW / INFO     →  Template rendering only
      The formatter's output is used directly.  No API call.

Caching
───────
LLM results are cached by md5(group_id + platform + body_preview[:200]).
Since group_id is a fresh UUID per run, re-runs on identical issues produce
identical group_ids only when the caller preserves them (e.g. in a workflow
checkpoint).  The cache is therefore useful mainly within a single run when
the writer is called multiple times for the same group (e.g. retry path).

Usage::

    writer = CommentWriter(llm_client=anthropic.Anthropic(), platform="github")
    comments = writer.polish(formatter_outputs, chunk_map)
    # Returns List[ReviewComment] with .polished=True on LLM-touched entries.
"""

from __future__ import annotations

import hashlib
import logging
import pickle
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import config
from stage4_comments.grouper import CommentGroup
from core.models import ReviewComment

logger = logging.getLogger(__name__)

_MAX_RETRIES     = 3
_INITIAL_BACKOFF = 2.0
_POLISH_CACHE    = Path("./workspace/comment_cache")

# ── Anthropic tool schema for LLM polish ─────────────────────────────────────

WRITE_COMMENT_TOOL: Dict[str, Any] = {
    "name":        "write_comment",
    "description": (
        "Write the final formatted review comment body for a code violation. "
        "Be concise (under 250 words), constructive, and include a concrete "
        "before/after code suggestion when possible."
    ),
    "input_schema": {
        "type": "object",
        "required": ["title", "body"],
        "properties": {
            "title": {
                "type":        "string",
                "description": "Short heading for the comment (max 80 chars).",
            },
            "body": {
                "type":        "string",
                "description": (
                    "Full comment body in the target platform's markdown. "
                    "Include: what the problem is, WHY it matters, and a "
                    "concrete fix (before/after code if possible)."
                ),
            },
        },
    },
}

_POLISH_SYSTEM = """\
You are a senior engineer writing a code review comment that will be posted
directly to a pull request.

Tone: collegial, direct, never accusatory.

Structure your comment as:
  1. One sentence: what the problem is.
  2. One sentence: why it matters (security, reliability, maintainability).
  3. Concrete fix — show before/after code if the suggestion is non-trivial.

Keep the total under 250 words. Use the target platform's markdown (specified
in the user message).  Call write_comment with the final body.\
"""


class CommentWriter:
    """
    Applies LLM polish to CRITICAL/HIGH comment bodies and assembles the
    final ReviewComment objects for all groups.

    Args:
        llm_client:    anthropic.Anthropic() sync client; may be None (skips
                       LLM polish, falls back to formatter output for all).
        platform:      Target platform string — passed into the polish prompt.
        model:         Anthropic model ID (defaults to config.REVIEW_MODEL).
        max_concurrency: BoundedSemaphore size for parallel polish calls.
        cache_dir:     Directory for pickle-based LLM result cache.
    """

    def __init__(
        self,
        llm_client            = None,
        platform:       str   = "github",
        model:          str   = config.REVIEW_MODEL,
        max_concurrency: int  = 6,
        cache_dir:      Optional[Path] = None,
    ) -> None:
        self._client     = llm_client
        self._platform   = platform
        self._model      = model
        self._semaphore  = threading.BoundedSemaphore(max_concurrency)
        self._cache_dir  = Path(cache_dir) if cache_dir else _POLISH_CACHE
        self._cache_lock = threading.Lock()
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ── Public ────────────────────────────────────────────────────────────────

    def polish(
        self,
        groups:         List[CommentGroup],
        formatted_bodies: Dict[str, str],   # group_id → formatter body
        chunk_map:      Dict = None,
    ) -> List[ReviewComment]:
        """
        Build final ReviewComment objects for each group.

        For CRITICAL/HIGH groups, attempts LLM polish on the formatted_body.
        For MEDIUM/LOW groups, uses the formatted_body directly.

        Args:
            groups:           All CommentGroup objects.
            formatted_bodies: {group.group_id: formatted_body_str}
            chunk_map:        Optional CodeChunk map for source snippets
                              (improves polish quality but not required).

        Returns:
            List[ReviewComment] in the same order as groups.
        """
        results: List[ReviewComment] = []
        for group in groups:
            base_body = formatted_bodies.get(group.group_id, "")
            if group.bucket == "high" and self._client is not None:
                final_body, polished = self._polish_group(group, base_body, chunk_map)
            else:
                final_body, polished = base_body, False

            results.append(ReviewComment.new(
                file_path    = group.file_path,
                line         = group.line,
                end_line     = group.end_line,
                body         = final_body,
                severity     = group.severity,
                comment_type = "inline",
                platform     = self._platform,
                issue_ids    = group.issue_ids,
                language     = group.language,
                polished     = polished,
            ))
        return results

    # ── Private ───────────────────────────────────────────────────────────────

    def _polish_group(
        self,
        group:     CommentGroup,
        base_body: str,
        chunk_map: Optional[Dict],
    ) -> tuple[str, bool]:
        """
        Return (final_body, polished).  Falls back to base_body on any error.
        """
        cache_key = self._make_cache_key(group, base_body)
        cached    = self._load_cache(cache_key)
        if cached is not None:
            logger.debug("[CommentWriter] Cache hit for group %s", group.group_id)
            return cached, True

        source_snippet = self._extract_source(group, chunk_map)

        user_msg = self._build_polish_prompt(group, base_body, source_snippet)

        try:
            with self._semaphore:
                polished_body = self._call_with_retry(user_msg)
        except Exception as exc:
            logger.warning(
                "[CommentWriter] LLM polish failed for %s:%d — %s. "
                "Using formatter output.",
                group.file_path, group.line, exc,
            )
            return base_body, False

        self._save_cache(cache_key, polished_body)
        return polished_body, True

    def _build_polish_prompt(
        self,
        group:          CommentGroup,
        base_body:      str,
        source_snippet: str,
    ) -> str:
        parts = [
            f"Platform: {self._platform}",
            f"File: {group.file_path} (lines {group.line}–{group.end_line})",
            f"Language: {group.language}",
            "",
            "VIOLATIONS TO COMMENT ON:",
        ]
        for issue in group.issues:
            parts.append(
                f"  [{issue.rule_id}] {issue.severity}: {issue.title}"
                f"\n  Description: {issue.description}"
                f"\n  Raw suggestion: {issue.suggestion}"
            )

        if source_snippet:
            parts += ["", "SOURCE CODE (context):", f"```{group.language}", source_snippet, "```"]

        parts += [
            "",
            "DRAFT COMMENT (improve this):",
            base_body,
            "",
            "Rewrite the comment as a polished, constructive review. "
            "Call write_comment with the final body.",
        ]
        return "\n".join(parts)

    def _call_with_retry(self, user_msg: str) -> str:
        last_exc: Optional[Exception] = None
        backoff = _INITIAL_BACKOFF

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return self._call_once(user_msg)
            except Exception as exc:
                last_exc = exc
                status   = getattr(exc, "status_code", None)
                if status in (400, 401, 403):
                    raise
                if attempt < _MAX_RETRIES:
                    time.sleep(backoff)
                    backoff *= 2

        raise last_exc  # type: ignore[misc]

    def _call_once(self, user_msg: str) -> str:
        response = self._client.messages.create(
            model       = self._model,
            max_tokens  = 512,
            system      = _POLISH_SYSTEM,
            messages    = [{"role": "user", "content": user_msg}],
            tools       = [WRITE_COMMENT_TOOL],
            tool_choice = {"type": "any"},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "write_comment":
                return block.input.get("body", "")
        return ""

    @staticmethod
    def _extract_source(
        group:     CommentGroup,
        chunk_map: Optional[Dict],
    ) -> str:
        """Pull relevant source lines from the chunk_map (best-effort)."""
        if chunk_map is None:
            return ""
        # Find any chunk whose file matches and whose line range overlaps the group
        for chunk in chunk_map.values():
            if chunk.file_path != group.file_path:
                continue
            if chunk.start_line <= group.end_line and chunk.end_line >= group.line:
                lines = chunk.content.splitlines()
                # Trim to the group's window (at most 20 lines)
                offset     = max(0, group.line - chunk.start_line)
                end_offset = min(len(lines), offset + 20)
                return "\n".join(lines[offset:end_offset])
        return ""

    # ── Cache ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _make_cache_key(group: CommentGroup, base_body: str) -> str:
        payload = group.group_id + base_body[:200]
        return hashlib.md5(payload.encode()).hexdigest()

    def _load_cache(self, key: str) -> Optional[str]:
        path = self._cache_dir / f"{key}.pkl"
        if not path.exists():
            return None
        try:
            with self._cache_lock, path.open("rb") as f:
                return pickle.load(f)
        except Exception:
            return None

    def _save_cache(self, key: str, data: str) -> None:
        path = self._cache_dir / f"{key}.pkl"
        try:
            with self._cache_lock, path.open("wb") as f:
                pickle.dump(data, f)
        except Exception as exc:
            logger.debug("[CommentWriter] Cache write failed: %s", exc)
