package com.ragleap.rag;

import com.ragleap.rag.cache.CacheStats;
import com.ragleap.rag.cache.QueryEmbeddingCache;
import com.ragleap.rag.chunker.TextChunker;
import com.ragleap.rag.cost.CostTracker;
import com.ragleap.rag.db.ConnectionPool;
import com.ragleap.rag.embedding.EmbeddingService;
import com.ragleap.rag.evaluation.CaseResult;
import com.ragleap.rag.evaluation.EvalCase;
import com.ragleap.rag.evaluation.EvalScorer;
import com.ragleap.rag.evaluation.EvaluationResult;
import com.ragleap.rag.evaluation.Evaluator;
import com.ragleap.rag.evaluation.RagAnswer;
import com.ragleap.rag.generation.GenerationResult;
import com.ragleap.rag.generation.GenerationService;
import com.ragleap.rag.generation.ProviderConfig;
import com.ragleap.rag.generation.Usage;
import com.ragleap.rag.guardrails.GuardrailRunner;
import com.ragleap.rag.guardrails.GuardrailViolation;
import com.ragleap.rag.ingestion.IngestResult;
import com.ragleap.rag.ingestion.IngestionService;
import com.ragleap.rag.ingestion.TranscriptionConfig;
import com.ragleap.rag.memory.ConversationMemory;
import com.ragleap.rag.memory.ConversationMessage;
import com.ragleap.rag.observability.ObservabilityHooks;
import com.ragleap.rag.queryrewrite.MultiQueryResult;
import com.ragleap.rag.queryrewrite.QueryRewriteResult;
import com.ragleap.rag.queryrewrite.QueryRewriter;
import com.ragleap.rag.schema.SchemaManager;
import com.ragleap.rag.vectorstore.DocumentSummary;
import com.ragleap.rag.vectorstore.PgVectorBackend;
import com.ragleap.rag.vectorstore.SearchResult;
import com.ragleap.rag.vectorstore.VectorBackend;
import java.lang.System.Logger.Level;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.function.Consumer;
import java.util.function.Function;
import java.util.function.UnaryOperator;

/**
 * The main entry point of ragleap-rag: chunking, embedding, retrieval over a pluggable vector backend, generation
 * with fallback and streaming, conversation memory, cost tracking and hooks (Java port of the Python RagLeap class).
 * Not available compared with Python: reranking, the Redis query cache, URL ingestion and the async variants.
 * Close it to release the connection pool it created.
 */
public final class RagLeap implements AutoCloseable {
    private static final System.Logger LOG = System.getLogger("com.ragleap.rag");
    static final String EMBEDDING_FAILED = "Sorry, I couldn't process your question (embedding failed).";

    /** The collaborators; tests build their own, the public constructor builds the real ones. */
    record Parts(ConnectionPool pool, String databaseUrl, int dimensions, VectorBackend backend,
                 Function<String, List<Double>> embedFn, String embedderModel, ConversationMemory memory,
                 GenerationService generator, IngestionService ingestion, QueryEmbeddingCache cache,
                 CostTracker costTracker, ProviderConfig budgetFallback,
                 List<UnaryOperator<String>> outputGuardrails, List<Consumer<Map<String, Object>>> onQuery,
                 List<Consumer<Map<String, Object>>> onAnswer) {
    }

    private final Parts p;
    private final Object costLock = new Object();
    private final Object cacheLock = new Object();

    public RagLeap(RagLeapConfig config) {
        this(wire(config));
    }

    RagLeap(Parts parts) {
        this.p = parts;
    }

