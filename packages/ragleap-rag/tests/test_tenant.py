"""Tenant views: scoped ingest, search, sessions and documents (pgvector and FAISS), plus stub checks."""
import asyncio
import socket
import uuid

import pytest
from conftest import TEST_DIMENSIONS
from ragleap import EmbeddingConfig, ProviderConfig, RagLeap
from ragleap.tenant import TenantRagLeap, TenantRequiredError
from ragleap.vectorstores import VectorBackend

TEXT = "Quarterly pricing sheet: the premium plan costs forty dollars per seat."


def _build(database_url, backend=None, **kw):
    rag = RagLeap(
        database_url=database_url,
        vector_backend=backend,
        embedder=EmbeddingConfig(provider="gemini", model="models/gemini-embedding-001", api_key="example-test-key", dimensions=TEST_DIMENSIONS),
        primary=ProviderConfig(provider="gemini", model="gemini-3.6-flash", api_key="example-test-key"),
        **kw,
    )
    rag.init_schema()
    return rag


@pytest.fixture(params=["pgvector", "faiss"])
def make_rag(request, database_url, tmp_path):
    def make(**kw):
        backend = None
        if request.param == "faiss":
            pytest.importorskip("faiss")
            from ragleap.vectorstores import FAISSBackend
            backend = FAISSBackend(persist_directory=str(tmp_path / uuid.uuid4().hex))
        return _build(database_url, backend, **kw)
    return make


class _Rec:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def f(*a, **k):
            self.calls.append((name, a, k))
        return f


# ---------- stub checks: what the view sends to the underlying instance ----------

@pytest.mark.parametrize("name,args", [
    ("ingest", ("f.txt", b"x")), ("ingest_url", ("http://example.com",)), ("ingest_image", ("f.png", b"x")),
    ("ingest_audio", ("f.mp3", b"x")), ("ingest_video", ("f.mp4", b"x")), ("ingest_text", ("f.txt", "x")),
])
def test_every_ingest_kind_tags_metadata(name, args):
    rec = _Rec()
    view = TenantRagLeap(rec, "acme")
    getattr(view, name)(*args)
    assert rec.calls[-1][0] == name and rec.calls[-1][2]["metadata"] == {"tenant_id": "acme"}
    getattr(view, name)(*args, metadata={"k": 1})
    assert rec.calls[-1][2]["metadata"] == {"k": 1, "tenant_id": "acme"}
    n = len(rec.calls)
    with pytest.raises(ValueError):
        getattr(view, name)(*args, metadata={"tenant_id": "globex"})
    assert len(rec.calls) == n


def test_search_methods_merge_filter_and_prefix_session():
    rec = _Rec()
    view = TenantRagLeap(rec, "acme")
    for name in ("ask", "retrieve", "ask_stream"):
        kw = {} if name == "retrieve" else {"session_id": "s"}
        getattr(view, name)("q", metadata_filter={"x": 1}, **kw)
        call = rec.calls[-1]
        assert call[0] == name and call[2]["metadata_filter"] == {"x": 1, "tenant_id": "acme"}
        if kw:
            assert call[2]["session_id"] == "acme:s"
        else:
            assert "session_id" not in call[2]
        with pytest.raises(ValueError):
            getattr(view, name)("q", metadata_filter={"tenant_id": "globex"})


def test_history_calls_use_the_prefixed_session():
    rec = _Rec()
    view = TenantRagLeap(rec, "acme")
    view.get_history("s")
    assert rec.calls[-1] == ("get_history", ("acme:s",), {})
    view.clear_session("s")
    assert rec.calls[-1] == ("clear_session", ("acme:s",), {})
    for bad in ("", 5, None):
        with pytest.raises(ValueError):
            view.get_history(bad)


@pytest.mark.parametrize("bad", ["", None, 5, "a:b", ":"])
def test_invalid_tenant_ids_are_refused(bad):
    with pytest.raises(ValueError):
        TenantRagLeap(_Rec(), bad)


class _NoMetaBackend(VectorBackend):
    def __init__(self):
        self.deleted = []

    def init_schema(self, dimensions):
        pass

    def insert_document(self, document_id, filename, metadata):
        pass

    def insert_chunk(self, document_id, document_name, chunk_index, text, token_count, embedding, metadata):
        pass

    def search_dense(self, embedding, top_k, metadata_filter=None):
        return []

    def list_documents(self, limit, offset):
        return [{"document_id": "d1", "filename": "f.txt"}] if offset == 0 else []

    def delete_document(self, document_id):
        self.deleted.append(document_id)
        return True

    def get_document_filename(self, document_id):
        return "f.txt"


