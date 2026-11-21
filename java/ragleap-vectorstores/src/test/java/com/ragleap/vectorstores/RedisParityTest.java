package com.ragleap.vectorstores;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.ragleap.rag.vectorstore.DocumentSummary;
import com.ragleap.rag.vectorstore.SearchResult;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.SQLException;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Pattern;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * Replays the scripted scenarios of the real Python RedisBackend (real redis-py client forced to RESP2, real SQLite
 * registry, fake RESP server) against the Java backend and its own fake RESP server: the commands (arguments as hex)
 * and the results must be identical. Errors that Python raises on malformed server data (AttributeError, ValueError)
 * are compared as "an error". Not live-verified: no real Redis Stack.
 */
@EnabledIfEnvironmentVariable(named = "RAGLEAP_VS_PARITY_DIR", matches = ".+")
class RedisParityTest {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Pattern ISO = Pattern.compile("^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{6})?$");
    private static final Set<String> MAPPED = Set.of("RemoteError", "IntegrityError", "RuntimeError", "NotInitialized",
            "AttributeError", "ValueError");

    private static final class FakeResp implements AutoCloseable {
        final ServerSocket ss;
        final List<ObjectNode> log = Collections.synchronizedList(new ArrayList<>());
        volatile Deque<JsonNode> script = new ArrayDeque<>();
        volatile int unscripted = 0;

        FakeResp() throws IOException {
            ss = new ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"));
            Thread accept = new Thread(() -> {
                while (!ss.isClosed()) {
                    try {
                        Socket c = ss.accept();
                        Thread t = new Thread(() -> serve(c));
                        t.setDaemon(true);
                        t.start();
                    } catch (IOException e) {
                        return;
                    }
                }
            });
            accept.setDaemon(true);
            accept.start();
        }