    private static Parts wire(RagLeapConfig c) {
        Integer dims = c.embedder().getDimensions();
        if (dims == null) {
            throw new IllegalArgumentException("The embedder configuration must set dimensions.");
        }
        ConnectionPool pool = new ConnectionPool(c.databaseUrl());
        VectorBackend backend = c.vectorBackend() != null ? c.vectorBackend() : new PgVectorBackend(c.databaseUrl());
        EmbeddingService embedder = new EmbeddingService(c.embedder());
        int size = c.chunkSize() != null ? c.chunkSize() : TextChunker.DEFAULT_CHUNK_SIZE;
        int overlap = c.chunkOverlap() != null ? c.chunkOverlap() : TextChunker.DEFAULT_CHUNK_OVERLAP;
        TextChunker chunker = new TextChunker(size, overlap);
        GenerationService generator = new GenerationService(c.primary(), c.fallbacks(), c.defaultTemperature(),
                c.defaultMaxTokens(), c.maxContextChars(), GenerationService.DEFAULT_SYSTEM_PROMPT);
        IngestionService ingestion = new IngestionService(backend, embedder::embedText, chunker,
                c.inputGuardrails(), c.onIngest(), (bytes, mime) -> generator.describeImage(bytes, mime));
        return new Parts(pool, c.databaseUrl(), dims, backend, embedder::embedText, embedder.getModel(),
                new ConversationMemory(pool), generator, ingestion,
                c.cacheEnabled() ? new QueryEmbeddingCache(c.cacheMaxSize()) : null,
                new CostTracker(c.pricingTable(), c.budgetUsdPerMonth()), c.budgetFallback(),
                c.outputGuardrails(), c.onQuery(), c.onAnswer());
    }

    @Override
    public void close() {
        if (p.pool() != null) {
            p.pool().close();
        }
    }

    /** Creates the storage structures if they do not exist (idempotent): vector backend and conversation memory. */
    public void initSchema() throws SQLException {
        p.backend().initSchema(p.dimensions());
        SchemaManager.initMemorySchema(p.pool());
    }

    // ---------------------------------------------------------------- cache

    private List<Double> embedQueryCached(String query) {
        if (p.cache() != null) {
            Optional<List<Double>> cached;
            synchronized (cacheLock) {
                cached = p.cache().get(query, p.embedderModel());
            }
            if (cached.isPresent()) {
                return cached.get();
            }
        }
        List<Double> embedding = p.embedFn().apply(query);
        if (p.cache() != null && embedding != null) {
            synchronized (cacheLock) {
                p.cache().set(query, p.embedderModel(), embedding);
            }
        }
        return embedding;
    }

    /** Query embedding cache statistics; all zeros when the cache is disabled. */
    public CacheInfo cacheStats() {
        if (p.cache() == null) {
            return new CacheInfo(0, 0, 0.0, 0, false);
        }
        CacheStats s;
        synchronized (cacheLock) {
            s = p.cache().stats();
        }
        return new CacheInfo(s.hits(), s.misses(), s.hitRate(), s.size(), true);
    }

    // ------------------------------------------------------------ ingestion

    public IngestResult ingest(String filename, byte[] rawBytes) throws SQLException {
        return p.ingestion().ingest(filename, rawBytes);
    }

    public IngestResult ingest(String filename, byte[] rawBytes, Map<String, Object> metadata) throws SQLException {
        return p.ingestion().ingest(filename, rawBytes, metadata);
    }

    public IngestResult ingestText(String filename, String text) throws SQLException {
        return p.ingestion().ingestText(filename, text);
    }

    public IngestResult ingestText(String filename, String text, Map<String, Object> metadata, boolean sanitize,
                                   boolean warnOnInjectionRisk) throws SQLException {
        return p.ingestion().ingestText(filename, text, metadata, sanitize, warnOnInjectionRisk);
    }

    /** Mode "ocr" reads text in the image (needs the Tesseract binary); "caption" describes it with a vision model. */
    public IngestResult ingestImage(String filename, byte[] rawBytes, String mode, String mimeType,
                                    Map<String, Object> metadata) throws SQLException {
        return p.ingestion().ingestImage(filename, rawBytes, mode, mimeType, metadata);
    }

    public IngestResult ingestAudio(String filename, byte[] rawBytes, TranscriptionConfig transcriber,
                                    Map<String, Object> metadata) throws SQLException {
        return p.ingestion().ingestAudio(filename, rawBytes, transcriber, metadata);
    }

