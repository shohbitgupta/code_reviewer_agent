"""
Embedding Tool — configurable backend (Voyage AI or OpenAI).

Supports two backends selected via config.EMBEDDING_BACKEND:
  "voyage"  →  voyageai client  (default; voyage-code-3 is best-in-class for code)
  "openai"  →  openai client    (text-embedding-3-small / text-embedding-3-large)

The public interface is identical regardless of backend so the rest of the
pipeline never needs to know which is active.

Switching backends:
    # .env or shell:
    EMBEDDING_BACKEND=openai
    EMBEDDING_MODEL=text-embedding-3-small
    EMBEDDING_DIMENSIONS=1536

Usage:
    tool   = EmbeddingTool()
    vector = tool.encode("def authenticate(token): ...")
    batch  = tool.encode_batch(["chunk 1 ...", "chunk 2 ..."])
"""

import logging
import os
import time
from typing import List

from core import config

logger = logging.getLogger(__name__)

# ── Lazy imports — only the active backend is imported ───────────────────────

def _get_voyage_client():
    """
    Construct a Voyage AI client from VOYAGE_API_KEY.

    Raises:
        RuntimeError: If VOYAGE_API_KEY is not set.
        ImportError: If the voyageai package is not installed.
    """
    try:
        import voyageai  # pip install voyageai
        api_key = os.getenv("VOYAGE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "VOYAGE_API_KEY env var is not set. "
                "Get a free key at https://dash.voyageai.com"
            )
        return voyageai.Client(api_key=api_key)
    except ImportError as exc:
        raise ImportError(
            "voyageai package not installed. Run: pip install voyageai"
        ) from exc


def _get_openai_client():
    """
    Construct an OpenAI client from OPENAI_API_KEY.

    Raises:
        RuntimeError: If OPENAI_API_KEY is not set.
        ImportError: If the openai package is not installed.
    """
    try:
        from openai import OpenAI  # pip install openai
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY env var is not set.")
        return OpenAI(api_key=api_key)
    except ImportError as exc:
        raise ImportError(
            "openai package not installed. Run: pip install openai"
        ) from exc


class EmbeddingTool:
    """
    Encodes text into dense float vectors using the configured backend.

    Args:
        backend:    "voyage" or "openai". Defaults to config.EMBEDDING_BACKEND.
        model:      Model name. Defaults to config.EMBEDDING_MODEL.
        dimensions: Expected output dimensions. Defaults to config.EMBEDDING_DIMENSIONS.

    The client is created lazily on first use to avoid import-time errors when
    the embedding backend is not yet configured.
    """

    def __init__(
        self,
        backend:    str = config.EMBEDDING_BACKEND,
        model:      str = config.EMBEDDING_MODEL,
        dimensions: int = config.EMBEDDING_DIMENSIONS,
    ):
        self.backend    = backend.lower()
        self.model      = model
        self.dimensions = dimensions
        self._client    = None   # lazy init

    # ── Public API ────────────────────────────────────────────────────────────

    def encode(self, text: str) -> List[float]:
        """
        Embed a single string.

        Args:
            text: The text to embed (source code, summary, or query).

        Returns:
            List[float] of length self.dimensions.
        """
        return self.encode_batch([text])[0]

    def encode_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Embed a batch of strings in a single API call.

        Args:
            texts: List of strings to embed (max 128 for Voyage, 2048 for OpenAI).

        Returns:
            List of float vectors, one per input string.
        """
        if not texts:
            return []
        client = self._get_client()
        backoff = 5.0
        for attempt in range(4):
            try:
                if self.backend == "voyage":
                    return self._voyage_encode(client, texts)
                if self.backend == "openai":
                    return self._openai_encode(client, texts)
                raise ValueError(f"Unknown embedding backend: {self.backend!r}")
            except Exception as exc:
                msg = str(exc).lower()
                is_rate_limit = "rate" in msg or "429" in msg or "quota" in msg
                if not is_rate_limit or attempt == 3:
                    raise
                logger.warning(
                    "[EmbeddingTool] Rate limit hit, retrying in %.0fs (attempt %d/4)",
                    backoff, attempt + 1,
                )
                time.sleep(backoff)
                backoff *= 2
        raise RuntimeError("unreachable")

    # ── Backend implementations ───────────────────────────────────────────────

    def _voyage_encode(self, client, texts: List[str]) -> List[List[float]]:
        """
        Voyage AI embedding.

        voyage-code-3 input type:
          "query"    → for retrieval queries (Stage 3)
          "document" → for chunks being indexed (Stage 1)
        Always use input_type="document" here (ingestion time).
        """
        result = client.embed(
            texts,
            model      = self.model,
            input_type = "document",
        )
        return result.embeddings

    def _openai_encode(self, client, texts: List[str]) -> List[List[float]]:
        """OpenAI embedding via the v1 client."""
        response = client.embeddings.create(
            input      = texts,
            model      = self.model,
            dimensions = self.dimensions,   # supports truncation on text-embedding-3-*
        )
        # Sort by index to guarantee order matches input
        return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]

    # ── Lazy client init ──────────────────────────────────────────────────────

    def _get_client(self):
        """Lazily construct and cache the backend client (Voyage or OpenAI) on first use."""
        if self._client is None:
            if self.backend == "voyage":
                self._client = _get_voyage_client()
                logger.info(
                    "[EmbeddingTool] Voyage AI backend ready (model=%s, dims=%d)",
                    self.model, self.dimensions,
                )
            elif self.backend == "openai":
                self._client = _get_openai_client()
                logger.info(
                    "[EmbeddingTool] OpenAI backend ready (model=%s, dims=%d)",
                    self.model, self.dimensions,
                )
            else:
                raise ValueError(f"Unknown embedding backend: {self.backend!r}")
        return self._client
