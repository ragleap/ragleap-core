"""ShardedStore: one SQLite file per tenant, routed by `scope.tenant`.

A tenant's data, index and quota live in its own file, so a big tenant does not slow down a small one and a
tenant-wide erase deletes the files. It offers the same methods as SQLiteStore. Store options (audit, clock,
quotas, token_estimator, limits) are passed to every shard: quotas apply per tenant, and one audit log sees
every tenant."""
from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional

from ragleap_security import Scope, require_scope

from .model import ImportResult, InvalidEntryError, MemoryEntry, SearchHit
from .sqlite_store import SQLiteStore


class _Holder:
    """One shard: its own lock, its open store (or None), and a flag set when it was evicted or erased."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.store: Optional[SQLiteStore] = None
        self.dead = False


class ShardedStore:
    def __init__(self, directory: "str | os.PathLike[str]", *, max_open: int = 64, **store_options: Any):
        if isinstance(max_open, bool) or not isinstance(max_open, int) or max_open < 1:
            raise InvalidEntryError("max_open must be a positive int")
        SQLiteStore(":memory:", **store_options).close()  # refuse bad options now, not on the first write
        self.directory = os.fspath(directory)
        self.max_open = max_open
        self._options = store_options
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self._table_lock = threading.Lock()  # guards only the table of holders, never held during database work
        self._table: "OrderedDict[str, _Holder]" = OrderedDict()

    # -- routing -----------------------------------------------------------------------------------

    def _base(self, tenant: str) -> str:
        return os.path.join(self.directory, hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:32])

    def _run(self, scope: Any, fn: Callable[[SQLiteStore, Scope], Any], *, drop: bool = False) -> Any:
        scope = require_scope(scope)
        while True:
            with self._table_lock:
                holder = self._table.get(scope.tenant)
                if holder is None:
                    holder = self._table[scope.tenant] = _Holder()
                self._table.move_to_end(scope.tenant)
            with holder.lock:
                if holder.dead:
                    continue  # evicted or erased while we waited: look the tenant up again
                if holder.store is None:
                    holder.store = SQLiteStore(self._base(scope.tenant) + ".db", **self._options)
                result = fn(holder.store, scope)
                if drop:  # the store was closed by fn: nobody may use this holder again
                    holder.dead = True
                    holder.store = None
            break
        if drop:
            with self._table_lock:
                if self._table.get(scope.tenant) is holder:
                    del self._table[scope.tenant]
        self._evict()
        return result

    def _evict(self) -> None:
        """Close the least recently used shards beyond max_open. A shard in use is never closed."""
        with self._table_lock:
            for tenant in list(self._table):
                if len(self._table) <= self.max_open:
                    return
                holder = self._table[tenant]
                if not holder.lock.acquire(blocking=False):
                    continue
                try:
                    holder.dead = True
                    if holder.store is not None:
                        holder.store.close()
                        holder.store = None
                    del self._table[tenant]
                finally:
                    holder.lock.release()

    # -- the SQLiteStore API -----------------------------------------------------------------------

    def add(self, scope: Scope, text: str, **kw: Any) -> MemoryEntry:
        return self._run(scope, lambda s, sc: s.add(sc, text, **kw))

    def get(self, scope: Scope, entry_id: str) -> MemoryEntry:
        return self._run(scope, lambda s, sc: s.get(sc, entry_id))

    def count(self, scope: Scope) -> int:
        return self._run(scope, lambda s, sc: s.count(sc))

    def usage(self, scope: Scope, period: Optional[str] = None) -> Dict[str, Any]:
        return self._run(scope, lambda s, sc: s.usage(sc, period))

    def search(self, scope: Scope, query: str, **kw: Any) -> List[SearchHit]:
        return self._run(scope, lambda s, sc: s.search(sc, query, **kw))

    def supersede(self, scope: Scope, old_id: str, text: str, **kw: Any) -> MemoryEntry:
        return self._run(scope, lambda s, sc: s.supersede(sc, old_id, text, **kw))

    def forget(self, scope: Scope, ids: Iterable[str]) -> int:
        return self._run(scope, lambda s, sc: s.forget(sc, ids))

    def export(self, scope: Scope) -> List[Dict[str, Any]]:
        return self._run(scope, lambda s, sc: s.export(sc))

    def sweep_expired(self, scope: Scope) -> int:
        return self._run(scope, lambda s, sc: s.sweep_expired(sc))

    def export_file(self, scope: Scope, path: "str | os.PathLike[str]") -> int:
        return self._run(scope, lambda s, sc: s.export_file(sc, path))

    def import_file(self, scope: Scope, path: "str | os.PathLike[str]", **options: Any) -> ImportResult:
        return self._run(scope, lambda s, sc: s.import_file(sc, path, **options))

    def import_stream(self, scope: Scope, stream: Any, **options: Any) -> ImportResult:
        return self._run(scope, lambda s, sc: s.import_stream(sc, stream, **options))

    def compact(self, scope: Scope) -> None:
        """Compact the shard of `scope.tenant`."""
        self._run(scope, lambda s, sc: s.compact())

    def erase(self, scope: Scope) -> int:
        """Erase what `scope` covers. A tenant-wide scope then deletes the tenant's files (database, journal
        and shared-memory file); a narrower scope keeps the file."""
        scope = require_scope(scope)
        whole_tenant = scope.subject is None and scope.agent is None and scope.session is None

        def work(store: SQLiteStore, sc: Scope) -> int:
            n = store.erase(sc)
            if whole_tenant:
                store.close()
                for suffix in (".db", ".db-wal", ".db-shm"):
                    try:
                        os.remove(self._base(sc.tenant) + suffix)
                    except FileNotFoundError:
                        pass
            return n

        return self._run(scope, work, drop=whole_tenant)

    def close(self) -> None:
        with self._table_lock:
            holders = list(self._table.values())
            self._table.clear()
        for holder in holders:
            with holder.lock:
                holder.dead = True
                if holder.store is not None:
                    holder.store.close()
                    holder.store = None