    public IngestResult ingestVideo(String filename, byte[] rawBytes, TranscriptionConfig transcriber,
                                    Map<String, Object> metadata) throws SQLException {
        return p.ingestion().ingestVideo(filename, rawBytes, transcriber, metadata);
    }

    /**
     * Ingests a mixed batch concurrently. Each item runs on its own: one failure does not stop or undo the others,
     * and the results come back in the order of the items. Unlike the Python package, the metadata of a file item is
     * kept (Python drops it for that item type).
     */
    public List<BatchResult> ingestBatch(List<BatchItem> items) {
        if (items == null || items.isEmpty()) {
            return List.of();
        }
        int threads = Math.max(1, Math.min(32, Math.min(items.size(), Runtime.getRuntime().availableProcessors() + 4)));
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        try {
            List<Future<BatchResult>> futures = new ArrayList<>();
            for (BatchItem item : items) {
                futures.add(pool.submit(() -> runBatchItem(item)));
            }
            List<BatchResult> results = new ArrayList<>();
            for (Future<BatchResult> f : futures) {
                results.add(f.get());
            }
            return results;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("ingestBatch was interrupted", e);
        } catch (ExecutionException e) {
            throw new IllegalStateException("ingestBatch failed unexpectedly", e.getCause());
        } finally {
            pool.shutdownNow();
        }
    }

    private BatchResult runBatchItem(BatchItem item) {
        try {
            Map<String, Object> metadata = item.metadata() != null ? item.metadata() : Map.of();
            IngestResult result = switch (item.kind()) {
                case FILE -> p.ingestion().ingest(item.filename(), item.rawBytes(), metadata);
                case IMAGE -> p.ingestion().ingestImage(item.filename(), item.rawBytes(), item.mode(),
                        item.mimeType(), metadata);
                case AUDIO -> p.ingestion().ingestAudio(item.filename(), item.rawBytes(), item.transcriber(),
                        metadata);
                case VIDEO -> p.ingestion().ingestVideo(item.filename(), item.rawBytes(), item.transcriber(),
                        metadata);
            };
            return new BatchResult(true, result, null);
        } catch (Exception e) {
            String message = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
            LOG.log(Level.WARNING, "ingestBatch item failed (kind=" + item.kind() + ", filename="
                    + item.filename() + "): " + message);
            return new BatchResult(false, null, message);
        }
    }

    // ------------------------------------------------------------ retrieval

    private List<SearchResult> search(String query, List<Double> embedding, int poolSize, boolean hybrid,
                                      Map<String, Object> filter) throws SQLException {
        return hybrid ? p.backend().searchHybrid(query, embedding, poolSize, filter)
                : p.backend().searchDense(embedding, poolSize, filter);
    }

    public List<SearchResult> retrieve(String query) throws SQLException {
        return retrieve(query, 5, true, null);
    }

    /** Embeds the query and searches, without generating an answer. No rewrite and no session. */
    public List<SearchResult> retrieve(String query, int topK, boolean hybrid, Map<String, Object> metadataFilter)
            throws SQLException {
        List<Double> embedding = embedQueryCached(query);
        if (embedding == null) {
            return List.of();
        }
        List<SearchResult> chunks = search(query, embedding, topK, hybrid, metadataFilter);
        Map<String, Object> event = new LinkedHashMap<>();
        event.put("query", query);
        event.put("hybrid", hybrid);
        event.put("rerank", false);
        event.put("top_k", topK);
        event.put("chunks_retrieved", chunks.size());
        event.put("streaming", false);
        event.put("query_rewrite", null);
        ObservabilityHooks.fireEvent(event, p.onQuery(), "on_query");
        return chunks;
    }

    // ------------------------------------------------------------------ ask

    private ProviderConfig overrideProvider() {
        synchronized (costLock) {
            return p.budgetFallback() != null && p.costTracker().isOverBudget() ? p.budgetFallback() : null;
        }
    }

