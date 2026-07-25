"""
Global configuration constants for the code reviewer agent.

Override any value via environment variables where noted.

Model selection is driven by MODEL_TYPE:
  FREE       → z-ai/glm-5.2-free  (ZhipuAI, OpenAI-compat, free tier)
  OPENAI     → gpt-4o             (OpenAI)
  ANTHROPIC  → claude-opus-4-8    (default)

Override the exact model ID with MODEL_NAME.
"""
import os

# ── LLM Models ────────────────────────────────────────────────────────────────
_MODEL_TYPE = os.getenv("MODEL_TYPE", "ANTHROPIC").upper().strip()
_MODEL_NAME = os.getenv("MODEL_NAME", "").strip()

if _MODEL_TYPE == "FREE":
    REVIEW_MODEL  = _MODEL_NAME or "z-ai/glm-5.2-free"
    SUMMARY_MODEL = _MODEL_NAME or "z-ai/glm-5.2-free"
elif _MODEL_TYPE == "OPENAI":
    REVIEW_MODEL  = _MODEL_NAME or "gpt-4o"
    SUMMARY_MODEL = _MODEL_NAME or "gpt-4o"
else:  # ANTHROPIC (default)
    REVIEW_MODEL  = _MODEL_NAME or "claude-opus-4-8"
    SUMMARY_MODEL = _MODEL_NAME or "claude-haiku-4-5-20251001"

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
