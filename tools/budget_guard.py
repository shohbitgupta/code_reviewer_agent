"""
Budget guard — a pre-call USD spend ceiling for LLM calls.

Two independent caps, both optional (0 disables):
  - MAX_REVIEW_COST_USD: hard ceiling for a single review run.
  - DAILY_BUDGET_USD:    rolling 24h ceiling across all runs, persisted to a
                         local ledger file so it means something across
                         separate CLI invocations (a single process's
                         in-memory total can't see prior runs today).

Deliberately scoped to a local JSON ledger under workspace/, not a database —
proportionate to a CLI batch tool, not an always-on service.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_LEDGER_PATH = Path("./workspace/budget_ledger.json")
_ONE_DAY_SECONDS = 24 * 60 * 60


class BudgetExceededError(RuntimeError):
    """Raised by check_before_call() when a configured spend ceiling has been crossed."""


class BudgetGuard:
    """Tracks cumulative LLM spend and blocks further calls once a ceiling is hit."""

    def __init__(
        self,
        max_review_cost_usd: float = 0.0,
        daily_budget_usd: float = 0.0,
        ledger_path: Optional[Path] = None,
    ) -> None:
        self._max_review_cost_usd = max_review_cost_usd
        self._daily_budget_usd = daily_budget_usd
        self._ledger_path = ledger_path or _DEFAULT_LEDGER_PATH
        self._lock = threading.Lock()
        self._run_spend_usd = 0.0

    def check_before_call(self) -> None:
        """Raise BudgetExceededError if either configured ceiling has already been crossed."""
        if self._max_review_cost_usd > 0 and self._run_spend_usd >= self._max_review_cost_usd:
            raise BudgetExceededError(
                f"Per-run budget exceeded: spent ${self._run_spend_usd:.4f} of "
                f"${self._max_review_cost_usd:.4f} (MAX_REVIEW_COST_USD)"
            )
        if self._daily_budget_usd > 0:
            daily_spend = self._read_daily_spend()
            if daily_spend >= self._daily_budget_usd:
                raise BudgetExceededError(
                    f"Daily budget exceeded: spent ${daily_spend:.4f} of "
                    f"${self._daily_budget_usd:.4f} (DAILY_BUDGET_USD) in the last 24h"
                )

    def record_call(self, model: str, usage: Optional[Dict[str, int]], cost_usd: float) -> None:
        """Record a completed call's cost against both the in-memory run total and the ledger."""
        with self._lock:
            self._run_spend_usd += cost_usd
            self._append_ledger(model=model, usage=usage or {}, cost_usd=cost_usd)

    @property
    def run_spend_usd(self) -> float:
        return self._run_spend_usd

    # ── Ledger persistence ────────────────────────────────────────────────────

    def _read_ledger(self) -> list:
        if not self._ledger_path.exists():
            return []
        try:
            with open(self._ledger_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[BudgetGuard] Failed to read ledger %s: %s", self._ledger_path, exc)
            return []

    def _read_daily_spend(self) -> float:
        cutoff = time.time() - _ONE_DAY_SECONDS
        entries = self._read_ledger()
        return sum(e.get("cost_usd", 0.0) for e in entries if e.get("ts", 0) >= cutoff)

    def _append_ledger(self, model: str, usage: Dict[str, int], cost_usd: float) -> None:
        entries = self._read_ledger()
        entries.append({"ts": time.time(), "model": model, "usage": usage, "cost_usd": cost_usd})
        # Prune anything older than a day — the ledger only needs to answer
        # "what was spent in the last 24h," not serve as a permanent log
        # (that's the event spine's job).
        cutoff = time.time() - _ONE_DAY_SECONDS
        entries = [e for e in entries if e.get("ts", 0) >= cutoff]
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._ledger_path, "w", encoding="utf-8") as f:
                json.dump(entries, f)
        except OSError as exc:
            logger.warning("[BudgetGuard] Failed to write ledger %s: %s", self._ledger_path, exc)
