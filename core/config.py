"""
Global configuration constants for the code reviewer agent.

Override any value via environment variables where noted.

Model selection is driven by MODEL_TYPE:
  FREE       → z-ai/glm-5.2-free  (ZhipuAI, OpenAI-compat, free tier)
  OPENAI     → gpt-4o             (OpenAI)
  ANTHROPIC  → claude-opus-4-8    (default)

Override the exact model IDs with MODEL_NAME (review) / FAST_MODEL_NAME (fast tier).
"""
import os
from pathlib import Path

# Load .env from the project root (if present) before reading any os.getenv calls.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

# ── LLM Models ────────────────────────────────────────────────────────────────
_MODEL_TYPE      = os.getenv("MODEL_TYPE",       "ANTHROPIC").upper().strip()
_MODEL_NAME      = os.getenv("MODEL_NAME",       "").strip()
_FAST_MODEL_NAME = os.getenv("FAST_MODEL_NAME",  "").strip()

# Public re-export — callers that need to branch on provider (e.g. Stage 3's
# client-side rate limiter for the GLM free tier) should use this rather than
# re-parsing the MODEL_TYPE env var themselves.
MODEL_TYPE = _MODEL_TYPE

if _MODEL_TYPE == "FREE":
    REVIEW_MODEL  = _MODEL_NAME      or "z-ai/glm-5.2-free"
    SUMMARY_MODEL = _MODEL_NAME      or "z-ai/glm-5.2-free"
    FAST_MODEL    = _FAST_MODEL_NAME or "z-ai/glm-5.2-free"   # no cheaper option on free tier
elif _MODEL_TYPE == "OPENAI":
    REVIEW_MODEL  = _MODEL_NAME      or "gpt-4o"
    SUMMARY_MODEL = _MODEL_NAME      or "gpt-4o"
    FAST_MODEL    = _FAST_MODEL_NAME or "gpt-4o-mini"
else:  # ANTHROPIC (default)
    REVIEW_MODEL  = _MODEL_NAME      or "claude-opus-4-8"
    SUMMARY_MODEL = _MODEL_NAME      or "claude-haiku-4-5-20251001"
    FAST_MODEL    = _FAST_MODEL_NAME or "claude-haiku-4-5-20251001"

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
PIPELINE_VERSION   = "3.0"

# ── Review chunk cap (O2) ─────────────────────────────────────────────────────
# Maximum chunks sent for LLM review per run.  Priority order is preserved —
# when the cap is hit, lower-priority chunks are dropped (not pre-flagged ones).
# Set to 0 to disable the cap entirely.
MAX_REVIEW_CHUNKS  = int(os.getenv("MAX_REVIEW_CHUNKS", "300"))

# ── Workspace ─────────────────────────────────────────────────────────────────
WORKSPACE_ROOT = "./workspace"
