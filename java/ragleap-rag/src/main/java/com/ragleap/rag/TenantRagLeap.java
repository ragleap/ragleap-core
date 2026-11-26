package com.ragleap.rag;

import com.ragleap.rag.ingestion.IngestResult;
import com.ragleap.rag.ingestion.TranscriptionConfig;
import com.ragleap.rag.memory.ConversationMessage;
import com.ragleap.rag.vectorstore.DocumentSummary;
import com.ragleap.rag.vectorstore.SearchResult;

import java.sql.SQLException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.function.Consumer;
import java.util.regex.Pattern;

/**
 * Opt-in multi-tenant scope (Java port of the Python TenantRagLeap). Get one with {@link RagLeap#forTenant}.
 * It is a view over the same connection pool and vector backend that
 * <ul>
 *   <li>tags everything it ingests with the metadata tenant_id,</li>
 *   <li>adds that tag to every search filter,</li>
 *   <li>prefixes conversation session ids ("acme:SESSION"),</li>
 *   <li>lists, deletes and updates only documents that carry that tag.</li>
 * </ul>
 * Ownership is read from the document metadata the backend returns from listDocuments. A backend that does not
 * return it makes the view see no documents, so it fails closed. Documents ingested without a tenant tag are
 * invisible to every view (there is no backfill). Methods the view does not define (ingestBatch, evaluate,
 * evaluateV2, cacheStats, initSchema, close) do not exist on it, as in Python.
 */
public final class TenantRagLeap {
    private static final Pattern TENANT_ID = Pattern.compile("[A-Za-z0-9_.@-]{1,128}");
    private static final String KEY = "tenant_id";
    private static final int PAGE = 1000;

    private final RagLeap rag;
    private final String tenantId;

    TenantRagLeap(RagLeap rag, String tenantId) {
        if (tenantId == null || !TENANT_ID.matcher(tenantId).matches()) {
            throw new IllegalArgumentException(
                    "tenant_id must be 1 to 128 characters from A-Z a-z 0-9 _ . @ - (it is put into filter expressions)");
        }
        this.rag = rag;
        this.tenantId = tenantId;
    }

    public String tenantId() {
        return tenantId;
    }

    // ---- scope helpers ----

    private String sid(String sessionId) {
        if (sessionId == null || sessionId.isEmpty()) {
            throw new IllegalArgumentException("session_id must be a non-empty string");
        }
        return tenantId + ":" + sessionId;
    }

    private Map<String, Object> mergeMetadata(Map<String, Object> metadata) {
        Map<String, Object> md = metadata == null ? new LinkedHashMap<>() : new LinkedHashMap<>(metadata);
        if (md.containsKey(KEY) && !Objects.equals(md.get(KEY), tenantId)) {
            throw new IllegalArgumentException("metadata sets a different tenant_id than this view");
        }
        md.put(KEY, tenantId);
        return md;
    }

    private Map<String, Object> mergeFilter(Map<String, Object> metadataFilter) {
        Map<String, Object> flt = metadataFilter == null ? new LinkedHashMap<>() : new LinkedHashMap<>(metadataFilter);
        if (flt.containsKey(KEY) && !Objects.equals(flt.get(KEY), tenantId)) {
            throw new IllegalArgumentException("metadata_filter sets a different tenant_id than this view");
        }
        flt.put(KEY, tenantId);
        return flt;
    }

    private AskOptions scoped(AskOptions options) {
        AskOptions o = options == null ? AskOptions.defaults() : options;
        Map<String, Object> flt = mergeFilter(o.metadataFilter());
        String session = o.sessionId() == null ? null : sid(o.sessionId());
        return new AskOptions(o.topK(), o.temperature(), o.systemPrompt(), o.maxTokens(), o.hybrid(), session, flt,
                o.responseFormat(), o.queryRewrite(), o.multiQueryN());
    }

    // ---- ingest ----

    public IngestResult ingest(String filename, byte[] rawBytes) throws SQLException {
        return ingest(filename, rawBytes, null);
    }

    public IngestResult ingest(String filename, byte[] rawBytes, Map<String, Object> metadata) throws SQLException {
        return rag.ingest(filename, rawBytes, mergeMetadata(metadata));
    }

    public IngestResult ingestText(String filename, String text) throws SQLException {
        return ingestText(filename, text, null, true, true);
    }

