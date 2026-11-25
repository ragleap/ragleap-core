package com.ragleap.rag.vectorstore;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.io.File;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
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
import java.util.UUID;
import java.util.concurrent.locks.ReentrantLock;

/**
 * Qdrant-backed vector storage for ragleap-rag (Java port).
 *
 * <p>Talks to Qdrant's plain REST API via {@link java.net.http.HttpClient} -
 * deliberately not the official Java client, which is gRPC-based and a much
 * heavier dependency than this project favors. No new Maven dependency is
 * needed: Jackson is already transitively available (same as
 * {@link FaissBackend}'s metadata handling).
 *
 * <p>Design notes, matching the real Python {@code QdrantBackend}'s own
 * documented reasoning:
 * <ul>
 *   <li><b>persistDirectory and url are both required.</b> A remote Qdrant
 *   instance persists vectors regardless of local process state, so a
 *   persistent SQLite sidecar for chunk text is mandatory to avoid orphaned
 *   vectors after a restart. There is no sensible default server.</li>
 *   <li><b>Deterministic point IDs.</b> Qdrant's point ID type hint allows
 *   arbitrary strings, but its real runtime validation requires string IDs
 *   to parse as valid UUIDs. IDs are derived deterministically from
 *   {@code documentId:chunkIndex} via {@link UUID#nameUUIDFromBytes} (MD5 -
 *   Python's source uses uuid5/SHA-1; different algorithm, same property
 *   that matters here: deterministic, valid-UUID-shaped, and idempotent on
 *   re-insert. Each language backend manages its own Qdrant collection
 *   independently, so the two algorithms never need to agree with each
 *   other.</li>
 *   <li><b>Cosine similarity normalization.</b> Qdrant's COSINE distance
 *   returns raw cosine similarity in [-1, 1], not normalized to [0, 1] like
 *   pgvector's convention - a real bug already found and fixed in the
 *   Python source (and, before that, in the Python MilvusBackend for the
 *   identical inconsistency). Normalized here via the same
 *   {@code (x + 1) / 2} transform from the start, rather than rediscovering
 *   the bug independently.</li>
 *   <li>{@code supportsSparse()} is {@code false} - Qdrant natively supports
 *   sparse vectors and hybrid search, but implementing that is real future
 *   scope, not guessed at here.</li>
 * </ul>
 */
public class QdrantBackend implements VectorBackend, AutoCloseable {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final String url;
    private final String apiKey;
    private final String collectionName;
    private final Connection conn;
    private final HttpClient http;
    private final ReentrantLock lock = new ReentrantLock();
    private Integer dimensions;

    public QdrantBackend(String persistDirectory, String url, String apiKey, String collectionName) throws SQLException {
        if (persistDirectory == null || persistDirectory.isBlank()) {
            throw new IllegalArgumentException(
                    "QdrantBackend requires persistDirectory - a remote Qdrant instance persists "
                            + "vectors regardless of local process state, so a persistent SQLite sidecar "
                            + "for chunk text is mandatory to avoid orphaned vectors after a restart.");
        }

        this.url = url != null ? url : System.getenv("QDRANT_URL");
        if (this.url == null || this.url.isBlank()) {
            throw new IllegalArgumentException(
                    "No url for QdrantBackend. Pass url explicitly, or set QDRANT_URL in your "
                            + "environment - e.g. a Qdrant Cloud cluster URL or a self-hosted instance's address.");
        }
        this.apiKey = apiKey != null ? apiKey : System.getenv("QDRANT_API_KEY");
        this.collectionName = collectionName != null ? collectionName : "ragleap";
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();

        File dir = new File(persistDirectory);
        if (!dir.exists() && !dir.mkdirs()) {
            throw new IllegalStateException("Could not create persist directory: " + persistDirectory);
        }
        Path sqliteFile = Paths.get(dir.getAbsolutePath(), "qdrant_meta.sqlite3");
        this.conn = DriverManager.getConnection("jdbc:sqlite:" + sqliteFile);
        try (Statement st = conn.createStatement()) {
            st.execute("CREATE TABLE IF NOT EXISTS documents (" +
                    "id TEXT PRIMARY KEY, filename TEXT NOT NULL, metadata TEXT NOT NULL, uploaded_at TEXT NOT NULL)");
            st.execute("CREATE TABLE IF NOT EXISTS chunks (" +
                    "vector_key TEXT PRIMARY KEY, qdrant_id TEXT NOT NULL, document_id TEXT NOT NULL, " +
                    "document_name TEXT NOT NULL, chunk_index INTEGER NOT NULL, " +
                    "text TEXT NOT NULL, token_count INTEGER, metadata TEXT NOT NULL)");
        }
    }

