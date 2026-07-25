"""
Step 1h — Metadata Extraction  ★ RERANK PREP ★

SummaryGenerator calls Claude Haiku to extract a concise 1–2 sentence
natural-language summary for every FUNCTION, METHOD, CLASS_HEAD, and CLASS
chunk.  The summary is then embedded to produce a summary_vector.

Without these summaries, reviewer queries like "missing error handling in auth"
cannot match code that only contains token-level names like decode / exp /
raise AuthError.

Cost reduction strategy:
  - Batch 20 chunks per LLM call  (20× cost reduction vs 1 chunk/call)
  - asyncio.Semaphore(5)          (max 5 concurrent LLM calls)
  - Skip MODULE / IMPORT / CONSTANT chunks entirely

Usage (sync entry point for pipeline):
    generator = SummaryGenerator(llm_client=anthropic_client, embed_tool=embed_tool)
    chunks    = generator.run(chunks)   # mutates chunks in-place
"""

import asyncio
import json
import logging
import time
from typing import List, Optional

from core.models import ChunkType, CodeChunk
from tools.embedding_tool import EmbeddingTool

logger = logging.getLogger(__name__)

SUMMARY_BATCH_SIZE   = 20    # chunks per LLM call
SEMAPHORE_LIMIT      = 5     # max concurrent LLM calls
MAX_CONTENT_CHARS    = 800   # truncate chunk content in prompt
MIN_BLOCK_LINES      = 30    # BLOCK chunks shorter than this are skipped

# Chunk types that receive summaries
SUMMARISE_TYPES = {
    ChunkType.FUNCTION,
    ChunkType.METHOD,
    ChunkType.CLASS_HEAD,
    ChunkType.CLASS,
    ChunkType.INTERFACE,
}


class SummaryGenerator:
    """
    Generates LLM summaries and summary embeddings for eligible CodeChunks.

    Args:
        embed_tool:     EmbeddingTool instance for embedding summaries.
        llm_client:     anthropic.AsyncAnthropic (or sync Anthropic) client.
                        If None, ANTHROPIC_API_KEY env var is used to create one.
        model:          Claude model ID for summarisation.
    """

    def __init__(
        self,
        embed_tool:  EmbeddingTool,
        llm_client  = None,
        model:       str = "claude-haiku-4-5-20251001",
    ):
        self.embed_tool = embed_tool
        # If a unified client is provided, inherit its resolved model name
        # so we don't send a hardcoded Anthropic model ID to non-Anthropic endpoints.
        self.model = (
            llm_client.model_name
            if llm_client and hasattr(llm_client, "model_name")
            else model
        )
        self._client    = llm_client  # lazy-initialised if None

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, chunks: List[CodeChunk]) -> List[CodeChunk]:
        """
        Generate summaries for eligible chunks.  Mutates each chunk in-place.

        Eligible chunks receive:
            chunk.summary           = "1-2 sentence description"
            chunk.summary_embedding = List[float]

        Ineligible chunks receive:
            chunk.summary           = ""   (empty string, not None)
            chunk.summary_embedding = None

        Returns:
            The same list (mutated in-place) for chaining convenience.
        """
        # Mark ineligible chunks now so they're never None later
        for c in chunks:
            if not self._should_summarise(c):
                c.summary           = ""
                c.summary_embedding = None

        return asyncio.run(self._run_async(chunks))

    # ── Async core ────────────────────────────────────────────────────────────

    async def _run_async(self, chunks: List[CodeChunk]) -> List[CodeChunk]:
        eligible  = [c for c in chunks if self._should_summarise(c)]
        if not eligible:
            return chunks

        batches   = [eligible[i:i + SUMMARY_BATCH_SIZE]
                     for i in range(0, len(eligible), SUMMARY_BATCH_SIZE)]
        semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
        start     = time.monotonic()

        async def process_batch(batch: List[CodeChunk]) -> None:
            async with semaphore:
                try:
                    summaries = await self._call_llm(batch)
                    # Embed all summaries in one batch call (sync — wrapped)
                    embeddings = await asyncio.get_event_loop().run_in_executor(
                        None, self.embed_tool.encode_batch, summaries
                    )
                    for chunk, summary, emb in zip(batch, summaries, embeddings):
                        chunk.summary           = summary
                        chunk.summary_embedding = emb
                except Exception as exc:
                    logger.warning("[SummaryGenerator] Batch failed: %s", exc)
                    for chunk in batch:
                        chunk.summary           = ""
                        chunk.summary_embedding = None

        await asyncio.gather(*[process_batch(b) for b in batches])

        generated = sum(1 for c in eligible if c.summary)
        elapsed   = time.monotonic() - start
        logger.info(
            "[SummaryGenerator] Generated %d summaries in %.1fs",
            generated, elapsed,
        )
        return chunks

    # ── LLM interaction ───────────────────────────────────────────────────────

    async def _call_llm(self, batch: List[CodeChunk]) -> List[str]:
        """Send one batched LLM request; return list of summary strings."""
        client = self._get_async_client()
        prompt = self._build_batch_prompt(batch)

        message = await client.messages.create(
            model      = self.model,
            max_tokens = 1024,
            system     = (
                "You are a code documentation assistant. "
                "Your summaries must be exactly 1-2 sentences, plain English, "
                "describing WHAT the code does and WHY it exists. "
                "Mention key patterns, risks, or dependencies if notable. "
                "Never start the first sentence with the function/class name as subject."
            ),
            messages   = [{"role": "user", "content": prompt}],
        )

        raw_text = message.content[0].text
        return self._parse_response(raw_text, len(batch))

    def _build_batch_prompt(self, batch: List[CodeChunk]) -> str:
        items = "\n\n".join(
            f"--- Chunk {i+1}: {c.symbol_name} ({c.chunk_type.value}) ---\n"
            f"```{c.language}\n{c.content[:MAX_CONTENT_CHARS]}\n```"
            for i, c in enumerate(batch)
        )
        return (
            f"Summarise each of the {len(batch)} code chunks below.\n"
            f"Return a JSON array of exactly {len(batch)} strings, one per chunk, in order.\n"
            f"Each summary: 1-2 sentences, plain English, no preamble.\n\n"
            f"{items}\n\n"
            "Return JSON array only:"
        )

    @staticmethod
    def _parse_response(response: str, batch_size: int) -> List[str]:
        """Parse the LLM's JSON array response."""
        clean = response.strip()
        # Strip markdown code fences if present
        if clean.startswith("```"):
            lines = clean.split("\n")
            clean = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        summaries = json.loads(clean)
        if not isinstance(summaries, list) or len(summaries) != batch_size:
            raise ValueError(
                f"Expected JSON array of {batch_size} items, got: {clean[:200]}"
            )
        return [str(s).strip() for s in summaries]

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _should_summarise(chunk: CodeChunk) -> bool:
        if chunk.chunk_type in SUMMARISE_TYPES:
            return True
        if chunk.chunk_type == ChunkType.BLOCK:
            lines = chunk.end_line - chunk.start_line + 1
            return lines > MIN_BLOCK_LINES
        return False

    def _get_async_client(self):
        if self._client is not None:
            return self._client
        from tools.llm_client import LLMClientFactory
        self._client = LLMClientFactory.create_async()
        return self._client
