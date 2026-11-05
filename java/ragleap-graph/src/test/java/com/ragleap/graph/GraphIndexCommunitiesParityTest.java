package com.ragleap.graph;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * Compares the GraphIndex community methods with the real Python methods (recorded by
 * parity/gen_fixtures_community_index.py): the Cypher texts (whitespace-normalized), every statement with its exact
 * parameters and their types (int vs float), every audit call, every summarizer input, and every result or error.
 */
@EnabledIfEnvironmentVariable(named = "RAGLEAP_GRAPH_PARITY_DIR", matches = ".+")
class GraphIndexCommunitiesParityTest {
    private static final ObjectMapper M = new ObjectMapper();

    private static JsonNode load() throws IOException {
        return M.readTree(Path.of(System.getenv("RAGLEAP_GRAPH_PARITY_DIR"), "community_index_cases.json").toFile());
    }

    private static String norm(String s) {
        return s.trim().replaceAll("\\s+", " ");
    }

    private static JsonNode ev(Object... items) {
        return M.valueToTree(Arrays.asList(items));
    }

    private static String text(JsonNode n) {
        return n == null || n.isNull() ? null : n.asText();
    }

    private static final class Script implements CypherRunner {
        final Map<String, String> nameByNorm;
        final JsonNode script;
        final String failName;
        final int failNth;
        final List<JsonNode> events;
        final Map<String, Integer> counts = new HashMap<>();
        boolean armed;

        Script(Map<String, String> nameByNorm, JsonNode script, JsonNode fail, List<JsonNode> events) {
            this.nameByNorm = nameByNorm;
            this.script = script;
            this.failName = fail == null || fail.isNull() ? null : fail.get(0).asText();
            this.failNth = fail == null || fail.isNull() ? 0 : fail.get(1).asInt();
            this.events = events;
        }

        @Override
        public List<Map<String, Object>> run(String cypher, Map<String, Object> params) {
            if (!armed) {
                return List.of();
            }
            String name = nameByNorm.get(norm(cypher));
            if (name == null) {
                throw new AssertionError("statement not in the Python fixtures: " + cypher);
            }
            events.add(ev("run", name, params));
            int n = counts.merge(name, 1, Integer::sum);
            if (name.equals(failName) && n == failNth) {
                throw new RuntimeException("boom");
            }
            JsonNode lists = script.get(name);
            if (lists == null || lists.size() == 0) {
                return List.of();
            }
            JsonNode rows = lists.get(Math.min(n - 1, lists.size() - 1));
            List<Map<String, Object>> out = new ArrayList<>();
            for (JsonNode r : rows) {
                out.add(M.convertValue(r, new TypeReference<LinkedHashMap<String, Object>>() { }));
            }
            return out;
        }
    }

    @SuppressWarnings("unchecked")
    private static Function<Map<String, Object>, String> summarizer(String kind, List<JsonNode> events) {
        int[] n = {0};
        return info -> {
            n[0]++;
            events.add(ev("summarizer", info));
            switch (kind) {
                case "text":
                case "fail_second":
                    if (kind.equals("fail_second") && n[0] == 2) {
                        throw new RuntimeException("nope");
                    }
                    return "  S:" + info.get("size") + ":" + String.join(",", (List<String>) info.get("display_names"))
                            + "  ";
                case "blank":
                    return "   ";
                case "raise":
                    throw new RuntimeException("nope");
                case "none":
                    return null;
                case "long":
                    return "x".repeat(20005);
                case "astral":
                    return new String(Character.toChars(0x1F600)).repeat(20001);
                default:
                    throw new AssertionError(kind);
            }
        };
    }

    @Test
    void cypherTextsMatchPython() throws IOException {
        JsonNode texts = load().get("texts");
        assertEquals(texts.size(), CommunityStore.STATEMENTS.size());
        texts.fields().forEachRemaining(e -> assertEquals(norm(e.getValue().asText()),
                norm(CommunityStore.STATEMENTS.get(e.getKey())), e.getKey()));
    }

