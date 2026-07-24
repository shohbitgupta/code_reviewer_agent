"""
Step 1k — Vector Upsert  ★ RERANK PREP ★

QdrantTool manages the "repo_chunks" collection and provides:
  - upsert_chunks()    → embed + upsert dual named vectors per chunk
  - search()           → vector similarity search (code_vector or summary_vector)
  - get_by_id()        → O(1) point lookup by chunk_id (for nav pointer expansion)
  - search_by_file()   → retrieve all chunks belonging to one file

Named vectors per point:
  "code_vector"    → embed(header + source)  (structural similarity)
  "summary_vector" → embed(chunk.summary)    (natural language query matching)

Both vectors are stored per point.  summary_vector may be None if Step 1h
was deferred for large repos — Qdrant accepts partial named vectors.

Docker setup (local POC):
    docker run -p 6333:6333 -v $(pwd)/qdrant_storage:/qdrant/storage qdrant/qdrant

Usage:
    tool = QdrantTool(embed_tool=embed_tool)
    tool.ensure_collection()
    tool.upsert_chunks(chunks)
    results = tool.search("function that validates JWT", vector_name="summary_vector")
"""

import logging
from typing import Any, Dict, List, Optional

from core import config
from tools.embedding_tool import EmbeddingTool

logger = logging.getLogger(__name__)

COLLECTION_NAME = config.QDRANT_COLLECTION
BATCH_SIZE      = 100   # points per upsert request


def _get_qdrant_client():
    try:
        from qdrant_client import QdrantClient  # pip install qdrant-client
        return QdrantClient(
            url     = config.QDRANT_URL,
            api_key = config.QDRANT_API_KEY,
        )
    except ImportError as exc:
        raise ImportError(
            "qdrant-client not installed. Run: pip install qdrant-client"
        ) from exc