def test_backend_that_hides_metadata_fails_closed(database_url):
    backend = _NoMetaBackend()
    view = _build(database_url, backend).for_tenant("acme")
    assert view.list_documents() == []
    assert view.delete_document("d1") is False
    with pytest.raises(ValueError):
        view.update_document("d1", "x")
    assert backend.deleted == []


# ---------- real backends ----------

def test_each_tenant_searches_only_its_own_chunks(make_rag):
    rag = make_rag()
    a, b = rag.for_tenant("acme"), rag.for_tenant("globex")
    a.ingest_text("acme.txt", TEXT)
    b.ingest_text("globex.txt", TEXT)
    rag.ingest_text("legacy.txt", TEXT)
    for hybrid in (True, False):
        chunks = a.retrieve(TEXT, top_k=10, hybrid=hybrid)
        assert chunks and {c["document_name"] for c in chunks} == {"acme.txt"}
        assert a.ask(TEXT, top_k=10, hybrid=hybrid)["sources"] == ["acme.txt"]
    assert {c["document_name"] for c in b.retrieve(TEXT, top_k=10)} == {"globex.txt"}
    assert {c["document_name"] for c in rag.retrieve(TEXT, top_k=10)} == {"acme.txt", "globex.txt", "legacy.txt"}


def test_stream_search_is_scoped(make_rag):
    rag = make_rag()
    a = rag.for_tenant("acme")
    a.ingest_text("acme.txt", TEXT)
    seen = []
    backend = rag._vector_backend
    for name, idx in (("search_hybrid", 3), ("search_dense", 2)):
        def spy(*args, _orig=getattr(backend, name), _idx=idx, **kw):
            seen.append(kw["metadata_filter"] if "metadata_filter" in kw else (args[_idx] if len(args) > _idx else None))
            return _orig(*args, **kw)
        setattr(backend, name, spy)
    list(a.ask_stream(TEXT, top_k=3))
    assert seen and all(f == {"tenant_id": "acme"} for f in seen), seen


def test_sessions_are_separate_per_tenant(make_rag):
    rag = make_rag()
    a, b = rag.for_tenant("acme"), rag.for_tenant("globex")
    a.ingest_text("acme.txt", TEXT)
    b.ingest_text("globex.txt", TEXT)
    a.ask("question from acme", session_id="s1")
    b.ask("question from globex", session_id="s1")
    ha, hb = str(a.get_history("s1")), str(b.get_history("s1"))
    assert "question from acme" in ha and "question from globex" not in ha
    assert "question from globex" in hb and "question from acme" not in hb
    assert rag.get_history("s1") == []
    a.clear_session("s1")
    assert a.get_history("s1") == [] and "question from globex" in str(b.get_history("s1"))


def test_list_delete_update_are_scoped(make_rag):
    rag = make_rag()
    a, b = rag.for_tenant("acme"), rag.for_tenant("globex")
    ra = a.ingest_text("acme.txt", TEXT)
    b.ingest_text("globex.txt", TEXT)
    rl = rag.ingest_text("legacy.txt", TEXT)
    assert [d["filename"] for d in a.list_documents()] == ["acme.txt"]
    assert [d["filename"] for d in b.list_documents()] == ["globex.txt"]
    assert len(rag.list_documents()) == 3
    assert b.delete_document(ra.document_id) is False
    assert a.delete_document(rl.document_id) is False
    assert len(rag.list_documents()) == 3
    with pytest.raises(ValueError):
        b.update_document(ra.document_id, "new text")
    assert ra.document_id in {d["document_id"] for d in rag.list_documents()}
    new = a.update_document(ra.document_id, "Updated acme text about premium seats.")
    docs = a.list_documents()
    assert [d["document_id"] for d in docs] == [new.document_id]
    assert docs[0]["metadata"]["tenant_id"] == "acme" and docs[0]["filename"] == "acme.txt"
    assert a.delete_document(new.document_id) is True
    assert a.list_documents() == [] and len(rag.list_documents()) == 2