    private static Map<String, Integer> usageMap(Usage u) {
        if (u == null) {
            return null;
        }
        Map<String, Integer> m = new LinkedHashMap<>();
        m.put("prompt_tokens", u.promptTokens());
        m.put("completion_tokens", u.completionTokens());
        m.put("total_tokens", u.totalTokens());
        return m;
    }

    private void recordRewriteCost(GenerationResult raw) {
        if (raw != null) {
            synchronized (costLock) {
                p.costTracker().record(raw.providerUsed(), raw.modelUsed(), usageMap(raw.usage()));
            }
        }
    }

    public AskResult ask(String query) throws SQLException {
        return ask(query, AskOptions.defaults());
    }

    /**
     * Answers a question grounded in the ingested documents. With a session id the earlier turns of that session
     * are used as context and this turn is stored. Query rewriting only changes what is retrieved; the answer is
     * always generated for the original question.
     */
    public AskResult ask(String query, AskOptions o) throws SQLException {
        boolean hasSession = o.sessionId() != null && !o.sessionId().isEmpty();
        String historyPrefix = hasSession ? p.memory().buildHistoryPrompt(o.sessionId()) : "";
        ProviderConfig override = overrideProvider();
        QueryRewrite strategy = o.queryRewrite();
        QueryRewriteInfo info = null;
        List<Double> queryEmbedding;
        if (strategy == QueryRewrite.CONTEXTUAL) {
            QueryRewriteResult r = QueryRewriter.contextualRewrite(p.generator(), query, historyPrefix, override);
            recordRewriteCost(r.rawResult());
            info = new QueryRewriteInfo("contextual", r.query(), null, null);
            queryEmbedding = embedQueryCached(r.query());
        } else if (strategy == QueryRewrite.HYDE) {
            QueryRewriteResult r = QueryRewriter.hydeDocument(p.generator(), query, override);
            recordRewriteCost(r.rawResult());
            info = new QueryRewriteInfo("hyde", null, r.query(), null);
            queryEmbedding = embedQueryCached(r.query());
        } else {
            queryEmbedding = embedQueryCached(query);
        }
        if (queryEmbedding == null) {
            return new AskResult(EMBEDDING_FAILED, List.of(), List.of(), null, null, null, 0, null, null, null,
                    null, null, null, null);
        }

        int poolSize = o.topK();
        List<SearchResult> chunks;
        if (strategy == QueryRewrite.MULTI_QUERY) {
            MultiQueryResult mq = QueryRewriter.multiQueryVariants(p.generator(), query, o.multiQueryN(), override);
            recordRewriteCost(mq.rawResult());
            info = new QueryRewriteInfo("multi_query", null, null, mq.variants());
            List<List<SearchResult>> ranked = new ArrayList<>();
            for (String variant : mq.variants()) {
                List<Double> variantEmbedding = embedQueryCached(variant);
                if (variantEmbedding == null) {
                    continue;
                }
                ranked.add(search(variant, variantEmbedding, poolSize, o.hybrid(), o.metadataFilter()));
            }
            List<SearchResult> fused = QueryRewriter.reciprocalRankFusion(ranked);
            chunks = new ArrayList<>(fused.subList(0, Math.min(Math.max(poolSize, 0), fused.size())));
        } else {
            chunks = search(query, queryEmbedding, poolSize, o.hybrid(), o.metadataFilter());
        }

        Map<String, Object> queryEvent = new LinkedHashMap<>();
        queryEvent.put("query", query);
        queryEvent.put("hybrid", o.hybrid());
        queryEvent.put("rerank", false);
        queryEvent.put("top_k", o.topK());
        queryEvent.put("chunks_retrieved", chunks.size());
        queryEvent.put("streaming", false);
        queryEvent.put("query_rewrite", strategy == null ? null : strategy.id());
        ObservabilityHooks.fireEvent(queryEvent, p.onQuery(), "on_query");

        GenerationResult g = p.generator().generateAnswer(query, chunks, o.temperature(), override, o.maxTokens(),
                historyPrefix, o.systemPrompt(), o.responseFormat());

        CostInfo cost;
        synchronized (costLock) {
            Double costUsd = p.costTracker().record(g.providerUsed(), g.modelUsed(), usageMap(g.usage()));
            cost = new CostInfo(costUsd, p.costTracker().getCumulativeCostUsd(), costUsd != null);
        }

        String answer = g.answer();
        Boolean blocked = null;
        if (!p.outputGuardrails().isEmpty()) {
            try {
                answer = GuardrailRunner.runGuardrails(answer, p.outputGuardrails());
                blocked = false;
            } catch (GuardrailViolation e) {
                answer = "Response blocked by guardrail: " + e.getMessage();
                blocked = true;
            }
        }

        Map<String, Object> answerEvent = new LinkedHashMap<>();
        answerEvent.put("query", query);
        answerEvent.put("provider_used", g.providerUsed());
        answerEvent.put("usage", usageMap(g.usage()));
        answerEvent.put("chunks_sent", g.chunksSent());
        answerEvent.put("guardrail_blocked", blocked != null && blocked);
        answerEvent.put("streaming", false);
        ObservabilityHooks.fireEvent(answerEvent, p.onAnswer(), "on_answer");

        if (hasSession) {
            p.memory().addMessage(o.sessionId(), "user", query);
            p.memory().addMessage(o.sessionId(), "assistant", answer);
        }
        return new AskResult(answer, g.sources(), g.citations(), g.providerUsed(), g.modelUsed(), g.usage(),
                g.chunksSent(), g.structured(), g.structuredValid(), g.structuredEnforcement(),
                g.structuredValidationMethod(), info, cost, blocked);
    }