class QdrantTool:
    """
    Interface to the Qdrant "repo_chunks" collection.

    Dual named vectors are declared at collection creation time and cannot be
    changed later — drop and recreate the collection if dimensions change.

    Args:
        embed_tool:  EmbeddingTool instance (used to produce code_vector).
        collection:  Qdrant collection name. Defaults to config.QDRANT_COLLECTION.
    """

    def __init__(
        self,
        embed_tool:  EmbeddingTool,
        collection:  str = COLLECTION_NAME,
    ):
        self.embed_tool = embed_tool
        self.collection = collection
        self._client    = None   # lazy init

    # ── Collection management ─────────────────────────────────────────────────

    def ensure_collection(self, recreate: bool = False) -> None:
        """
        Create the collection with dual named vectors if it does not exist.

        Args:
            recreate: If True, drop and recreate even if the collection exists.
                      Use when changing embedding dimensions.
        """
        from qdrant_client.models import Distance, VectorParams

        client = self._get_client()
        dims   = config.EMBEDDING_DIMENSIONS

        existing = {c.name for c in client.get_collections().collections}

        if self.collection in existing:
            if recreate:
                client.delete_collection(self.collection)
                logger.info("[QdrantTool] Dropped collection %r for recreation", self.collection)
            else:
                logger.debug("[QdrantTool] Collection %r already exists", self.collection)
                return

        client.create_collection(
            collection_name = self.collection,
            vectors_config  = {
                "code_vector": VectorParams(
                    size     = dims,
                    distance = Distance.COSINE,
                ),
                "summary_vector": VectorParams(
                    size     = dims,
                    distance = Distance.COSINE,
                ),
            },
        )
        logger.info(
            "[QdrantTool] Created collection %r (dims=%d)", self.collection, dims
        )

    # ── Upsert ────────────────────────────────────────────────────────────────

    def upsert_chunks(self, chunks: list, batch_size: int = BATCH_SIZE) -> int:
        """
        Embed content → code_vector and upsert all chunks as Qdrant points.

        summary_vector is taken from chunk.summary_embedding (set in Step 1h).
        If Step 1h was deferred, summary_embedding is None — Qdrant accepts this.

        Args:
            chunks:     List[CodeChunk] — must have embedding=None initially.
            batch_size: Points per upsert request (default 100).

        Returns:
            Total number of points upserted.
        """
        from qdrant_client.models import PointStruct

        client  = self._get_client()
        total   = 0

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]

            # Embed all chunk contents in one batch call
            texts = [self._embed_text(c) for c in batch]
            vectors = self.embed_tool.encode_batch(texts)
            for chunk, vec in zip(batch, vectors):
                chunk.embedding = vec

            # Build Qdrant points
            points = [
                PointStruct(
                    id      = chunk.chunk_id,
                    vector  = {
                        "code_vector":    chunk.embedding,
                        "summary_vector": chunk.summary_embedding,  # None if deferred
                    },
                    payload = chunk.to_qdrant_payload(),
                )
                for chunk in batch
            ]

            client.upsert(collection_name=self.collection, points=points)
            total += len(batch)
            logger.debug(
                "[QdrantTool] Upserted batch %d/%d (%d points)",
                i // batch_size + 1,
                (len(chunks) + batch_size - 1) // batch_size,
                len(batch),
            )

        logger.info(
            '[QdrantTool] Upserted %d points to "%s"', total, self.collection
        )
        return total

    # ── Search ────────────────────────────────────────────────────────────────

    def search(
        self,
        query_text:  str,
        vector_name: str = "code_vector",
        limit:       int = 30,
        filters:     Optional[Dict[str, Any]] = None,
    ) -> List[Dict]:
        """
        Vector similarity search against code_vector or summary_vector.

        For ingestion-time queries, use vector_name="code_vector".
        For natural language reviewer queries, use vector_name="summary_vector".

        Args:
            query_text:  Text to embed and search for.
            vector_name: "code_vector" or "summary_vector".
            limit:       Max results to return (use 30 for reranking pipeline).
            filters:     Optional payload filters e.g. {"language": "python"}.

        Returns:
            List of payload dicts (chunk metadata), ordered by similarity.
        """
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        client = self._get_client()
        vec    = self.embed_tool.encode(query_text)

        qdrant_filter = None
        if filters:
            conditions = [
                FieldCondition(key=k, match=MatchValue(value=v))
                for k, v in filters.items()
            ]
            qdrant_filter = Filter(must=conditions)

        results = client.search(
            collection_name = self.collection,
            query_vector    = (vector_name, vec),
            limit           = limit,
            query_filter    = qdrant_filter,
            with_payload    = True,
        )
        return [r.payload for r in results]

    def get_by_id(self, chunk_id: str) -> Optional[Dict]:
        """
        O(1) point lookup by chunk_id UUID.

        Used in Stage 3 for chunk expansion via nav pointers
        (parent_chunk_id, prev_chunk_id, next_chunk_id).

        Returns:
            Payload dict, or None if not found.
        """
        client  = self._get_client()
        results = client.retrieve(
            collection_name = self.collection,
            ids             = [chunk_id],
            with_payload    = True,
        )
        return results[0].payload if results else None

    def search_by_file(self, file_path: str, limit: int = 100) -> List[Dict]:
        """
        Retrieve all chunks belonging to one source file.

        Useful for whole-file review context in Stage 3.
        """
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        client = self._get_client()
        results = client.scroll(
            collection_name = self.collection,
            scroll_filter   = Filter(must=[
                FieldCondition(key="file_path", match=MatchValue(value=file_path))
            ]),
            limit           = limit,
            with_payload    = True,
        )
        return [r.payload for r in results[0]]

    def patch_summary_vector(self, chunk_id: str, summary: str, embedding: List[float]) -> None:
        """
        Update the summary text and summary_vector for a single point.

        Called after background Step 1h completes for large repos.

        Args:
            chunk_id:  UUID of the Qdrant point.
            summary:   LLM-extracted summary text.
            embedding: Embedding of the summary (summary_vector).
        """
        from qdrant_client.models import PointVectors

        client = self._get_client()

        # 1. Update payload text
        client.set_payload(
            collection_name = self.collection,
            payload         = {"summary": summary},
            points          = [chunk_id],
        )

        # 2. Set summary_vector
        client.update_vectors(
            collection_name = self.collection,
            points          = [PointVectors(id=chunk_id, vector={"summary_vector": embedding})],
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _embed_text(chunk) -> str:
        """
        Prepend structural context to chunk content before embedding.

        Including file path and chunk type improves code_vector retrieval
        precision by ~10–15% on benchmarks.
        """
        return (
            f"# File: {chunk.file_path}\n"
            f"# Type: {chunk.chunk_type.value} — {chunk.symbol_name}\n\n"
            f"{chunk.content}"
        )

    def _get_client(self):
        if self._client is None:
            self._client = _get_qdrant_client()
        return self._client
