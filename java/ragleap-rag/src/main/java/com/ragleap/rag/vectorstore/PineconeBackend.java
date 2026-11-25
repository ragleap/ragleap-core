package com.ragleap.rag.vectorstore;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.io.File;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.sql.Types;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.locks.ReentrantLock;
import java.util.logging.Logger;

/**
 * Pinecone-backed vector storage for ragleap-rag (Java port).
 *
 * <p><b>NOT LIVE-VERIFIED.</b> Same honest caveat the Python
 * {@code PineconeBackend} carries: no Pinecone account was available, and
 * unlike Qdrant/Weaviate there is no self-hosted or emulator option -
 * Pinecone is managed-only. Grounded in Pinecone's official REST API
 * reference (fetched and read before writing any code), and tested via a
 * local HTTP stub server rather than a real service.
 *
 * <p>Specifics of what was and was not confirmed against the docs:
 * <ul>
 *   <li><b>API version pinned to 2026-04</b> via the required
 *   {@code X-Pinecone-Api-Version} header. Pinecone's 2026-07 version
 *   introduced a schema-based Documents API and changed the create-index
 *   request; pinning keeps the classic dense-vector API this backend
 *   (and the Python source's {@code dimension}/{@code metric}/
 *   {@code ServerlessSpec} usage) is built around.</li>
 *   <li>Control plane ({@code GET/POST /indexes}, {@code GET /indexes/{name}})
 *   and data plane ({@code /vectors/upsert}, {@code /query}) shapes were
 *   confirmed on the 2026-04 reference pages. The create-index body was
 *   confirmed on the 2024/2025 reference pages and is assumed unchanged
 *   in 2026-04.</li>
 *   <li><b>Delete:</b> {@code POST /vectors/delete} with {@code {"ids":[...]}}
 *   follows an example found on a 2024-07 documentation page; the 2026-04
 *   delete reference page was not read. The likeliest place for a real
 *   instance to disagree with this class.</li>
 *   <li><b>Score normalization.</b> With {@code metric: cosine}, Pinecone's
 *   own explainer documents cosine similarity as ranging over [-1, 1], and
 *   an independent walkthrough shows an orthogonal vector scoring 0.0 - i.e.
 *   raw cosine, not a [0, 1] score. Normalized here via {@code (x + 1) / 2},
 *   matching QdrantBackend/MilvusBackend. The Python PineconeBackend
 *   returned the raw score unnormalized until the fix for issue 577 (merged
 *   to Python main, not yet released), the same inconsistency Qdrant and
 *   Milvus had. This specific claim was not verified on Pinecone's Query API
 *   reference page or against a live index.</li>
 * </ul>
 *
 * <p>Design notes matching the Python source: persistDirectory and an API
 * key are required (Pinecone vectors persist remotely regardless of local
 * process state, so a persistent SQLite sidecar for chunk text is
 * mandatory); vector IDs are {@code documentId:chunkIndex} used directly
 * (Pinecone accepts arbitrary string IDs); caller metadata is stored
 * natively in Pinecone so {@code metadataFilter} is a real native filter
 * ({@code {"key": {"$eq": value}}}), not a post-filter; serverless indexes
 * are polled for readiness after creation. {@code supportsSparse()} is
 * {@code false}.
 *
 * <p>Uses {@link java.net.http.HttpClient} directly - no new Maven
 * dependency, same approach as the other REST-based backends.
 */
public class PineconeBackend implements VectorBackend, AutoCloseable {

    private static final Logger LOG = Logger.getLogger(PineconeBackend.class.getName());
    private static final ObjectMapper MAPPER = new ObjectMapper();

    static final String API_VERSION = "2026-04";
    static final String DEFAULT_CONTROL_PLANE_URL = "https://api.pinecone.io";

    private final String apiKey;
    private final String indexName;
    private final String cloud;
    private final String region;
    private final String controlPlaneUrl;
    private final long readyPollIntervalMillis;
    private final long readyTimeoutMillis;
    private final Connection conn;
    private final HttpClient http;
    private final ReentrantLock lock = new ReentrantLock();
    private Integer dimensions;
    private String dataPlaneUrl;

    public PineconeBackend(String persistDirectory, String apiKey, String indexName,
                           String cloud, String region) throws SQLException {
        this(persistDirectory, apiKey, indexName, cloud, region, DEFAULT_CONTROL_PLANE_URL, 2000, 60000);
    }

    public PineconeBackend(String persistDirectory, String apiKey) throws SQLException {
        this(persistDirectory, apiKey, null, null, null);
    }

