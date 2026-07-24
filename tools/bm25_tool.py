"""
Hybrid BM25 retrieval tool (Priority 3).

Builds a BM25 sparse index over CodeChunk symbol names and identifiers
extracted from content.  Combined with Qdrant dense-vector search via
Reciprocal Rank Fusion (RRF), this produces hybrid retrieval that:

  - Dense vectors   → semantic similarity ("authentication flow")
  - BM25 sparse     → exact identifier matching ("UserAuthService.authenticate")
  - RRF fusion      → merges both ranked lists without score normalisation

The index is persisted as a pickle file inside the workspace so Stage 3
can load it without re-running ingestion.

Requires: rank_bm25 (pip install rank_bm25)
Falls back gracefully if the package is not installed — search() returns [].

Usage:
    index = BM25Index()
    index.build(chunks)
    index.save(path / "bm25_index.pkl")

    # Stage 3 retrieval
    index = BM25Index.load(path / "bm25_index.pkl")
    chunk_ids = index.search("UserAuthService authenticate", top_k=10)

    # Hybrid fusion with Qdrant results
    fused = BM25Index.rrf_fuse(
        bm25_ids   = chunk_ids,
        qdrant_ids = qdrant_chunk_ids,
        top_k      = 5,
    )
"""

from __future__ import annotations

import logging
import pickle
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.models import ChunkType, CodeChunk

logger = logging.getLogger(__name__)

# Tokenisation: split on non-alphanumeric, lowercase, drop very short tokens
_TOKEN_RE = re.compile(r"[A-Za-z][a-z]+|[A-Z]{2,}(?=[A-Z][a-z]|\d|\b)|[A-Z][a-z]*|\d+")


def _tokenise(text: str) -> List[str]:
    """
    Split camelCase / snake_case / PascalCase identifiers into sub-tokens.

    Examples:
        "UserAuthService"  → ["user", "auth", "service"]
        "get_access_token" → ["get", "access", "token"]
        "HTTPSClient"      → ["https", "client"]
    """
    tokens = _TOKEN_RE.findall(text)
    return [t.lower() for t in tokens if len(t) > 1]


def _chunk_tokens(chunk: CodeChunk) -> List[str]:
    """
    Build the token corpus for a chunk: symbol name + top identifiers from content.

    Symbol name tokens are added 3× to boost exact-name matches.
    """
    tokens = _tokenise(chunk.symbol_name) * 3

    # Extract identifiers from the first 30 lines of content (avoid noise from comments)
    content_lines = chunk.content.splitlines()[:30]
    for line in content_lines:
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        tokens.extend(_tokenise(stripped))

    # Add layer as a searchable token
    if chunk.layer != "unknown":
        tokens.append(chunk.layer)

    return tokens


class BM25Index:
    """
    BM25 sparse index over CodeChunk tokens.

    The index maps chunk_id → document (list of tokens) and provides
    ranked retrieval by BM25 score.
    """

    def __init__(self) -> None:
        self._chunk_ids: List[str] = []
        self._bm25 = None   # rank_bm25.BM25Okapi instance

    # ── Build ─────────────────────────────────────────────────────────────────

    def build(self, chunks: List[CodeChunk]) -> None:
        """
        Build the BM25 index from a list of chunks.

        Skips MODULE chunks (file-level overviews) to avoid diluting scores
        with boilerplate content.

        Args:
            chunks: All CodeChunk objects from Step 1g.
        """
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            logger.warning(
                "[BM25Index] rank_bm25 not installed — BM25 index disabled. "
                "Run: pip install rank_bm25"
            )
            return

        skip = {ChunkType.MODULE}
        corpus: List[List[str]] = []
        ids: List[str] = []

        for chunk in chunks:
            if chunk.chunk_type in skip:
                continue
            tokens = _chunk_tokens(chunk)
            if not tokens:
                continue
            corpus.append(tokens)
            ids.append(chunk.chunk_id)

        if not corpus:
            logger.warning("[BM25Index] No indexable chunks found.")
            return

        self._chunk_ids = ids
        self._bm25 = BM25Okapi(corpus)
        logger.info("[BM25Index] Indexed %d chunks", len(ids))

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 20) -> List[str]:
        """
        Return up to *top_k* chunk_ids ranked by BM25 score.

        Returns [] if the index was not built (rank_bm25 unavailable or no chunks).
        """
        if self._bm25 is None or not self._chunk_ids:
            return []

        query_tokens = _tokenise(query)
        if not query_tokens:
            return []

        scores = self._bm25.get_scores(query_tokens)
        ranked = sorted(
            range(len(scores)), key=lambda i: -scores[i]
        )[:top_k]
        return [self._chunk_ids[i] for i in ranked if scores[i] > 0]

    # ── RRF fusion ────────────────────────────────────────────────────────────

    @staticmethod
    def rrf_fuse(
        bm25_ids:   List[str],
        qdrant_ids: List[str],
        top_k:      int = 10,
        k:          int = 60,
    ) -> List[str]:
        """
        Reciprocal Rank Fusion of two ranked lists.

        RRF score for a document d = Σ 1 / (k + rank(d))
        where rank is 1-indexed position in each list.

        Args:
            bm25_ids:   Ranked chunk_ids from BM25 search.
            qdrant_ids: Ranked chunk_ids from Qdrant dense search.
            top_k:      Number of results to return.
            k:          RRF smoothing constant (default 60, as per original paper).

        Returns:
            Fused, re-ranked list of chunk_ids.
        """
        scores: Dict[str, float] = {}

        for rank, cid in enumerate(bm25_ids, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)

        for rank, cid in enumerate(qdrant_ids, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)

        ranked = sorted(scores.items(), key=lambda x: -x[1])
        return [cid for cid, _ in ranked[:top_k]]

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: Path) -> None:
        """Persist the index to disk as a pickle file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            pickle.dump({"chunk_ids": self._chunk_ids, "bm25": self._bm25}, f)
        logger.info("[BM25Index] Saved to %s", path)

    @classmethod
    def load(cls, path: Path) -> "BM25Index":
        """
        Load a previously saved BM25 index.

        Returns an empty (no-op) index if the file does not exist.
        """
        path = Path(path)
        idx = cls()
        if not path.exists():
            logger.warning("[BM25Index] Index file not found at %s", path)
            return idx
        with path.open("rb") as f:
            data = pickle.load(f)
        idx._chunk_ids = data.get("chunk_ids", [])
        idx._bm25 = data.get("bm25")
        logger.info("[BM25Index] Loaded %d chunks from %s", len(idx._chunk_ids), path)
        return idx

    def __len__(self) -> int:
        return len(self._chunk_ids)
