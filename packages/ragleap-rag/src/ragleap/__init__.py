"""
ragleap-rag: a fast, honest, self-hosted RAG engine.

    from ragleap import RagLeap, ProviderConfig, EmbeddingConfig

    rag = RagLeap(
        database_url="postgresql://user:pass@localhost/mydb",
        embedder=EmbeddingConfig(provider="gemini", model="models/gemini-embedding-001", dimensions=3072, api_key="..."),
        primary=ProviderConfig(provider="gemini", model="gemini-3.6-flash", api_key="..."),
    )
    rag.init_schema()                  # one-time, idempotent

    result = rag.ingest("doc.pdf", raw_bytes)
    print(result.document_id, result.chunks_stored)

    answer = rag.ask("What does this document say about X?")
    print(answer["answer"], answer["sources"], answer["usage"])
"""
import uuid
import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Optional

from ragleap.chunker import TextChunker
from ragleap.embedding import EmbeddingService, EmbeddingConfig
from ragleap.vectorstores import VectorBackend, PgVectorBackend
from ragleap.generation import GenerationService, ProviderConfig
from ragleap.cost import CostTracker
from ragleap import query_rewrite as _query_rewrite
from ragleap.guardrails import GuardrailViolation, run_guardrails
from ragleap import evaluation as _evaluation
from ragleap.observability import fire_event
from ragleap.parsers import extract_text
from ragleap.memory import ConversationMemory
from ragleap.reranking import RerankerService
from ragleap.db import ConnectionPool
from ragleap.cache import QueryEmbeddingCache, RedisQueryEmbeddingCache
from ragleap import sanitization as _sanitization
from ragleap import web as _web
from ragleap import ocr as _ocr
from ragleap import transcription as _transcription
from ragleap import video as _video
from ragleap.transcription import TranscriptionConfig
from ragleap import schema as _schema

logger = logging.getLogger(__name__)

__version__ = "0.15.0"
__all__ = ["RagLeap", "ProviderConfig", "EmbeddingConfig", "IngestResult", "TranscriptionConfig", "VectorBackend", "PgVectorBackend"]


@dataclass
class IngestResult:
    document_id: str
    chunks_stored: int


