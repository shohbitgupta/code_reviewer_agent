"""
Parallel worker pool for the ingestion pipeline.

WorkerPool wraps concurrent.futures.ThreadPoolExecutor and provides:
  - Ordered results  (submit in order, collect in order)
  - Auto worker count (tuned per workload class)
  - Serial fallback   (small batches or max_workers=1)
  - Exception propagation (Future.result() re-raises in the caller thread)

Why threads, not processes?
  - File reading:      releases the GIL  (kernel I/O)
  - tree-sitter parse: releases the GIL  (C extension)
  - JSON cache I/O:    releases the GIL  (kernel I/O)
  - Pure-Python steps: GIL-bound but benefit from I/O overlap

ProcessPoolExecutor would give true CPU parallelism for pure-Python steps but
the pickling overhead for ParsedFile objects (with raw_lines and symbols)
outweighs the gain for typical repos (< 500 files).  If a single repo has
thousands of large files, consider setting PIPELINE_WORKERS_MODE=process in
the environment.

Usage::

    pool = WorkerPool()                    # auto-size
    pool = WorkerPool(max_workers=8)       # explicit
    pool = WorkerPool(max_workers=1)       # force serial

    results = pool.map(parse_fn, file_metas)   # ordered, parallel
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from typing import Callable, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")

# Items below this threshold are processed serially — thread overhead isn't worth it.
_SERIAL_THRESHOLD = 4


def _cpu() -> int:
    return os.cpu_count() or 4


def _default_workers(n_items: int, io_bound: bool = True) -> int:
    """
    Choose a sensible default worker count.

      io_bound=True  (file reading + tree-sitter):   cpu * 2, capped at 16
      io_bound=False (pure Python, GIL-bound):        cpu,     capped at 8

    Never exceeds the number of work items.
    """
    if io_bound:
        cap = min(16, _cpu() * 2)
    else:
        cap = min(8, _cpu())
    return min(cap, n_items)


class WorkerPool:
    """
    Thin, order-preserving parallel map backed by ThreadPoolExecutor.

    Parameters
    ----------
    max_workers:
        Maximum number of worker threads.  None = auto-size (recommended).
        Set to 1 to force fully serial execution (useful for debugging).
    io_bound:
        Hint used by auto-sizing.  True (default) for I/O + tree-sitter
        workloads; False for pure-Python workloads.
    """

    def __init__(
        self,
        max_workers: Optional[int] = None,
        *,
        io_bound: bool = True,
    ) -> None:
        self._max_workers = max_workers
        self._io_bound    = io_bound

    # ── Public API ────────────────────────────────────────────────────────────

    def map(
        self,
        fn: Callable[[T], R],
        items: List[T],
    ) -> List[R]:
        """
        Apply *fn* to every item in *items* and return results in the same order.

        Exceptions raised inside *fn* propagate to the caller.

        Falls back to serial execution when:
          - len(items) <= _SERIAL_THRESHOLD
          - max_workers == 1
          - items is empty
        """
        n = len(items)
        if n == 0:
            return []

        workers = self._max_workers
        if workers is None:
            workers = _default_workers(n, self._io_bound)

        # Serial path
        if workers <= 1 or n <= _SERIAL_THRESHOLD:
            return [fn(item) for item in items]

        workers = min(workers, n)   # never more threads than items

        logger.debug("[WorkerPool] %d items → %d threads", n, workers)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            # Preserve order: submit all, then collect in submission order.
            futures: List[Future] = [executor.submit(fn, item) for item in items]
            return [f.result() for f in futures]
