package com.ragleap.vectorstores;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.NullNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.ragleap.rag.vectorstore.DocumentSummary;
import com.ragleap.rag.vectorstore.SearchResult;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.sql.SQLException;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.regex.Pattern;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * Replays the scripted scenarios of the real Python OpenSearchBackend (real opensearch-py 3.2.0 client, fake HTTP
 * server) against the Java backend and a JDK fake server: the recorded requests and the results must be identical.
 * Not live-verified: no real OpenSearch.
 */
@EnabledIfEnvironmentVariable(named = "RAGLEAP_VS_PARITY_DIR", matches = ".+")
class OpenSearchParityTest {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Pattern ISO = Pattern.compile("^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{6})?\\+00:00$");

    private static final class FakeServer implements AutoCloseable {
        final HttpServer server;
        final List<ObjectNode> log = Collections.synchronizedList(new ArrayList<>());
        volatile Deque<JsonNode> script = new ArrayDeque<>();
        volatile int unscripted = 0;

        FakeServer() throws IOException {
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.createContext("/", ex -> {
                try {
                    handle(ex);
                } catch (RuntimeException e) {
                    e.printStackTrace();
                    ex.sendResponseHeaders(500, -1);
                } finally {
                    ex.close();
                }
            });
            server.start();
        }

        String url() {
            return "http://127.0.0.1:" + server.getAddress().getPort();
        }

        void reset(JsonNode scriptNode) {
            log.clear();
            unscripted = 0;
            Deque<JsonNode> d = new ArrayDeque<>();
            scriptNode.forEach(d::add);
            script = d;
        }

        ArrayNode logArray() {
            ArrayNode a = MAPPER.createArrayNode();
            synchronized (log) {
                log.forEach(a::add);
            }
            return a;
        }

        private void handle(HttpExchange ex) throws IOException {
            byte[] raw = ex.getRequestBody().readAllBytes();
            ObjectNode entry = MAPPER.createObjectNode();
            entry.put("method", ex.getRequestMethod());
            URI u = ex.getRequestURI();
            entry.put("path", u.getRawPath());
            List<List<String>> pairs = new ArrayList<>();
            String rq = u.getRawQuery();
            if (rq != null && !rq.isEmpty()) {
                for (String part : rq.split("&")) {
                    int eq = part.indexOf('=');
                    String k = eq < 0 ? part : part.substring(0, eq);
                    String v = eq < 0 ? "" : part.substring(eq + 1);
                    pairs.add(List.of(URLDecoder.decode(k, StandardCharsets.UTF_8), URLDecoder.decode(v, StandardCharsets.UTF_8)));
                }
            }
            pairs.sort(Comparator.<List<String>, String>comparing(p -> p.get(0)).thenComparing(p -> p.get(1)));
            ArrayNode q = entry.putArray("query");
            for (List<String> p : pairs) {
                q.addArray().add(p.get(0)).add(p.get(1));
            }
            JsonNode body = raw.length == 0 ? NullNode.getInstance() : MAPPER.readTree(raw);
            if (body.isObject() && body.path("uploaded_at").isTextual()
                    && ISO.matcher(body.get("uploaded_at").asText()).matches()) {
                ((ObjectNode) body).put("uploaded_at", "<iso-utc-micro>");
            }
            entry.set("body", body);
            String auth = ex.getRequestHeaders().getFirst("Authorization");
            if (auth == null) {
                entry.putNull("authorization");
            } else {
                entry.put("authorization", auth);
            }
            log.add(entry);
            JsonNode scripted = script.pollFirst();
            int status;
            JsonNode resp;
            if (scripted == null) {
                unscripted++;
                status = 418;
                resp = NullNode.getInstance();
            } else {
                status = scripted.get(0).asInt();
                resp = scripted.get(1);
            }
            if (resp == null || resp.isNull() || ex.getRequestMethod().equals("HEAD")) {
                ex.sendResponseHeaders(status, -1);
            } else {
                byte[] data = MAPPER.writeValueAsBytes(resp);
                ex.getResponseHeaders().set("Content-Type", "application/json");
                ex.sendResponseHeaders(status, data.length);
                ex.getResponseBody().write(data);
            }
        }

        @Override
        public void close() {
            server.stop(0);
        }
    }

    private static JsonNode norm(JsonNode n) throws IOException {
        return MAPPER.readTree(MAPPER.writeValueAsString(n));
    }

    private static String text(JsonNode n) {
        return n.isNull() ? null : n.asText();
    }

    private static Map<String, Object> meta(JsonNode n) {
        return n.isNull() ? null : MAPPER.convertValue(n, new TypeReference<LinkedHashMap<String, Object>>() {
        });
    }

    private static List<Double> embedding(JsonNode n) {
        return MAPPER.convertValue(n, new TypeReference<List<Double>>() {
        });
    }

    private static ArrayNode searchJson(List<SearchResult> rs) {
        ArrayNode a = MAPPER.createArrayNode();
        for (SearchResult r : rs) {
            ObjectNode o = a.addObject();
            o.put("chunk_id", r.chunkId());
            o.put("text", r.text());
            o.put("similarity_score", r.similarityScore());
            o.put("document_id", r.documentId());
            o.put("document_name", r.documentName());
            o.put("chunk_index", r.chunkIndex());
        }
        return a;
    }