    /**
     * Like {@link #ask} but hands the answer to onPiece as it is generated. An output guardrail violation can only
     * be logged here, because the text has already been passed on. Query rewriting and the response format are not
     * used when streaming.
     */
    public void askStream(String query, AskOptions o, Consumer<String> onPiece) throws SQLException {
        List<Double> queryEmbedding = embedQueryCached(query);
        if (queryEmbedding == null) {
            onPiece.accept(EMBEDDING_FAILED);
            return;
        }
        List<SearchResult> chunks = search(query, queryEmbedding, o.topK(), o.hybrid(), o.metadataFilter());

        Map<String, Object> queryEvent = new LinkedHashMap<>();
        queryEvent.put("query", query);
        queryEvent.put("hybrid", o.hybrid());
        queryEvent.put("rerank", false);
        queryEvent.put("top_k", o.topK());
        queryEvent.put("chunks_retrieved", chunks.size());
        queryEvent.put("streaming", true);
        ObservabilityHooks.fireEvent(queryEvent, p.onQuery(), "on_query");

        boolean hasSession = o.sessionId() != null && !o.sessionId().isEmpty();
        String historyPrefix = hasSession ? p.memory().buildHistoryPrompt(o.sessionId()) : "";
        ProviderConfig override = overrideProvider();

        StringBuilder full = new StringBuilder();
        p.generator().generateAnswerStream(query, chunks, o.temperature(), override, o.maxTokens(), historyPrefix,
                o.systemPrompt(), piece -> {
                    full.append(piece);
                    onPiece.accept(piece);
                });
        String answer = full.toString();
        if (!p.outputGuardrails().isEmpty()) {
            try {
                GuardrailRunner.runGuardrails(answer, p.outputGuardrails());
            } catch (GuardrailViolation e) {
                LOG.log(Level.WARNING, "Output guardrail would have blocked this streamed response, but the text "
                        + "was already passed on and cannot be taken back: " + e.getMessage());
            }
        }

        Map<String, Object> answerEvent = new LinkedHashMap<>();
        answerEvent.put("query", query);
        answerEvent.put("answer_length", answer.codePointCount(0, answer.length()));
        answerEvent.put("streaming", true);
        ObservabilityHooks.fireEvent(answerEvent, p.onAnswer(), "on_answer");

        if (hasSession) {
            p.memory().addMessage(o.sessionId(), "user", query);
            p.memory().addMessage(o.sessionId(), "assistant", answer);
        }
    }

