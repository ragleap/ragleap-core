"""
Opt-in multi-tenant scope for ragleap-rag.

rag.for_tenant("acme") returns a TenantRagLeap: a view over the same
connection pool and vector backend that
  - tags everything it ingests with metadata {"tenant_id": "acme"},
  - adds that tag to every search filter,
  - prefixes conversation session ids ("acme:<session_id>"),
  - lists, deletes and updates only documents that carry that tag.

Ownership is read from the document metadata the backend returns from
list_documents(). A backend that does not return it makes the view see
no documents, so it fails closed. Documents ingested without a tenant tag
are invisible to every view.

The view's methods take everything after the query as keywords, so a
metadata_filter cannot be passed by position past the scope. Methods the
view does not define do not exist on it.
"""
import asyncio


class TenantRequiredError(RuntimeError):
    """Raised when RagLeap(require_tenant=True) gets a call without a tenant scope."""


_PAGE = 1000


class TenantRagLeap:
    def __init__(self, rag, tenant_id):
        if not isinstance(tenant_id, str) or not tenant_id or ":" in tenant_id:
            raise ValueError("tenant_id must be a non-empty string without ':'")
        self._rag = rag
        self.tenant_id = tenant_id

    # ---- scope helpers ----
    def _sid(self, session_id):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        return self.tenant_id + ":" + session_id

    def _merge_metadata(self, metadata):
        md = dict(metadata or {})
        if "tenant_id" in md and md["tenant_id"] != self.tenant_id:
            raise ValueError("metadata sets a different tenant_id than this view")
        md["tenant_id"] = self.tenant_id
        return md

    def _merge_filter(self, metadata_filter):
        flt = dict(metadata_filter or {})
        if "tenant_id" in flt and flt["tenant_id"] != self.tenant_id:
            raise ValueError("metadata_filter sets a different tenant_id than this view")
        flt["tenant_id"] = self.tenant_id
        return flt

    def _scope_kw(self, kw):
        out = dict(kw)
        out["metadata_filter"] = self._merge_filter(out.get("metadata_filter"))
        if out.get("session_id") is not None:
            out["session_id"] = self._sid(out["session_id"])
        return out

    # ---- ingest ----
    def ingest(self, filename, raw_bytes, metadata=None, **kw):
        return self._rag.ingest(filename, raw_bytes, metadata=self._merge_metadata(metadata), **kw)  # scope: ingest

    def ingest_url(self, url, metadata=None, **kw):
        return self._rag.ingest_url(url, metadata=self._merge_metadata(metadata), **kw)  # scope: ingest_url

    def ingest_image(self, filename, raw_bytes, metadata=None, **kw):
        return self._rag.ingest_image(filename, raw_bytes, metadata=self._merge_metadata(metadata), **kw)  # scope: ingest_image

    def ingest_audio(self, filename, raw_bytes, metadata=None, **kw):
        return self._rag.ingest_audio(filename, raw_bytes, metadata=self._merge_metadata(metadata), **kw)  # scope: ingest_audio

    def ingest_video(self, filename, raw_bytes, metadata=None, **kw):
        return self._rag.ingest_video(filename, raw_bytes, metadata=self._merge_metadata(metadata), **kw)  # scope: ingest_video

    def ingest_text(self, filename, text, metadata=None, **kw):
        return self._rag.ingest_text(filename, text, metadata=self._merge_metadata(metadata), **kw)  # scope: ingest_text

    # ---- search ----
    def ask(self, query, top_k=5, **kw):
        kw = self._scope_kw(kw)  # scope: ask
        return self._rag.ask(query, top_k=top_k, **kw)

    def retrieve(self, query, top_k=5, **kw):
        kw = self._scope_kw(kw)  # scope: retrieve
        return self._rag.retrieve(query, top_k=top_k, **kw)

    def ask_stream(self, query, top_k=5, **kw):
        kw = self._scope_kw(kw)  # scope: ask_stream
        return self._rag.ask_stream(query, top_k=top_k, **kw)

    # ---- sessions ----
    def get_history(self, session_id):
        return self._rag.get_history(self._sid(session_id))  # scope: history

    def clear_session(self, session_id):
        return self._rag.clear_session(self._sid(session_id))  # scope: clear

    # ---- documents ----
    def _all_docs(self):
        offset = 0
        while True:
            page = self._rag.list_documents(limit=_PAGE, offset=offset)
            for d in page:
                yield d
            if len(page) < _PAGE:
                return
            offset += _PAGE

    def _is_mine(self, doc):
        md = doc.get("metadata")
        return isinstance(md, dict) and md.get("tenant_id") == self.tenant_id

    def _find(self, document_id):
        for d in self._all_docs():
            if d.get("document_id") == str(document_id) and self._is_mine(d):
                return d
        return None

    def list_documents(self, limit=100, offset=0):
        mine = [d for d in self._all_docs() if self._is_mine(d)]  # scope: list
        return mine[offset:offset + limit]

    def delete_document(self, document_id):
        if self._find(document_id) is None:  # scope: delete
            return False
        return self._rag.delete_document(document_id)

    def update_document(self, document_id, text, filename=None):
        doc = self._find(document_id)
        if doc is None:  # scope: update
            raise ValueError("No document found with id " + str(document_id))
        self._rag.delete_document(document_id)
        return self.ingest_text(filename or doc["filename"], text, metadata=doc.get("metadata"))

    # ---- async (run the scoped sync methods in a worker thread) ----
    async def aingest(self, filename, raw_bytes, metadata=None, **kw):
        return await asyncio.to_thread(self.ingest, filename, raw_bytes, metadata, **kw)

    async def aingest_text(self, filename, text, metadata=None, **kw):
        return await asyncio.to_thread(self.ingest_text, filename, text, metadata, **kw)

    async def aingest_url(self, url, metadata=None, **kw):
        return await asyncio.to_thread(self.ingest_url, url, metadata, **kw)

    async def aingest_image(self, filename, raw_bytes, metadata=None, **kw):
        return await asyncio.to_thread(self.ingest_image, filename, raw_bytes, metadata, **kw)

    async def aingest_audio(self, filename, raw_bytes, metadata=None, **kw):
        return await asyncio.to_thread(self.ingest_audio, filename, raw_bytes, metadata, **kw)

    async def aingest_video(self, filename, raw_bytes, metadata=None, **kw):
        return await asyncio.to_thread(self.ingest_video, filename, raw_bytes, metadata, **kw)

    async def aask(self, query, top_k=5, **kw):
        return await asyncio.to_thread(self.ask, query, top_k, **kw)