    /** Package-private: lets tests point the control plane at a local stub and shrink the readiness poll. */
    PineconeBackend(String persistDirectory, String apiKey, String indexName, String cloud, String region,
                    String controlPlaneUrl, long readyPollIntervalMillis, long readyTimeoutMillis) throws SQLException {
        if (persistDirectory == null || persistDirectory.isBlank()) {
            throw new IllegalArgumentException(
                    "PineconeBackend requires persistDirectory - Pinecone vectors persist remotely "
                            + "regardless of local state; without a persistent SQLite sidecar for chunk "
                            + "text, a process restart would leave searchable vectors with no matching text.");
        }
        String resolvedKey = apiKey != null ? apiKey : System.getenv("PINECONE_API_KEY");
        if (resolvedKey == null || resolvedKey.isBlank()) {
            throw new IllegalArgumentException(
                    "No API key for PineconeBackend. Pass apiKey explicitly, or set PINECONE_API_KEY "
                            + "in your environment.");
        }
        this.apiKey = resolvedKey;
        this.indexName = indexName != null && !indexName.isBlank() ? indexName : "ragleap";
        this.cloud = cloud != null && !cloud.isBlank() ? cloud : "aws";
        this.region = region != null && !region.isBlank() ? region : "us-east-1";
        this.controlPlaneUrl = controlPlaneUrl;
        this.readyPollIntervalMillis = readyPollIntervalMillis;
        this.readyTimeoutMillis = readyTimeoutMillis;
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();

        File dir = new File(persistDirectory);
        if (!dir.exists() && !dir.mkdirs()) {
            throw new IllegalStateException("Could not create persist directory: " + persistDirectory);
        }
        Path sqliteFile = Paths.get(dir.getAbsolutePath(), "pinecone_meta.sqlite3");
        this.conn = DriverManager.getConnection("jdbc:sqlite:" + sqliteFile);
        try (Statement st = conn.createStatement()) {
            st.execute("CREATE TABLE IF NOT EXISTS documents (" +
                    "id TEXT PRIMARY KEY, filename TEXT NOT NULL, metadata TEXT NOT NULL, uploaded_at TEXT NOT NULL)");
            st.execute("CREATE TABLE IF NOT EXISTS chunks (" +
                    "vector_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, " +
                    "document_name TEXT NOT NULL, chunk_index INTEGER NOT NULL, " +
                    "text TEXT NOT NULL, token_count INTEGER, metadata TEXT NOT NULL)");
        }
    }

    // package-private for direct test verification, not part of the public API
    String vectorId(String documentId, int chunkIndex) {
        return documentId + ":" + chunkIndex;
    }

    // package-private for direct test verification, not part of the public API
    ObjectNode buildFilter(Map<String, Object> metadataFilter) {
        if (metadataFilter == null || metadataFilter.isEmpty()) return null;
        ObjectNode filter = MAPPER.createObjectNode();
        for (Map.Entry<String, Object> e : metadataFilter.entrySet()) {
            ObjectNode eq = MAPPER.createObjectNode();
            eq.putPOJO("$eq", e.getValue());
            filter.set(e.getKey(), eq);
        }
        return filter;
    }

    @Override
    public void initSchema(int dimensions) throws SQLException {
        this.dimensions = dimensions;

        JsonNode list = request(controlPlaneUrl, "GET", "/indexes", null);
        boolean exists = false;
        for (JsonNode idx : list.path("indexes")) {
            if (indexName.equals(idx.path("name").asText())) {
                exists = true;
                break;
            }
        }

        if (!exists) {
            ObjectNode serverless = MAPPER.createObjectNode();
            serverless.put("cloud", cloud);
            serverless.put("region", region);
            ObjectNode spec = MAPPER.createObjectNode();
            spec.set("serverless", serverless);

            ObjectNode body = MAPPER.createObjectNode();
            body.put("name", indexName);
            body.put("dimension", dimensions);
            body.put("metric", "cosine");
            body.set("spec", spec);
            request(controlPlaneUrl, "POST", "/indexes", body);

            // Serverless indexes are not instantly queryable - poll readiness.
            long deadline = System.currentTimeMillis() + readyTimeoutMillis;
            boolean ready = false;
            while (System.currentTimeMillis() < deadline) {
                JsonNode desc = request(controlPlaneUrl, "GET", "/indexes/" + indexName, null);
                if (desc.path("status").path("ready").asBoolean(false)) {
                    ready = true;
                    break;
                }
                try {
                    Thread.sleep(readyPollIntervalMillis);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    throw new SQLException("Interrupted while waiting for Pinecone index readiness", e);
                }
            }
            if (!ready) {
                LOG.warning("PineconeBackend: index '" + indexName + "' did not report ready within "
                        + readyTimeoutMillis + "ms - proceeding anyway, but early queries may fail.");
            }
        }

        JsonNode desc = request(controlPlaneUrl, "GET", "/indexes/" + indexName, null);
        String host = desc.path("host").asText("");
        if (host.isBlank()) {
            throw new SQLException("Pinecone describe-index response for '" + indexName + "' had no host");
        }
        this.dataPlaneUrl = host.startsWith("http://") || host.startsWith("https://") ? host : "https://" + host;
    }