    private static ArrayNode listJson(List<DocumentSummary> ds) {
        ArrayNode a = MAPPER.createArrayNode();
        for (DocumentSummary d : ds) {
            ObjectNode o = a.addObject();
            o.put("document_id", d.documentId());
            o.put("filename", d.filename());
            o.put("uploaded_at", d.uploadedAt() == null ? null : OpenSearchBackend.pyIso(d.uploadedAt()));
            o.set("metadata", MAPPER.valueToTree(d.metadata()));
            o.put("chunk_count", d.chunkCount());
        }
        return a;
    }

    private static ObjectNode runOp(OpenSearchBackend be, JsonNode op) {
        ObjectNode out = MAPPER.createObjectNode();
        String call = op.get("call").asText();
        JsonNode a = op.get("args");
        try {
            switch (call) {
                case "init_schema" -> {
                    be.initSchema(a.get(0).asInt());
                    out.putNull("ok");
                }
                case "insert_document" -> {
                    be.insertDocument(text(a.get(0)), text(a.get(1)), meta(a.get(2)));
                    out.putNull("ok");
                }
                case "insert_chunk" -> {
                    be.insertChunk(text(a.get(0)), text(a.get(1)), a.get(2).asInt(), text(a.get(3)),
                            a.get(4).isNull() ? null : a.get(4).asInt(), embedding(a.get(5)), meta(a.get(6)));
                    out.putNull("ok");
                }
                case "search_dense" -> out.set("ok", searchJson(be.searchDense(embedding(a.get(0)), a.get(1).asInt(), meta(a.get(2)))));
                case "supports_sparse" -> out.put("ok", be.supportsSparse());
                case "list_documents" -> out.set("ok", listJson(be.listDocuments(a.get(0).asInt(), a.get(1).asInt())));
                case "delete_document" -> out.put("ok", be.deleteDocument(text(a.get(0))));
                case "get_document_filename" -> {
                    Optional<String> f = be.getDocumentFilename(text(a.get(0)));
                    if (f.isPresent()) {
                        out.put("ok", f.get());
                    } else {
                        out.putNull("ok");
                    }
                }
                default -> throw new UnsupportedOperationException("unknown op " + call);
            }
        } catch (SQLException e) {
            ObjectNode err = MAPPER.createObjectNode();
            err.put("error", "TransportError");
            return err;
        } catch (IllegalStateException e) {
            ObjectNode err = MAPPER.createObjectNode();
            err.put("error", "RuntimeError");
            err.put("message", e.getMessage());
            return err;
        } catch (IllegalArgumentException e) {
            ObjectNode err = MAPPER.createObjectNode();
            err.put("error", "ValueError");
            err.put("message", e.getMessage());
            return err;
        }
        return out;
    }

    private static JsonNode fixtures() throws IOException {
        return MAPPER.readTree(Path.of(System.getenv("RAGLEAP_VS_PARITY_DIR"), "opensearch_cases.json").toFile());
    }

    @Test
    void requestsAndResultsMatchPython() throws Exception {
        JsonNode fx = fixtures();
        try (FakeServer fake = new FakeServer()) {
            int scenarios = 0;
            int requests = 0;
            for (JsonNode c : fx.get("cases")) {
                String name = c.get("name").asText();
                fake.reset(c.get("script"));
                JsonNode ctor = c.get("ctor");
                String idx = ctor.has("index_name") ? ctor.get("index_name").asText() : "ragleap_idx";
                String docs = ctor.hasNonNull("documents_index_name") ? ctor.get("documents_index_name").asText() : null;
                String user = null;
                String pw = null;
                if (ctor.hasNonNull("http_auth")) {
                    user = ctor.get("http_auth").get(0).asText();
                    pw = ctor.get("http_auth").get(1).asText();
                }
                OpenSearchBackend be = new OpenSearchBackend(fake.url(), idx, docs, false, user, pw);
                ArrayNode actual = MAPPER.createArrayNode();
                for (JsonNode op : c.get("ops")) {
                    actual.add(runOp(be, op));
                }
                assertEquals(norm(c.get("results")), norm(actual), name + ": results");
                assertEquals(norm(c.get("log")), norm(fake.logArray()), name + ": requests");
                assertEquals(0, fake.unscripted, name + ": unscripted request");
                assertTrue(fake.script.isEmpty(), name + ": scripted answers left over");
                scenarios++;
                requests += c.get("log").size();
            }
            System.out.println("opensearch parity: " + scenarios + " scenarios, " + requests + " requests");
            assertEquals(fx.get("cases").size(), scenarios);
        }
    }

    @Test
    void constructorErrorsMatchPython() throws Exception {
        for (JsonNode c : fixtures().get("ctor_cases")) {
            String url = c.get("url").isNull() ? null : c.get("url").asText();
            IllegalArgumentException e = assertThrows(IllegalArgumentException.class, () -> new OpenSearchBackend(url));
            assertEquals("ValueError", c.get("result").get("error").asText());
            assertEquals(c.get("result").get("message").asText(), e.getMessage());
        }
    }

    @Test
    void parseUrlMatchesPython() throws Exception {
        int n = 0;
        for (JsonNode c : fixtures().get("parse_cases")) {
            String url = c.get("url").asText();
            JsonNode res = c.get("result");
            if (res.has("ok")) {
                OpenSearchBackend.HostPort hp = OpenSearchBackend.parseUrl(url);
                assertEquals(res.get("ok").get(0).asText(), hp.host(), "host of " + url);
                assertEquals(res.get("ok").get(1).asText(), hp.port().toString(), "port of " + url);
            } else {
                assertThrows(IllegalArgumentException.class, () -> OpenSearchBackend.parseUrl(url), "expected error for " + url);
            }
            n++;
        }
        System.out.println("opensearch parse parity: " + n + " urls");
    }
}