    // ------------------------------------------------- history, evaluation

    public List<ConversationMessage> getHistory(String sessionId) throws SQLException {
        return p.memory().getHistory(sessionId);
    }

    public void clearSession(String sessionId) throws SQLException {
        p.memory().clearSession(sessionId);
    }

    /**
     * Runs labeled test cases through {@link #ask} and reports deterministic quality signals: retrieval hit rate,
     * keyword coverage and citation groundedness. It is not an LLM-as-judge.
     */
    public EvaluationResult evaluate(List<EvalCase> testCases, AskOptions options) throws SQLException {
        if (testCases == null || testCases.isEmpty()) {
            throw new IllegalArgumentException("evaluate() requires at least one test case.");
        }
        List<CaseResult> results = new ArrayList<>();
        for (EvalCase c : testCases) {
            AskResult a = ask(c.query(), options);
            List<RagAnswer.CitationPreview> previews = new ArrayList<>();
            if (a.citations() != null) {
                a.citations().forEach(x -> previews.add(new RagAnswer.CitationPreview(x.textPreview())));
            }
            results.add(EvalScorer.scoreCase(c, new RagAnswer(a.answer(), a.sources(), previews)));
        }
        EvaluationResult summary = EvalScorer.aggregate(results);
        LOG.log(Level.INFO, "Evaluation complete: " + results.size() + " cases, retrievalHitRate="
                + summary.retrievalHitRate() + ", keywordCoverageRate=" + summary.keywordCoverageRate()
                + ", groundednessRate=" + summary.groundednessRate());
        return summary;
    }

    /**
     * Evaluation 2.0: like {@link #evaluate} and also ranking metrics (from the citations), latency and token usage per
     * case, an optional support checker of your own (no judge is bundled) and a mean-and-tail summary. Compare two runs
     * with EvalCompare.compareRuns(run.toScores(), other.toScores()).
     */
    public Evaluator.Run evaluateV2(List<Evaluator.Case> testCases, AskOptions options, List<Integer> metricKs,
                                    Evaluator.SupportChecker supportChecker, double failBelow) throws SQLException {
        return Evaluator.evaluate(q -> {
            AskResult a = ask(q, options);
            return new Evaluator.AnswerView(a.answer(), a.sources(), a.citations(), a.usage());
        }, testCases, metricKs, supportChecker, failBelow, null);
    }

    // ------------------------------------------------------------ documents

    public List<DocumentSummary> listDocuments() throws SQLException {
        return listDocuments(100, 0);
    }

    /** Previously ingested documents, most recently uploaded first. */
    public List<DocumentSummary> listDocuments(int limit, int offset) throws SQLException {
        return p.backend().listDocuments(limit, offset);
    }

    /** Deletes a document and its chunks; false if there was no such document. */
    public boolean deleteDocument(String documentId) throws SQLException {
        boolean deleted = p.backend().deleteDocument(documentId);
        if (deleted) {
            LOG.log(Level.INFO, "Deleted document " + documentId);
        } else {
            LOG.log(Level.WARNING, "deleteDocument: no document found with id " + documentId);
        }
        return deleted;
    }

    /**
     * Replaces a document's content by deleting it and ingesting the new text; the document gets a new id. Pass a
     * filename to rename it, or null to keep the stored filename.
     */
    public IngestResult updateDocument(String documentId, String text, String filename) throws SQLException {
        String existing = filename;
        if (existing == null) {
            Optional<String> stored = p.backend().getDocumentFilename(documentId);
            if (stored.isEmpty()) {
                throw new IllegalArgumentException("No document found with id " + documentId);
            }
            existing = stored.get();
        }
        deleteDocument(documentId);
        return p.ingestion().ingestText(existing, text);
    }
}
