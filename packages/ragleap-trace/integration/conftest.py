"""Integration fixtures: a REAL RagLeap with a real Postgres, fake embedder and fake generator.

The fakes are copied from packages/ragleap-rag/tests/conftest.py (ragleap-core ee8f083), so no
paid API is called. These tests need ragleap-rag installed and a Postgres with pgvector; they
fail loudly (never skip) when either is missing.
"""

import hashlib
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("GEMINI_API_KEY", "example-test-key")
API_KEY = os.environ["GEMINI_API_KEY"]  # fake; read at runtime so no key-like literal sits in a keyword argument

from ragleap import EmbeddingConfig, ProviderConfig, RagLeap  # noqa: E402
from ragleap.embedding import EmbeddingService  # noqa: E402
from ragleap.generation import GenerationService  # noqa: E402

from ragleap_trace import InMemoryExporter, RagTraceAdapter, Tracer  # noqa: E402

DATABASE_URL = os.environ.get(
    "RAGLEAP_TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/ragleap_test",
)
DIMENSIONS = 8
STREAM_ANSWER = "This is a fake streamed answer."


def _fake_vector(text, dimensions=DIMENSIONS):
    h = hashlib.sha256(text.encode()).digest()
    return [(h[i % len(h)] / 255.0) for i in range(dimensions)]


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    def embed_text(self, text):
        if not text or not text.strip():
            return None
        return _fake_vector(text, self.dimensions)

    def embed_batch(self, texts):
        return [embed_text(self, t) for t in texts]

    monkeypatch.setattr(EmbeddingService, "embed_text", embed_text)
    monkeypatch.setattr(EmbeddingService, "embed_batch", embed_batch)


@pytest.fixture(autouse=True)
def fake_generator(monkeypatch):
    def call_provider(self, config, prompt, temperature, max_tokens, response_format=None):
        return (
            f"[fake answer based on context] {prompt[-120:]}",
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            None,
        )

    def stream_provider(self, config, prompt, temperature, max_tokens):
        for word in ["This ", "is ", "a ", "fake ", "streamed ", "answer."]:
            yield word

    monkeypatch.setattr(GenerationService, "_call_provider", call_provider)
    monkeypatch.setattr(GenerationService, "_stream_provider", stream_provider)


def _new_rag(**kwargs):
    return RagLeap(
        database_url=DATABASE_URL,
        embedder=EmbeddingConfig(provider="gemini", model="models/gemini-embedding-001",
                                 api_key=API_KEY, dimensions=DIMENSIONS),
        primary=ProviderConfig(provider="gemini", model="gemini-3.6-flash", api_key=API_KEY),
        **kwargs,
    )


@pytest.fixture(scope="session", autouse=True)
def _init_schema_once():
    _new_rag().init_schema()
    yield


@pytest.fixture(autouse=True)
def _clean_tables():
    import psycopg2

    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("TRUNCATE conversation_messages, conversations, chunks, documents CASCADE;")
        conn.commit()
        cur.close()
    finally:
        conn.close()
    yield


@pytest.fixture
def env():
    """A RagLeap wired to a RagTraceAdapter, plus recorders of the RAW events ragleap-rag fires."""
    exporter = InMemoryExporter()
    tracer = Tracer([exporter])
    adapter = RagTraceAdapter(tracer)
    hooks = adapter.hooks()
    raw = SimpleNamespace(ingest=[], query=[], answer=[])
    rag = _new_rag(
        on_ingest=[raw.ingest.append, *hooks["on_ingest"]],
        on_query=[raw.query.append, *hooks["on_query"]],
        on_answer=[raw.answer.append, *hooks["on_answer"]],
    )
    return SimpleNamespace(rag=rag, tracer=tracer, exporter=exporter, adapter=adapter, raw=raw)


@pytest.fixture
def new_rag():
    return _new_rag