class RagLeap:
    """
    The main entry point for ragleap-rag. Wires together chunking,
    embedding, hybrid retrieval, and generation (with fallback,
    streaming, and token usage reporting) over a pluggable vector
    backend (Postgres/pgvector by default) plus Postgres-backed
    conversation memory, which is always required regardless of
    which vector backend is chosen.
    """

    def __init__(
        self,
        database_url: str,
        primary: ProviderConfig,
        embedder: EmbeddingConfig,
        vector_backend: Optional[VectorBackend] = None,
        fallbacks: Optional[List[ProviderConfig]] = None,
        default_temperature: float = 0.3,
        default_max_tokens: int = 1024,
        max_context_chars: int = 12000,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
        cache_enabled: bool = True,
        cache_max_size: int = 1000,
        cache_backend: str = "memory",
        redis_url: Optional[str] = None,
        cache_ttl_seconds: int = 86400,
        input_guardrails: Optional[List[Callable[[str], str]]] = None,
        output_guardrails: Optional[List[Callable[[str], str]]] = None,
        on_ingest: Optional[List[Callable[[Dict], None]]] = None,
        on_query: Optional[List[Callable[[Dict], None]]] = None,
        on_answer: Optional[List[Callable[[Dict], None]]] = None,
        pricing_table: Optional[Dict] = None,
        budget_usd_per_month: Optional[float] = None,
        budget_fallback: Optional[ProviderConfig] = None,
    ):
        """
        database_url is always required - conversation memory is
        always Postgres-backed regardless of which vector_backend is
        chosen (FAISS/Pinecone/etc. only store vectors, not chat
        history). Pass vector_backend= to swap vector storage away
        from the default PgVectorBackend (pip install ragleap-rag[faiss]
        etc. for the optional backend-specific dependencies).
        """
        self.database_url = database_url
        self.embedding_dimensions = embedder.dimensions
        self._pool = ConnectionPool(database_url)
        self._vector_backend = vector_backend or PgVectorBackend(database_url)
        self._input_guardrails = input_guardrails
        self._output_guardrails = output_guardrails
        self._on_ingest = on_ingest
        self._on_query = on_query
        self._on_answer = on_answer
        self._cost_tracker = CostTracker(pricing_table=pricing_table, budget_usd_per_month=budget_usd_per_month)
        self._budget_fallback = budget_fallback

        self._chunker = TextChunker(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        self._embedder = EmbeddingService(embedder)
        self._memory = ConversationMemory(pool=self._pool)
        self._reranker = None  # lazy-loaded on first rerank=True call
        self._cache_enabled = cache_enabled
        if not cache_enabled:
            self._query_cache = None
        elif cache_backend == "redis":
            if not redis_url:
                raise ValueError("cache_backend='redis' requires redis_url= to be set.")
            self._query_cache = RedisQueryEmbeddingCache(redis_url, max_size=cache_max_size, ttl_seconds=cache_ttl_seconds)
        elif cache_backend == "memory":
            self._query_cache = QueryEmbeddingCache(max_size=cache_max_size)
        else:
            raise ValueError(f"Unknown cache_backend '{cache_backend}'. Use 'memory' or 'redis'.")
        self._generator = GenerationService(
            primary=primary,
            fallbacks=fallbacks,
            default_temperature=default_temperature,
            default_max_tokens=default_max_tokens,
            max_context_chars=max_context_chars,
        )

    def init_schema(self) -> None:
        """
        Create the required storage structures if they don't already
        exist. Idempotent. Initializes both the vector backend's
        schema and the (always-Postgres) conversation memory schema.
        """
        self._vector_backend.init_schema(self.embedding_dimensions)
        _schema.init_memory_schema(self.database_url)

    def _embed_query_cached(self, query: str) -> Optional[List[float]]:
        """Embed a query, using the cache if enabled. Cache key is
        (query text, embedding model) - safe across different queries
        and different embedder configs on the same instance."""
        if self._cache_enabled:
            cached = self._query_cache.get(query, self._embedder.model)
            if cached is not None:
                return cached

        embedding = self._embedder.embed_text(query)

        if self._cache_enabled and embedding is not None:
            self._query_cache.set(query, self._embedder.model, embedding)

        return embedding

    def cache_stats(self) -> dict:
        """Return query embedding cache stats: hits, misses, hit_rate, size.
        Returns all zeros if caching is disabled."""
        if not self._cache_enabled:
            return {"hits": 0, "misses": 0, "hit_rate": 0.0, "size": 0, "enabled": False}
        stats = self._query_cache.stats()
        stats["enabled"] = True
        return stats

    def ingest(self, filename: str, raw_bytes: bytes, metadata: Optional[Dict] = None) -> IngestResult:
        """
        Extract text from raw file bytes (any extension in
        ragleap.parsers.SUPPORTED_EXTENSIONS; everything beyond txt, md,
        pdf and docx needs the 'formats' extra), chunk, embed, and
        store it. Returns an IngestResult with the new document_id and
        chunk count.
        """
        text = extract_text(filename, raw_bytes)
        return self.ingest_text(filename, text, metadata=metadata)

    def ingest_url(self, url: str, metadata: Optional[Dict] = None, allow_private_urls: bool = False) -> IngestResult:
        """
        Fetch a web page, extract clean readable text (stripping nav/
        ads/footers via trafilatura), and ingest it. Requires the
        'web' extra: pip install ragleap-rag[web]. The URL itself is
        used as the stored filename/document_name.

        URLs that resolve to non-public addresses (loopback, private,
        link-local) are refused with UnsafeURLError (a ValueError) unless
        allow_private_urls=True.
        """
        text = _web.fetch_url_text(url, allow_private=allow_private_urls)
        if text is None:
            raise ValueError(f"Could not extract usable text from URL: {url}")
        return self.ingest_text(url, text, metadata=metadata)

    def ingest_image(
        self,
        filename: str,
        raw_bytes: bytes,
        mode: str = "ocr",
        mime_type: str = "image/jpeg",
        metadata: Optional[Dict] = None,
    ) -> IngestResult:
        """
        Ingest an image. Two modes, for two different kinds of images:

        mode="ocr" (default) reads literal text visible in the image
        (scanned documents, screenshots, photos of text). Requires the
        'ocr' extra AND the Tesseract binary installed on the system.

        mode="caption" describes the image's contents using a vision-
        capable model instead - for photos, diagrams, or charts with
        no readable text. Currently requires Gemini configured as the
        primary or a fallback provider.
        """
        if mode == "ocr":
            text = _ocr.extract_text_from_image(raw_bytes)
        elif mode == "caption":
            text = self._generator.describe_image(raw_bytes, mime_type=mime_type)
        else:
            raise ValueError(f"Unknown mode '{mode}'. Use 'ocr' or 'caption'.")

        return self.ingest_text(filename, text, metadata=metadata)

    def ingest_audio(
        self,
        filename: str,
        raw_bytes: bytes,
        transcriber: Optional["TranscriptionConfig"] = None,
        metadata: Optional[Dict] = None,
    ) -> IngestResult:
        """
        Transcribe audio to text and ingest it. Pass a TranscriptionConfig
        to choose the provider (whisper or deepgram) and options; if
        omitted, defaults to OpenAI's hosted Whisper API using
        OPENAI_API_KEY from the environment.
        """
        config = transcriber or _transcription.TranscriptionConfig(provider="whisper")
        service = _transcription.TranscriptionService(config)
        text = service.transcribe(filename, raw_bytes)
        return self.ingest_text(filename, text, metadata=metadata)

    def ingest_video(
        self,
        filename: str,
        raw_bytes: bytes,
        transcriber: Optional["TranscriptionConfig"] = None,
        metadata: Optional[Dict] = None,
    ) -> IngestResult:
        """
        Extract the audio track from a video file (requires the
        ffmpeg binary installed on the system - not pip-installable),
        transcribe it, and ingest the result. This is audio ingestion
        plus an extraction step - no separate video-specific
        transcription logic. Same transcriber= options and the same
        honest limitations as ingest_audio() apply (see its docstring).

        If the video already has a matching subtitle file (.vtt/.srt),
        ingesting that directly via ingest() is cheaper and more
        accurate than re-transcribing the audio.
        """
        audio_bytes = _video.extract_audio_from_video(raw_bytes, filename)
        return self.ingest_audio(filename, audio_bytes, transcriber=transcriber, metadata=metadata)

    def ingest_text(
        self,
        filename: str,
        text: str,
        metadata: Optional[Dict] = None,
        sanitize: bool = True,
        warn_on_injection_risk: bool = True,
    ) -> IngestResult:
        """
        Same as ingest(), but for text you've already extracted yourself.

        sanitize=True (default) strips null bytes, control characters,
        and invisible/zero-width Unicode from the text before chunking.
        warn_on_injection_risk=True (default) logs a warning if common
        prompt-injection trigger phrases are found - this is a heuristic
        signal for review, not a guarantee the content is safe, and
        nothing is blocked automatically.
        """
        if sanitize:
            text = _sanitization.sanitize_text(text)

        if warn_on_injection_risk:
            risk_matches = _sanitization.detect_injection_risk(text)
            if risk_matches:
                logger.warning(
                    f"Possible prompt-injection content in '{filename}': "
                    f"matched phrase(s) {risk_matches}. This is a heuristic "
                    f"signal, not a block - review the content if unexpected."
                )

        text = run_guardrails(text, self._input_guardrails)

        chunks = self._chunker.chunk_text(text)
        if not chunks:
            raise ValueError("No chunks produced from input text — is it empty?")

        document_id = str(uuid.uuid4())
        self._vector_backend.insert_document(document_id, filename, metadata or {})

        stored = 0
        for chunk in chunks:
            embedding = self._embedder.embed_text(chunk["text"])
            if embedding is None:
                logger.warning(f"Skipping chunk {chunk['chunk_index']} — embedding failed")
                continue

            self._vector_backend.insert_chunk(
                document_id=document_id, document_name=filename, chunk_index=chunk["chunk_index"],
                text=chunk["text"], token_count=chunk["token_count"], embedding=embedding, metadata=metadata or {},
            )
            stored += 1

        if stored == 0:
            self._vector_backend.delete_document(document_id)
            raise ValueError(f"All {len(chunks)} chunk(s) failed to embed — nothing was stored.")

        logger.info(f"Ingested '{filename}': {stored}/{len(chunks)} chunks stored")

        fire_event(
            {"document_id": document_id, "filename": filename, "chunks_stored": stored, "chunks_attempted": len(chunks)},
            self._on_ingest, "on_ingest",
        )

        return IngestResult(document_id=document_id, chunks_stored=stored)

    def ask(
        self,
        query: str,
        top_k: int = 5,
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        hybrid: bool = True,
        session_id: Optional[str] = None,
        rerank: bool = False,
        metadata_filter: Optional[Dict] = None,
        response_format: Optional[Dict] = None,
        query_rewrite: Optional[str] = None,
        multi_query_n: int = 3,
    ) -> Dict:
        """
        Answer a question grounded in previously ingested documents.
        Pass session_id to enable persistent, multi-turn conversation
        memory (Postgres-backed) — prior turns in that session are
        injected as context. Omit it for a fully stateless call.

        hybrid=True requests dense+sparse fusion, but gracefully
        degrades to dense-only if the active vector backend doesn't
        support sparse search (see VectorBackend.supports_sparse()) -
        not every backend can do full-text search.

        response_format=<JSON schema dict> requests structured output.
        The result gains "structured" (parsed object or None),
        "structured_valid" (bool), "structured_enforcement" ("native"
        or "json_object_fallback" depending on provider support), and
        "structured_validation_method" ("jsonschema" if the [structured]
        extra is installed, else "basic_type_check_only"). "answer"
        still contains the JSON as a string either way, for backward
        compatibility with existing callers.

        query_rewrite=<"contextual"|"hyde"|"multi_query"> improves
        retrieval by transforming the query before it's embedded/
        searched. "contextual" resolves follow-up questions ("what
        about its pricing?") into standalone questions using session_id
        history - needs session_id set, no-ops otherwise. "hyde"
        generates a hypothetical answer and embeds that instead of the
        raw query (Gao et al. 2022) - often better semantic match than
        embedding a short question. "multi_query" generates
        multi_query_n alternative phrasings, retrieves for each, and
        merges via Reciprocal Rank Fusion - can improve recall, but
        generated variants can be "nearly identical and lacking in
        diversity" (a documented RAG-Fusion limitation, not unique to
        this implementation), and costs multi_query_n retrieval calls
        instead of one. Every strategy fails open: if the extra LLM
        call itself fails, retrieval proceeds with the original query
        unmodified - a broken rewrite step never breaks retrieval.
        The final answer generation always uses your original query,
        never the rewritten form - rewriting only affects what gets
        retrieved. Result gains "query_rewrite" with the strategy used
        and what was actually retrieved-with (rewritten_query,
        hyde_document, or query_variants) when a strategy is set.

        Returns: {"answer": str, "sources": List[str], "provider_used": str,
                  "usage": dict|None, "chunks_sent": int}
        """
        history_prefix = self._memory.build_history_prompt(session_id) if session_id else ""

        override_provider = (
            self._budget_fallback
            if (self._budget_fallback and self._cost_tracker.is_over_budget())
            else None
        )

        query_rewrite_info = None
        if query_rewrite == "contextual":
            retrieval_query, rewrite_result = _query_rewrite.contextual_rewrite(self._generator, query, history_prefix, override_provider)
            if rewrite_result is not None:
                self._cost_tracker.record(rewrite_result.get("provider_used"), rewrite_result.get("model_used"), rewrite_result.get("usage"))
            query_rewrite_info = {"strategy": "contextual", "rewritten_query": retrieval_query}
            query_embedding = self._embed_query_cached(retrieval_query)
        elif query_rewrite == "hyde":
            hyde_text, rewrite_result = _query_rewrite.hyde_document(self._generator, query, override_provider)
            if rewrite_result is not None:
                self._cost_tracker.record(rewrite_result.get("provider_used"), rewrite_result.get("model_used"), rewrite_result.get("usage"))
            query_rewrite_info = {"strategy": "hyde", "hyde_document": hyde_text}
            query_embedding = self._embed_query_cached(hyde_text)
        else:
            query_embedding = self._embed_query_cached(query)

        if query_embedding is None:
            return {"answer": "Sorry, I couldn't process your question (embedding failed).",
                    "sources": [], "citations": [], "provider_used": None, "usage": None, "chunks_sent": 0}

        pool_size = top_k * 4 if rerank else top_k

        if query_rewrite == "multi_query":
            variants, rewrite_result = _query_rewrite.multi_query_variants(self._generator, query, multi_query_n, override_provider)
            if rewrite_result is not None:
                self._cost_tracker.record(rewrite_result.get("provider_used"), rewrite_result.get("model_used"), rewrite_result.get("usage"))
            query_rewrite_info = {"strategy": "multi_query", "query_variants": variants}
            ranked_lists = []
            for variant in variants:
                variant_embedding = self._embed_query_cached(variant)
                if variant_embedding is None:
                    continue
                if hybrid:
                    ranked_lists.append(self._vector_backend.search_hybrid(variant, variant_embedding, top_k=pool_size, metadata_filter=metadata_filter))
                else:
                    ranked_lists.append(self._vector_backend.search_dense(variant_embedding, top_k=pool_size, metadata_filter=metadata_filter))
            chunks = _query_rewrite.reciprocal_rank_fusion(ranked_lists)[:pool_size]
        elif hybrid:
            chunks = self._vector_backend.search_hybrid(query, query_embedding, top_k=pool_size, metadata_filter=metadata_filter)
        else:
            chunks = self._vector_backend.search_dense(query_embedding, top_k=pool_size, metadata_filter=metadata_filter)

        if rerank and chunks:
            if self._reranker is None:
                self._reranker = RerankerService()
            chunks = self._reranker.rerank(query, chunks, top_k=top_k)

        fire_event(
            {"query": query, "hybrid": hybrid, "rerank": rerank, "top_k": top_k, "chunks_retrieved": len(chunks), "streaming": False, "query_rewrite": query_rewrite},
            self._on_query, "on_query",
        )

        result = self._generator.generate_answer(
            query, chunks, temperature=temperature, system_prompt=system_prompt,
            max_tokens=max_tokens, history_prefix=history_prefix,
            override_provider=override_provider, response_format=response_format,
        )

        if query_rewrite_info is not None:
            result["query_rewrite"] = query_rewrite_info

        cost_usd = self._cost_tracker.record(result.get("provider_used"), result.get("model_used"), result.get("usage"))
        result["cost"] = {
            "cost_usd": cost_usd,
            "cumulative_cost_usd": self._cost_tracker.cumulative_cost_usd,
            "pricing_available": cost_usd is not None,
        }

        if self._output_guardrails:
            try:
                result["answer"] = run_guardrails(result["answer"], self._output_guardrails)
                result["guardrail_blocked"] = False
            except GuardrailViolation as e:
                result["answer"] = f"Response blocked by guardrail: {e}"
                result["guardrail_blocked"] = True

        fire_event(
            {"query": query, "provider_used": result.get("provider_used"), "usage": result.get("usage"),
             "chunks_sent": result.get("chunks_sent"), "guardrail_blocked": result.get("guardrail_blocked", False), "streaming": False},
            self._on_answer, "on_answer",
        )

        if session_id:
            self._memory.add_message(session_id, "user", query)
            self._memory.add_message(session_id, "assistant", result["answer"])

        return result

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        hybrid: bool = True,
        rerank: bool = False,
        metadata_filter: Optional[Dict] = None,
    ) -> List[Dict]:
        """
        Retrieve chunks for a query without generating an answer - the
        same embed -> search -> (optional rerank) pipeline ask() uses,
        stopping before generation. Useful for callers (e.g.
        ragleap-graph's GraphRetriever) that want raw retrieved chunks
        to combine with other retrieval signals themselves, without
        paying for or waiting on an LLM generation call they don't need.

        Does not support query_rewrite= or session_id= - those involve
        LLM calls and conversation-history orchestration that ask()
        owns. Pass an already-rewritten query string directly if needed.
        """
        query_embedding = self._embed_query_cached(query)
        if query_embedding is None:
            return []

        pool_size = top_k * 4 if rerank else top_k
        if hybrid:
            chunks = self._vector_backend.search_hybrid(
                query, query_embedding, top_k=pool_size, metadata_filter=metadata_filter
            )
        else:
            chunks = self._vector_backend.search_dense(
                query_embedding, top_k=pool_size, metadata_filter=metadata_filter
            )

        if rerank and chunks:
            if self._reranker is None:
                self._reranker = RerankerService()
            chunks = self._reranker.rerank(query, chunks, top_k=top_k)

        fire_event(
            {"query": query, "hybrid": hybrid, "rerank": rerank, "top_k": top_k,
             "chunks_retrieved": len(chunks), "streaming": False, "query_rewrite": None},
            self._on_query, "on_query",
        )
        return chunks

    def ask_stream(
        self,
        query: str,
        top_k: int = 5,
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        hybrid: bool = True,
        session_id: Optional[str] = None,
        rerank: bool = False,
        metadata_filter: Optional[Dict] = None,
    ) -> Iterator[str]:
        """Same as ask(), but yields the answer incrementally as it's
        generated. If session_id is set, the full assembled answer is
        stored to memory once streaming completes."""
        query_embedding = self._embed_query_cached(query)
        if query_embedding is None:
            yield "Sorry, I couldn't process your question (embedding failed)."
            return

        pool_size = top_k * 4 if rerank else top_k
        if hybrid:
            chunks = self._vector_backend.search_hybrid(query, query_embedding, top_k=pool_size, metadata_filter=metadata_filter)
        else:
            chunks = self._vector_backend.search_dense(query_embedding, top_k=pool_size, metadata_filter=metadata_filter)

        if rerank and chunks:
            if self._reranker is None:
                self._reranker = RerankerService()
            chunks = self._reranker.rerank(query, chunks, top_k=top_k)

        fire_event(
            {"query": query, "hybrid": hybrid, "rerank": rerank, "top_k": top_k, "chunks_retrieved": len(chunks), "streaming": True},
            self._on_query, "on_query",
        )

        history_prefix = self._memory.build_history_prompt(session_id) if session_id else ""

        override_provider = (
            self._budget_fallback
            if (self._budget_fallback and self._cost_tracker.is_over_budget())
            else None
        )

        pieces = []
        for piece in self._generator.generate_answer_stream(
            query, chunks, temperature=temperature, system_prompt=system_prompt,
            max_tokens=max_tokens, history_prefix=history_prefix,
            override_provider=override_provider,
        ):
            pieces.append(piece)
            yield piece

        full_answer = "".join(pieces)
        # Streaming never has token usage data (see generate_answer_stream's docstring),
        # so cost is always reported as unavailable here rather than guessed.
        self._cost_tracker.record(None, None, None)
        if self._output_guardrails:
            try:
                run_guardrails(full_answer, self._output_guardrails)
            except GuardrailViolation as e:
                logger.warning(
                    f"Output guardrail would have blocked this streamed response, "
                    f"but tokens were already yielded to the caller and cannot be "
                    f"retroactively un-sent: {e}"
                )

        fire_event(
            {"query": query, "answer_length": len(full_answer), "streaming": True},
            self._on_answer, "on_answer",
        )

        if session_id:
            self._memory.add_message(session_id, "user", query)
            self._memory.add_message(session_id, "assistant", full_answer)

    def get_history(self, session_id: str) -> List[Dict]:
        """Return the stored conversation history for a session, oldest first."""
        return self._memory.get_history(session_id)

    def clear_session(self, session_id: str) -> None:
        """Delete a session and its full message history."""
        self._memory.clear_session(session_id)

    def evaluate(self, test_cases: List[Dict], **ask_kwargs) -> Dict:
        """
        Run a labeled test set through ask() and report deterministic
        quality signals - retrieval hit rate, keyword coverage, and
        citation groundedness. NOT an LLM-as-judge framework (no
        faithfulness/relevancy scoring) - see ragleap.evaluation for
        the full honest scope of what this does and does not measure.

        test_cases: list of {"query": str, "expected_document": str
        (optional), "expected_keywords": List[str] (optional)}.
        Any extra ask_kwargs (top_k, rerank, hybrid, etc.) are passed
        through to every ask() call, except the evaluation options
        metric_ks, support_checker and fail_below (see
        ragleap.evaluation.evaluate()). Cases may also list
        relevant_documents to get ranking metrics.
        """
        return _evaluation.evaluate(self, test_cases, **ask_kwargs)

    def list_documents(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        """
        List previously ingested documents, most recently uploaded first.
        Returns: [{"document_id": str, "filename": str, "uploaded_at": ...,
                   "chunk_count": int}, ...]
        """
        return self._vector_backend.list_documents(limit=limit, offset=offset)

    def delete_document(self, document_id: str) -> bool:
        """
        Delete a document and all its chunks. Returns True if a document
        was actually deleted, False if document_id did not exist.
        """
        deleted = self._vector_backend.delete_document(document_id)
        if deleted:
            logger.info(f"Deleted document {document_id}")
        else:
            logger.warning(f"delete_document: no document found with id {document_id}")
        return deleted

    def update_document(self, document_id: str, text: str, filename: Optional[str] = None) -> IngestResult:
        """
        Replace a document's content. This is implemented as delete +
        re-ingest under the hood, not an in-place edit - chunk boundaries
        and embeddings for the old content are not preserved, and the
        document gets a new document_id. Use filename= to also rename it;
        omit to keep the existing filename.
        """
        existing_filename = filename
        if existing_filename is None:
            existing_filename = self._vector_backend.get_document_filename(document_id)
            if existing_filename is None:
                raise ValueError(f"No document found with id {document_id}")

        self.delete_document(document_id)
        return self.ingest_text(existing_filename, text)

    async def aingest(self, filename: str, raw_bytes: bytes, metadata: Optional[Dict] = None) -> IngestResult:
        """
        Async version of ingest(). Runs the existing sync implementation
        in a worker thread, so it does not block the event loop - the
        underlying DB/embedding calls are not natively async, but a
        blocking call to psycopg2 or an HTTP embedding API would
        otherwise stall an async web server (FastAPI, etc.) on every
        request.
        """
        import asyncio
        return await asyncio.to_thread(self.ingest, filename, raw_bytes, metadata)

    async def aingest_text(self, filename: str, text: str) -> IngestResult:
        """Async version of ingest_text(). See aingest() for details."""
        import asyncio
        return await asyncio.to_thread(self.ingest_text, filename, text)

    async def aingest_url(self, url: str, metadata: Optional[Dict] = None) -> IngestResult:
        """Async version of ingest_url(). See aingest() for details."""
        import asyncio
        return await asyncio.to_thread(self.ingest_url, url, metadata)

    async def aingest_image(
        self,
        filename: str,
        raw_bytes: bytes,
        mode: str = "ocr",
        mime_type: str = "image/jpeg",
        metadata: Optional[Dict] = None,
    ) -> IngestResult:
        """Async version of ingest_image(). See aingest() for details."""
        import asyncio
        return await asyncio.to_thread(self.ingest_image, filename, raw_bytes, mode, mime_type, metadata)

    async def aingest_audio(
        self,
        filename: str,
        raw_bytes: bytes,
        transcriber: Optional["TranscriptionConfig"] = None,
        metadata: Optional[Dict] = None,
    ) -> IngestResult:
        """Async version of ingest_audio(). See aingest() for details."""
        import asyncio
        return await asyncio.to_thread(self.ingest_audio, filename, raw_bytes, transcriber, metadata)

    async def aingest_video(
        self,
        filename: str,
        raw_bytes: bytes,
        transcriber: Optional["TranscriptionConfig"] = None,
        metadata: Optional[Dict] = None,
    ) -> IngestResult:
        """Async version of ingest_video(). See aingest() for details."""
        import asyncio
        return await asyncio.to_thread(self.ingest_video, filename, raw_bytes, transcriber, metadata)

    async def ingest_batch(self, items: List[Dict]) -> List[Dict]:
        """
        Ingest a mixed batch of documents concurrently - any
        combination of types, e.g. a PDF, a video, an image, and a
        URL all in one call. Each item runs independently: one
        item's failure does not stop or roll back the others, and
        each successfully ingested item is committed on its own (the
        same as calling each ingest_*() method separately, just
        concurrent instead of sequential).

        items: list of dicts, each with:
          - "type": one of "file", "url", "image", "audio", "video"
            ("file" covers ingest()'s full format range - pdf, docx,
            xlsx, etc. - dispatched by filename extension)
          - "filename": required for file/image/audio/video
          - "raw_bytes": required for file/image/audio/video
          - "url": required for type="url"
          - "mode": optional, image only ("ocr" or "caption")
          - "transcriber": optional, audio/video only (TranscriptionConfig)
          - "metadata": optional, any type

        Returns a list of dicts, one per input item, in the same
        order: {"success": bool, "result": IngestResult | None,
                "error": str | None}. Check "success" per item rather
        than assuming the whole batch succeeded - a batch of 10 with
        3 failures still stores the other 7.
        """
        async def _run_one(item: Dict) -> Dict:
            try:
                item_type = item.get("type")
                metadata = item.get("metadata")

                if item_type == "file":
                    result = await self.aingest(item["filename"], item["raw_bytes"], metadata=metadata)
                elif item_type == "url":
                    result = await self.aingest_url(item["url"], metadata=metadata)
                elif item_type == "image":
                    result = await self.aingest_image(
                        item["filename"], item["raw_bytes"],
                        mode=item.get("mode", "ocr"),
                        mime_type=item.get("mime_type", "image/jpeg"),
                        metadata=metadata,
                    )
                elif item_type == "audio":
                    result = await self.aingest_audio(
                        item["filename"], item["raw_bytes"],
                        transcriber=item.get("transcriber"), metadata=metadata,
                    )
                elif item_type == "video":
                    result = await self.aingest_video(
                        item["filename"], item["raw_bytes"],
                        transcriber=item.get("transcriber"), metadata=metadata,
                    )
                else:
                    raise ValueError(f"Unknown batch item type '{item_type}'. Use file, url, image, audio, or video.")

                return {"success": True, "result": result, "error": None}
            except Exception as e:
                logger.warning(f"ingest_batch item failed (type={item.get('type')}, filename={item.get('filename') or item.get('url')}): {e}")
                return {"success": False, "result": None, "error": str(e)}

        import asyncio
        return await asyncio.gather(*[_run_one(item) for item in items])

    async def aask(
        self,
        query: str,
        top_k: int = 5,
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        hybrid: bool = True,
        session_id: Optional[str] = None,
        rerank: bool = False,
    ) -> Dict:
        """Async version of ask(). See aingest() for details on the
        threading approach."""
        import asyncio
        return await asyncio.to_thread(
            self.ask, query, top_k=top_k, temperature=temperature,
            system_prompt=system_prompt, max_tokens=max_tokens, hybrid=hybrid,
            session_id=session_id, rerank=rerank,
        )

    async def aask_stream(
        self,
        query: str,
        top_k: int = 5,
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        hybrid: bool = True,
        session_id: Optional[str] = None,
        rerank: bool = False,
        metadata_filter: Optional[Dict] = None,
    ):
        """
        Async version of ask_stream(). Runs the sync generator in a
        worker thread and re-yields pieces as they become available,
        so the event loop is not blocked while waiting on each chunk.
        """
        import asyncio
        import queue

        q: queue.Queue = queue.Queue()
        SENTINEL = object()

        def _run():
            try:
                for piece in self.ask_stream(
                    query, top_k=top_k, temperature=temperature,
                    system_prompt=system_prompt, max_tokens=max_tokens,
                    hybrid=hybrid, session_id=session_id,
                    rerank=rerank, metadata_filter=metadata_filter,
                ):
                    q.put(piece)
            finally:
                q.put(SENTINEL)

        task = asyncio.create_task(asyncio.to_thread(_run))
        try:
            while True:
                piece = await asyncio.to_thread(q.get)
                if piece is SENTINEL:
                    break
                yield piece
        finally:
            await task
