package com.ragleap.rag.evaluation;

import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.ragleap.rag.generation.Citation;
import com.ragleap.rag.generation.Usage;
import java.io.IOException;
import java.nio.file.Path;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.function.DoubleSupplier;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * Compares Evaluator with the real Python evaluate() (recorded by parity/gen_fixtures_eval_run.py): every float by its
 * exact bit pattern, every list in order, every error by its message, the inputs the support checker received, and
 * compare_runs on two runs. One mapping: Python's RuntimeError from a checker is RuntimeException in Java.
 */
@EnabledIfEnvironmentVariable(named = "RAGLEAP_RAG_PARITY_DIR", matches = ".+")
class EvaluatorParityTest {
    private static final ObjectMapper M = new ObjectMapper();

    private static Double dbl(JsonNode n) {
        if (n == null || n.isNull()) {
            return null;
        }
        return Double.longBitsToDouble(Long.parseUnsignedLong(n.asText().substring(2), 16));
    }

    private static JsonNode hx(Double d) {
        return d == null ? M.nullNode() : M.getNodeFactory().textNode("h:" + String.format("%016x", Double.doubleToLongBits(d)));
    }

    private static List<String> strs(JsonNode a) {
        List<String> out = new ArrayList<>();
        a.forEach(x -> out.add(x.asText()));
        return out;
    }

    private static String text(JsonNode n) {
        return n == null || n.isNull() ? null : n.asText();
    }

    private static Evaluator.Run run(JsonNode sc, JsonNode answers, JsonNode clock, List<JsonNode> inputs) throws SQLException {
        List<Evaluator.Case> cases = new ArrayList<>();
        for (JsonNode c : sc.get("cases")) {
            cases.add(new Evaluator.Case(c.get("query").asText(), text(c.get("expected_document")),
                    strs(c.get("expected_keywords")), strs(c.get("relevant_documents"))));
        }
        Map<String, Evaluator.AnswerView> views = new HashMap<>();
        answers.fields().forEachRemaining(e -> {
            JsonNode a = e.getValue();
            List<Citation> cits = new ArrayList<>();
            int n = 0;
            for (JsonNode c : a.get("citations")) {
                cits.add(new Citation(++n, text(c.get("document_name")), null, null, 0, c.get("text_preview").asText()));
            }
            Usage usage = a.get("usage").isNull() ? null : new Usage(a.get("usage").get("prompt_tokens").asInt(),
                    a.get("usage").get("completion_tokens").asInt(), a.get("usage").get("total_tokens").asInt());
            views.put(e.getKey(), new Evaluator.AnswerView(a.get("answer").asText(), strs(a.get("sources")), cits, usage));
        });
        int[] ci = {0};
        DoubleSupplier clk = () -> dbl(clock.get(ci[0]++));
        List<Integer> ks = new ArrayList<>();
        sc.get("ks").forEach(k -> ks.add(k.asInt()));
        Evaluator.SupportChecker checker = null;
        if (!sc.get("checker").isNull()) {
            JsonNode script = sc.get("checker");
            int[] si = {0};
            checker = (txt, cits) -> {
                ArrayNode pairs = M.createArrayNode();
                for (Citation c : cits) {
                    ArrayNode p = pairs.addArray();
                    p.add(c.documentName());
                    p.add(c.textPreview());
                }
                ArrayNode in = M.createArrayNode();
                in.add(txt);
                in.add(pairs);
                inputs.add(in);
                JsonNode k = script.get(si[0]++);
                if (k.get(0).asText().equals("raise")) {
                    throw new RuntimeException("boom");
                }
                return dbl(k.get(1));
            };
        }
        return Evaluator.evaluate(views::get, cases, ks, checker, dbl(sc.get("fail_below")), clk);
    }

    private static JsonNode stats(EvalMetrics.Stats s) {
        if (s == null) {
            return M.nullNode();
        }
        ObjectNode o = M.createObjectNode();
        o.put("n", s.n());
        o.set("mean", hx(s.mean()));
        o.set("min", hx(s.min()));
        o.set("p10", hx(s.p10()));
        o.set("p50", hx(s.p50()));
        o.set("p95", hx(s.p95()));
        if (s.failRate() != null) {
            o.set("fail_rate", hx(s.failRate()));
        }
        return o;
    }

