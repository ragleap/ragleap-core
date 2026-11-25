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

/**
 * Milvus-backed vector storage for ragleap-rag (Java port).
 *
 * <p><b>NOT LIVE-VERIFIED</b> - same honest caveat the Python
 * {@code MilvusBackend} carries: no Milvus or Zilliz Cloud instance was
 * available to test against. Self-hosting Milvus requires a three-service
 * stack (etcd, MinIO, milvus-standalone), which was judged too resource-
 * risky to stand up on this project's VPS given it was already showing
 * real CPU contention before this backend was written. Unlike the Python
 * source (introspected against an installed client package), this class
 * is grounded in Milvus's official RESTful API v2 reference documentation
 * (milvus.io/api-reference/restful/v2.4.x/), fetched and read directly
 * before writing any code - every endpoint path and request/response
 * shape below matches the documented API, not assumption. Treat as
 * best-effort until confirmed live, exactly as the Python source itself
 * says.
 *
 * <p>Talks to Milvus's plain RESTful API v2 via
 * {@link java.net.http.HttpClient} - not a gRPC client, same reasoning as
 * {@link QdrantBackend} and {@link WeaviateBackend}. No new Maven
 * dependency needed.
 *
 * <p>Design notes, matching the real Python source's own reasoning where
 * it applies:
 * <ul>
 *   <li><b>persistDirectory and a URI are both required.</b> A remote
 *   Milvus/Zilliz Cloud instance persists vectors regardless of local
 *   process state, so a persistent SQLite sidecar for chunk text is
 *   mandatory.</li>
 *   <li><b>No UUID indirection needed.</b> Unlike Pinecone/Qdrant/
 *   Weaviate, Milvus's {@code idType: "VarChar"} primary key accepts
 *   arbitrary string IDs directly - {@code documentId:chunkIndex} is used
 *   as-is as the Milvus primary key, exactly matching the Python source's
 *   own documented design choice.</li>
 *   <li><b>Cosine similarity normalization.</b> Milvus with
 *   {@code metricType: "COSINE"} returns raw cosine similarity in
 *   [-1, 1] per Milvus's own documentation (not a true distance - higher
 *   is already more similar). Normalized here via the same
 *   {@code (x + 1) / 2} transform already confirmed correct, live,
 *   against Qdrant's identical COSINE-metric behavior in this same
 *   codebase.</li>
 *   <li>{@code supportsSparse()} is {@code false} - Milvus supports
 *   sparse vectors/BM25/hybrid search, but implementing that is real
 *   future scope, not guessed at here.</li>
 * </ul>
 */
public class MilvusBackend implements VectorBackend, AutoCloseable {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final String uri;
    private final String token;
    private final String collectionName;
    private final Connection conn;
    private final HttpClient http;
    private final ReentrantLock lock = new ReentrantLock();
    private Integer dimensions;

    public MilvusBackend(String persistDirectory, String uri, String token, String collectionName) throws SQLException {
        if (persistDirectory == null || persistDirectory.isBlank()) {
            throw new IllegalArgumentException(
                    "MilvusBackend requires persistDirectory - a remote Milvus/Zilliz Cloud instance "
                            + "persists vectors regardless of local process state, so a persistent SQLite "
                            + "sidecar for chunk text is mandatory to avoid orphaned vectors after a restart.");
        }

        String resolvedUri = uri != null ? uri : System.getenv("MILVUS_URI");
        if (resolvedUri == null || resolvedUri.isBlank()) {
            throw new IllegalArgumentException(
                    "No uri for MilvusBackend. Pass uri explicitly (a Zilliz Cloud endpoint or a "
                            + "self-hosted Milvus instance's address), or set MILVUS_URI in your environment.");
        }
        this.uri = resolvedUri;
        this.token = token != null ? token : System.getenv("MILVUS_TOKEN");
        this.collectionName = collectionName != null && !collectionName.isBlank() ? collectionName : "ragleap";

        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();

        File dir = new File(persistDirectory);
        if (!dir.exists() && !dir.mkdirs()) {
            throw new IllegalStateException("Could not create persist directory: " + persistDirectory);
        }
        Path sqliteFile = Paths.get(dir.getAbsolutePath(), "milvus_meta.sqlite3");
        this.conn = DriverManager.getConnection("jdbc:sqlite:" + sqliteFile);
        try (Statement st = conn.createStatement()) {
            st.execute("CREATE TABLE IF NOT EXISTS documents (" +
                    "id TEXT PRIMARY KEY, filename TEXT NOT NULL, metadata TEXT NOT NULL, uploaded_at TEXT NOT NULL)");
            st.execute("CREATE TABLE IF NOT EXISTS chunks (" +
                    "vector_key TEXT PRIMARY KEY, document_id TEXT NOT NULL, " +
                    "document_name TEXT NOT NULL, chunk_index INTEGER NOT NULL, " +
                    "text TEXT NOT NULL, token_count INTEGER, metadata TEXT NOT NULL)");
        }
    }

