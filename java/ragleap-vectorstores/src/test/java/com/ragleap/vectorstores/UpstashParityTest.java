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
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.SQLException;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Pattern;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * Replays the scripted scenarios of the real Python UpstashBackend (real upstash-vector 0.8.0 SDK, real SQLite
 * registry, fake HTTP server) against the Java backend and a JDK fake server: requests and results must be identical.
 * Scenarios marked lenient (odd namespace characters) compare the percent-DECODED request path, because
 * java.net.URI cannot send some characters the SDK sends raw. Not live-verified: no real Upstash index.
 */
@EnabledIfEnvironmentVariable(named = "RAGLEAP_VS_PARITY_DIR", matches = ".+")
class UpstashParityTest {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Pattern ISO = Pattern.compile("^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{6})?$");
    private static final Set<String> MAPPED = Set.of("RemoteError", "IntegrityError", "RuntimeError", "NotInitialized", "ValueError");

    private static final class FakeServer implements AutoCloseable {
        final HttpServer server;
        final List<ObjectNode> log = Collections.synchronizedList(new ArrayList<>());
        volatile Deque<JsonNode> script = new ArrayDeque<>();
        volatile int unscripted = 0;
        volatile int badAuth = 0;

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
            badAuth = 0;
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
            String full = ex.getRequestURI().toString();   // toString keeps "//info" intact, getRawPath would not
            int q = full.indexOf('?');
            entry.put("path", q < 0 ? full : full.substring(0, q));
            entry.set("body", raw.length == 0 ? NullNode.getInstance() : MAPPER.readTree(raw));
            if (!"Bearer tok".equals(ex.getRequestHeaders().getFirst("Authorization"))) {
                badAuth++;
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
            if (resp == null || resp.isNull()) {
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

    private static String decodeValid(String s) {
        byte[] in = s.getBytes(StandardCharsets.UTF_8);
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        for (int i = 0; i < in.length; i++) {
            if (in[i] == '%' && i + 2 < in.length && Character.digit((char) in[i + 1], 16) >= 0
                    && Character.digit((char) in[i + 2], 16) >= 0) {
                b.write((Character.digit((char) in[i + 1], 16) << 4) | Character.digit((char) in[i + 2], 16));
                i += 2;
            } else {
                b.write(in[i]);
            }
        }
        return new String(b.toByteArray(), StandardCharsets.UTF_8);
    }

    private static JsonNode forCompare(JsonNode log, boolean decode) throws IOException {
        ArrayNode out = MAPPER.createArrayNode();
        for (JsonNode e : norm(log)) {
            ObjectNode o = (ObjectNode) e;
            if (decode) {
                o.put("path", decodeValid(o.get("path").asText()));
            }
            out.add(o);
        }
        return out;
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
            String iso = UpstashBackend.pyIsoNaive(d.uploadedAt());
            assertTrue(ISO.matcher(iso).matches(), "uploaded_at format: " + iso);
            o.put("uploaded_at", "<iso-naive>");
            o.set("metadata", MAPPER.valueToTree(d.metadata()));
            o.put("chunk_count", d.chunkCount());
        }
        return a;
    }

    private static ObjectNode err(String name) {
        ObjectNode e = MAPPER.createObjectNode();
        e.put("error", name);
        return e;
    }

    private static ObjectNode runOp(UpstashBackend be, JsonNode op, String url) {
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
            return err(call.equals("insert_document") ? "IntegrityError" : "RemoteError");
        } catch (IllegalStateException e) {
            if (call.equals("init_schema")) {
                ObjectNode o = err("RuntimeError");
                o.put("message", e.getMessage().replace(url, "<url>"));
                return o;
            }
            return err("NotInitialized");
        } catch (IllegalArgumentException e) {
            ObjectNode o = err("ValueError");
            o.put("message", e.getMessage().length() > 120 ? e.getMessage().substring(0, 120) : e.getMessage());
            return o;
        }
        return out;
    }

    /** Java returns an int chunk index (SearchResult); Python passes null, strings and floats through unchanged. */
    private static JsonNode normExpected(String call, JsonNode result) {
        JsonNode copy = result.deepCopy();
        if (call.equals("search_dense") && copy.has("ok")) {
            for (JsonNode o : copy.get("ok")) {
                JsonNode c = o.get("chunk_index");
                int v = c == null || c.isNull() ? 0 : c.isNumber() ? c.intValue() : Integer.parseInt(c.asText());
                ((ObjectNode) o).put("chunk_index", v);
            }
        }
        return copy;
    }

    private static JsonNode fixtures() throws IOException {
        return MAPPER.readTree(Path.of(System.getenv("RAGLEAP_VS_PARITY_DIR"), "upstash_cases.json").toFile());
    }

