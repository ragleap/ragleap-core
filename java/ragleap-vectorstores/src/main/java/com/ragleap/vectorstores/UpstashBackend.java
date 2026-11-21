package com.ragleap.vectorstores;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.ragleap.rag.vectorstore.DocumentSummary;
import com.ragleap.rag.vectorstore.SearchResult;
import com.ragleap.rag.vectorstore.VectorBackend;
import java.io.IOException;
import java.math.BigInteger;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Duration;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.StringJoiner;
import java.util.concurrent.locks.ReentrantLock;

/**
 * Java port of the Python UpstashBackend (ragleap-vectorstores), talking to an existing Upstash Vector index over
 * its REST API with {@link java.net.http.HttpClient} and Jackson (no vendor SDK). Chunk text and metadata live in
 * Upstash; the document registry (filename, upload time) is a small local SQLite file, as in Python. The index must
 * already exist as a pure dense index; {@link #initSchema} only verifies it.
 *
 * <p>Verified only by recorded-request parity against the real Python backend and the real upstash-vector 0.8.0
 * SDK. NOT live-verified against a real Upstash index.
 *
 * <p>Differences from Python: no retry on connection failures (the Python SDK retries 3 times, 1 s apart); a 30 s
 * request timeout (SDK: 600 s); the url is used exactly as given (like the SDK: no scheme is added); a namespace is
 * sent percent-encoded (the SDK leaves a few characters raw that java.net.URI cannot send: invalid percent escapes,
 * square brackets, | and ^); a filter value that is a list or map throws IllegalArgumentException; only the info
 * fields that are needed are read; a missing or non-numeric chunk_index comes back as 0 (SearchResult takes an
 * int); failures throw SQLException and an index that is not a pure dense index of the requested dimension throws
 * IllegalStateException (Python: UpstashError and RuntimeError). Like the SDK, the HTTP status is ignored and the
 * response body decides.
 */
public final class UpstashBackend implements VectorBackend, AutoCloseable {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String MISSING = "UpstashBackend requires both url= and token= - get these from the Upstash "
            + "console (Vector > your index > REST API). The index itself must already exist (created via the console) "
            + "as a pure dense index; UpstashBackend cannot create one.";
    private static final char[] HEX = "0123456789ABCDEF".toCharArray();

    private final String url;
    private final String token;
    private final String namespace;
    private final HttpClient http;
    private final Connection conn;
    private final ReentrantLock lock = new ReentrantLock();
    private volatile boolean initialized;

    public UpstashBackend(String url, String token) throws SQLException {
        this(url, token, "", null, null);
    }

    public UpstashBackend(String url, String token, String namespace, String registryPath) throws SQLException {
        this(url, token, namespace, registryPath, null);
    }

