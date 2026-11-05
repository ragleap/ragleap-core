package com.ragleap.rag.evaluation;

import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * Compares EvalCompare with the real Python compare_runs (recorded by parity/gen_fixtures_eval_compare.py): every float
 * by its exact bit pattern (strings "h:" + hex), every list in order, every error by its message.
 */
@EnabledIfEnvironmentVariable(named = "RAGLEAP_RAG_PARITY_DIR", matches = ".+")
class EvalCompareParityTest {
    private static final ObjectMapper M = new ObjectMapper();

    private static Double dbl(JsonNode n) {
        if (n == null || n.isNull()) {
            return null;
        }
        return Double.longBitsToDouble(Long.parseUnsignedLong(n.asText().substring(2), 16));
    }

    private static String hex(double d) {
        return "h:" + String.format("%016x", Double.doubleToLongBits(d));
    }

    private static JsonNode hexNode(Double d) {
        return d == null ? M.nullNode() : M.getNodeFactory().textNode(hex(d));
    }

    private static Map<String, Double> doubles(JsonNode obj, String field) {
        Map<String, Double> out = new LinkedHashMap<>();
        if (obj != null && !obj.isNull()) {
            JsonNode inner = field == null ? obj : obj.get(field);
            if (inner != null && !inner.isNull()) {
                inner.fields().forEachRemaining(e -> out.put(e.getKey(), dbl(field == null ? e.getValue() : e.getValue())));
            }
        }
        return out;
    }

    private static EvalCompare.RunScores run(JsonNode r) {
        JsonNode summary = r.get("summary");
        Map<String, Double> means = new LinkedHashMap<>();
        Double support = null;
        EvalCompare.Latency latency = null;
        if (summary != null && !summary.isNull()) {
            JsonNode retrieval = summary.get("retrieval");
            if (retrieval != null && !retrieval.isNull()) {
                retrieval.fields().forEachRemaining(e -> means.put(e.getKey(), dbl(e.getValue().get("mean"))));
            }
            if (summary.hasNonNull("support")) {
                support = dbl(summary.get("support").get("mean"));
            }
            if (summary.hasNonNull("latency_s")) {
                latency = new EvalCompare.Latency(dbl(summary.get("latency_s").get("mean")),
                        dbl(summary.get("latency_s").get("p95")));
            }
        }
        List<EvalCompare.CaseScores> cases = new ArrayList<>();
        for (JsonNode c : r.get("results")) {
            Boolean hit = c.get("retrieval_hit").isNull() ? null : c.get("retrieval_hit").asBoolean();
            cases.add(new EvalCompare.CaseScores(c.get("query").asText(), hit, dbl(c.get("keyword_coverage")),
                    dbl(c.get("groundedness")), dbl(c.get("support")), doubles(c.get("metrics"), null)));
        }
        return new EvalCompare.RunScores(dbl(r.get("retrieval_hit_rate")), dbl(r.get("keyword_coverage_rate")),
                dbl(r.get("groundedness_rate")), means, support, latency, cases);
    }

    private static JsonNode node(EvalCompare.Comparison x) {
        ObjectNode o = M.createObjectNode();
        o.put("cases", x.cases());
        o.set("tolerance", hexNode(x.tolerance()));
        ObjectNode metrics = o.putObject("metrics");
        x.metrics().forEach((k, v) -> {
            ObjectNode m = metrics.putObject(k);
            m.set("baseline", hexNode(v.baseline()));
            m.set("candidate", hexNode(v.candidate()));
            m.set("delta", hexNode(v.delta()));
        });
        ArrayNode reg = o.putArray("regressions");
        x.regressions().forEach(reg::add);
        ArrayNode imp = o.putArray("improvements");
        x.improvements().forEach(imp::add);
        ArrayNode changes = o.putArray("case_changes");
        for (EvalCompare.CaseChange ch : x.caseChanges()) {
            ObjectNode c = changes.addObject();
            c.put("query", ch.query());
            c.put("metric", ch.metric());
            c.put("kind", ch.kind());
            c.set("baseline", ch.baseline() instanceof Boolean b ? M.getNodeFactory().booleanNode(b) : hexNode((Double) ch.baseline()));
            c.set("candidate", ch.candidate() instanceof Boolean b ? M.getNodeFactory().booleanNode(b) : hexNode((Double) ch.candidate()));
        }
        if (x.latency() == null) {
            o.putNull("latency_s");
        } else {
            ObjectNode l = o.putObject("latency_s");
            l.set("baseline_mean", hexNode(x.latency().baselineMean()));
            l.set("candidate_mean", hexNode(x.latency().candidateMean()));
            l.set("baseline_p95", hexNode(x.latency().baselineP95()));
            l.set("candidate_p95", hexNode(x.latency().candidateP95()));
        }
        o.put("has_regressions", x.hasRegressions());
        return o;
    }

    @Test
    void matchesPython() throws IOException {
        JsonNode root = M.readTree(Path.of(System.getenv("RAGLEAP_RAG_PARITY_DIR"), "eval_compare_cases.json").toFile());
        List<String> bad = new ArrayList<>();
        int compared = 0;
        int errors = 0;
        int changes = 0;
        for (JsonNode sc : root.get("scenarios")) {
            String name = sc.get("name").asText();
            JsonNode want = sc.get("out");
            JsonNode got;
            try {
                EvalCompare.Comparison x = EvalCompare.compareRuns(run(sc.get("base")), run(sc.get("cand")),
                        dbl(sc.get("tolerance")));
                changes += x.caseChanges().size();
                got = node(x);
            } catch (IllegalArgumentException e) {
                got = M.createObjectNode().put("error", e.getMessage());
            }
            compared++;
            if (want.has("error")) {
                errors++;
            }
            if (!want.equals(got)) {
                bad.add(name + "\n   Python: " + want + "\n   Java:   " + got);
            }
        }
        System.out.println("eval compare parity: scenarios " + compared + ", errors matched " + errors + ", case changes "
                + changes + ", differing " + bad.size());
        assertTrue(compared > 0 && errors > 0 && changes > 0, "nothing compared");
        assertTrue(bad.isEmpty(), String.join("\n", bad.subList(0, Math.min(3, bad.size()))));
    }
}
