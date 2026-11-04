package com.ragleap.graph;

import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * Compares Communities with the real Python code (recorded by parity/gen_fixtures_communities.py): every community,
 * every float bit, every error. Python drops bool, str and None weights; Java edges are doubles, so those edges are
 * left out here (the recorded Python result ignores them too). Scenarios whose resolution or min_size has no Java type
 * (bool, str, None, a non-integer min_size) are skipped.
 */
@EnabledIfEnvironmentVariable(named = "RAGLEAP_GRAPH_PARITY_DIR", matches = ".+")
class CommunitiesParityTest {
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static JsonNode load() throws IOException {
        return MAPPER.readTree(Path.of(System.getenv("RAGLEAP_GRAPH_PARITY_DIR"), "communities_cases.json").toFile());
    }

    /** The tagged number as a double, or null when it has no Java equivalent. */
    private static Double number(JsonNode tag) {
        switch (tag.get("k").asText()) {
            case "num":
                return tag.get("v").asDouble();
            case "nan":
                return Double.NaN;
            case "inf":
                return Double.POSITIVE_INFINITY;
            case "ninf":
                return Double.NEGATIVE_INFINITY;
            default:
                return null;
        }
    }

    private static List<Communities.Edge> edges(JsonNode arr) {
        List<Communities.Edge> out = new ArrayList<>();
        for (JsonNode e : arr) {
            Double w = number(e.get(2));
            if (w != null) {
                out.add(new Communities.Edge(e.get(0).asText(), e.get(1).asText(), w));
            }
        }
        return out;
    }

    private static List<String> strings(JsonNode arr) {
        List<String> out = new ArrayList<>();
        arr.forEach(x -> out.add(x.asText()));
        return out;
    }

    private static List<List<String>> partition(JsonNode arr) {
        List<List<String>> out = new ArrayList<>();
        arr.forEach(x -> out.add(strings(x)));
        return out;
    }

    private static boolean sameDouble(double expected, double actual) {
        return Double.compare(expected, actual) == 0;
    }

    @Test
    void detectAndModularityMatchPython() throws IOException {
        JsonNode root = load();
        List<String> bad = new ArrayList<>();
        int checked = 0;
        int errors = 0;
        for (JsonNode sc : root.get("detect")) {
            if (!sc.get("java").asBoolean()) {
                continue;
            }
            String name = sc.get("name").asText();
            double resolution = number(sc.get("resolution"));
            int minSize = sc.get("min_size").get("v").asInt();
            List<Communities.Edge> es = edges(sc.get("edges"));
            checked++;
            if (sc.get("ok").asBoolean()) {
                List<Communities.Community> got;
                try {
                    got = Communities.detect(es, resolution, minSize);
                } catch (RuntimeException ex) {
                    bad.add(name + ": unexpected " + ex);
                    continue;
                }
                JsonNode want = sc.get("result");
                if (got.size() != want.size()) {
                    bad.add(name + ": " + got.size() + " communities, Python " + want.size());
                    continue;
                }
                for (int i = 0; i < got.size(); i++) {
                    JsonNode w = want.get(i);
                    Communities.Community g = got.get(i);
                    if (!g.members().equals(strings(w.get("members"))) || g.size() != w.get("size").asInt()
                            || !sameDouble(w.get("internal_weight").asDouble(), g.internalWeight())
                            || !g.membersHash().equals(w.get("members_hash").asText())) {
                        bad.add(name + ": community " + i + " differs");
                    }
                }
                double mod = Communities.modularity(es, partition(sc.get("partition")), resolution);
                if (!sameDouble(sc.get("modularity").asDouble(), mod)) {
                    bad.add(name + ": modularity " + mod + ", Python " + sc.get("modularity").asDouble());
                }
            } else {
                String param = sc.get("error_param").asText();
                try {
                    Communities.detect(es, resolution, minSize);
                    bad.add(name + ": expected an error");
                } catch (IllegalArgumentException ex) {
                    errors++;
                    if (!ex.getMessage().startsWith(param + " must be")) {
                        bad.add(name + ": error message " + ex.getMessage());
                    }
                }
            }
        }
        int modChecked = 0;
        for (JsonNode m : root.get("modularity")) {
            String name = m.get("name").asText();
            List<Communities.Edge> es = edges(m.get("edges"));
            List<List<String>> part = partition(m.get("communities"));
            double resolution = m.get("resolution").asDouble();
            modChecked++;
            if (m.get("ok").asBoolean()) {
                double got = Communities.modularity(es, part, resolution);
                if (!sameDouble(m.get("value").asDouble(), got)) {
                    bad.add(name + ": modularity " + got + ", Python " + m.get("value").asDouble());
                }
            } else {
                try {
                    Communities.modularity(es, part, resolution);
                    bad.add(name + ": expected an error");
                } catch (IllegalArgumentException ex) {
                    errors++;
                }
            }
        }
        System.out.println("communities parity: detect scenarios compared " + checked + ", modularity cases " + modChecked
                + ", errors matched " + errors);
        assertTrue(checked > 0 && errors > 0, "nothing compared");
        assertTrue(bad.isEmpty(), bad.toString());
    }
}
