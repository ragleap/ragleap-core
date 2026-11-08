"""ragleap-memory: durable, tenant-scoped long-term memory for LLM applications.

Needs SQLite with FTS5. Not independently audited. See docs/threat-model.md."""
from ragleap_memory.model import (KINDS, TRUSTS, EmbedderError, EmbedResult, ImportResult, InvalidEntryError, MemoryEntry,
                                  MemoryStoreError, NotFoundError, QuotaExceededError, SearchHit, estimate_tokens)
from ragleap_memory.sharded import ShardedStore
from ragleap_memory.sqlite_store import SCHEMA_VERSION, SQLiteStore

__version__ = "0.3.0"

__all__ = [
    "EmbedResult", "EmbedderError", "ImportResult", "InvalidEntryError", "KINDS", "MemoryEntry", "MemoryStoreError", "NotFoundError", "QuotaExceededError",
    "SCHEMA_VERSION", "SQLiteStore", "SearchHit", "ShardedStore", "TRUSTS", "__version__", "estimate_tokens",
]
