package com.ragleap.graph;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.nio.file.Path;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * Compares the temporal statements with the real Python code (recorded by parity/gen_fixtures_temporal.py): the statement
 * texts (whitespace-normalized) and, for find_relations, the exact statement and parameters of 36 call shapes. One known
 * difference: Java reads the as_of parameter with datetime($as_of) and sends an ISO string (the HTTP Query API takes no
 * typed parameters), so Python's "$as_of" is replaced by "datetime($as_of)" before comparing and the instants are compared.
 */
@EnabledIfEnvironmentVariable(named = "RAGLEAP_GRAPH_PARITY_DIR", matches = ".+")
class TemporalParityTest {
    private static final ObjectMapper M = new ObjectMapper();

    private static JsonNode load() throws IOException {
        return M.readTree(Path.of(System.getenv("RAGLEAP_GRAPH_PARITY_DIR"), "temporal_cases.json").toFile());
    }

    private static String norm(String s) {
        return s.trim().replaceAll("\\s+", " ");
    }

    @Test
    void statementTextsMatchPython() throws IOException {
        JsonNode t = load().get("texts");
        assertEquals(norm(t.get("refresh_temporal").asText()), norm(GraphIndex.RECOMPUTE_RELATES_AS_TEMPORAL));
        assertEquals(norm(t.get("comm_edges_open").asText()), norm(CommunityStore.RELATES_EDGES_OPEN));
        assertEquals(norm(t.get("comm_relations_open").asText()), norm(CommunityStore.RELATIONS_OPEN));
    }

    @Test
    void findRelationsMatchesPython() throws IOException {
        JsonNode cases = load().get("cases");
        List<String> bad = new ArrayList<>();
        int withAsOf = 0;
        for (JsonNode c : cases) {
            TemporalRelationsTest.Capture cap = new TemporalRelationsTest.Capture();
            GraphIndex g = new GraphIndex(cap, AuditSink.NOOP, x -> { }, ExtractionConfig.regex(), c.get("temporal").asBoolean());
            cap.armed = true;
            OffsetDateTime asOf = c.get("as_of").isNull() ? null : OffsetDateTime.parse(c.get("as_of").asText());
            String rtype = c.get("relation_type").isNull() ? null : c.get("relation_type").asText();
            g.findRelations("Acme Corp", rtype, "ns", null, 25, c.get("direction").asText(), asOf);
            String name = c.get("temporal") + "/" + c.get("direction") + "/" + rtype + "/" + c.get("as_of");
            JsonNode py = c.get("calls").get(0);
            if (cap.queries.size() != 1) {
                bad.add(name + ": " + cap.queries.size() + " statements");
                continue;
            }
            String pyQuery = norm(py.get(0).asText()).replace("$as_of", "datetime($as_of)");
            if (!pyQuery.equals(norm(cap.queries.get(0)))) {
                bad.add(name + ": statement differs\n  python: " + pyQuery + "\n  java:   " + norm(cap.queries.get(0)));
            }
            Map<String, Object> jp = cap.params.get(0);
            JsonNode pp = py.get(1);
            if (jp.size() != pp.size()) {
                bad.add(name + ": parameter names " + jp.keySet() + " vs " + pp);
                continue;
            }
            for (Map.Entry<String, Object> e : jp.entrySet()) {
                JsonNode want = pp.get(e.getKey());
                if (want == null) {
                    bad.add(name + ": unexpected parameter " + e.getKey());
                } else if (e.getKey().equals("as_of")) {
                    withAsOf++;
                    if (!OffsetDateTime.parse((String) e.getValue()).toInstant().equals(OffsetDateTime.parse(want.asText()).toInstant())) {
                        bad.add(name + ": as_of " + e.getValue() + " vs " + want.asText());
                    }
                } else if (want.isNull() ? e.getValue() != null : !String.valueOf(e.getValue()).equals(want.asText())) {
                    bad.add(name + ": parameter " + e.getKey() + " " + e.getValue() + " vs " + want);
                }
            }
        }
        System.out.println("temporal parity: cases " + cases.size() + ", as_of parameters compared " + withAsOf + ", differing " + bad.size());
        assertTrue(cases.size() == 36 && withAsOf == 24, "cases " + cases.size() + ", as_of " + withAsOf);
        assertTrue(bad.isEmpty(), String.join("\n", bad.subList(0, Math.min(3, bad.size()))));
    }
}
