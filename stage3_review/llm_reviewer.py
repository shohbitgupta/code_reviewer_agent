"""
Stage 3 — LLM Reviewer

Wraps the Anthropic messages.create() call with:
  - Structured output via tool_use (report_issues tool)
    → no fragile string parsing; schema validated at the API layer
  - Exponential-backoff retry for transient errors (429, 500, 503)
  - Content-hash cache  → identical chunk+rules → return cached result,
    no API call.  Survives pipeline restarts on large repos.
  - Bounded semaphore   → caps concurrent requests to respect API rate limits
  - Sliding-window rate limiter (opt-in via requests_per_minute) → caps total
    request volume, including retries, over time. A concurrency cap alone
    only bounds how many requests are in flight at once; providers with a
    hard per-minute ceiling (e.g. GLM's free tier: 50 req/min counting failed
    attempts) can still be exceeded by a handful of threads cycling through
    requests quickly — this limiter throttles the sustained rate instead.

The cache is keyed by md5(chunk.content) + md5(sorted rule_ids) so:
  - Unchanged chunks across re-runs hit the cache instantly
  - Changing the standards file invalidates all cached entries for that language

Usage::

    reviewer = LLMReviewer(llm_client=LLMClientFactory.create())
    raw, was_cached = reviewer.review(system_prompt, user_prompt, chunk, relevant_rules)
    # raw → List[dict]  (validated tool_use input)
"""

from __future__ import annotations

import hashlib
import json
import logging
import pickle
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from core import config
from core.models import CodeChunk

logger = logging.getLogger(__name__)

# ── Anthropic tool schema ─────────────────────────────────────────────────────

REPORT_ISSUES_TOOL: Dict[str, Any] = {
    "name":        "report_issues",
    "description": (
        "Report all coding-standard violations found in the reviewed chunk. "
        "Call with an empty `issues` list if no violations are found."
    ),
    "input_schema": {
        "type": "object",
        "required": ["issues"],
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "rule_id", "severity", "title",
                        "description", "line", "suggestion", "confidence",
                    ],
                    "properties": {
                        "rule_id":     {"type": "string"},
                        "severity":    {
                            "type": "string",
                            "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"],
                        },
                        "title":       {"type": "string"},
                        "description": {"type": "string"},
                        "line":        {"type": "integer", "minimum": 1},
                        "suggestion":  {"type": "string"},
                        "confidence":  {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "category": {"type": "string"},
                    },
                },
            }
        },
    },
}

# ── Retry configuration ───────────────────────────────────────────────────────
_MAX_RETRIES         = 3
_INITIAL_BACKOFF     = 2.0    # seconds; doubles on each retry (5xx / other transient errors)
_RATE_LIMIT_BACKOFF  = 15.0   # seconds; fallback wait on 429 when no Retry-After header is sent


class _RateLimiter:
    """
    Thread-safe sliding-window rate limiter.

    Blocks ``acquire()`` callers so that no more than ``max_per_minute`` calls
    succeed in any trailing 60-second window, shared across every thread that
    holds a reference to this instance. A concurrency cap (e.g. a bounded
    semaphore) only limits how many requests are in flight at once — it does
    nothing to stop a small number of threads from *sustaining* a request rate
    above a provider's per-minute ceiling. This limiter throttles that
    sustained rate directly, independent of concurrency.
    """

    def __init__(self, max_per_minute: int) -> None:
        self._max_per_minute = max_per_minute
        self._lock = threading.Lock()
        self._timestamps: Deque[float] = deque()

    def acquire(self) -> None:
        """Block until issuing one more request keeps the trailing 60s count under the cap."""
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 60.0:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._max_per_minute:
                    self._timestamps.append(now)
                    return
                wait = 60.0 - (now - self._timestamps[0]) + 0.05
            time.sleep(max(wait, 0.05))