    @Test
    void communityMethodsMatchPython() throws IOException {
        JsonNode root = load();
        Map<String, String> nameByNorm = new HashMap<>();
        root.get("texts").fields().forEachRemaining(e -> nameByNorm.put(norm(e.getValue().asText()), e.getKey()));
        List<String> bad = new ArrayList<>();
        int compared = 0;
        int raised = 0;
        int eventsTotal = 0;
        for (JsonNode sc : root.get("scenarios")) {
            String name = sc.get("name").asText();
            String op = sc.get("op").asText();
            JsonNode kw = sc.get("kwargs");
            List<JsonNode> events = new ArrayList<>();
            AuditSink audit = (u, ns, action, doc, count, detail) -> events.add(ev("audit", action, u, ns, doc, count, detail));
            Script runner = sc.get("driver").asBoolean() ? new Script(nameByNorm, sc.get("script"), sc.get("fail"), events) : null;
            GraphIndex index = new GraphIndex(runner, audit, x -> { });
            if (runner != null) {
                runner.armed = true;
            }
            JsonNode actual = null;
            String message = null;
            try {
                switch (op) {
                    case "detect" -> actual = M.valueToTree(detectCanon(index.detectCommunities(text(kw.get("namespace")),
                            text(kw.get("user_id")), kw.get("resolution").asDouble(), kw.get("min_size").asInt(),
                            kw.get("include_relations").asBoolean(), kw.get("max_edges").asInt())));
                    case "refresh" -> {
                        String kind = text(sc.get("summarizer"));
                        actual = M.valueToTree(refreshCanon(index.refreshCommunities(text(kw.get("namespace")),
                                text(kw.get("user_id")), kw.get("resolution").asDouble(), kw.get("min_size").asInt(),
                                kw.get("include_relations").asBoolean(), kw.get("max_edges").asInt(),
                                kind == null ? null : summarizer(kind, events), kw.get("force").asBoolean(),
                                kw.get("max_summary_items").asInt())));
                    }
                    case "find" -> {
                        List<String> names = null;
                        if (!kw.get("entity_names").isNull()) {
                            names = new ArrayList<>();
                            for (JsonNode n : kw.get("entity_names")) {
                                names.add(n.asText());
                            }
                        }
                        actual = M.valueToTree(findCanon(index.findCommunities(names, text(kw.get("namespace")),
                                text(kw.get("user_id")), kw.get("limit").asInt(), kw.get("include_members").asBoolean())));
                    }
                    default -> throw new AssertionError(op);
                }
            } catch (IllegalArgumentException ex) {
                message = ex.getMessage();
            }
            compared++;
            eventsTotal += events.size();
            if (sc.has("raises")) {
                raised++;
                if (!sc.get("message").asText().equals(message)) {
                    bad.add(name + ": error '" + message + "', Python '" + sc.get("message").asText() + "'");
                }
            } else if (message != null) {
                bad.add(name + ": unexpected error " + message);
            } else if (!sc.get("return").equals(actual)) {
                bad.add(name + ": result " + actual + ", Python " + sc.get("return"));
            }
            if (!sc.get("events").equals(M.valueToTree(events))) {
                bad.add(name + ": events differ, Java " + M.valueToTree(events).toString().substring(0,
                        Math.min(400, M.valueToTree(events).toString().length())));
            }
        }
        System.out.println("community index parity: scenarios compared " + compared + ", errors matched " + raised
                + ", events compared " + eventsTotal);
        assertTrue(compared > 0 && raised > 0 && eventsTotal > 0, "nothing compared");
        assertTrue(bad.isEmpty(), String.join("\n", bad));
    }

    private static List<Object> detectCanon(List<Communities.Community> cs) {
        List<Object> out = new ArrayList<>();
        for (Communities.Community c : cs) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("members", c.members());
            m.put("size", c.size());
            m.put("internal_weight", c.internalWeight());
            m.put("members_hash", c.membersHash());
            out.add(m);
        }
        return out;
    }

    private static Map<String, Object> refreshCanon(CommunityStore.Refresh r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("success", r.success());
        m.put("communities", r.communities());
        m.put("created", r.created());
        m.put("kept", r.kept());
        m.put("removed", r.removed());
        m.put("summarized", r.summarized());
        m.put("summary_errors", r.summaryErrors());
        m.put("error", r.error());
        return m;
    }

    private static List<Object> findCanon(List<CommunityStore.Hit> hits) {
        List<Object> out = new ArrayList<>();
        for (CommunityStore.Hit h : hits) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("community_id", h.communityId());
            m.put("size", h.size());
            m.put("internal_weight", h.internalWeight());
            m.put("summary", h.summary());
            m.put("matched_entities", h.matchedEntities());
            if (h.members() != null) {
                m.put("members", h.members());
            }
            out.add(m);
        }
        return out;
    }
}
