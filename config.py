# Backward-compatibility shim — import from core.config instead.
"""
Backward-compatibility shim.

Re-exports all configuration constants from core.config so legacy
`import config` statements keep working. Prefer importing from
core.config directly in new code.
"""
from core.config import *  # noqa: F401, F403
from core import config as _c
SUMMARY_MODEL        = _c.SUMMARY_MODEL
REVIEW_MODEL         = _c.REVIEW_MODEL
EMBEDDING_BACKEND    = _c.EMBEDDING_BACKEND
EMBEDDING_MODEL      = _c.EMBEDDING_MODEL
EMBEDDING_DIMENSIONS = _c.EMBEDDING_DIMENSIONS
QDRANT_URL           = _c.QDRANT_URL
QDRANT_API_KEY       = _c.QDRANT_API_KEY
QDRANT_COLLECTION    = _c.QDRANT_COLLECTION
PIPELINE_VERSION     = _c.PIPELINE_VERSION
WORKSPACE_ROOT       = _c.WORKSPACE_ROOT
