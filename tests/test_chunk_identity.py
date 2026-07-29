"""
Chunk identity — deterministic chunk_id regression gate.

CodeChunk.new() derives chunk_id from (repo_name, file_path, chunk_type,
symbol_name, start_line) via uuid.uuid5(), NOT a random uuid.uuid4(). This is
what makes Qdrant's incremental upsert, the parse cache, and the summary
cache all work across separate pipeline invocations of the same repo — see
CLAUDE.md invariant #9. Nothing else in this repo exercises this property;
it's the entire point of that fix, so it gets its own test file.

Deterministic and free to run: no LLM, no network, no Qdrant.

Run with:
    pytest tests/test_chunk_identity.py -s -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import ChunkType, CodeChunk


def _new(**overrides):
    defaults = dict(
        repo_name="owner__repo",
        file_path="app/services/cart.py",
        language="python",
        chunk_type=ChunkType.FUNCTION,
        symbol_name="checkout",
        start_line=42,
        end_line=60,
        content="def checkout(): pass",
    )
    defaults.update(overrides)
    return CodeChunk.new(**defaults)


def test_chunk_id_is_uuid_shaped():
    """Qdrant point IDs must be a valid UUID or unsigned integer."""
    import uuid
    chunk = _new()
    uuid.UUID(chunk.chunk_id)  # raises ValueError if not UUID-shaped


def test_chunk_id_stable_across_reconstruction():
    """Same identity fields, rebuilt from scratch, must produce the same chunk_id."""
    a = _new()
    b = _new()
    assert a.chunk_id == b.chunk_id


def test_chunk_id_unaffected_by_content_change():
    """Content is deliberately excluded from chunk_id — an edit is still 'the same chunk'."""
    a = _new(content="def checkout(): pass")
    b = _new(content="def checkout(): return True  # totally different body")
    assert a.chunk_id == b.chunk_id
    assert a.content_hash != b.content_hash, "content_hash must still detect the change"


@pytest.mark.parametrize("field, override", [
    ("repo_name",   {"repo_name": "other__repo"}),
    ("file_path",   {"file_path": "app/services/other.py"}),
    ("chunk_type",  {"chunk_type": ChunkType.METHOD}),
    ("symbol_name", {"symbol_name": "other_symbol"}),
    ("start_line",  {"start_line": 43}),
])
def test_chunk_id_changes_with_each_identity_field(field, override):
    """Changing any single identity field must change chunk_id (no accidental collisions)."""
    a = _new()
    b = _new(**override)
    assert a.chunk_id != b.chunk_id, f"changing {field} should change chunk_id"


def test_different_repos_do_not_collide():
    """
    repo_name must be part of the hash: one Qdrant collection holds chunks from
    every repo ever ingested (core/config.py QDRANT_COLLECTION), and similarity
    search never filters by repo (stage3_review/context_builder.py) — without
    repo_name, two repos with a same-named symbol at the same line would
    silently overwrite each other's Qdrant point.
    """
    a = _new(repo_name="repo_one")
    b = _new(repo_name="repo_two")
    assert a.chunk_id != b.chunk_id
