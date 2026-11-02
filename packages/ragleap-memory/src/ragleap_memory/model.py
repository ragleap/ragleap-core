"""Data model for ragleap-memory: entries, search hits, trust labels and errors."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

from ragleap_security import Scope

KINDS = ("episode", "fact", "summary")
# Lowest to highest. "owner": written by the owner. "model": written by a model. "untrusted": derived
# from content nobody vouched for. The default for new entries is the lowest.
TRUSTS = ("untrusted", "model", "owner")

ID_RE = re.compile(r"[0-9a-f]{32}\Z")
SOURCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}\Z")


class MemoryStoreError(Exception):
    """Base class for memory store errors. Messages never contain entry text."""


class InvalidEntryError(MemoryStoreError, ValueError):
    """An entry or argument is invalid."""


class QuotaExceededError(MemoryStoreError):
    """A write would take a tenant over its entry or token quota. The message never contains entry text."""


@dataclass(frozen=True)
class ImportResult:
    """What an import did. `dropped_links` counts supersede/derived links that pointed outside the file
    (or across scopes) and were therefore not kept."""
    imported: int
    skipped_duplicates: int
    dropped_links: int


class NotFoundError(MemoryStoreError):
    """No such entry, or it is not visible to the scope that asked. The two cases look the same."""


def estimate_tokens(text: str) -> int:
    """Rough, conservative token count: one token per 3 UTF-8 bytes, at least 1. It over-counts English
    (about 4 bytes per token) and fits scripts that need 3 bytes per character. It is an estimate, not
    any model's real tokenizer; pass your own `token_estimator` to the store if you need a different one."""
    return max(1, math.ceil(len(text.encode("utf-8")) / 3))


def trust_rank(trust: str) -> int:
    return TRUSTS.index(trust)


@dataclass(frozen=True)
class MemoryEntry:
    id: str
    scope: Scope
    kind: str
    text: str
    source: str
    trust: str
    importance: float
    created_at: str
    expires_at: "str | None" = None
    superseded_by: "str | None" = None
    derived_from: Tuple[str, ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)
    tokens: int = 0  # estimated tokens of `text`, computed when the entry was written

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "scope": self.scope.key, "kind": self.kind, "text": self.text,
            "source": self.source, "trust": self.trust, "importance": self.importance,
            "created_at": self.created_at, "expires_at": self.expires_at,
            "superseded_by": self.superseded_by, "derived_from": list(self.derived_from),
            "metadata": self.metadata, "tokens": self.tokens,
        }


@dataclass(frozen=True)
class SearchHit:
    entry: MemoryEntry
    score: float  # higher is more relevant (negated SQLite bm25)
    tokens: int = 0  # the entry's estimated tokens (what a token budget counts)