class LLMReviewer:
    """
    Thread-safe LLM reviewer.

    Args:
        llm_client:    UnifiedLLMClient from LLMClientFactory (sync).
        model:         Model ID (default: config.REVIEW_MODEL).
        max_tokens:    Max tokens in the completion (default 1024).
        max_concurrency: Max simultaneous LLM requests (default 8).
        cache_dir:     If set, persists raw issue dicts to avoid re-calling
                       the LLM for unchanged chunks across pipeline runs.
        requests_per_minute: If set, throttles total request volume (including
                       retries) to stay under this many calls per rolling 60s
                       window. Intended for providers with a hard per-minute
                       ceiling, such as GLM's free tier. None (default) disables
                       throttling — appropriate for providers with much higher,
                       account-level limits (Anthropic, OpenAI paid tiers).
    """

    def __init__(
        self,
        llm_client,
        model:               str            = config.REVIEW_MODEL,
        max_tokens:          int            = 1500,
        max_concurrency:     int            = 8,
        cache_dir:           Optional[Path] = None,
        requests_per_minute: Optional[int]  = None,
    ) -> None:
        self._client       = llm_client
        self._model        = model
        self._max_tokens    = max_tokens
        self._semaphore    = threading.BoundedSemaphore(max_concurrency)
        self._cache_dir    = Path(cache_dir) if cache_dir else None
        self._cache_lock   = threading.Lock()
        self._rate_limiter = _RateLimiter(requests_per_minute) if requests_per_minute else None

        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ── Public ────────────────────────────────────────────────────────────────

    def review(
        self,
        system_prompt: str,
        user_prompt:   str,
        chunk:         CodeChunk,
        rule_ids:      List[str],
        model:         str = "",
    ) -> tuple:
        """
        Review one chunk.  Returns (raw_issue_dicts, was_cached).

        Args:
            model: Override the instance model for this call (O1 tiering).
                   Empty string = use the instance default (self._model).

        Thread-safe: multiple WorkerPool threads can call this concurrently.
        The semaphore limits actual API concurrency; the cache is lock-guarded.
        """
        effective_model = model or self._model
        cache_key = self._make_cache_key(chunk, rule_ids, effective_model)

        cached = self._load_cache(cache_key)
        if cached is not None:
            logger.debug(
                "[LLMReviewer] Cache hit for %s:%s",
                chunk.file_path, chunk.symbol_name,
            )
            return cached, True

        with self._semaphore:
            raw = self._call_with_retry(system_prompt, user_prompt, chunk, effective_model)

        self._save_cache(cache_key, raw)
        return raw, False

    # ── Private: API call + retry ─────────────────────────────────────────────

    def _call_with_retry(
        self,
        system_prompt: str,
        user_prompt:   str,
        chunk:         CodeChunk,
        model:         str = "",
    ) -> List[Dict]:
        """Exponential-backoff retry for transient API errors."""
        last_exc: Optional[Exception] = None
        backoff = _INITIAL_BACKOFF

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return self._call_once(system_prompt, user_prompt, model)
            except Exception as exc:
                last_exc = exc
                status = getattr(exc, "status_code", None)

                # Non-retryable: validation errors or auth failures
                if status in (400, 401, 403):
                    logger.error(
                        "[LLMReviewer] Non-retryable error %s for %s:%s — %s",
                        status, chunk.file_path, chunk.symbol_name, exc,
                    )
                    raise

                # Retryable: rate limit or server error
                wait = self._retry_delay(exc, status, backoff)
                logger.warning(
                    "[LLMReviewer] Attempt %d/%d failed for %s:%s (status=%s): %s "
                    "— waiting %.1fs before retry",
                    attempt, _MAX_RETRIES,
                    chunk.file_path, chunk.symbol_name, status, exc, wait,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(wait)
                    backoff *= 2

        logger.error(
            "[LLMReviewer] All %d attempts exhausted for %s:%s",
            _MAX_RETRIES, chunk.file_path, chunk.symbol_name,
        )
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _retry_delay(exc: Exception, status: Optional[int], default_backoff: float) -> float:
        """
        Compute the wait time before the next retry attempt.

        429 (rate limit) is handled differently from other transient errors:
        a per-minute ceiling isn't cleared by a short exponential curve
        (2s/4s/8s) — that just re-spends the same exhausted budget on more
        failed attempts. Prefer the server's Retry-After header when present;
        otherwise fall back to a fixed wait long enough to matter against a
        60-second window. All other retryable statuses (5xx) use the normal
        doubling backoff, since those are transient outages, not a quota.
        """
        if status == 429:
            response = getattr(exc, "response", None)
            retry_after = None
            if response is not None:
                headers = getattr(response, "headers", {})
                retry_after = headers.get("retry-after") or headers.get("Retry-After")
            if retry_after:
                try:
                    return float(retry_after) + 0.5
                except ValueError:
                    pass
            return _RATE_LIMIT_BACKOFF
        return default_backoff

    def _call_once(self, system_prompt: str, user_prompt: str, model: str = "") -> List[Dict]:
        """Single messages.create() call.  Extracts tool_use result."""
        if self._rate_limiter is not None:
            self._rate_limiter.acquire()

        response = self._client.messages.create(
            model      = model or self._model,
            max_tokens = self._max_tokens,
            system     = system_prompt,
            messages   = [{"role": "user", "content": user_prompt}],
            tools      = [REPORT_ISSUES_TOOL],
            tool_choice = {"type": "any"},   # force tool call; never free text
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "report_issues":
                return block.input.get("issues", [])

        # Should not happen with tool_choice="any", but fail safely
        logger.warning("[LLMReviewer] No tool_use block in response; returning []")
        return []

    # ── Private: content-hash cache ───────────────────────────────────────────

    @staticmethod
    def _make_cache_key(chunk: CodeChunk, rule_ids: List[str], model: str = "") -> str:
        """
        Stable cache key = md5(chunk content) + md5(sorted rule_ids) + model tag.

        Content-based: if the source changes, the key changes.
        Rules-based:   if standards are updated, old entries are bypassed.
        Model-based:   fast vs full model results are stored separately (O1).
        """
        content_hash = hashlib.md5(chunk.content.encode()).hexdigest()
        rules_hash   = hashlib.md5(
            ",".join(sorted(rule_ids)).encode()
        ).hexdigest()[:8]
        model_tag    = model.split("/")[-1][:12] if model else ""
        return f"{content_hash}_{rules_hash}_{model_tag}"

    def _cache_path(self, key: str) -> Optional[Path]:
        """Return the pickle path for *key*, or None if caching is disabled."""
        if self._cache_dir is None:
            return None
        return self._cache_dir / f"{key}.pkl"

    def _load_cache(self, key: str) -> Optional[List[Dict]]:
        """Load cached issue dicts for *key*; returns None on any miss or read error."""
        path = self._cache_path(key)
        if path is None or not path.exists():
            return None
        try:
            with self._cache_lock:
                with path.open("rb") as f:
                    return pickle.load(f)
        except Exception:
            return None

    def _save_cache(self, key: str, data: List[Dict]) -> None:
        """Persist *data* under *key*; write failures are logged and swallowed."""
        path = self._cache_path(key)
        if path is None:
            return
        try:
            with self._cache_lock:
                with path.open("wb") as f:
                    pickle.dump(data, f)
        except Exception as exc:
            logger.debug("[LLMReviewer] Cache write failed: %s", exc)