    @Override
    public void insertDocument(String documentId, String filename, Map<String, Object> metadata) throws SQLException {
        String metaJson = toJson(metadata);
        String uploadedAt = OffsetDateTime.now(ZoneOffset.UTC).toString();
        try (PreparedStatement ps = conn.prepareStatement(
                "INSERT INTO documents (id, filename, metadata, uploaded_at) VALUES (?, ?, ?, ?)")) {
            ps.setString(1, documentId);
            ps.setString(2, filename);
            ps.setString(3, metaJson);
            ps.setString(4, uploadedAt);
            ps.executeUpdate();
        }
    }

    @Override
    public void insertChunk(String documentId, String documentName, int chunkIndex, String text,
                             Integer tokenCount, List<Double> embedding, Map<String, Object> metadata) throws SQLException {
        if (dataPlaneUrl == null) {
            throw new IllegalStateException("PineconeBackend.initSchema() must be called before insertChunk()");
        }
        String vectorId = vectorId(documentId, chunkIndex);
        String metaJson = toJson(metadata);

        lock.lock();
        try (PreparedStatement ps = conn.prepareStatement(
                "INSERT INTO chunks (vector_id, document_id, document_name, chunk_index, text, token_count, metadata) " +
                        "VALUES (?, ?, ?, ?, ?, ?, ?)")) {
            ps.setString(1, vectorId);
            ps.setString(2, documentId);
            ps.setString(3, documentName);
            ps.setInt(4, chunkIndex);
            ps.setString(5, text);
            if (tokenCount != null) {
                ps.setInt(6, tokenCount);
            } else {
                ps.setNull(6, Types.INTEGER);
            }
            ps.setString(7, metaJson);
            ps.executeUpdate();
        } finally {
            lock.unlock();
        }

        ObjectNode pineconeMetadata = MAPPER.createObjectNode();
        // Stored fields are written after the caller metadata, so caller metadata can never overwrite them.
        if (metadata != null) {
            for (Map.Entry<String, Object> e : metadata.entrySet()) {
                pineconeMetadata.putPOJO(e.getKey(), e.getValue());
            }
        }
        pineconeMetadata.put("document_id", documentId);
        pineconeMetadata.put("document_name", documentName);
        pineconeMetadata.put("chunk_index", chunkIndex);

        ArrayNode values = MAPPER.createArrayNode();
        for (Double d : embedding) values.add(d);

        ObjectNode vector = MAPPER.createObjectNode();
        vector.put("id", vectorId);
        vector.set("values", values);
        vector.set("metadata", pineconeMetadata);

        ArrayNode vectors = MAPPER.createArrayNode();
        vectors.add(vector);
        ObjectNode body = MAPPER.createObjectNode();
        body.set("vectors", vectors);

        request(dataPlaneUrl, "POST", "/vectors/upsert", body);
    }

