package com.ragleap.rag.evaluation;

import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * Compares EvalMetrics with the real Python code (recorded by parity/gen_fixtures_eval_metrics.py): every double is
 * compared by its exact bit pattern, every error by its message. Python 3.11 recording.
 */
@EnabledIfEnvironmentVariable(named = "RAGLEAP_RAG_PARITY_DIR", matches = ".+")
class EvalMetricsParityTest {
    private static String hex(double d) {
        return String.format("%016x", Double.doubleToLongBits(d));
    }

    private static double unhex(String h) {
        return Double.longBitsToDouble(Long.parseUnsignedLong(h, 16));
    }

    private static List<String> strings(String field) {
        return field.isEmpty() ? new ArrayList<>() : new ArrayList<>(Arrays.asList(field.split("\\|", -1)));
    }

    private static List<Integer> ints(String field) {
        List<Integer> out = new ArrayList<>();
        for (String s : strings(field)) {
            out.add(Integer.parseInt(s));
        }
        return out;
    }

    private static String statsPayload(EvalMetrics.Stats s) {
        String p = "n=" + s.n() + "|mean=" + hex(s.mean()) + "|min=" + hex(s.min()) + "|p10=" + hex(s.p10())
                + "|p50=" + hex(s.p50()) + "|p95=" + hex(s.p95());
        return s.failRate() == null ? p : p + "|fail_rate=" + hex(s.failRate());
    }

    @Test
    void matchesPython() throws IOException {
        Path file = Path.of(System.getenv("RAGLEAP_RAG_PARITY_DIR"), "eval_metrics_cases.txt");
        List<String> bad = new ArrayList<>();
        int rm = 0;
        int pc = 0;
        int sm = 0;
        int errors = 0;
        for (String line : Files.readAllLines(file, StandardCharsets.UTF_8)) {
            if (line.isEmpty()) {
                continue;
            }
            String[] f = line.split("\t", -1);
            String name = f[0] + " " + f[1];
            String expected = f[f.length - 2] + "\t" + f[f.length - 1];
            String actual;
            switch (f[0]) {
                case "RM" -> {
                    rm++;
                    try {
                        Map<String, Double> r = EvalMetrics.retrievalMetrics(strings(f[2]), strings(f[3]), ints(f[4]));
                        List<String> parts = new ArrayList<>();
                        r.forEach((k, v) -> parts.add(k + "=" + hex(v)));
                        actual = "OK\t" + String.join("|", parts);
                    } catch (IllegalArgumentException e) {
                        errors++;
                        actual = "ERR\t" + e.getMessage();
                    }
                }
                case "PC" -> {
                    pc++;
                    List<Double> values = new ArrayList<>();
                    for (String h : strings(f[2])) {
                        values.add(unhex(h));
                    }
                    try {
                        actual = "OK\t" + hex(EvalMetrics.percentile(values, Double.parseDouble(f[3])));
                    } catch (IllegalArgumentException e) {
                        errors++;
                        actual = "ERR\t" + e.getMessage().split(" got ", 2)[0];
                    }
                }
                case "SM" -> {
                    sm++;
                    List<Double> values = new ArrayList<>();
                    for (String h : strings(f[2])) {
                        values.add(h.equals("N") ? null : unhex(h));
                    }
                    EvalMetrics.Stats s = EvalMetrics.summarize(values, f[3].equals("N") ? null : Double.parseDouble(f[3]));
                    actual = s == null ? "NONE\t" : "OK\t" + statsPayload(s);
                }
                default -> actual = "unknown line type";
            }
            if (!expected.equals(actual)) {
                bad.add(name + "\n   Python: " + expected + "\n   Java:   " + actual);
            }
        }
        System.out.println("eval metrics parity: RM " + rm + ", PC " + pc + ", SM " + sm + ", errors matched " + errors
                + ", differing " + bad.size());
        assertTrue(rm > 0 && pc > 0 && sm > 0 && errors > 0, "nothing compared");
        assertTrue(bad.isEmpty(), String.join("\n", bad.subList(0, Math.min(5, bad.size()))));
    }
}
