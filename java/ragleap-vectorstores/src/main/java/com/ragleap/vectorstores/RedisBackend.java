package com.ragleap.vectorstores;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.ragleap.rag.vectorstore.DocumentSummary;
import com.ragleap.rag.vectorstore.SearchResult;
import com.ragleap.rag.vectorstore.VectorBackend;
import java.io.IOException;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.locks.ReentrantLock;
import java.util.regex.Pattern;

/**
 * Java port of the Python RedisBackend (ragleap-vectorstores) for Redis Stack (RediSearch): chunks are Redis hashes
 * with a float32 vector, searched with FT.SEARCH (HNSW, cosine); the document registry is a local SQLite file, as in
 * Python. Commands are sent over a small built-in RESP2 client ({@link RespClient}), no vendor SDK.
 *
 * <p>Verified only by recorded-command parity against the real Python backend and the real redis-py client (forced to
 * RESP2) against a fake Redis server. NOT live-verified against a real Redis Stack.
 *
 * <p>Differences from Python: the connection is a single socket (no pool); only redis:// and rediss:// urls with
 * optional user, password and database; a filter value that is a list or map throws IllegalArgumentException;
 * malformed server replies and failures throw SQLException; a missing search module throws IllegalStateException
 * (Python: RuntimeError). Like Python, only document_id and tenant_id can be filtered (any other filter key throws IllegalArgumentException, a tenant_id
 * filter on an index that was created without that field throws IllegalStateException), an existing
 * index is reused without checking its dimension, and at most 10000 chunks of a document are deleted.
 */
public final class RedisBackend implements VectorBackend, AutoCloseable {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String MISSING = "RedisBackend requires redis_url= - e.g. 'redis://localhost:6379/0'. "
            + "This must point at a Redis Stack instance (or plain Redis with the RediSearch module loaded) - "
            + "init_schema() will raise a clear error if the 'search' module is not available.";
    private static final String TAG_SPECIAL = ",.<>{}[]\"\\':;!@#$%^&*()-+=~| ";
    private static final Pattern PY_FLOAT = Pattern.compile(
            "[+-]?(?:\\d+(?:_\\d+)*(?:\\.(?:\\d+(?:_\\d+)*)?)?|\\.\\d+(?:_\\d+)*)(?:[eE][+-]?\\d+(?:_\\d+)*)?");

    private final String redisUrl;
    private final String indexName;
    private final String keyPrefix;
    private final Connection conn;
    private final ReentrantLock lock = new ReentrantLock();
    private volatile RespClient client;
    private volatile boolean tenantField = true;   // false only when an existing index was created without tenant_id

    public RedisBackend(String redisUrl) throws SQLException {
        this(redisUrl, "ragleap_idx", null, null);
    }

    public RedisBackend(String redisUrl, String indexName, String keyPrefix, String registryPath) throws SQLException {
        if (redisUrl == null || redisUrl.isEmpty()) {
            throw new IllegalArgumentException(MISSING);
        }
        this.redisUrl = redisUrl;
        this.indexName = indexName == null ? "ragleap_idx" : indexName;
        // Python: the default prefix derives from the index name so two indexes never share keys.
        this.keyPrefix = keyPrefix == null || keyPrefix.isEmpty() ? "ragleap:" + this.indexName + ":chunk:" : keyPrefix;
        Path file = registryPath != null && !registryPath.isEmpty() ? Path.of(registryPath)
                : Path.of(System.getProperty("user.home"), ".ragleap_vectorstores", "redis_documents.sqlite3");
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
        RespClient c = client;
        if (c != null) {
            c.close();
        }
        conn.close();
    }

    // ---- helpers that copy Python behaviour ----

    /** Escapes RediSearch TAG special characters with a backslash (same character set as Python). */
    static String escapeTag(String value) {
        StringBuilder sb = new StringBuilder();
        value.codePoints().forEach(cp -> {
            if (cp < 128 && TAG_SPECIAL.indexOf(cp) >= 0) {
                sb.append('\\');
            }
            sb.appendCodePoint(cp);
        });
        return sb.toString();
    }

