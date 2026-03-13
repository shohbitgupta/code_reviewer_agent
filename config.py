"""
Global configuration constants for the code reviewer agent.

Override any value via environment variables where noted.
"""
import os

# ── LLM Models ────────────────────────────────────────────────────────────────
SUMMARY_MODEL = "claude-haiku-4-5-20251001"  # Step 1h — cheap/fast summarisation
REVIEW_MODEL  = "claude-sonnet-4-6"           # Stage 3 — full reasoning

# ── Embeddings ────────────────────────────────────────────────────────────────
# Backend: "voyage" (default, best for code) or "openai"
EMBEDDING_BACKEND    = os.getenv("EMBEDDING_BACKEND", "voyage")
EMBEDDING_MODEL      = os.getenv("EMBEDDING_MODEL", "voyage-code-3")  # or "text-embedding-3-small"
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))  # voyage=1024, openai=1536

# ── Qdrant ────────────────────────────────────────────────────────────────────
QDRANT_URL        = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY    = os.getenv("QDRANT_API_KEY")        # None for local POC
QDRANT_COLLECTION = "repo_chunks"

# ── Pipeline ──────────────────────────────────────────────────────────────────
PIPELINE_VERSION = "3.0"

# ── Workspace ─────────────────────────────────────────────────────────────────
WORKSPACE_ROOT = "./workspace"