    public MilvusBackend(String persistDirectory, String uri) throws SQLException {
        this(persistDirectory, uri, null, null);
    }

    // package-private for direct test verification, not part of the public API
    String vectorKey(String documentId, int chunkIndex) {
        return documentId + ":" + chunkIndex;
    }

    @Override
    public void initSchema(int dimensions) throws SQLException {
        this.dimensions = dimensions;

        ObjectNode hasBody = MAPPER.createObjectNode();
        hasBody.put("collectionName", collectionName);
        JsonNode hasResponse = request("/v2/vectordb/collections/has", hasBody);
        boolean exists = hasResponse.path("data").path("has").asBoolean(false);

        if (!exists) {
            ObjectNode params = MAPPER.createObjectNode();
            params.put("max_length", "512");

            ObjectNode createBody = MAPPER.createObjectNode();
            createBody.put("collectionName", collectionName);
            createBody.put("dimension", dimensions);
            createBody.put("idType", "VarChar");
            createBody.put("metricType", "COSINE");
            createBody.set("params", params);
            request("/v2/vectordb/collections/create", createBody);
        }
    }

    /**
     * Milvus filters are boolean expression strings, not a structured
     * object like the other backends - builds a simple AND-of-equalities
     * expression, matching the Python source's own approach exactly.
     */
    // package-private for direct test verification, not part of the public API
    String buildFilterExpr(Map<String, Object> metadataFilter) {
        if (metadataFilter == null || metadataFilter.isEmpty()) return "";
        List<String> parts = new ArrayList<>();
        for (Map.Entry<String, Object> e : metadataFilter.entrySet()) {
            String key = e.getKey();
            Object v = e.getValue();
            if (key == null || !key.matches("[A-Za-z_][A-Za-z0-9_]*")) {
                throw new IllegalArgumentException("Invalid metadata filter key: " + pyQuote(key));
            }
            String valueRepr;
            if (v instanceof Boolean b) {
                valueRepr = b ? "true" : "false";
            } else if (v instanceof String s) {
                valueRepr = "\"" + escapeForFilter(s) + "\"";
            } else if (v instanceof Integer || v instanceof Long || v instanceof Short || v instanceof Byte || v instanceof java.math.BigInteger) {
                valueRepr = String.valueOf(v);
            } else if ((v instanceof Double || v instanceof Float) && Double.isFinite(((Number) v).doubleValue())) {
                valueRepr = com.ragleap.rag.parsers.PyFloat.repr(v instanceof Float f ? Double.parseDouble(f.toString()) : (Double) v);
            } else {
                throw new IllegalArgumentException("Unsupported metadata filter value for " + pyQuote(key) + ": " + pyRepr(v));
            }
            parts.add(key + " == " + valueRepr);
        }
        return String.join(" and ", parts);
    }

    private String escapeForFilter(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\r", "\\r");
    }