    /**
     * The prefilter in front of the KNN clause: only document_id and tenant_id may be filtered (any other key throws
     * IllegalArgumentException, because ignoring it would be unsafe for tenant isolation); both go into ONE parenthesised
     * group (RediSearch rejects two groups before =>[KNN ...]); a tenant_id filter on an index created without that field
     * throws IllegalStateException.
     */
    static String filterClause(Map<String, Object> metadataFilter, boolean tenantField, String indexName) {
        if (metadataFilter == null || metadataFilter.isEmpty()) {
            return "*";
        }
        List<String> unknown = new ArrayList<>();
        for (String key : metadataFilter.keySet()) {
            if (!key.equals("document_id") && !key.equals("tenant_id")) {
                unknown.add(key);
            }
        }
        if (!unknown.isEmpty()) {
            unknown.sort(null);
            throw new IllegalArgumentException("RedisBackend can only filter by document_id and tenant_id, got "
                    + PyRepr.list(unknown) + "; other keys used to be ignored silently, which is unsafe for tenant isolation");
        }
        if (metadataFilter.containsKey("tenant_id") && !tenantField) {
            throw new IllegalStateException("RedisBackend: index '" + indexName + "' was created without a tenant_id field; "
                    + "use a new index_name to enable tenant filtering");
        }
        List<String> clauses = new ArrayList<>();
        for (String key : List.of("document_id", "tenant_id")) {
            if (metadataFilter.containsKey(key)) {
                clauses.add("@" + key + ":{" + escapeTag(pyStr(metadataFilter.get(key))) + "}");
            }
        }
        return "(" + String.join(" ", clauses) + ")";
    }

    /** True when any string anywhere in the reply contains the text (Python: the text is in repr(info)). */
    static boolean containsText(Object reply, String needle) {
        if (reply instanceof byte[] b) {
            return new String(b, StandardCharsets.UTF_8).contains(needle);
        }
        if (reply instanceof List<?> l) {
            for (Object o : l) {
                if (containsText(o, needle)) {
                    return true;
                }
            }
            return false;
        }
        return reply != null && String.valueOf(reply).contains(needle);
    }

    /** numpy.array(embedding, dtype=float32).tobytes(): little-endian float32 values. */
    static byte[] floatBytes(List<Double> embedding) {
        ByteBuffer b = ByteBuffer.allocate(4 * embedding.size()).order(ByteOrder.LITTLE_ENDIAN);
        for (Double d : embedding) {
            b.putFloat((float) d.doubleValue());
        }
        return b.array();
    }