def test_list_documents_pages_inside_the_tenant(make_rag):
    rag = make_rag()
    a, b = rag.for_tenant("acme"), rag.for_tenant("globex")
    for i in range(3):
        a.ingest_text("acme%d.txt" % i, TEXT + " " + str(i))
        b.ingest_text("globex%d.txt" % i, TEXT + " " + str(i))
    assert len(a.list_documents()) == 3
    assert len(a.list_documents(limit=2)) == 2
    assert len(a.list_documents(limit=2, offset=1)) == 2
    assert a.list_documents(limit=2, offset=3) == []
    assert all(d["filename"].startswith("acme") for d in a.list_documents())


def test_require_tenant_blocks_unscoped_calls(make_rag):
    rag = make_rag(require_tenant=True)
    a = rag.for_tenant("acme")
    ra = a.ingest_text("acme.txt", TEXT)
    calls = [
        lambda: rag.ingest_text("x.txt", TEXT), lambda: rag.ingest("x.txt", b"hello world"),
        lambda: rag.ask("q"), lambda: rag.retrieve("q"), lambda: rag.ask_stream("q"),
        lambda: rag.get_history("s"), lambda: rag.clear_session("s"), lambda: rag.list_documents(),
        lambda: rag.delete_document(ra.document_id), lambda: rag.update_document(ra.document_id, "t"),
    ]
    for call in calls:
        with pytest.raises(TenantRequiredError):
            call()
    assert a.ask(TEXT, top_k=3)["sources"] == ["acme.txt"]
    assert len(a.list_documents()) == 1


def test_async_view_and_async_guard(make_rag):
    rag = make_rag(require_tenant=True)
    a = rag.for_tenant("acme")

    async def go():
        await a.aingest_text("acme.txt", TEXT)
        answer = await a.aask(TEXT, top_k=3)
        with pytest.raises(TenantRequiredError):
            await rag.aingest_text("x.txt", TEXT)
        with pytest.raises(TenantRequiredError):
            await rag.aask("q")
        return answer

    assert asyncio.run(go())["sources"] == ["acme.txt"]


# ---------- guards must stop the call before any work starts ----------

@pytest.fixture
def no_network(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise OSError("network disabled in this test")
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_require_tenant_stops_every_ingest_kind_before_any_work(make_rag, no_network):
    rag = make_rag(require_tenant=True)
    calls = [
        lambda: rag.ingest("f.txt", b"hello world"),
        lambda: rag.ingest_url("http://127.0.0.1:1/x"),
        lambda: rag.ingest_image("f.png", b"not an image"),
        lambda: rag.ingest_audio("f.mp3", b"not audio"),
        lambda: rag.ingest_video("f.mp4", b"not video"),
    ]
    for call in calls:
        with pytest.raises(TenantRequiredError):
            call()
    assert rag._vector_backend.list_documents(limit=10, offset=0) == []


def test_require_tenant_guards_update_of_an_unknown_document(make_rag):
    rag = make_rag(require_tenant=True)
    with pytest.raises(TenantRequiredError):
        rag.update_document(str(uuid.uuid4()), "text")


def test_require_tenant_blocks_async_before_a_worker_thread_starts(make_rag, no_network, monkeypatch):
    rag = make_rag(require_tenant=True)
    started = []

    async def trap(fn, *args, **kwargs):
        started.append(getattr(fn, "__name__", "?"))
        raise RuntimeError("worker thread would have started")

    monkeypatch.setattr(asyncio, "to_thread", trap)

    async def go():
        factories = [
            lambda: rag.aingest("f.txt", b"x"), lambda: rag.aingest_text("f.txt", "x"),
            lambda: rag.aingest_url("http://127.0.0.1:1/x"), lambda: rag.aingest_image("f.png", b"x"),
            lambda: rag.aingest_audio("f.mp3", b"x"), lambda: rag.aingest_video("f.mp4", b"x"),
            lambda: rag.ingest_batch([{"filename": "f.txt", "text": "x"}]), lambda: rag.aask("q"),
        ]
        for factory in factories:
            with pytest.raises(TenantRequiredError):
                await factory()
        with pytest.raises(TenantRequiredError):
            rag.aask_stream("q")

    asyncio.run(go())
    assert started == []


def test_require_tenant_stops_ingest_before_text_extraction(make_rag, monkeypatch):
    import ragleap
    rag = make_rag(require_tenant=True)
    seen = []
    monkeypatch.setattr(ragleap, "extract_text", lambda *a, **k: seen.append(a) or "some text")
    with pytest.raises(TenantRequiredError):
        rag.ingest("f.txt", b"hello world")
    assert seen == []