    public UpstashBackend(String url, String token, String namespace, String registryPath, HttpClient httpClient)
            throws SQLException {
        if (url == null || url.isEmpty() || token == null || token.isEmpty()) {
            throw new IllegalArgumentException(MISSING);
        }
        this.url = url;
        this.token = token;
        this.namespace = namespace == null ? "" : namespace;
        this.http = httpClient != null ? httpClient : HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1).connectTimeout(Duration.ofSeconds(10)).build();
        Path file = registryPath != null && !registryPath.isEmpty() ? Path.of(registryPath)
                : Path.of(System.getProperty("user.home"), ".ragleap_vectorstores", "upstash_documents.sqlite3");
        try {
            Path parent = file.toAbsolutePath().getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
        } catch (IOException e) {
            throw new SQLException("Could not create the registry directory for " + file, e);
        }
        this.conn = DriverManager.getConnection("jdbc:sqlite:" + file.toAbsolutePath());
        try (Statement st = conn.createStatement()) {
            st.execute("CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, filename TEXT NOT NULL, "
                    + "metadata TEXT NOT NULL, uploaded_at TEXT NOT NULL)");
        }
    }

    @Override
    public void close() throws SQLException {
        conn.close();
    }

    // ---- helpers that copy Python behaviour ----

    /** datetime.isoformat() of a naive UTC datetime: microseconds omitted when exactly zero. */
    static String pyIsoNaive(OffsetDateTime t) {
        String s = OpenSearchBackend.pyIso(t.withOffsetSameInstant(ZoneOffset.UTC));
        return s.substring(0, s.length() - 6);
    }

    /** The Upstash filter string: key = value clauses joined with AND, strings single-quoted with quotes doubled. */
    static String buildFilter(Map<String, Object> filter) {
        if (filter == null || filter.isEmpty()) {
            return "";
        }
        for (String key : filter.keySet()) {
            if (key == null || !key.matches("[A-Za-z_][A-Za-z0-9_.]*")) {
                throw new IllegalArgumentException("Invalid metadata filter key: " + (key == null ? "None" : PyRepr.str(key)));
            }
        }
        StringJoiner j = new StringJoiner(" AND ");
        for (Map.Entry<String, Object> e : filter.entrySet()) {
            j.add(e.getKey() + " = " + filterValue(e.getValue()));
        }
        return j.toString();
    }

    static String filterValue(Object v) {
        if (v instanceof Boolean b) {
            return b ? "true" : "false";
        }
        if (v instanceof Double || v instanceof Float) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d)) {
                return "nan";
            }
            if (Double.isInfinite(d)) {
                return d > 0 ? "inf" : "-inf";
            }
            return PyJson.floatRepr(d);
        }
        if (v instanceof Integer || v instanceof Long || v instanceof Short || v instanceof Byte
                || v instanceof BigInteger) {
            return v.toString();
        }
        if (v == null) {
            return "'None'";
        }
        if (v instanceof String s) {
            return "'" + s.replace("'", "''") + "'";
        }
        throw new IllegalArgumentException("unsupported filter value type: " + v.getClass().getSimpleName());
    }

    private static boolean hex(char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
    }

    /** Percent-encodes a namespace for the URL path (slashes stay; valid %XX escapes stay; the rest is encoded). */
    static String encodePath(String s) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); ) {
            int cp = s.codePointAt(i);
            int len = Character.charCount(cp);
            if (cp < 128 && ((cp >= 'a' && cp <= 'z') || (cp >= 'A' && cp <= 'Z') || (cp >= '0' && cp <= '9')
                    || "-._~/!$&'()*+,;=:@".indexOf(cp) >= 0)) {
                sb.append((char) cp);
            } else if (cp == '%' && i + 2 < s.length() && hex(s.charAt(i + 1)) && hex(s.charAt(i + 2))) {
                sb.append('%');
            } else {
                for (byte b : new String(Character.toChars(cp)).getBytes(StandardCharsets.UTF_8)) {
                    int c = b & 0xff;
                    sb.append('%').append(HEX[c >> 4]).append(HEX[c & 15]);
                }
            }
            i += len;
        }
        return sb.toString();
    }

    private String nsPath(String path) {
        return namespace.isEmpty() ? path : path + "/" + encodePath(namespace);
    }

    // ---- HTTP ----

    private JsonNode post(String path, JsonNode body, boolean nullResultOk) throws SQLException {
        try {
            HttpRequest.Builder b = HttpRequest.newBuilder()
                    .uri(URI.create(url + path))
                    .timeout(Duration.ofSeconds(30))
                    .header("Authorization", "Bearer " + token)
                    .header("Content-Type", "application/json");
            b.POST(body == null ? HttpRequest.BodyPublishers.noBody()
                    : HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)));
            HttpResponse<String> r = http.send(b.build(), HttpResponse.BodyHandlers.ofString());
            JsonNode n;
            try {
                n = MAPPER.readTree(r.body());
            } catch (JsonProcessingException e) {
                throw new SQLException("Upstash returned invalid JSON for POST " + path, e);
            }
            if (n == null || n.isMissingNode() || !n.isObject()) {
                throw new SQLException("Upstash returned no JSON object for POST " + path + " (HTTP " + r.statusCode() + ")");
            }
            if (n.has("error")) {
                throw new SQLException("Upstash error for POST " + path + ": " + n.get("error").asText());
            }
            if (!n.has("result")) {
                throw new SQLException("Upstash response without result for POST " + path);
            }
            JsonNode res = n.get("result");
            if (res.isNull() && !nullResultOk) {
                throw new SQLException("Upstash returned a null result for POST " + path);
            }
            return res;
        } catch (IOException | IllegalArgumentException e) {
            throw new SQLException("Upstash request failed: POST " + path, e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new SQLException("Upstash request interrupted: POST " + path, e);
        }
    }

    private static boolean present(JsonNode n) {
        return n != null && !n.isNull() && !(n.isObject() && n.isEmpty());
    }

    // ---- VectorBackend ----

    @Override
    public void initSchema(int dimensions) throws SQLException {
        // Python assigns the index before calling info(), so even a failed check leaves the backend usable.
        initialized = true;
        JsonNode info = post("/info", null, false);
        if (present(info.get("sparseIndex")) || !present(info.get("denseIndex"))) {
            throw new IllegalStateException("UpstashBackend: index at " + url + " is not a pure dense index (it's "
                    + "configured for hybrid/sparse vectors) - live-verified via info(), not assumed. Create a new "
                    + "index in the Upstash console with type 'Dense' instead.");
        }
        JsonNode dim = info.get("dimension");
        if (dim == null) {
            throw new SQLException("Upstash info response without dimension");
        }
        if (!(dim.isNumber() && dim.doubleValue() == dimensions)) {
            throw new IllegalStateException("UpstashBackend: index at " + url + " has dimension " + dim.asText()
                    + ", but " + dimensions + " was requested. The index's dimension is fixed at creation time in the "
                    + "Upstash console and cannot be changed by this backend.");
        }
    }

    @Override
    public void insertDocument(String documentId, String filename, Map<String, Object> metadata) throws SQLException {
        lock.lock();
        try (PreparedStatement ps = conn.prepareStatement(
                "INSERT INTO documents (id, filename, metadata, uploaded_at) VALUES (?, ?, ?, ?)")) {
            ps.setString(1, documentId);
            ps.setString(2, filename);
            ps.setString(3, PyJson.dumps(metadata == null ? Map.of() : metadata));
            ps.setString(4, pyIsoNaive(OffsetDateTime.now(ZoneOffset.UTC)));
            ps.executeUpdate();
        } finally {
            lock.unlock();
        }
    }

    @Override
    public void insertChunk(String documentId, String documentName, int chunkIndex, String text, Integer tokenCount,
                            List<Double> embedding, Map<String, Object> metadata) throws SQLException {
        if (!initialized) {
            throw new IllegalStateException("UpstashBackend: call initSchema() before insertChunk()");
        }
        Map<String, Object> merged = new LinkedHashMap<>();
        merged.put("document_id", documentId);
        merged.put("document_name", documentName);
        merged.put("chunk_index", chunkIndex);
        merged.put("token_count", tokenCount == null ? 0 : tokenCount);
        merged.put("text", text);
        if (metadata != null) {
            for (Map.Entry<String, Object> e : metadata.entrySet()) {
                if (!merged.containsKey(e.getKey())) {   // stored fields win, like the fixed Python's setdefault
                    merged.put(e.getKey(), e.getValue());
                }
            }
        }
        var arr = MAPPER.createArrayNode();
        ObjectNode v = arr.addObject();
        v.put("id", documentId + ":" + chunkIndex);
        v.set("vector", MAPPER.valueToTree(embedding));
        v.putNull("sparseVector");
        v.set("metadata", MAPPER.valueToTree(merged));
        v.putNull("data");
        lock.lock();
        try {
            post(nsPath("/upsert"), arr, true);
        } finally {
            lock.unlock();
        }
    }

    private static String textOrNull(JsonNode n) {
        return n == null || n.isNull() ? null : n.asText();
    }

    private static int chunkIndexOf(JsonNode meta) {
        JsonNode n = meta.get("chunk_index");
        if (n == null || n.isNull()) {
            return 0;
        }
        if (n.isNumber()) {
            return n.intValue();
        }
        if (n.isTextual()) {
            try {
                return OpenSearchBackend.pyInt(n.asText()).intValue();
            } catch (NumberFormatException e) {
                return 0;
            }
        }
        return 0;
    }

    @Override
    public List<SearchResult> searchDense(List<Double> embedding, int topK, Map<String, Object> metadataFilter)
            throws SQLException {
        if (embedding == null || embedding.isEmpty() || !initialized) {
            return List.of();
        }
        ObjectNode q = MAPPER.createObjectNode();
        q.put("topK", topK);
        q.put("includeVectors", false);
        q.put("includeMetadata", true);
        q.put("includeData", false);
        q.put("filter", buildFilter(metadataFilter));
        q.set("vector", MAPPER.valueToTree(embedding));
        JsonNode res = post(nsPath("/query"), q, false);
        if (!res.isArray()) {
            throw new SQLException("Upstash query result is not a list");
        }
        List<SearchResult> out = new ArrayList<>();
        for (JsonNode o : res) {
            if (!o.isObject() || !o.has("id") || !o.has("score")) {
                throw new SQLException("Upstash query result without id or score");
            }
            JsonNode meta = o.get("metadata");
            if (meta == null || meta.isNull()) {
                meta = MAPPER.createObjectNode();
            } else if (!meta.isObject()) {
                throw new SQLException("Upstash query result with non-object metadata");
            }
            out.add(new SearchResult(o.get("id").asText(), textOrNull(meta.get("text")),
                    OpenSearchBackend.round4(o.get("score").asDouble()),
                    textOrNull(meta.get("document_id")), textOrNull(meta.get("document_name")), chunkIndexOf(meta)));
        }
        return out;
    }

    @Override
    public boolean supportsSparse() {
        return false;
    }

    private record Row(String id, String filename, String uploadedAt, String metadata) {
    }

    @Override
    public List<DocumentSummary> listDocuments(int limit, int offset) throws SQLException {
        List<Row> rows = new ArrayList<>();
        try (PreparedStatement ps = conn.prepareStatement("SELECT id, filename, uploaded_at, metadata FROM documents "
                + "ORDER BY uploaded_at DESC LIMIT ? OFFSET ?")) {
            ps.setInt(1, limit);
            ps.setInt(2, offset);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    rows.add(new Row(rs.getString(1), rs.getString(2), rs.getString(3), rs.getString(4)));
                }
            }
        }
        List<DocumentSummary> out = new ArrayList<>();
        for (Row r : rows) {
            long count = 0;
            if (initialized) {
                String cursor = "";
                while (true) {
                    ObjectNode b = MAPPER.createObjectNode();
                    b.put("cursor", cursor);
                    b.put("limit", 1000);
                    b.put("includeVectors", false);
                    b.put("includeMetadata", false);
                    b.put("includeData", false);
                    b.put("prefix", r.id() + ":");
                    JsonNode res = post(nsPath("/range"), b, false);
                    if (!res.isObject() || !res.has("nextCursor") || !res.has("vectors") || !res.get("vectors").isArray()) {
                        throw new SQLException("Upstash range result without nextCursor or vectors");
                    }
                    for (JsonNode v : res.get("vectors")) {
                        if (!v.isObject() || !v.has("id")) {
                            throw new SQLException("Upstash range vector without id");
                        }
                        count++;
                    }
                    // Like the fixed Python: follow the cursor until it is empty or repeats
                    JsonNode nc = res.get("nextCursor");
                    if (nc.isNull()) {
                        break;
                    }
                    if (!nc.isTextual()) {
                        throw new SQLException("Upstash range nextCursor is not a string");
                    }
                    String next = nc.asText();
                    if (next.isEmpty() || next.equals(cursor)) {
                        break;
                    }
                    cursor = next;
                }
            }
            OffsetDateTime uploaded;
            try {
                uploaded = LocalDateTime.parse(r.uploadedAt()).atOffset(ZoneOffset.UTC);
            } catch (DateTimeParseException e) {
                uploaded = null;
            }
            Map<String, Object> meta;
            try {
                meta = MAPPER.readValue(r.metadata(), new TypeReference<LinkedHashMap<String, Object>>() {
                });
            } catch (JsonProcessingException e) {
                throw new SQLException("stored metadata of document " + r.id() + " is not valid JSON", e);
            }
            out.add(new DocumentSummary(r.id(), r.filename(), uploaded, meta, count));
        }
        return out;
    }

    @Override
    public boolean deleteDocument(String documentId) throws SQLException {
        lock.lock();
        try {
            if (initialized) {
                ObjectNode b = MAPPER.createObjectNode();
                b.put("prefix", documentId + ":");
                JsonNode res = post(nsPath("/delete"), b, false);
                if (!res.isObject() || !res.has("deleted")) {
                    throw new SQLException("Upstash delete result without deleted");
                }
            }
            try (PreparedStatement ps = conn.prepareStatement("DELETE FROM documents WHERE id = ?")) {
                ps.setString(1, documentId);
                return ps.executeUpdate() > 0;
            }
        } finally {
            lock.unlock();
        }
    }

    @Override
    public Optional<String> getDocumentFilename(String documentId) throws SQLException {
        try (PreparedStatement ps = conn.prepareStatement("SELECT filename FROM documents WHERE id = ?")) {
            ps.setString(1, documentId);
            try (ResultSet rs = ps.executeQuery()) {
                return rs.next() ? Optional.ofNullable(rs.getString(1)) : Optional.empty();
            }
        }
    }
}