    public IngestResult ingestText(String filename, String text, Map<String, Object> metadata) throws SQLException {
        return ingestText(filename, text, metadata, true, true);
    }

    public IngestResult ingestText(String filename, String text, Map<String, Object> metadata, boolean sanitize,
                                   boolean warnOnInjectionRisk) throws SQLException {
        return rag.ingestText(filename, text, mergeMetadata(metadata), sanitize, warnOnInjectionRisk);
    }

    public IngestResult ingestImage(String filename, byte[] rawBytes, String mode, String mimeType,
                                    Map<String, Object> metadata) throws SQLException {
        return rag.ingestImage(filename, rawBytes, mode, mimeType, mergeMetadata(metadata));
    }

    public IngestResult ingestAudio(String filename, byte[] rawBytes, TranscriptionConfig transcriber,
                                    Map<String, Object> metadata) throws SQLException {
        return rag.ingestAudio(filename, rawBytes, transcriber, mergeMetadata(metadata));
    }

    public IngestResult ingestVideo(String filename, byte[] rawBytes, TranscriptionConfig transcriber,
                                    Map<String, Object> metadata) throws SQLException {
        return rag.ingestVideo(filename, rawBytes, transcriber, mergeMetadata(metadata));
    }

    // ---- search ----

    public AskResult ask(String query) throws SQLException {
        return ask(query, AskOptions.defaults());
    }

    public AskResult ask(String query, AskOptions options) throws SQLException {
        return rag.ask(query, scoped(options));
    }

    public void askStream(String query, AskOptions options, Consumer<String> onPiece) throws SQLException {
        rag.askStream(query, scoped(options), onPiece);
    }

    public List<SearchResult> retrieve(String query) throws SQLException {
        return retrieve(query, 5, true, null);
    }

    public List<SearchResult> retrieve(String query, int topK, boolean hybrid, Map<String, Object> metadataFilter)
            throws SQLException {
        return rag.retrieve(query, topK, hybrid, mergeFilter(metadataFilter));
    }

    // ---- sessions ----

    public List<ConversationMessage> getHistory(String sessionId) throws SQLException {
        return rag.getHistory(sid(sessionId));
    }

    public void clearSession(String sessionId) throws SQLException {
        rag.clearSession(sid(sessionId));
    }

    // ---- documents ----

    private List<DocumentSummary> mine() throws SQLException {
        List<DocumentSummary> out = new ArrayList<>();
        int offset = 0;
        while (true) {
            List<DocumentSummary> page = rag.listDocuments(PAGE, offset);
            for (DocumentSummary d : page) {
                if (isMine(d)) {
                    out.add(d);
                }
            }
            if (page.size() < PAGE) {
                return out;
            }
            offset += PAGE;
        }
    }

    private boolean isMine(DocumentSummary d) {
        Map<String, Object> md = d.metadata();
        return md != null && tenantId.equals(md.get(KEY));
    }

    private DocumentSummary find(String documentId) throws SQLException {
        for (DocumentSummary d : mine()) {
            if (Objects.equals(d.documentId(), documentId)) {
                return d;
            }
        }
        return null;
    }

    public List<DocumentSummary> listDocuments() throws SQLException {
        return listDocuments(100, 0);
    }

    /** Only this tenant's documents. limit and offset apply after the tenant filter and must not be negative. */
    public List<DocumentSummary> listDocuments(int limit, int offset) throws SQLException {
        if (limit < 0 || offset < 0) {
            throw new IllegalArgumentException("limit and offset must not be negative");
        }
        List<DocumentSummary> mine = mine();
        int from = Math.min(offset, mine.size());
        int to = (int) Math.min((long) offset + limit, mine.size());
        return new ArrayList<>(mine.subList(from, to));
    }

    /** Deletes a document of this tenant; false if there is no such document for this tenant. */
    public boolean deleteDocument(String documentId) throws SQLException {
        if (find(documentId) == null) {
            return false;
        }
        return rag.deleteDocument(documentId);
    }

    /** Replaces a document of this tenant; the new document keeps the stored metadata and gets a new id. */
    public IngestResult updateDocument(String documentId, String text, String filename) throws SQLException {
        DocumentSummary doc = find(documentId);
        if (doc == null) {
            throw new IllegalArgumentException("No document found with id " + documentId);
        }
        rag.deleteDocument(documentId);
        String name = filename == null || filename.isEmpty() ? doc.filename() : filename;
        return ingestText(name, text, doc.metadata());
    }
}