    @Test
    void requestsAndResultsMatchPython() throws Exception {
        JsonNode fx = fixtures();
        Path tmp = Files.createTempDirectory("vs-upstash-java-");
        try (FakeServer fake = new FakeServer()) {
            int scenarios = 0;
            int requests = 0;
            int i = 0;
            for (JsonNode c : fx.get("cases")) {
                String name = c.get("name").asText();
                i++;
                if (c.get("ctor").path("raw").asBoolean(false)) {
                    System.out.println("upstash parity: " + name + " is checked by the raw-socket test");
                    continue;
                }
                boolean unmappable = false;
                for (JsonNode r : c.get("results")) {
                    if (r.has("error") && !MAPPED.contains(r.get("error").asText())) {
                        unmappable = true;
                    }
                }
                if (unmappable) {
                    System.out.println("upstash parity: skipped " + name + " (Python raised " + c.get("results") + ")");
                    continue;
                }
                fake.reset(c.get("script"));
                JsonNode ctor = c.get("ctor");
                String ns = ctor.hasNonNull("namespace") ? ctor.get("namespace").asText() : "";
                String suffix = ctor.hasNonNull("url_suffix") ? ctor.get("url_suffix").asText() : "";
                boolean lenient = c.path("lenient").asBoolean(false);
                try (UpstashBackend be = new UpstashBackend(fake.url() + suffix, "tok", ns,
                        tmp.resolve("reg" + i + ".sqlite3").toString())) {
                    ArrayNode actual = MAPPER.createArrayNode();
                    ArrayNode expected = MAPPER.createArrayNode();
                    JsonNode ops = c.get("ops");
                    for (int k = 0; k < ops.size(); k++) {
                        actual.add(runOp(be, ops.get(k), fake.url()));
                        expected.add(normExpected(ops.get(k).get("call").asText(), c.get("results").get(k)));
                    }
                    assertEquals(norm(expected), norm(actual), name + ": results");
                }
                assertEquals(forCompare(c.get("log"), lenient), forCompare(fake.logArray(), lenient), name + ": requests");
                assertEquals(0, fake.unscripted, name + ": unscripted request");
                assertEquals(0, fake.badAuth, name + ": wrong Authorization header");
                if (!lenient) {
                    assertTrue(fake.script.isEmpty(), name + ": scripted answers left over");
                }
                scenarios++;
                requests += c.get("log").size();
            }
            System.out.println("upstash parity: " + scenarios + " scenarios, " + requests + " requests");
        }
    }

    private static String readLine(java.io.InputStream in) throws IOException {
        StringBuilder sb = new StringBuilder();
        int b;
        while ((b = in.read()) >= 0) {
            if (b == '\n') {
                break;
            }
            if (b != '\r') {
                sb.append((char) b);
            }
        }
        return b < 0 && sb.length() == 0 ? null : sb.toString();
    }

    /**
     * A url that ends in a slash makes the SDK send "//info": the url is used exactly as given. A raw socket server
     * reads the literal request line (the JDK HttpServer cannot serve a double slash path).
     */
    @Test
    void trailingSlashUrlSendsDoubleSlashPathsLikeTheSdk() throws Exception {
        JsonNode c = null;
        for (JsonNode x : fixtures().get("cases")) {
            if (x.get("name").asText().equals("trailing_slash_url")) {
                c = x;
            }
        }
        assertTrue(c != null, "scenario trailing_slash_url is missing from the fixtures");
        Deque<JsonNode> script = new ArrayDeque<>();
        c.get("script").forEach(script::add);
        List<String> lines = Collections.synchronizedList(new ArrayList<>());
        List<JsonNode> bodies = Collections.synchronizedList(new ArrayList<>());
        Path tmp = Files.createTempDirectory("vs-upstash-raw-");
        try (java.net.ServerSocket ss = new java.net.ServerSocket(0, 50, java.net.InetAddress.getByName("127.0.0.1"))) {
            Thread t = new Thread(() -> {
                try {
                    while (!script.isEmpty()) {
                        try (java.net.Socket s = ss.accept()) {
                            java.io.InputStream in = s.getInputStream();
                            String requestLine = readLine(in);
                            int length = 0;
                            String h;
                            while ((h = readLine(in)) != null && !h.isEmpty()) {
                                if (h.toLowerCase().startsWith("content-length:")) {
                                    length = Integer.parseInt(h.substring(15).trim());
                                }
                            }
                            byte[] body = in.readNBytes(length);
                            lines.add(requestLine);
                            bodies.add(body.length == 0 ? NullNode.getInstance() : MAPPER.readTree(body));
                            JsonNode scripted = script.pollFirst();
                            byte[] data = MAPPER.writeValueAsBytes(scripted.get(1));
                            String head = "HTTP/1.1 " + scripted.get(0).asInt() + " X\r\nContent-Type: application/json\r\n"
                                    + "Content-Length: " + data.length + "\r\nConnection: close\r\n\r\n";
                            s.getOutputStream().write(head.getBytes(StandardCharsets.US_ASCII));
                            s.getOutputStream().write(data);
                            s.getOutputStream().flush();
                        }
                    }
                } catch (IOException | RuntimeException e) {
                    e.printStackTrace();
                }
            });
            t.start();
            try (UpstashBackend be = new UpstashBackend("http://127.0.0.1:" + ss.getLocalPort() + "/", "tok", "",
                    tmp.resolve("r.sqlite3").toString())) {
                be.initSchema(8);
                be.insertChunk("d1", "f.txt", 0, "t", 1, List.of(0.5), Map.of());
            }
            t.join(5000);
        }
        JsonNode log = c.get("log");
        assertEquals(log.size(), lines.size(), "number of requests");
        for (int i = 0; i < log.size(); i++) {
            assertEquals(log.get(i).get("method").asText() + " " + log.get(i).get("path").asText() + " HTTP/1.1",
                    lines.get(i), "request line " + i);
            assertEquals(norm(log.get(i).get("body")), norm(bodies.get(i)), "body " + i);
        }
        assertEquals("POST //info HTTP/1.1", lines.get(0));
    }

    @Test
    void constructorErrorsMatchPython() throws Exception {
        Path tmp = Files.createTempDirectory("vs-upstash-ctor-");
        int n = 0;
        for (JsonNode c : fixtures().get("ctor_cases")) {
            String url = c.get("url").isNull() ? null : c.get("url").asText();
            String token = c.get("token").isNull() ? null : c.get("token").asText();
            IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                    () -> new UpstashBackend(url, token, "", tmp.resolve("c.sqlite3").toString()));
            assertEquals(c.get("result").get("message").asText(), e.getMessage());
            n++;
        }
        System.out.println("upstash ctor parity: " + n + " cases");
    }
}