    /** Python str() of a filter value. */
    static String pyStr(Object v) {
        if (v == null) {
            return "None";
        }
        if (v instanceof Boolean b) {
            return b ? "True" : "False";
        }
        if (v instanceof String s) {
            return s;
        }
        if (v instanceof Integer || v instanceof Long || v instanceof Short || v instanceof Byte
                || v instanceof BigInteger) {
            return v.toString();
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
        throw new IllegalArgumentException("unsupported filter value type: " + v.getClass().getSimpleName());
    }

    private static boolean isPySpace(int cp) {
        return (cp >= 9 && cp <= 13) || (cp >= 0x1c && cp <= 0x20) || cp == 0x85 || cp == 0xa0 || cp == 0x1680
                || (cp >= 0x2000 && cp <= 0x200a) || cp == 0x2028 || cp == 0x2029 || cp == 0x202f || cp == 0x205f
                || cp == 0x3000;
    }

    private static String stripPy(String s) {
        int start = 0;
        int end = s.length();
        while (start < end && isPySpace(s.codePointAt(start))) {
            start += Character.charCount(s.codePointAt(start));
        }
        while (end > start && isPySpace(s.codePointBefore(end))) {
            end -= Character.charCount(s.codePointBefore(end));
        }
        return s.substring(start, end);
    }

    /** Python float(str): surrounding whitespace, underscores between digits, inf and nan words. */
    static double pyFloat(String s) {
        String t = stripPy(s);
        switch (t.toLowerCase(Locale.ROOT)) {
            case "inf", "+inf", "infinity", "+infinity":
                return Double.POSITIVE_INFINITY;
            case "-inf", "-infinity":
                return Double.NEGATIVE_INFINITY;
            case "nan", "+nan", "-nan":
                return Double.NaN;
            default:
                break;
        }
        if (!PY_FLOAT.matcher(t).matches()) {
            throw new NumberFormatException("could not convert string to float: '" + s + "'");
        }
        return Double.parseDouble(t.replace("_", ""));
    }

    /** Python min(a, b) for floats: returns b only when b < a, so a NaN b is ignored. */
    static double pyMin(double a, double b) {
        return b < a ? b : a;
    }

    /** Python max(a, b) for floats: returns b only when b > a, so a NaN b is ignored. */
    static double pyMax(double a, double b) {
        return b > a ? b : a;
    }

    private static String str(Object o) {
        if (o instanceof byte[] b) {
            return new String(b, StandardCharsets.UTF_8);
        }
        if (o instanceof String s) {
            return s;
        }
        if (o instanceof Long l) {
            return l.toString();
        }
        return null;
    }

    @SuppressWarnings("unchecked")
    private static List<Object> asList(Object o) throws SQLException {
        if (o instanceof List<?> l) {
            return (List<Object>) l;
        }
        throw new SQLException("unexpected Redis reply (not an array)");
    }

    private static long total(Object reply) throws SQLException {
        List<Object> l = asList(reply);
        if (l.isEmpty() || !(l.get(0) instanceof Long n)) {
            throw new SQLException("unexpected FT.SEARCH reply (no total)");
        }
        return n;
    }

    private String vectorKey(String documentId, int chunkIndex) {
        return keyPrefix + documentId + ":" + chunkIndex;
    }

    private RespClient requireClient() {
        RespClient c = client;
        if (c == null) {
            throw new IllegalStateException("RedisBackend: call initSchema() first");
        }
        return c;
    }

    // ---- VectorBackend ----

    @Override
    public void initSchema(int dimensions) throws SQLException {
        // Python assigns the client before the first command, so even a failed check leaves the backend usable.
        RespClient c = new RespClient(redisUrl);
        client = c;
        Set<String> modules = new HashSet<>();
        for (Object m : asList(c.command("MODULE", "LIST"))) {
            if (m instanceof List<?> entry) {
                for (int i = 0; i < entry.size(); i++) {
                    if ("name".equals(str(entry.get(i)))) {
                        if (i + 1 < entry.size() && str(entry.get(i + 1)) != null) {
                            modules.add(str(entry.get(i + 1)));
                        }
                        break;
                    }
                }
            }
        }
        if (!modules.contains("search")) {
            throw new IllegalStateException("RedisBackend: no 'search' module loaded on " + redisUrl + ". This backend "
                    + "requires Redis Stack (or plain Redis + the RediSearch module) - live-verified via MODULE LIST, "
                    + "not assumed.");
        }
        try {
            Object info = c.command("FT.INFO", indexName);
            tenantField = containsText(info, "tenant_id");
            return;   // the index already exists: reused, its dimension is not checked (like Python)
        } catch (SQLException e) {
            // no such index (or any failure): create it
        }
        c.command("FT.CREATE", indexName, "ON", "HASH", "PREFIX", "1", keyPrefix, "SCORE", "1.0", "SCHEMA",
                "document_id", "TAG", "SEPARATOR", ",", "tenant_id", "TAG", "SEPARATOR", ",",
                "document_name", "TEXT", "WEIGHT", "1.0",
                "chunk_index", "NUMERIC", "token_count", "NUMERIC", "text", "TEXT", "WEIGHT", "1.0",
                "vector", "VECTOR", "HNSW", "6", "TYPE", "FLOAT32", "DIM", Integer.toString(dimensions),
                "DISTANCE_METRIC", "COSINE");
    }

    @Override
    public void insertDocument(String documentId, String filename, Map<String, Object> metadata) throws SQLException {
        lock.lock();
        try (PreparedStatement ps = conn.prepareStatement(
                "INSERT INTO documents (id, filename, metadata, uploaded_at) VALUES (?, ?, ?, ?)")) {
            ps.setString(1, documentId);
            ps.setString(2, filename);
            ps.setString(3, PyJson.dumps(metadata == null ? Map.of() : metadata));
            ps.setString(4, UpstashBackend.pyIsoNaive(OffsetDateTime.now(ZoneOffset.UTC)));
            ps.executeUpdate();
        } finally {
            lock.unlock();
        }
    }

    @Override
    public void insertChunk(String documentId, String documentName, int chunkIndex, String text, Integer tokenCount,
                            List<Double> embedding, Map<String, Object> metadata) throws SQLException {
        String key = vectorKey(documentId, chunkIndex);
        byte[] vector = floatBytes(embedding);
        lock.lock();
        try {
            List<Object> args = new ArrayList<>();
            for (Object a : new Object[] {"HSET", key, "document_id", documentId, "document_name", documentName,
                    "chunk_index", Integer.toString(chunkIndex), "token_count",
                    Integer.toString(tokenCount == null ? 0 : tokenCount), "text", text, "vector", vector,
                    "metadata", PyJson.dumps(metadata == null ? Map.of() : metadata)}) {
                args.add(a);
            }
            Object tenant = metadata == null ? null : metadata.get("tenant_id");
            if (tenant != null) {
                args.add("tenant_id");
                args.add(pyStr(tenant));
            }
            requireClient().command(args.toArray());
        } finally {
            lock.unlock();
        }
    }

    private static Map<String, String> fields(Object o) throws SQLException {
        List<Object> l = asList(o);
        Map<String, String> m = new HashMap<>();
        for (int i = 0; i + 1 < l.size(); i += 2) {
            String k = str(l.get(i));
            if (k != null) {
                m.put(k, str(l.get(i + 1)));
            }
        }
        return m;
    }

    private static String need(Map<String, String> m, String key) throws SQLException {
        if (!m.containsKey(key) || m.get(key) == null) {
            throw new SQLException("FT.SEARCH document without field " + key);
        }
        return m.get(key);
    }

    @Override
    public List<SearchResult> searchDense(List<Double> embedding, int topK, Map<String, Object> metadataFilter)
            throws SQLException {
        RespClient c = client;
        if (embedding == null || embedding.isEmpty() || c == null) {
            return List.of();
        }
        String filter = filterClause(metadataFilter, tenantField, indexName);
        Object reply = c.command("FT.SEARCH", indexName, filter + "=>[KNN " + topK + " @vector $vec AS score]",
                "RETURN", "5", "text", "score", "document_id", "document_name", "chunk_index",
                "SORTBY", "score", "ASC", "DIALECT", "2", "LIMIT", "0", Integer.toString(topK),
                "PARAMS", "2", "vec", floatBytes(embedding));
        List<Object> l = asList(reply);
        List<SearchResult> out = new ArrayList<>();
        for (int i = 1; i + 1 < l.size(); i += 2) {
            String key = str(l.get(i));
            Map<String, String> f = fields(l.get(i + 1));
            String text = need(f, "text");
            String score = need(f, "score");
            String docId = need(f, "document_id");
            String docName = need(f, "document_name");
            String chunkIndex = need(f, "chunk_index");
            try {
                // RediSearch returns a cosine DISTANCE (0 identical, 1 orthogonal, 2 opposite): mapped to a 0 to 1
                // similarity like the other backends, 1 - distance / 2, clamped like the fixed Python
                out.add(new SearchResult(key, text, OpenSearchBackend.round4(pyMax(0.0, pyMin(1.0, 1.0 - pyFloat(score) / 2.0))), docId, docName,
                        OpenSearchBackend.pyInt(chunkIndex).intValue()));
            } catch (NumberFormatException e) {
                throw new SQLException("FT.SEARCH document with a non-numeric score or chunk_index", e);
            }
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
        RespClient c = client;
        List<DocumentSummary> out = new ArrayList<>();
        for (Row r : rows) {
            long count = 0;
            if (c != null) {
                count = total(c.command("FT.SEARCH", indexName, "(@document_id:{" + escapeTag(r.id()) + "})",
                        "DIALECT", "2", "LIMIT", "0", "0"));
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
            RespClient c = client;
            if (c != null) {
                List<Object> l = asList(c.command("FT.SEARCH", indexName, "(@document_id:{" + escapeTag(documentId) + "})",
                        "DIALECT", "2", "LIMIT", "0", "10000"));
                List<Object> cmd = new ArrayList<>();
                cmd.add("DEL");
                for (int i = 1; i < l.size(); i += 2) {
                    String key = str(l.get(i));
                    if (key != null) {
                        cmd.add(key);
                    }
                }
                if (cmd.size() > 1) {
                    c.command(cmd.toArray());
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
