package com.ragleap.vectorstores;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.ragleap.rag.vectorstore.DocumentSummary;
import com.ragleap.rag.vectorstore.SearchResult;
import com.ragleap.rag.vectorstore.VectorBackend;
import java.io.IOException;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.math.RoundingMode;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.sql.SQLException;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;

/**
 * Java port of the Python OpenSearchBackend (ragleap-vectorstores), talking to OpenSearch over its REST API with
 * {@link java.net.http.HttpClient} and Jackson (no vendor SDK). Chunks live in one index (k-NN field, engine lucene,
 * space cosinesimil); the document registry is a second index named by default "&lt;index&gt;_documents".
 *
 * <p>Verified only by recorded-request parity against the real Python backend and the real opensearch-py client
 * (same requests, same results). NOT live-verified against a running OpenSearch.
 *
 * <p>Differences from Python: TLS certificates are verified by default (Python's verify_certs defaults to false;
 * pass your own HttpClient for custom trust); only basic authentication and use_ssl are supported (no
 * opensearch-py client_kwargs); like Python, the URL scheme is ignored and only useSsl chooses http or https.
 * A dimension mismatch in {@link #initSchema} throws IllegalStateException (Python: RuntimeError); every transport
 * or HTTP failure throws SQLException (Python: TransportError).
 */
public final class OpenSearchBackend implements VectorBackend {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    /** The only filterable fields (the others are stored unindexed), sorted like Python's sorted(allowed). */
    private static final List<String> ALLOWED_FILTER_KEYS =
            List.of("chunk_index", "document_id", "document_name", "tenant_id", "token_count");
    private static final System.Logger LOG = System.getLogger("com.ragleap.vectorstores");
    private static final String MISSING_URL = "OpenSearchBackend requires opensearch_url= - e.g. "
            + "'http://localhost:9200'. This must point at a real running OpenSearch instance - there is no "
            + "embedded/local mode for OpenSearch, unlike Chroma/LanceDB.";
    private static final char[] HEX = "0123456789ABCDEF".toCharArray();

    record HostPort(String host, BigInteger port) {
    }

    private record Reply(int status, String body) {
    }

    private final String baseUrl;
    private final String indexName;
    private final String documentsIndexName;
    private final String authHeader;
    private final HttpClient http;

    public OpenSearchBackend(String opensearchUrl) {
        this(opensearchUrl, "ragleap_idx", null, false, null, null, null);
    }

    public OpenSearchBackend(String opensearchUrl, String indexName, String documentsIndexName, boolean useSsl,
                             String username, String password) {
        this(opensearchUrl, indexName, documentsIndexName, useSsl, username, password, null);
    }