    @Override
    public List<SearchResult> searchDense(List<Double> embedding, int topK, Map<String, Object> metadataFilter) throws SQLException {
        if (embedding == null || embedding.isEmpty() || dataPlaneUrl == null) {
            return List.of();
        }
        if (dimensions != null && embedding.size() != dimensions) {
            return List.of();
        }

        ArrayNode queryVector = MAPPER.createArrayNode();
        for (Double d : embedding) queryVector.add(d);

        ObjectNode body = MAPPER.createObjectNode();
        body.set("vector", queryVector);
        body.put("topK", topK);
        body.put("includeMetadata", false);
        ObjectNode filter = buildFilter(metadataFilter);
        if (filter != null) {
            body.set("filter", filter);
        }

        JsonNode response = request(dataPlaneUrl, "POST", "/query", body);

        List<SearchResult> results = new ArrayList<>();
        for (JsonNode match : response.path("matches")) {
            String vectorId = match.path("id").asText();
            double rawScore = match.path("score").asDouble();

            String documentId, documentName, text;
            int chunkIndex;
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT document_id, document_name, chunk_index, text FROM chunks WHERE vector_id = ?")) {
                ps.setString(1, vectorId);
                try (ResultSet rs = ps.executeQuery()) {
                    if (!rs.next()) continue; // orphaned vector, no matching local text row
                    documentId = rs.getString(1);
                    documentName = rs.getString(2);
                    chunkIndex = rs.getInt(3);
                    text = rs.getString(4);
                }
            }

            // Raw cosine in [-1, 1] -> [0, 1], matching the other backends.
            double similarityScore = Math.round(((rawScore + 1) / 2) * 10000.0) / 10000.0;
            results.add(new SearchResult(vectorId, text, similarityScore, documentId, documentName, chunkIndex));
        }
        return results;
    }

    @Override
    public boolean supportsSparse() {
        return false;
    }

    @Override
    public List<DocumentSummary> listDocuments(int limit, int offset) throws SQLException {
        List<DocumentSummary> out = new ArrayList<>();
        String sql = "SELECT d.id, d.filename, d.uploaded_at, d.metadata, COUNT(c.vector_id) AS chunk_count " +
                "FROM documents d LEFT JOIN chunks c ON c.document_id = d.id " +
                "GROUP BY d.id ORDER BY d.uploaded_at DESC LIMIT ? OFFSET ?";
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            ps.setInt(1, limit);
            ps.setInt(2, offset);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    out.add(new DocumentSummary(
                            rs.getString(1),
                            rs.getString(2),
                            OffsetDateTime.parse(rs.getString(3)),
                            fromJson(rs.getString(4)),
                            rs.getLong(5)
                    ));
                }
            }
        }
        return out;
    }

    @Override
    public boolean deleteDocument(String documentId) throws SQLException {
        lock.lock();
        try {
            List<String> vectorIds = new ArrayList<>();
            try (PreparedStatement ps = conn.prepareStatement("SELECT vector_id FROM chunks WHERE document_id = ?")) {
                ps.setString(1, documentId);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) vectorIds.add(rs.getString(1));
                }
            }

            if (!vectorIds.isEmpty() && dataPlaneUrl != null) {
                ArrayNode ids = MAPPER.createArrayNode();
                vectorIds.forEach(ids::add);
                ObjectNode body = MAPPER.createObjectNode();
                body.set("ids", ids);
                request(dataPlaneUrl, "POST", "/vectors/delete", body);
            }

            int deletedCount;
            try (PreparedStatement ps = conn.prepareStatement("DELETE FROM documents WHERE id = ?")) {
                ps.setString(1, documentId);
                deletedCount = ps.executeUpdate();
            }
            try (PreparedStatement ps = conn.prepareStatement("DELETE FROM chunks WHERE document_id = ?")) {
                ps.setString(1, documentId);
                ps.executeUpdate();
            }
            return deletedCount > 0;
        } finally {
            lock.unlock();
        }
    }

    @Override
    public Optional<String> getDocumentFilename(String documentId) throws SQLException {
        try (PreparedStatement ps = conn.prepareStatement("SELECT filename FROM documents WHERE id = ?")) {
            ps.setString(1, documentId);
            try (ResultSet rs = ps.executeQuery()) {
                return rs.next() ? Optional.of(rs.getString(1)) : Optional.empty();
            }
        }
    }

    @Override
    public void close() throws SQLException {
        conn.close();
    }

    // --- internals ---

    private JsonNode request(String baseUrl, String method, String path, JsonNode body) throws SQLException {
        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(URI.create(baseUrl + path))
                    .timeout(Duration.ofSeconds(30))
                    .header("Api-Key", apiKey)
                    .header("X-Pinecone-Api-Version", API_VERSION)
                    .header("Content-Type", "application/json")
                    .header("Accept", "application/json");
            HttpRequest.BodyPublisher publisher = body != null
                    ? HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))
                    : HttpRequest.BodyPublishers.noBody();
            builder.method(method, publisher);

            HttpResponse<String> response = http.send(builder.build(), HttpResponse.BodyHandlers.ofString());

            if (response.statusCode() >= 400) {
                throw new SQLException("Pinecone request failed: " + method + " " + path +
                        " -> HTTP " + response.statusCode() + ": " + response.body());
            }
            String text = response.body();
            if (text == null || text.isBlank()) {
                return MAPPER.createObjectNode();
            }
            return MAPPER.readTree(text);
        } catch (IOException | InterruptedException e) {
            if (e instanceof InterruptedException) Thread.currentThread().interrupt();
            throw new SQLException("Pinecone request failed: " + method + " " + path, e);
        }
    }

    private String toJson(Map<String, Object> map) {
        try {
            return MAPPER.writeValueAsString(map != null ? map : Map.of());
        } catch (Exception e) {
            throw new RuntimeException("Failed to serialize metadata", e);
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> fromJson(String json) {
        try {
            return MAPPER.readValue(json, Map.class);
        } catch (Exception e) {
            throw new RuntimeException("Failed to parse metadata", e);
        }
    }
}