    private static JsonNode node(Evaluator.Run r) {
        ObjectNode o = M.createObjectNode();
        o.set("retrieval_hit_rate", hx(r.retrievalHitRate()));
        o.set("keyword_coverage_rate", hx(r.keywordCoverageRate()));
        o.set("groundedness_rate", hx(r.groundednessRate()));
        ArrayNode results = o.putArray("results");
        for (Evaluator.CaseOutcome c : r.results()) {
            ObjectNode x = results.addObject();
            x.put("query", c.query());
            x.put("answer", c.answer());
            x.set("sources", M.valueToTree(c.sources()));
            x.set("ranked_documents", M.valueToTree(c.rankedDocuments()));
            if (c.retrievalHit() == null) {
                x.putNull("retrieval_hit");
            } else {
                x.put("retrieval_hit", c.retrievalHit().booleanValue());
            }
            x.set("keyword_coverage", hx(c.keywordCoverage()));
            x.set("keywords_found", M.valueToTree(c.keywordsFound()));
            x.set("groundedness", hx(c.groundedness()));
            if (c.metrics() == null) {
                x.putNull("metrics");
            } else {
                ObjectNode m = x.putObject("metrics");
                c.metrics().forEach((k, v) -> m.set(k, hx(v)));
            }
            x.set("support", hx(c.support()));
            x.put("support_error", c.supportError());
            x.set("latency_s", hx(c.latencyS()));
            if (c.usage() == null) {
                x.putNull("usage");
            } else {
                ObjectNode u = x.putObject("usage");
                u.put("prompt_tokens", c.usage().promptTokens());
                u.put("completion_tokens", c.usage().completionTokens());
                u.put("total_tokens", c.usage().totalTokens());
            }
        }
        Evaluator.Summary s = r.summary();
        ObjectNode sm = o.putObject("summary");
        sm.put("cases", s.cases());
        sm.set("fail_below", hx(s.failBelow()));
        sm.set("latency_s", stats(s.latency()));
        sm.set("keyword_coverage", stats(s.keywordCoverage()));
        sm.set("groundedness", stats(s.groundedness()));
        sm.set("support", stats(s.support()));
        ObjectNode ret = sm.putObject("retrieval");
        s.retrieval().forEach((k, v) -> ret.set(k, stats(v)));
        if (s.usageTotals() == null) {
            sm.putNull("usage_totals");
        } else {
            ObjectNode ut = sm.putObject("usage_totals");
            s.usageTotals().forEach((k, v) -> ut.put(k, (int) (long) v));
        }
        sm.put("support_errors", s.supportErrors());
        return o;
    }

    private static JsonNode cmpNode(EvalCompare.Comparison c) {
        ObjectNode o = M.createObjectNode();
        ObjectNode metrics = o.putObject("metrics");
        c.metrics().forEach((k, v) -> metrics.set(k, hx(v.delta())));
        o.set("regressions", M.valueToTree(c.regressions()));
        o.set("improvements", M.valueToTree(c.improvements()));
        ArrayNode changes = o.putArray("changes");
        c.caseChanges().forEach(x -> changes.add(x.query() + "|" + x.metric() + "|" + x.kind()));
        o.put("has_regressions", c.hasRegressions());
        if (c.latency() == null) {
            o.putNull("latency");
        } else {
            ArrayNode l = o.putArray("latency");
            l.add(hx(c.latency().baselineMean()));
            l.add(hx(c.latency().candidateMean()));
            l.add(hx(c.latency().baselineP95()));
            l.add(hx(c.latency().candidateP95()));
        }
        return o;
    }

    private static JsonNode fixed(JsonNode want) {
        JsonNode results = want.get("results");
        if (results != null) {
            for (JsonNode r : results) {
                JsonNode se = r.get("support_error");
                if (se != null && se.isTextual() && se.asText().startsWith("RuntimeError: ")) {
                    ((ObjectNode) r).put("support_error", "RuntimeException: " + se.asText().substring(14));
                }
            }
        }
        return want;
    }

    @Test
    void matchesPython() throws IOException, SQLException {
        JsonNode root = M.readTree(Path.of(System.getenv("RAGLEAP_RAG_PARITY_DIR"), "eval_run_cases.json").toFile());
        List<String> bad = new ArrayList<>();
        int compared = 0;
        int errors = 0;
        int pairs = 0;
        int checked = 0;
        for (JsonNode sc : root.get("scenarios")) {
            String name = sc.get("name").asText();
            List<JsonNode> inputs = new ArrayList<>();
            JsonNode got;
            Evaluator.Run a = null;
            try {
                a = run(sc, sc.get("answers"), sc.get("clock"), inputs);
                got = node(a);
            } catch (IllegalArgumentException e) {
                got = M.createObjectNode().put("error", e.getMessage());
            }
            JsonNode want = fixed(sc.get("out").deepCopy());
            compared++;
            if (want.has("error")) {
                errors++;
            }
            if (!want.equals(got)) {
                bad.add(name + "\n   Python: " + want + "\n   Java:   " + got);
            }
            checked += inputs.size();
            if (!sc.get("checker_inputs").equals(M.valueToTree(inputs))) {
                bad.add(name + ": checker inputs differ");
            }
            if (sc.has("cmp") && a != null) {
                pairs++;
                Evaluator.Run b = run(sc, sc.get("answers_b"), sc.get("clock_b"), new ArrayList<>());
                JsonNode cmp = cmpNode(EvalCompare.compareRuns(a.toScores(), b.toScores(), dbl(sc.get("tolerance"))));
                if (!sc.get("cmp").equals(cmp)) {
                    bad.add(name + " compare\n   Python: " + sc.get("cmp") + "\n   Java:   " + cmp);
                }
            }
        }
        System.out.println("eval run parity: scenarios " + compared + ", errors matched " + errors + ", compared pairs " + pairs
                + ", checker calls " + checked + ", differing " + bad.size());
        assertTrue(compared > 0 && errors > 0 && pairs > 0 && checked > 0, "nothing compared");
        assertTrue(bad.isEmpty(), String.join("\n", bad.subList(0, Math.min(3, bad.size()))));
    }
}