    public QdrantBackend(String persistDirectory, String url) throws SQLException {
        this(persistDirectory, url, null, null);
    }

    private String vectorKey(String documentId, int chunkIndex) {
        return documentId + ":" + chunkIndex;
    }

    private String deterministicId(String vectorKey) {
        return UUID.nameUUIDFromBytes(vectorKey.getBytes(StandardCharsets.UTF_8)).toString();
    }

    @Override
    public void initSchema(int dimensions) throws SQLException {
        this.dimensions = dimensions;

        JsonNode existing = request("GET", "/collections/" + collectionName, null, true);
        if (existing == null) {
            ObjectNode vectors = MAPPER.createObjectNode();
            vectors.put("size", dimensions);
            vectors.put("distance", "Cosine");
            ObjectNode body = MAPPER.createObjectNode();
            body.set("vectors", vectors);
            request("PUT", "/collections/" + collectionName, body, false);
        }
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
        if (dimensions != null && embedding.size() != dimensions) {
            throw new SQLException("expected " + dimensions + " dimensions, not " + embedding.size());
        }
        String vectorKey = vectorKey(documentId, chunkIndex);
        String qdrantId = deterministicId(vectorKey);
        String metaJson = toJson(metadata);

        lock.lock();
        try (PreparedStatement ps = conn.prepareStatement(
                "INSERT INTO chunks (vector_key, qdrant_id, document_id, document_name, chunk_index, text, token_count, metadata) " +
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)")) {
            ps.setString(1, vectorKey);
            ps.setString(2, qdrantId);
            ps.setString(3, documentId);
            ps.setString(4, documentName);
            ps.setInt(5, chunkIndex);
            ps.setString(6, text);
            if (tokenCount != null) {
                ps.setInt(7, tokenCount);
            } else {
                ps.setNull(7, Types.INTEGER);
            }
            ps.setString(8, metaJson);
            ps.executeUpdate();
        } finally {
            lock.unlock();
        }

        ObjectNode payload = MAPPER.createObjectNode();
        // Stored fields are written after the caller metadata, so caller metadata can never overwrite them.
        if (metadata != null) {
            for (Map.Entry<String, Object> e : metadata.entrySet()) {
                payload.putPOJO(e.getKey(), e.getValue());
            }
        }
        payload.put("document_id", documentId);
        payload.put("document_name", documentName);
        payload.put("chunk_index", chunkIndex);

        ArrayNode vector = MAPPER.createArrayNode();
        for (Double d : embedding) vector.add(d);

        ObjectNode point = MAPPER.createObjectNode();
        point.put("id", qdrantId);
        point.set("vector", vector);
        point.set("payload", payload);

        ArrayNode points = MAPPER.createArrayNode();
        points.add(point);
        ObjectNode body = MAPPER.createObjectNode();
        body.set("points", points);

        try {
            request("PUT", "/collections/" + collectionName + "/points?wait=true", body, false);
        } catch (SQLException | RuntimeException e) {
            removeSidecarRow(vectorKey, e);
            throw e;
        }
    }

    /** Removes the local row of a chunk whose server write failed, so the same chunk can be retried. */
    private void removeSidecarRow(String vectorKey, Exception original) {
        lock.lock();
        try (PreparedStatement ps = conn.prepareStatement("DELETE FROM chunks WHERE vector_key = ?")) {
            ps.setString(1, vectorKey);
            ps.executeUpdate();
        } catch (SQLException cleanupError) {
            original.addSuppressed(cleanupError);
        } finally {
            lock.unlock();
        }
    }

