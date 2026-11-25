package com.ragleap.rag.vectorstore;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;

import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Port of the Python #1384 backend hardening: stored fields win over caller
 * metadata (Pinecone, Qdrant, Weaviate, Milvus) and Milvus filters validate
 * keys, escape strings and format numbers like Python.
 *
 * <p>Offline: every backend talks to a local stub HTTP server that records the
 * last request body per "METHOD path". NOT verified against real Pinecone,
 * Qdrant, Weaviate or Milvus servers. Uses try/finally instead of JUnit
 * lifecycle annotations.
 */
class TenantBackendStoredFieldsTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final List<Double> VEC = List.of(0.1, 0.2, 0.3, 0.4);

    /** Local stub server: records the last body per key, answers canned JSON per key. */
    private static final class Stub implements AutoCloseable {
        final HttpServer server;
        final String url;
        final Map<String, String> bodies = new ConcurrentHashMap<>();
        final Map<String, String> answers = new ConcurrentHashMap<>();
        final String defaultAnswer;

        Stub(String defaultAnswer) throws Exception {
            this.defaultAnswer = defaultAnswer;
            server = HttpServer.create(new InetSocketAddress("localhost", 0), 0);
            url = "http://localhost:" + server.getAddress().getPort();
            server.createContext("/", exchange -> {
                String key = exchange.getRequestMethod() + " " + exchange.getRequestURI().getPath();
                bodies.put(key, new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
                byte[] out = answers.getOrDefault(key, this.defaultAnswer).getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().add("Content-Type", "application/json");
                exchange.sendResponseHeaders(200, out.length);
                try (OutputStream os = exchange.getResponseBody()) {
                    os.write(out);
                }
            });
            server.start();
        }

        JsonNode body(String key) throws Exception {
            String b = bodies.get(key);
            assertTrue(b != null, "the stub never received " + key + "; got " + bodies.keySet());
            return MAPPER.readTree(b);
        }

        @Override
        public void close() {
            server.stop(0);
        }
    }

    private static Map<String, Object> hostile(boolean milvus) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("document_id", "HIJACK");
        m.put("document_name", "hijack.txt");
        m.put("chunk_index", 99);
        m.put("tenant_id", "acme");
        m.put("page", 3);
        if (milvus) {
            m.put("id", "HIJACK");
            m.put("vector", List.of(9, 9));
        }
        return m;
    }

    private static void assertStoredFieldsWin(JsonNode fields) {
        assertEquals("doc1", fields.path("document_id").asText(), "document_id must be the stored one: " + fields);
        assertEquals("real.txt", fields.path("document_name").asText(), "document_name must be the stored one: " + fields);
        assertEquals(0, fields.path("chunk_index").asInt(-1), "chunk_index must be the stored one: " + fields);
        assertTrue(fields.path("chunk_index").isNumber(), "chunk_index must stay a number: " + fields);
        assertEquals("acme", fields.path("tenant_id").asText(), "caller tenant_id must be kept: " + fields);
        assertEquals(3, fields.path("page").asInt(-1), "caller page must be kept: " + fields);
    }

    @Test
    void qdrantStoredFieldsWinOverCallerMetadata() throws Exception {
        Path dir = Files.createTempDirectory("qd_stored");
        try (Stub stub = new Stub("{}")) {
            QdrantBackend b = new QdrantBackend(dir.toString(), stub.url, null, "c1");
            b.initSchema(4);
            b.insertChunk("doc1", "real.txt", 0, "text", 5, VEC, hostile(false));
            JsonNode payload = stub.body("PUT /collections/c1/points").path("points").get(0).path("payload");
            assertStoredFieldsWin(payload);
            b.close();
        }
    }

    @Test
    void weaviateStoredFieldsWinOverCallerMetadata() throws Exception {
        Path dir = Files.createTempDirectory("wv_stored");
        try (Stub stub = new Stub("{}")) {
            WeaviateBackend b = new WeaviateBackend(dir.toString(), stub.url, null, "C1");
            b.initSchema(4);
            b.insertChunk("doc1", "real.txt", 0, "text", 5, VEC, hostile(false));
            JsonNode props = stub.body("POST /v1/objects").path("properties");
            assertStoredFieldsWin(props);
            b.close();
        }
    }

    @Test
    void pineconeStoredFieldsWinOverCallerMetadata() throws Exception {
        Path dir = Files.createTempDirectory("pc_stored");
        try (Stub stub = new Stub("{}")) {
            stub.answers.put("GET /indexes", "{\"indexes\":[]}");
            stub.answers.put("POST /indexes", "{\"name\":\"idx\"}");
            stub.answers.put("GET /indexes/idx", "{\"name\":\"idx\",\"host\":\"" + stub.url
                    + "\",\"status\":{\"ready\":true,\"state\":\"Ready\"}}");
            stub.answers.put("POST /vectors/upsert", "{\"upsertedCount\":1}");
            PineconeBackend b = new PineconeBackend(dir.toString(), "test-key", "idx", "aws", "us-east-1", stub.url, 10, 2000);
            b.initSchema(4);
            b.insertChunk("doc1", "real.txt", 0, "text", 5, VEC, hostile(false));
            JsonNode md = stub.body("POST /vectors/upsert").path("vectors").get(0).path("metadata");
            assertStoredFieldsWin(md);
            b.close();
        }
    }

    @Test
    void milvusStoredFieldsAndIdAndVectorWinOverCallerMetadata() throws Exception {
        Path dir = Files.createTempDirectory("mv_stored");
        try (Stub stub = new Stub("{\"code\":0,\"data\":{}}")) {
            stub.answers.put("POST /v2/vectordb/collections/has", "{\"code\":0,\"data\":{\"has\":false}}");
            MilvusBackend b = new MilvusBackend(dir.toString(), stub.url, null, "coll");
            b.initSchema(4);
            b.insertChunk("doc1", "real.txt", 0, "text", 5, VEC, hostile(true));
            JsonNode row = stub.body("POST /v2/vectordb/entities/insert").path("data").get(0);
            assertStoredFieldsWin(row);
            assertTrue(!"HIJACK".equals(row.path("id").asText()), "id must be the real vector key: " + row);
            assertTrue(row.path("id").asText().contains("doc1"), "id must be the real vector key: " + row);
            assertEquals(4, row.path("vector").size(), "vector must be the real embedding: " + row);
            assertEquals(0.1, row.path("vector").get(0).asDouble(), 1e-12, "vector must be the real embedding: " + row);
            b.close();
        }
    }

    // ---- Milvus filter expression -------------------------------------------------

    private static String expr(Map<String, Object> filter) throws Exception {
        Path dir = Files.createTempDirectory("mv_filter");
        try (Stub stub = new Stub("{\"code\":0,\"data\":{}}")) {
            MilvusBackend b = new MilvusBackend(dir.toString(), stub.url, null, "coll");
            try {
                return b.buildFilterExpr(filter);
            } finally {
                b.close();
            }
        }
    }

    private static Map<String, Object> one(String k, Object v) {
        Map<String, Object> m = new HashMap<>();
        m.put(k, v);
        return m;
    }

    @Test
    void milvusFilterEmptyAndTenant() throws Exception {
        assertEquals("", expr(null));
        assertEquals("", expr(new HashMap<>()));
        assertEquals("tenant_id == \"acme\"", expr(one("tenant_id", "acme")));
    }

    @Test
    void milvusFilterBooleansAreLowercase() throws Exception {
        assertEquals("flag == true", expr(one("flag", true)));
        assertEquals("flag == false", expr(one("flag", false)));
    }

    @Test
    void milvusFilterEscapesQuoteBackslashNewlineAndCarriageReturn() throws Exception {
        assertEquals("t == \"a\\\"b\"", expr(one("t", "a\"b")));
        assertEquals("t == \"a\\\\b\"", expr(one("t", "a\\b")));
        assertEquals("t == \"a\\nb\"", expr(one("t", "a\nb")));
        assertEquals("t == \"a\\rb\"", expr(one("t", "a\rb")));
        // an injection attempt stays ONE quoted string
        String hostile = "x\" or tenant_id != \"";
        assertEquals("tenant_id == \"x\\\" or tenant_id != \\\"\"", expr(one("tenant_id", hostile)));
        // injection through a newline stays inside the string too
        assertEquals("t == \"x\\n or t != \\\"\\\"\"", expr(one("t", "x\n or t != \"\"")));
    }

    @Test
    void milvusFilterIntegersPlainAndDoublesLikePython() throws Exception {
        assertEquals("n == 5", expr(one("n", 5)));
        assertEquals("n == -7", expr(one("n", -7L)));
        assertEquals("n == 1.5", expr(one("n", 1.5)));
        assertEquals("n == 2.0", expr(one("n", 2.0)));
        assertEquals("n == 1e-07", expr(one("n", 1e-7)));
        assertEquals("n == 1e+22", expr(one("n", 1e22)));
        assertEquals("n == 1.5", expr(one("n", 1.5f)));
        assertEquals("n == 0.1", expr(one("n", 0.1f)));
    }

    @Test
    void milvusFilterBadKeysThrowWithPythonRepr() throws Exception {
        String[][] bad = {
                {"a b", "Invalid metadata filter key: 'a b'"},
                {"1a", "Invalid metadata filter key: '1a'"},
                {"a-b", "Invalid metadata filter key: 'a-b'"},
                {"", "Invalid metadata filter key: ''"},
                {"x == 1 or y", "Invalid metadata filter key: 'x == 1 or y'"},
                {"it's", "Invalid metadata filter key: \"it's\""},
                {"a\u0001b", "Invalid metadata filter key: 'a\\x01b'"},
                {"a\u007fb", "Invalid metadata filter key: 'a\\x7fb'"},
                {"a\tb", "Invalid metadata filter key: 'a\\tb'"},
                {"a\nb", "Invalid metadata filter key: 'a\\nb'"},
                {"a\rb", "Invalid metadata filter key: 'a\\rb'"},
                {"a\\b", "Invalid metadata filter key: 'a\\\\b'"},
                {"it's \"x\"", "Invalid metadata filter key: 'it\\'s \"x\"'"},
        };
        for (String[] c : bad) {
            IllegalArgumentException ex = assertThrows(IllegalArgumentException.class, () -> expr(one(c[0], "v")), "key " + c[0]);
            assertEquals(c[1], ex.getMessage());
        }
        IllegalArgumentException nk = assertThrows(IllegalArgumentException.class, () -> expr(one(null, "v")));
        assertEquals("Invalid metadata filter key: None", nk.getMessage());
    }

    @Test
    void milvusFilterUnsupportedValuesThrow() throws Exception {
        Object[] bad = {Double.NaN, Double.POSITIVE_INFINITY, Float.NEGATIVE_INFINITY, null, List.of(1), Map.of("a", 1)};
        for (Object v : bad) {
            IllegalArgumentException ex = assertThrows(IllegalArgumentException.class, () -> expr(one("k", v)), "value " + v);
            assertTrue(ex.getMessage().startsWith("Unsupported metadata filter value for 'k': "), ex.getMessage());
        }
        String pre = "Unsupported metadata filter value for 'k': ";
        assertEquals(pre + "nan", assertThrows(IllegalArgumentException.class, () -> expr(one("k", Double.NaN))).getMessage());
        assertEquals(pre + "inf", assertThrows(IllegalArgumentException.class, () -> expr(one("k", Double.POSITIVE_INFINITY))).getMessage());
        assertEquals(pre + "-inf", assertThrows(IllegalArgumentException.class, () -> expr(one("k", Float.NEGATIVE_INFINITY))).getMessage());
        assertEquals(pre + "None", assertThrows(IllegalArgumentException.class, () -> expr(one("k", null))).getMessage());
        assertEquals(pre + "[1, 'a']", assertThrows(IllegalArgumentException.class, () -> expr(one("k", List.of(1, "a")))).getMessage());
        assertEquals(pre + "{'a': 1}", assertThrows(IllegalArgumentException.class, () -> expr(one("k", Map.of("a", 1)))).getMessage());
    }

    @Test
    void milvusFilterMultipleKeysAreAnded() throws Exception {
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("tenant_id", "acme");
        f.put("page", 3);
        assertEquals("tenant_id == \"acme\" and page == 3", expr(f));
    }
}
