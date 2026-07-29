"""
Event spine — a durable, queryable record of what happened during a review run.

Scoped deliberately small: a local append-only JSONL file per run under
`workspace/events/`, not a Postgres/hypertable service. This repo runs as a
CLI batch tool, not an always-on service, so a linear-scan JSONL file is
proportionate to the actual volume (hundreds of events per run, not millions).

Stateless per the tools/ convention — all state lives in the JSONL file on
disk, not in memory across instances, so multiple EventSpine objects pointed
at the same run_id/path are safe to use concurrently (each append is a single
atomic write).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_EVENTS_DIR = Path("./workspace/events")


class EventSpine:
    """Appends structured events to `workspace/events/<run_id>.jsonl`."""

    def __init__(self, run_id: str, path: Optional[Path] = None) -> None:
        self.run_id = run_id
        self._path = path or (_DEFAULT_EVENTS_DIR / f"{run_id}.jsonl")
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event_type: str, **fields: Any) -> None:
        """Append one event. Never raises — a logging failure must not break a review."""
        event = {"ts": time.time(), "run_id": self.run_id, "event_type": event_type, **fields}
        try:
            line = json.dumps(event, default=str)
        except (TypeError, ValueError):
            logger.warning("[EventSpine] Failed to serialize event_type=%r — dropping", event_type)
            return
        try:
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError as exc:
            logger.warning("[EventSpine] Failed to write event to %s: %s", self._path, exc)

    def query(self, event_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Read back all events (optionally filtered by event_type) via a linear scan."""
        if not self._path.exists():
            return []
        events: List[Dict[str, Any]] = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type is None or event.get("event_type") == event_type:
                    events.append(event)
        return events