    public OpenSearchBackend(String opensearchUrl, String indexName, String documentsIndexName, boolean useSsl,
                             String username, String password, HttpClient httpClient) {
        if (opensearchUrl == null || opensearchUrl.isEmpty()) {
            throw new IllegalArgumentException(MISSING_URL);
        }
        HostPort hp = parseUrl(opensearchUrl);
        if (hp.port().signum() < 0 || hp.port().compareTo(BigInteger.valueOf(65535)) > 0) {
            throw new IllegalArgumentException("port out of range: " + hp.port());
        }
        this.indexName = indexName == null ? "ragleap_idx" : indexName;
        this.documentsIndexName = documentsIndexName == null || documentsIndexName.isEmpty()
                ? this.indexName + "_documents" : documentsIndexName;
        this.baseUrl = (useSsl ? "https" : "http") + "://" + hp.host() + ":" + hp.port();
        this.authHeader = username == null ? null : "Basic " + Base64.getEncoder().encodeToString(
                (username + ":" + (password == null ? "" : password)).getBytes(StandardCharsets.UTF_8));
        this.http = httpClient != null ? httpClient : HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1).connectTimeout(Duration.ofSeconds(10)).build();
    }

    // ---- helpers that copy Python behaviour exactly ----

    /** Python's _parse_url: scheme ignored, host and port split by hand, default port 9200, port via int(). */
    static HostPort parseUrl(String url) {
        int sep = url.indexOf("://");
        String stripped = sep < 0 ? url : url.substring(sep + 3);
        int slash = stripped.indexOf('/');
        String hostPart = slash < 0 ? stripped : stripped.substring(0, slash);
        int colon = hostPart.indexOf(':');
        if (colon < 0) {
            return new HostPort(hostPart, BigInteger.valueOf(9200));
        }
        return new HostPort(hostPart.substring(0, colon), pyInt(hostPart.substring(colon + 1)));
    }

    private static boolean isPySpace(int cp) {
        return (cp >= 9 && cp <= 13) || (cp >= 0x1c && cp <= 0x20) || cp == 0x85 || cp == 0xa0 || cp == 0x1680
                || (cp >= 0x2000 && cp <= 0x200a) || cp == 0x2028 || cp == 0x2029 || cp == 0x202f || cp == 0x205f
                || cp == 0x3000;
    }

    /** Python int(str): Unicode whitespace stripped, optional sign, decimal digits (any script), single underscores. */
    static BigInteger pyInt(String s) {
        StringBuilder t = new StringBuilder();
        for (int i = 0; i < s.length(); ) {
            int cp = s.codePointAt(i);
            i += Character.charCount(cp);
            if (isPySpace(cp)) {
                t.append(' ');
            } else if (Character.getType(cp) == Character.DECIMAL_DIGIT_NUMBER && Character.digit(cp, 10) >= 0) {
                t.append((char) ('0' + Character.digit(cp, 10)));
            } else if (cp < 128) {
                t.append((char) cp);
            } else {
                t.append('?');
            }
        }
        String v = t.toString().strip();
        if (v.indexOf(' ') >= 0 && !v.isBlank()) {
            // strip() above also removes other ASCII whitespace; a remaining space is interior
            v = v.trim();
        }
        int i = 0;
        boolean negative = false;
        if (i < v.length() && (v.charAt(i) == '+' || v.charAt(i) == '-')) {
            negative = v.charAt(i) == '-';
            i++;
        }
        StringBuilder digits = new StringBuilder();
        boolean prevDigit = false;
        for (; i < v.length(); i++) {
            char c = v.charAt(i);
            if (c >= '0' && c <= '9') {
                digits.append(c);
                prevDigit = true;
            } else if (c == '_' && prevDigit && i + 1 < v.length() && v.charAt(i + 1) >= '0' && v.charAt(i + 1) <= '9') {
                prevDigit = false;
            } else {
                throw new NumberFormatException("invalid literal for int(): '" + s + "'");
            }
        }
        if (digits.length() == 0) {
            throw new NumberFormatException("invalid literal for int(): '" + s + "'");
        }
        BigInteger n = new BigInteger(digits.toString());
        return negative ? n.negate() : n;
    }

    /** urllib.parse.quote(s, safe=",*") as opensearch-py builds paths. */
    static String quote(String s) {
        StringBuilder sb = new StringBuilder();
        for (byte b : s.getBytes(StandardCharsets.UTF_8)) {
            int c = b & 0xff;
            if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
                    || c == '_' || c == '.' || c == '-' || c == '~' || c == ',' || c == '*') {
                sb.append((char) c);
            } else {
                sb.append('%').append(HEX[c >> 4]).append(HEX[c & 15]);
            }
        }
        return sb.toString();
    }

    /** datetime.isoformat() of an aware datetime: microseconds omitted when exactly zero. */
    static String pyIso(OffsetDateTime t) {
        StringBuilder sb = new StringBuilder(String.format(Locale.ROOT, "%04d-%02d-%02dT%02d:%02d:%02d",
                t.getYear(), t.getMonthValue(), t.getDayOfMonth(), t.getHour(), t.getMinute(), t.getSecond()));
        int micro = t.getNano() / 1000;
        if (micro != 0) {
            sb.append(String.format(Locale.ROOT, ".%06d", micro));
        }
        int off = t.getOffset().getTotalSeconds();
        sb.append(off < 0 ? '-' : '+');
        off = Math.abs(off);
        sb.append(String.format(Locale.ROOT, "%02d:%02d", off / 3600, (off % 3600) / 60));
        return sb.toString();
    }

    /** Python round(x, 4): the exact binary value rounded half-even to 4 places. */
    static double round4(double v) {
        if (Double.isNaN(v) || Double.isInfinite(v)) {
            return v;
        }
        return new BigDecimal(v).setScale(4, RoundingMode.HALF_EVEN).doubleValue();
    }

    private static JsonNode tree(String json) {
        try {
            return MAPPER.readTree(json);
        } catch (JsonProcessingException e) {
            throw new IllegalArgumentException(e);
        }
    }

    private static JsonNode parse(String body) throws SQLException {
        try {
            JsonNode n = MAPPER.readTree(body);
            if (n == null || n.isMissingNode()) {
                throw new SQLException("OpenSearch returned an empty response");
            }
            return n;
        } catch (JsonProcessingException e) {
            throw new SQLException("OpenSearch returned invalid JSON", e);
        }
    }

    // ---- HTTP ----

    private Reply call(String method, String path, String query, JsonNode body) throws SQLException {
        try {
            HttpRequest.Builder b = HttpRequest.newBuilder()
                    .uri(URI.create(baseUrl + path + (query == null ? "" : "?" + query)))
                    .timeout(Duration.ofSeconds(30))
                    .header("Content-Type", "application/json");
            if (authHeader != null) {
                b.header("Authorization", authHeader);
            }
            b.method(method, body == null ? HttpRequest.BodyPublishers.noBody()
                    : HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)));
            HttpResponse<String> r = http.send(b.build(), HttpResponse.BodyHandlers.ofString());
            return new Reply(r.statusCode(), r.body());
        } catch (IOException | IllegalArgumentException e) {
            throw new SQLException("OpenSearch request failed: " + method + " " + path, e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new SQLException("OpenSearch request interrupted: " + method + " " + path, e);
        }
    }

    private String request(String method, String path, String query, JsonNode body) throws SQLException {
        Reply r = call(method, path, query, body);
        if (r.status() < 200 || r.status() >= 300) {
            String text = r.body() == null ? "" : r.body();
            throw new SQLException("OpenSearch request failed: " + method + " " + path + " -> HTTP " + r.status()
                    + ": " + (text.length() > 300 ? text.substring(0, 300) : text));
        }
        return r.body() == null ? "" : r.body();
    }

    private boolean exists(String index) throws SQLException {
        Reply r = call("HEAD", "/" + quote(index), null, null);
        if (r.status() >= 200 && r.status() < 300) {
            return true;
        }
        if (r.status() == 404) {
            return false;
        }
        throw new SQLException("OpenSearch request failed: HEAD /" + quote(index) + " -> HTTP " + r.status());
    }

    private String indexPath() {
        return "/" + quote(indexName);
    }

    private String docsPath() {
        return "/" + quote(documentsIndexName);
    }

    // ---- VectorBackend ----

    @Override
    public void initSchema(int dimensions) throws SQLException {
        if (!exists(indexName)) {
            request("PUT", indexPath(), null, tree("""
                    {"settings":{"index":{"knn":true}},"mappings":{"properties":{
                    "embedding":{"type":"knn_vector","dimension":%d,"method":{"name":"hnsw","engine":"lucene","space_type":"cosinesimil"}},
                    "document_id":{"type":"keyword"},"document_name":{"type":"keyword"},"tenant_id":{"type":"keyword"},
                    "chunk_index":{"type":"integer"},
                    "token_count":{"type":"integer"},"text":{"type":"text"},"metadata":{"type":"object","enabled":false}}}}
                    """.formatted(dimensions)));
        } else {
            JsonNode existing = parse(request("GET", indexPath() + "/_mapping", null, null));
            JsonNode dim = existing.path(indexName).path("mappings").path("properties").path("embedding").path("dimension");
            if (!dim.isMissingNode() && !dim.isNull()) {
                boolean same = dim.isNumber() && dim.doubleValue() == dimensions;
                if (!same) {
                    throw new IllegalStateException("OpenSearchBackend: index '" + indexName + "' already exists with "
                            + "dimension " + dim.asText() + ", but this call requested dimension " + dimensions
                            + ". Use a different index_name or a matching dimension.");
                }
            }
            try {
                request("PUT", indexPath() + "/_mapping", null, tree("{\"properties\":{\"tenant_id\":{\"type\":\"keyword\"}}}"));
            } catch (SQLException | RuntimeException e) {
                // a filter on an unmapped field matches nothing, so this stays safe
                LOG.log(System.Logger.Level.WARNING, "OpenSearchBackend: could not add the tenant_id mapping to '" + indexName
                        + "': " + e.getMessage());
            }
        }
        if (!exists(documentsIndexName)) {
            request("PUT", docsPath(), null, tree("""
                    {"mappings":{"properties":{"filename":{"type":"keyword"},"metadata":{"type":"object","enabled":false},
                    "uploaded_at":{"type":"date"}}}}
                    """));
        }
    }

    @Override
    public void insertDocument(String documentId, String filename, Map<String, Object> metadata) throws SQLException {
        ObjectNode body = MAPPER.createObjectNode();
        body.put("filename", filename);
        body.put("metadata", PyJson.dumps(metadata == null ? Map.of() : metadata));
        body.put("uploaded_at", pyIso(OffsetDateTime.now(ZoneOffset.UTC)));
        if (documentId == null || documentId.isEmpty()) {
            request("POST", docsPath() + "/_doc", "refresh=true", body);   // opensearch-py: empty id means POST
        } else {
            request("PUT", docsPath() + "/_doc/" + quote(documentId), "refresh=true", body);
        }
    }

    @Override
    public void insertChunk(String documentId, String documentName, int chunkIndex, String text, Integer tokenCount,
                            List<Double> embedding, Map<String, Object> metadata) throws SQLException {
        String chunkId = documentId + ":" + chunkIndex;
        ObjectNode body = MAPPER.createObjectNode();
        body.set("embedding", MAPPER.valueToTree(embedding));
        body.put("document_id", documentId);
        body.put("document_name", documentName);
        body.put("chunk_index", chunkIndex);
        body.put("token_count", tokenCount == null ? 0 : tokenCount);
        body.put("text", text);
        body.put("metadata", PyJson.dumps(metadata == null ? Map.of() : metadata));
        Object tenant = metadata == null ? null : metadata.get("tenant_id");
        if (tenant != null) {
            body.put("tenant_id", RedisBackend.pyStr(tenant));
        }
        request("PUT", indexPath() + "/_doc/" + quote(chunkId), "refresh=true", body);
    }

    @Override
    public List<SearchResult> searchDense(List<Double> embedding, int topK, Map<String, Object> metadataFilter)
            throws SQLException {
        if (embedding == null || embedding.isEmpty()) {
            return List.of();
        }
        ObjectNode knn = MAPPER.createObjectNode();
        ObjectNode emb = knn.putObject("embedding");
        emb.set("vector", MAPPER.valueToTree(embedding));
        emb.put("k", topK);
        ObjectNode q = MAPPER.createObjectNode();
        q.put("size", topK);
        if (metadataFilter != null && !metadataFilter.isEmpty()) {
            List<String> unknown = new ArrayList<>();
            for (String key : metadataFilter.keySet()) {
                if (!ALLOWED_FILTER_KEYS.contains(key)) {
                    unknown.add(key);
                }
            }
            if (!unknown.isEmpty()) {
                unknown.sort(null);
                throw new IllegalArgumentException("OpenSearchBackend can only filter by " + PyRepr.list(ALLOWED_FILTER_KEYS)
                        + ", got " + PyRepr.list(unknown) + ": other metadata is stored unindexed, so such a filter would "
                        + "silently match nothing");
            }
            ObjectNode bool = q.putObject("query").putObject("bool");
            bool.putArray("must").addObject().set("knn", knn);
            ArrayNode filters = bool.putArray("filter");
            for (Map.Entry<String, Object> e : metadataFilter.entrySet()) {
                filters.addObject().putObject("term").set(e.getKey(), MAPPER.valueToTree(e.getValue()));
            }
        } else {
            q.putObject("query").set("knn", knn);
        }
        JsonNode hits = parse(request("POST", indexPath() + "/_search", null, q)).path("hits").path("hits");
        List<SearchResult> out = new ArrayList<>();
        for (JsonNode hit : hits) {
            JsonNode src = hit.path("_source");
            JsonNode t = src.get("text");
            String text = t == null ? "" : (t.isNull() ? null : t.asText());
            out.add(new SearchResult(hit.path("_id").asText(), text, round4(hit.path("_score").asDouble()),
                    src.hasNonNull("document_id") ? src.get("document_id").asText() : null,
                    src.hasNonNull("document_name") ? src.get("document_name").asText() : null,
                    chunkIndex(src)));
        }
        return out;
    }

    private static int chunkIndex(JsonNode src) {
        JsonNode n = src.get("chunk_index");
        if (n == null) {
            return 0;
        }
        if (n.isNumber()) {
            return n.intValue();
        }
        if (n.isTextual()) {
            return pyInt(n.asText()).intValue();
        }
        throw new IllegalArgumentException("unusable chunk_index: " + n);
    }

    @Override
    public boolean supportsSparse() {
        return false;
    }

    @Override
    public List<DocumentSummary> listDocuments(int limit, int offset) throws SQLException {
        ObjectNode q = MAPPER.createObjectNode();
        q.put("size", limit);
        q.put("from", offset);
        q.putArray("sort").addObject().putObject("uploaded_at").put("order", "desc");
        q.putObject("query").putObject("match_all");
        JsonNode hits = parse(request("POST", docsPath() + "/_search", null, q)).path("hits").path("hits");
        List<DocumentSummary> out = new ArrayList<>();
        for (JsonNode hit : hits) {
            JsonNode src = hit.path("_source");
            String docId = hit.path("_id").asText();
            ObjectNode count = MAPPER.createObjectNode();
            count.putObject("query").putObject("term").put("document_id", docId);
            JsonNode cr = parse(request("POST", indexPath() + "/_count", null, count));
            OffsetDateTime uploaded = null;
            JsonNode u = src.get("uploaded_at");
            if (u != null && u.isTextual()) {
                try {
                    uploaded = OffsetDateTime.parse(u.asText());
                } catch (DateTimeParseException e) {
                    uploaded = null;
                }
            }
            Map<String, Object> meta = new LinkedHashMap<>();
            JsonNode m = src.get("metadata");
            if (m != null && m.isTextual()) {
                try {
                    meta = MAPPER.readValue(m.asText(), new TypeReference<LinkedHashMap<String, Object>>() {
                    });
                } catch (JsonProcessingException e) {
                    throw new SQLException("stored metadata of document " + docId + " is not valid JSON", e);
                }
            }
            out.add(new DocumentSummary(docId, src.hasNonNull("filename") ? src.get("filename").asText() : null,
                    uploaded, meta, cr.has("count") ? cr.get("count").asLong() : 0L));
        }
        return out;
    }

    @Override
    public boolean deleteDocument(String documentId) throws SQLException {
        ObjectNode q = MAPPER.createObjectNode();
        q.putObject("query").putObject("term").put("document_id", documentId);
        request("POST", indexPath() + "/_delete_by_query", "refresh=true", q);
        if (documentId == null || documentId.isEmpty()) {
            return false;   // opensearch-py refuses an empty id before sending; Python then returns False
        }
        try {
            request("DELETE", docsPath() + "/_doc/" + quote(documentId), "refresh=true", null);
            return true;
        } catch (SQLException e) {
            return false;
        }
    }

    @Override
    public Optional<String> getDocumentFilename(String documentId) throws SQLException {
        if (documentId == null || documentId.isEmpty()) {
            return Optional.empty();
        }
        try {
            JsonNode fn = parse(request("GET", docsPath() + "/_doc/" + quote(documentId), null, null))
                    .path("_source").path("filename");
            return fn.isMissingNode() || fn.isNull() ? Optional.empty() : Optional.of(fn.asText());
        } catch (SQLException e) {
            return Optional.empty();
        }
    }
}