    /** Python repr of a string (None for null), so error texts equal the Python ones. */
    private static String pyQuote(String s) {
        if (s == null) return "None";
        char q = (s.indexOf('\'') >= 0 && s.indexOf('"') < 0) ? '"' : '\'';
        StringBuilder sb = new StringBuilder().append(q);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == q || c == '\\') sb.append('\\').append(c);
            else if (c == '\n') sb.append("\\n");
            else if (c == '\r') sb.append("\\r");
            else if (c == '\t') sb.append("\\t");
            else if (c < 0x20 || c == 0x7f) sb.append(String.format("\\x%02x", (int) c));
            else sb.append(c);
        }
        return sb.append(q).toString();
    }

    /** Python repr of the few value types a caller can pass, for identical error texts. */
    private static String pyRepr(Object v) {
        if (v == null) return "None";
        if (v instanceof String s) return pyQuote(s);
        if (v instanceof Double || v instanceof Float) {
            return com.ragleap.rag.parsers.PyFloat.repr(v instanceof Float f ? Double.parseDouble(f.toString()) : (Double) v);
        }
        if (v instanceof List<?> l) {
            List<String> items = new ArrayList<>();
            for (Object o : l) items.add(pyRepr(o));
            return "[" + String.join(", ", items) + "]";
        }
        if (v instanceof Map<?, ?> m) {
            List<String> items = new ArrayList<>();
            for (Map.Entry<?, ?> e : m.entrySet()) items.add(pyRepr(e.getKey()) + ": " + pyRepr(e.getValue()));
            return "{" + String.join(", ", items) + "}";
        }
        return String.valueOf(v);
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
        String vectorKey = vectorKey(documentId, chunkIndex);
        String metaJson = toJson(metadata);

        lock.lock();
        try (PreparedStatement ps = conn.prepareStatement(
                "INSERT INTO chunks (vector_key, document_id, document_name, chunk_index, text, token_count, metadata) " +
                        "VALUES (?, ?, ?, ?, ?, ?, ?)")) {
            ps.setString(1, vectorKey);
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

        ObjectNode row = MAPPER.createObjectNode();
        // Stored fields are written after the caller metadata, so caller metadata can never overwrite them.
        if (metadata != null) {
            for (Map.Entry<String, Object> e : metadata.entrySet()) {
                row.putPOJO(e.getKey(), e.getValue());
            }
        }
        row.put("id", vectorKey);
        ArrayNode vector = MAPPER.createArrayNode();
        for (Double d : embedding) vector.add(d);
        row.set("vector", vector);
        row.put("document_id", documentId);
        row.put("document_name", documentName);
        row.put("chunk_index", chunkIndex);

        ArrayNode data = MAPPER.createArrayNode();
        data.add(row);

        ObjectNode body = MAPPER.createObjectNode();
        body.put("collectionName", collectionName);
        body.set("data", data);

        request("/v2/vectordb/entities/insert", body);
    }

    @Override
    public List<SearchResult> searchDense(List<Double> embedding, int topK, Map<String, Object> metadataFilter) throws SQLException {
        if (embedding == null || embedding.isEmpty()) {
            return List.of();
        }
        if (dimensions != null && embedding.size() != dimensions) {
            return List.of();
        }

        ArrayNode queryVector = MAPPER.createArrayNode();
        for (Double d : embedding) queryVector.add(d);
        ArrayNode data = MAPPER.createArrayNode();
        data.add(queryVector);

        ObjectNode body = MAPPER.createObjectNode();
        body.put("collectionName", collectionName);
        body.set("data", data);
        body.put("limit", topK);
        String filterExpr = buildFilterExpr(metadataFilter);
        if (!filterExpr.isEmpty()) {
            body.put("filter", filterExpr);
        }

        JsonNode response = request("/v2/vectordb/entities/search", body);
        JsonNode hits = response.path("data");

        List<SearchResult> results = new ArrayList<>();
        for (JsonNode hit : hits) {
            String vectorKey = hit.path("id").asText();
            double distance = hit.path("distance").asDouble();

            String documentId, documentName, text;
            int chunkIndex;
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT document_id, document_name, chunk_index, text FROM chunks WHERE vector_key = ?")) {
                ps.setString(1, vectorKey);
                try (ResultSet rs = ps.executeQuery()) {
                    if (!rs.next()) continue; // orphaned hit, no matching local text row
                    documentId = rs.getString(1);
                    documentName = rs.getString(2);
                    chunkIndex = rs.getInt(3);
                    text = rs.getString(4);
                }
            }

            // Milvus with metricType "COSINE" returns raw cosine
            // similarity in [-1, 1] per its own documentation (not a
            // true distance). Normalized to [0, 1] to match pgvector's
            // convention, same transform confirmed live against Qdrant's
            // identical COSINE-metric behavior elsewhere in this codebase.
            double similarityScore = Math.round(((distance + 1) / 2) * 10000.0) / 10000.0;
            results.add(new SearchResult(vectorKey, text, similarityScore, documentId, documentName, chunkIndex));
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
            List<String> vectorKeys = new ArrayList<>();
            try (PreparedStatement ps = conn.prepareStatement("SELECT vector_key FROM chunks WHERE document_id = ?")) {
                ps.setString(1, documentId);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) vectorKeys.add(rs.getString(1));
                }
            }

            if (!vectorKeys.isEmpty()) {
                StringBuilder idsLiteral = new StringBuilder("[");
                for (int i = 0; i < vectorKeys.size(); i++) {
                    if (i > 0) idsLiteral.append(",");
                    idsLiteral.append("\"").append(escapeForFilter(vectorKeys.get(i))).append("\"");
                }
                idsLiteral.append("]");

                ObjectNode body = MAPPER.createObjectNode();
                body.put("collectionName", collectionName);
                body.put("filter", "id in " + idsLiteral);
                request("/v2/vectordb/entities/delete", body);
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

    private JsonNode request(String path, JsonNode body) throws SQLException {
        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(URI.create(uri + path))
                    .timeout(Duration.ofSeconds(30))
                    .header("Content-Type", "application/json");
            if (token != null && !token.isBlank()) {
                builder.header("Authorization", "Bearer " + token);
            }
            builder.POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)));

            HttpResponse<String> response = http.send(builder.build(), HttpResponse.BodyHandlers.ofString());

            if (response.statusCode() >= 400) {
                throw new SQLException("Milvus request failed: POST " + path +
                        " -> HTTP " + response.statusCode() + ": " + response.body());
            }

            JsonNode parsed = MAPPER.readTree(response.body());
            // Milvus's own "code" field: 0 in older API versions, 200 in
            // newer ones - both documented as success across different
            // milvus.io RESTful API version pages, so both are accepted.
            int code = parsed.path("code").asInt(0);
            if (code != 0 && code != 200) {
                throw new SQLException("Milvus request failed: POST " + path +
                        " -> code " + code + ": " + parsed.path("message").asText(""));
            }
            return parsed;
        } catch (IOException | InterruptedException e) {
            if (e instanceof InterruptedException) Thread.currentThread().interrupt();
            throw new SQLException("Milvus request failed: POST " + path, e);
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