    @Override
    public List<SearchResult> searchDense(List<Double> embedding, int topK, Map<String, Object> metadataFilter) throws SQLException {
        if (embedding == null || embedding.isEmpty()) {
            return List.of();
        }
        if (dimensions != null && embedding.size() != dimensions) {
            return List.of();
        }

        ArrayNode vector = MAPPER.createArrayNode();
        for (Double d : embedding) vector.add(d);

        ObjectNode body = MAPPER.createObjectNode();
        body.set("query", vector);
        body.put("limit", topK);
        body.put("with_payload", false);
        JsonNode filter = buildFilter(metadataFilter);
        if (filter != null) {
            body.set("filter", filter);
        }

        JsonNode response = request("POST", "/collections/" + collectionName + "/points/query", body, false);
        JsonNode points = response.path("result").path("points");

        List<SearchResult> results = new ArrayList<>();
        for (JsonNode point : points) {
            String qdrantId = point.path("id").asText();
            double rawScore = point.path("score").asDouble();

            String documentId, documentName, text;
            int chunkIndex;
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT document_id, document_name, chunk_index, text FROM chunks WHERE qdrant_id = ?")) {
                ps.setString(1, qdrantId);
                try (ResultSet rs = ps.executeQuery()) {
                    if (!rs.next()) continue; // orphaned point, no matching local text row
                    documentId = rs.getString(1);
                    documentName = rs.getString(2);
                    chunkIndex = rs.getInt(3);
                    text = rs.getString(4);
                }
            }

            // Qdrant's COSINE distance returns raw similarity in [-1, 1];
            // normalized to [0, 1] to match pgvector's convention.
            double similarityScore = Math.round(((rawScore + 1) / 2) * 10000.0) / 10000.0;
            results.add(new SearchResult(qdrantId, text, similarityScore, documentId, documentName, chunkIndex));
        }
        return results;
    }

    private JsonNode buildFilter(Map<String, Object> metadataFilter) {
        if (metadataFilter == null || metadataFilter.isEmpty()) return null;
        ArrayNode must = MAPPER.createArrayNode();
        for (Map.Entry<String, Object> e : metadataFilter.entrySet()) {
            ObjectNode match = MAPPER.createObjectNode();
            match.putPOJO("value", e.getValue());
            ObjectNode condition = MAPPER.createObjectNode();
            condition.put("key", e.getKey());
            condition.set("match", match);
            must.add(condition);
        }
        ObjectNode filter = MAPPER.createObjectNode();
        filter.set("must", must);
        return filter;
    }

    @Override
    public boolean supportsSparse() {
        return false;
    }

    @Override
    public List<DocumentSummary> listDocuments(int limit, int offset) throws SQLException {
        List<DocumentSummary> out = new ArrayList<>();
        String sql = "SELECT d.id, d.filename, d.uploaded_at, d.metadata, COUNT(c.vector_key) AS chunk_count " +
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
            List<String> qdrantIds = new ArrayList<>();
            try (PreparedStatement ps = conn.prepareStatement("SELECT qdrant_id FROM chunks WHERE document_id = ?")) {
                ps.setString(1, documentId);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) qdrantIds.add(rs.getString(1));
                }
            }

            if (!qdrantIds.isEmpty()) {
                ArrayNode idsNode = MAPPER.createArrayNode();
                qdrantIds.forEach(idsNode::add);
                ObjectNode body = MAPPER.createObjectNode();
                body.set("points", idsNode);
                request("POST", "/collections/" + collectionName + "/points/delete?wait=true", body, false);
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

    /**
     * Issues a request to the Qdrant REST API. When allowNotFound is true
     * and the server returns 404, returns null instead of throwing -
     * used by initSchema's collection-existence check.
     */
    private JsonNode request(String method, String path, JsonNode body, boolean allowNotFound) throws SQLException {
        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(URI.create(url + path))
                    .timeout(Duration.ofSeconds(30))
                    .header("Content-Type", "application/json");
            if (apiKey != null && !apiKey.isBlank()) {
                builder.header("api-key", apiKey);
            }
            HttpRequest.BodyPublisher publisher = body != null
                    ? HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))
                    : HttpRequest.BodyPublishers.noBody();
            builder.method(method, publisher);

            HttpResponse<String> response = http.send(builder.build(), HttpResponse.BodyHandlers.ofString());

            if (allowNotFound && response.statusCode() == 404) {
                return null;
            }
            if (response.statusCode() >= 400) {
                throw new SQLException("Qdrant request failed: " + method + " " + path +
                        " -> HTTP " + response.statusCode() + ": " + response.body());
            }
            return MAPPER.readTree(response.body());
        } catch (java.io.IOException | InterruptedException e) {
            if (e instanceof InterruptedException) Thread.currentThread().interrupt();
            throw new SQLException("Qdrant request failed: " + method + " " + path, e);
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
