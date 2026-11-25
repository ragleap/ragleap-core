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
 * Weaviate-backed vector storage for ragleap-rag (Java port).
 *
 * <p>Unlike the Python {@code WeaviateBackend} - which its own source
 * documents as "NOT LIVE-VERIFIED, no Weaviate instance was available to
 * test against during development" - this class and its test suite were
 * built directly against a real, running Weaviate instance from the start.
 * Every endpoint shape, status code, and the exact cosine-distance-to-
 * similarity conversion below were confirmed via live probes before any
 * code was written, not assumed from documentation.
 *
 * <p>Talks to Weaviate's plain REST/GraphQL API via
 * {@link java.net.http.HttpClient} - not the official Java client (if one
 * even fits this project's dependency philosophy), same reasoning as
 * {@link QdrantBackend}. No new Maven dependency needed.
 *
 * <p>Design notes, matching the real Python source's own reasoning where
 * it applies:
 * <ul>
 *   <li><b>persistDirectory and a URL are both required.</b> A remote
 *   Weaviate instance persists vectors regardless of local process state,
 *   so a persistent SQLite sidecar for chunk text is mandatory.</li>
 *   <li><b>Deterministic object IDs</b> via {@link UUID#nameUUIDFromBytes},
 *   derived from {@code documentId:chunkIndex} - Weaviate requires its own
 *   UUID as the object identifier, unlike Pinecone's free-form string IDs.
 *   Deterministic derivation means a document deleted and re-ingested gets
 *   the same Weaviate object ID back.</li>
 *   <li><b>Self-provided vectors</b> ({@code "vectorizer": "none"}) -
 *   ragleap-rag always brings its own embeddings; Weaviate's built-in
 *   vectorizer integrations are intentionally not used, matching this
 *   library's BYOK philosophy.</li>
 *   <li><b>Cosine distance to similarity.</b> Weaviate's GraphQL search
 *   returns {@code distance} (lower is more similar), not a similarity
 *   score. Live-verified: an identical vector returns distance 0, an
 *   orthogonal one returns distance 1 - confirming
 *   {@code similarity = 1 - distance / 2} (the [0, 1] scale of the other backends) for the class's cosine
 *   distance metric, which is set explicitly at schema-creation time
 *   here rather than relying on Weaviate's (also-confirmed) default.</li>
 *   <li>{@code supportsSparse()} is {@code false} - Weaviate natively
 *   supports BM25/hybrid search, but implementing that is real future
 *   scope, not guessed at here.</li>
 * </ul>
 */
public class WeaviateBackend implements VectorBackend, AutoCloseable {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final String url;
    private final String apiKey;
    private final String collectionName;
    private final Connection conn;
    private final HttpClient http;
    private final ReentrantLock lock = new ReentrantLock();
    private Integer dimensions;
    /** Property names known to exist on the class (Weaviate creates a property when the first object that has it is stored). */
    private final java.util.Set<String> knownProperties = java.util.concurrent.ConcurrentHashMap.newKeySet();

    public WeaviateBackend(String persistDirectory, String url, String apiKey, String collectionName) throws SQLException {
        if (persistDirectory == null || persistDirectory.isBlank()) {
            throw new IllegalArgumentException(
                    "WeaviateBackend requires persistDirectory - a remote Weaviate instance persists "
                            + "vectors regardless of local process state, so a persistent SQLite sidecar "
                            + "for chunk text is mandatory to avoid orphaned vectors after a restart.");
        }

        String resolvedUrl = url != null ? url : System.getenv("WEAVIATE_URL");
        if (resolvedUrl == null || resolvedUrl.isBlank()) {
            throw new IllegalArgumentException(
                    "No url for WeaviateBackend. Pass url explicitly (a Weaviate Cloud cluster URL or a "
                            + "self-hosted instance's address), or set WEAVIATE_URL in your environment.");
        }
        this.url = resolvedUrl;
        this.apiKey = apiKey != null ? apiKey : System.getenv("WEAVIATE_API_KEY");

        // Weaviate class names must start with an uppercase letter -
        // normalize rather than fail confusingly later, matching the
        // Python source's own convention.
        String name = collectionName != null && !collectionName.isBlank() ? collectionName : "RagleapChunk";
        this.collectionName = Character.toUpperCase(name.charAt(0)) + name.substring(1);

        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();

        File dir = new File(persistDirectory);
        if (!dir.exists() && !dir.mkdirs()) {
            throw new IllegalStateException("Could not create persist directory: " + persistDirectory);
        }
        Path sqliteFile = Paths.get(dir.getAbsolutePath(), "weaviate_meta.sqlite3");
        this.conn = DriverManager.getConnection("jdbc:sqlite:" + sqliteFile);
        try (Statement st = conn.createStatement()) {
            st.execute("CREATE TABLE IF NOT EXISTS documents (" +
                    "id TEXT PRIMARY KEY, filename TEXT NOT NULL, metadata TEXT NOT NULL, uploaded_at TEXT NOT NULL)");
            st.execute("CREATE TABLE IF NOT EXISTS chunks (" +
                    "vector_key TEXT PRIMARY KEY, weaviate_uuid TEXT NOT NULL, document_id TEXT NOT NULL, " +
                    "document_name TEXT NOT NULL, chunk_index INTEGER NOT NULL, " +
                    "text TEXT NOT NULL, token_count INTEGER, metadata TEXT NOT NULL)");
        }
    }

    public WeaviateBackend(String persistDirectory, String url) throws SQLException {
        this(persistDirectory, url, null, null);
    }

    /**
     * Package-private, for test verification of the uppercase-first-letter
     * normalization applied in the constructor - not part of the public
     * VectorBackend contract.
     */
    String getCollectionName() {
        return collectionName;
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
        knownProperties.clear();

        JsonNode existing = request("GET", "/v1/schema/" + collectionName, null, true);
        if (existing == null) {
            ObjectNode vectorIndexConfig = MAPPER.createObjectNode();
            vectorIndexConfig.put("distance", "cosine");

            ObjectNode body = MAPPER.createObjectNode();
            body.put("class", collectionName);
            body.put("vectorizer", "none");
            body.set("vectorIndexConfig", vectorIndexConfig);
            request("POST", "/v1/schema", body, false);
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
        String weaviateUuid = deterministicId(vectorKey);
        String metaJson = toJson(metadata);

        lock.lock();
        try (PreparedStatement ps = conn.prepareStatement(
                "INSERT INTO chunks (vector_key, weaviate_uuid, document_id, document_name, chunk_index, text, token_count, metadata) " +
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)")) {
            ps.setString(1, vectorKey);
            ps.setString(2, weaviateUuid);
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

        ObjectNode properties = MAPPER.createObjectNode();
        // Stored fields are written after the caller metadata, so caller metadata can never overwrite them.
        if (metadata != null) {
            for (Map.Entry<String, Object> e : metadata.entrySet()) {
                properties.putPOJO(e.getKey(), e.getValue());
            }
        }
        properties.put("document_id", documentId);
        properties.put("document_name", documentName);
        properties.put("chunk_index", chunkIndex);

        ArrayNode vector = MAPPER.createArrayNode();
        for (Double d : embedding) vector.add(d);

        ObjectNode body = MAPPER.createObjectNode();
        body.put("class", collectionName);
        body.put("id", weaviateUuid);
        body.set("properties", properties);
        body.set("vector", vector);

        try {
            request("POST", "/v1/objects", body, false);
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

        List<String> needed = new ArrayList<>(List.of("document_id", "document_name", "chunk_index"));
        if (metadataFilter != null) {
            needed.addAll(metadataFilter.keySet());
        }
        if (!propertiesExist(needed)) {
            return List.of(); // nothing stored yet that could match
        }

        StringBuilder vectorLiteral = new StringBuilder("[");
        for (int i = 0; i < embedding.size(); i++) {
            if (i > 0) vectorLiteral.append(",");
            vectorLiteral.append(embedding.get(i));
        }
        vectorLiteral.append("]");

        String whereClause = buildWhereClause(metadataFilter);
        String query = String.format(
                "{ Get { %s(nearVector: {vector: %s}, limit: %d%s) { document_id document_name chunk_index _additional { id distance } } } }",
                collectionName, vectorLiteral, topK, whereClause
        );

        ObjectNode body = MAPPER.createObjectNode();
        body.put("query", query);

        JsonNode response = request("POST", "/v1/graphql", body, false);
        if (response.has("errors") && !response.get("errors").isEmpty()) {
            throw new SQLException("Weaviate GraphQL query returned errors: " + response.get("errors"));
        }

        JsonNode objects = response.path("data").path("Get").path(collectionName);

        List<SearchResult> results = new ArrayList<>();
        for (JsonNode obj : objects) {
            String weaviateUuid = obj.path("_additional").path("id").asText();
            double distance = obj.path("_additional").path("distance").asDouble();

            String documentId, documentName, text;
            int chunkIndex;
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT document_id, document_name, chunk_index, text FROM chunks WHERE weaviate_uuid = ?")) {
                ps.setString(1, weaviateUuid);
                try (ResultSet rs = ps.executeQuery()) {
                    if (!rs.next()) continue; // orphaned object, no matching local text row
                    documentId = rs.getString(1);
                    documentName = rs.getString(2);
                    chunkIndex = rs.getInt(3);
                    text = rs.getString(4);
                }
            }

            // Weaviate's cosine distance is in [0, 2]: 0 identical, 1 orthogonal, 2 opposite
            // (live-verified). It is normalized with 1 - distance / 2 so scores sit on the same
            // [0, 1] scale as pgvector and Qdrant: 1.0 identical, 0.5 orthogonal, 0.0 opposite.
            // The earlier 1 - distance returned raw cosine in [-1, 1]; found by the
            // cross-backend benchmark (issue 565).
            double similarityScore = Math.round((1.0 - distance / 2.0) * 10000.0) / 10000.0;
            results.add(new SearchResult(weaviateUuid, text, similarityScore, documentId, documentName, chunkIndex));
        }
        return results;
    }

    /**
     * True when every named property exists on the class. Weaviate creates a property only when the
     * first object that has it is stored, so a search on a new collection (or a filter on a key no
     * chunk has) would otherwise fail with a GraphQL error instead of returning no results. The
     * schema is read only when a name is not already known.
     */
    private boolean propertiesExist(java.util.Collection<String> needed) throws SQLException {
        if (knownProperties.containsAll(needed)) {
            return true;
        }
        JsonNode schema = request("GET", "/v1/schema/" + collectionName, null, true);
        if (schema == null) {
            return true; // class not found: let the query itself report that
        }
        JsonNode props = schema.path("properties");
        if (props.isArray()) {
            for (JsonNode p : props) {
                knownProperties.add(p.path("name").asText());
            }
        }
        return knownProperties.containsAll(needed);
    }

    private String buildWhereClause(Map<String, Object> metadataFilter) {
        if (metadataFilter == null || metadataFilter.isEmpty()) return "";

        List<String> operands = new ArrayList<>();
        for (Map.Entry<String, Object> e : metadataFilter.entrySet()) {
            operands.add(String.format(
                    "{path: [\"%s\"], operator: Equal, %s}",
                    escapeGraphQlString(e.getKey()), graphQlValueField(e.getValue())
            ));
        }

        String where;
        if (operands.size() == 1) {
            where = operands.get(0);
        } else {
            where = "{operator: And, operands: [" + String.join(", ", operands) + "]}";
        }
        return ", where: " + where;
    }

    private String graphQlValueField(Object value) {
        if (value instanceof Boolean b) {
            return "valueBoolean: " + b;
        } else if (value instanceof Integer || value instanceof Long) {
            return "valueInt: " + value;
        } else if (value instanceof Double || value instanceof Float) {
            return "valueNumber: " + value;
        } else {
            return "valueText: \"" + escapeGraphQlString(String.valueOf(value)) + "\"";
        }
    }

    private String escapeGraphQlString(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
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
            List<String> weaviateUuids = new ArrayList<>();
            try (PreparedStatement ps = conn.prepareStatement("SELECT weaviate_uuid FROM chunks WHERE document_id = ?")) {
                ps.setString(1, documentId);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) weaviateUuids.add(rs.getString(1));
                }
            }

            for (String uuid : weaviateUuids) {
                request("DELETE", "/v1/objects/" + collectionName + "/" + uuid, null, true);
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
     * Issues a request against Weaviate's REST/GraphQL API. When
     * allowNotFound is true, a 404 (schema-existence check) or empty 204
     * (object delete) response returns null instead of throwing.
     */
    private JsonNode request(String method, String path, JsonNode body, boolean allowNotFound) throws SQLException {
        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(URI.create(url + path))
                    .timeout(Duration.ofSeconds(30))
                    .header("Content-Type", "application/json");
            if (apiKey != null && !apiKey.isBlank()) {
                builder.header("Authorization", "Bearer " + apiKey);
            }
            HttpRequest.BodyPublisher publisher = body != null
                    ? HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body))
                    : HttpRequest.BodyPublishers.noBody();
            builder.method(method, publisher);

            HttpResponse<String> response = http.send(builder.build(), HttpResponse.BodyHandlers.ofString());

            if (allowNotFound && (response.statusCode() == 404 || response.statusCode() == 204)) {
                return null;
            }
            if (response.statusCode() >= 400) {
                throw new SQLException("Weaviate request failed: " + method + " " + path +
                        " -> HTTP " + response.statusCode() + ": " + response.body());
            }
            if (response.body() == null || response.body().isBlank()) {
                return null;
            }
            return MAPPER.readTree(response.body());
        } catch (IOException | InterruptedException e) {
            if (e instanceof InterruptedException) Thread.currentThread().interrupt();
            throw new SQLException("Weaviate request failed: " + method + " " + path, e);
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