        String url() {
            return "redis://127.0.0.1:" + ss.getLocalPort() + "/0";
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

        private static String readLine(InputStream in) throws IOException {
            StringBuilder sb = new StringBuilder();
            int b;
            while ((b = in.read()) >= 0) {
                if (b == '\r') {
                    in.read();
                    return sb.toString();
                }
                sb.append((char) b);
            }
            return null;
        }

        private void serve(Socket c) {
            try (c) {
                InputStream in = new java.io.BufferedInputStream(c.getInputStream());
                while (true) {
                    String line = readLine(in);
                    if (line == null || !line.startsWith("*")) {
                        return;
                    }
                    int n = Integer.parseInt(line.substring(1));
                    List<byte[]> args = new ArrayList<>();
                    for (int i = 0; i < n; i++) {
                        int len = Integer.parseInt(readLine(in).substring(1));
                        args.add(in.readNBytes(len));
                        in.readNBytes(2);
                    }
                    String cmd = new String(args.get(0), StandardCharsets.ISO_8859_1).toUpperCase();
                    if (cmd.equals("HELLO") || cmd.equals("CLIENT")) {
                        c.getOutputStream().write((cmd.equals("HELLO") ? "-ERR unknown command\r\n" : "+OK\r\n")
                                .getBytes(StandardCharsets.US_ASCII));
                        continue;
                    }
                    ObjectNode entry = MAPPER.createObjectNode();
                    ArrayNode hex = entry.putArray("cmd");
                    for (byte[] a : args) {
                        hex.add(HexFormat.of().formatHex(a));
                    }
                    log.add(entry);
                    JsonNode scripted = script.pollFirst();
                    ByteArrayOutputStream o = new ByteArrayOutputStream();
                    if (scripted == null) {
                        unscripted++;
                        o.write("-ERR unscripted\r\n".getBytes(StandardCharsets.US_ASCII));
                    } else {
                        enc(o, scripted);
                    }
                    c.getOutputStream().write(o.toByteArray());
                    c.getOutputStream().flush();
                }
            } catch (IOException | RuntimeException e) {
                // connection closed by the test
            }
        }

        private static void enc(ByteArrayOutputStream o, JsonNode s) {
            String t = s.get(0).asText();
            switch (t) {
                case "s" -> o.writeBytes(("+" + s.get(1).asText() + "\r\n").getBytes(StandardCharsets.UTF_8));
                case "e" -> o.writeBytes(("-" + s.get(1).asText() + "\r\n").getBytes(StandardCharsets.UTF_8));
                case "i" -> o.writeBytes((":" + s.get(1).asLong() + "\r\n").getBytes(StandardCharsets.US_ASCII));
                case "n" -> o.writeBytes("$-1\r\n".getBytes(StandardCharsets.US_ASCII));
                case "b" -> {
                    byte[] d = s.get(1).asText().getBytes(StandardCharsets.UTF_8);
                    o.writeBytes(("$" + d.length + "\r\n").getBytes(StandardCharsets.US_ASCII));
                    o.writeBytes(d);
                    o.writeBytes("\r\n".getBytes(StandardCharsets.US_ASCII));
                }
                case "a" -> {
                    JsonNode a = s.get(1);
                    o.writeBytes(("*" + a.size() + "\r\n").getBytes(StandardCharsets.US_ASCII));
                    for (JsonNode x : a) {
                        enc(o, x);
                    }
                }
                default -> throw new IllegalArgumentException("unknown reply kind " + t);
            }
        }

        @Override
        public void close() throws IOException {
            ss.close();
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

    private static ObjectNode runOp(RedisBackend be, JsonNode op, String url) {
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
            if (call.equals("init_schema") || call.equals("search_dense")) {
                ObjectNode o = err("RuntimeError");
                o.put("message", e.getMessage().replace(url, "<url>"));
                return o;
            }
            return err("NotInitialized");
        } catch (IllegalArgumentException e) {
            ObjectNode o = err("ValueError");
            o.put("message", e.getMessage());
            return o;
        }
        return out;
    }

    /** Python raises AttributeError or ValueError on malformed search replies; Java throws SQLException for both. */
    private static JsonNode normExpected(String call, JsonNode result) {
        if (call.equals("search_dense") && result.has("error")) {
            String e = result.get("error").asText();
            if (e.equals("AttributeError") || (e.equals("ValueError") && !result.has("message"))) {
                return err("RemoteError");
            }
        }
        return result;
    }

    private static JsonNode fixtures() throws IOException {
        return MAPPER.readTree(Path.of(System.getenv("RAGLEAP_VS_PARITY_DIR"), "redis_cases.json").toFile());
    }

    @Test
    void commandsAndResultsMatchPython() throws Exception {
        JsonNode fx = fixtures();
        Path tmp = Files.createTempDirectory("vs-redis-java-");
        try (FakeResp fake = new FakeResp()) {
            int scenarios = 0;
            int commands = 0;
            int i = 0;
            for (JsonNode c : fx.get("cases")) {
                String name = c.get("name").asText();
                i++;
                for (JsonNode r : c.get("results")) {
                    if (r.has("error")) {
                        assertTrue(MAPPED.contains(r.get("error").asText()), name + ": unmapped Python error " + r);
                    }
                }
                fake.reset(c.get("script"));
                JsonNode ctor = c.get("ctor");
                String idx = ctor.hasNonNull("index_name") ? ctor.get("index_name").asText() : null;
                String prefix = ctor.hasNonNull("key_prefix") ? ctor.get("key_prefix").asText() : null;
                try (RedisBackend be = new RedisBackend(fake.url(), idx, prefix, tmp.resolve("reg" + i + ".sqlite3").toString())) {
                    ArrayNode actual = MAPPER.createArrayNode();
                    ArrayNode expected = MAPPER.createArrayNode();
                    JsonNode ops = c.get("ops");
                    for (int k = 0; k < ops.size(); k++) {
                        actual.add(runOp(be, ops.get(k), fake.url()));
                        expected.add(normExpected(ops.get(k).get("call").asText(), c.get("results").get(k)));
                    }
                    assertEquals(norm(expected), norm(actual), name + ": results");
                }
                assertEquals(norm(c.get("log")), norm(fake.logArray()), name + ": commands");
                assertEquals(0, fake.unscripted, name + ": unscripted command");
                assertTrue(fake.script.isEmpty(), name + ": scripted replies left over");
                scenarios++;
                commands += c.get("log").size();
            }
            System.out.println("redis parity: " + scenarios + " scenarios, " + commands + " commands");
        }
    }

    @Test
    void constructorErrorsMatchPython() throws Exception {
        Path tmp = Files.createTempDirectory("vs-redis-ctor-");
        int n = 0;
        for (JsonNode c : fixtures().get("ctor_cases")) {
            String url = c.get("url").isNull() ? null : c.get("url").asText();
            IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                    () -> new RedisBackend(url, "ragleap_idx", null, tmp.resolve("c.sqlite3").toString()));
            assertEquals(c.get("result").get("message").asText(), e.getMessage());
            n++;
        }
        System.out.println("redis ctor parity: " + n + " cases");
    }
}
