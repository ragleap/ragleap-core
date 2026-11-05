package com.ragleap.rag.evaluation;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;
import java.util.TreeSet;

/**
 * Port of Python's compare_runs: diffs two evaluation runs of the same cases and flags regressions. A score is a
 * regression when it dropped by more than the tolerance, an improvement when it rose by more; differences are rounded
 * to 12 decimals first, so float noise below 1e-12 counts as no change. Aggregate scores and every case are compared;
 * latency is reported, never flagged. Metric names are compared in natural string order (they are ASCII).
 */
public final class EvalCompare {
    private EvalCompare() {
    }

    /** Scores of one case; null means not applicable. metrics are the ranking metrics (may be null or empty). */
    public record CaseScores(String query, Boolean retrievalHit, Double keywordCoverage, Double groundedness,
                             Double support, Map<String, Double> metrics) {
    }

    public record Latency(double mean, double p95) {
    }

    /**
     * What compareRuns reads from a run: the three top-level rates, the mean of every ranking metric (the summary's
     * retrieval stats), the mean support (null when no case was scored), the latency stats (null when absent) and the
     * cases in order.
     */
    public record RunScores(Double retrievalHitRate, Double keywordCoverageRate, Double groundednessRate,
                            Map<String, Double> retrievalMeans, Double supportMean, Latency latency,
                            List<CaseScores> cases) {
    }

    public record Metric(Double baseline, Double candidate, Double delta) {
    }

    /** kind is "regression" or "improvement"; baseline and candidate are Boolean for retrieval_hit, else Double. */
    public record CaseChange(String query, String metric, String kind, Object baseline, Object candidate) {
    }

    public record LatencyComparison(double baselineMean, double candidateMean, double baselineP95, double candidateP95) {
    }

    public record Comparison(int cases, double tolerance, Map<String, Metric> metrics, List<String> regressions,
                             List<String> improvements, List<CaseChange> caseChanges, LatencyComparison latency,
                             boolean hasRegressions) {
    }

    public static Comparison compareRuns(RunScores baseline, RunScores candidate) {
        return compareRuns(baseline, candidate, 0.0);
    }

    public static Comparison compareRuns(RunScores baseline, RunScores candidate, double tolerance) {
        if (tolerance < 0) {
            throw new IllegalArgumentException("tolerance must not be negative, got " + tolerance);
        }
        List<CaseScores> baseCases = baseline.cases() == null ? List.of() : baseline.cases();
        List<CaseScores> candCases = candidate.cases() == null ? List.of() : candidate.cases();
        if (baseCases.size() != candCases.size()) {
            throw new IllegalArgumentException("runs are not comparable: " + baseCases.size() + " cases against "
                    + candCases.size());
        }
        for (int i = 0; i < baseCases.size(); i++) {
            if (!Objects.equals(baseCases.get(i).query(), candCases.get(i).query())) {
                throw new IllegalArgumentException("runs are not comparable: case " + i + " has a different query");
            }
        }
        Map<String, Double> baseScores = runScores(baseline);
        Map<String, Double> candScores = runScores(candidate);
        Map<String, Metric> metrics = new TreeMap<>();
        List<String> regressions = new ArrayList<>();
        List<String> improvements = new ArrayList<>();
        TreeSet<String> names = new TreeSet<>(baseScores.keySet());
        names.addAll(candScores.keySet());
        for (String name : names) {
            Double b = baseScores.get(name);
            Double c = candScores.get(name);
            if (b == null || c == null) {
                metrics.put(name, new Metric(b, c, null));
                continue;
            }
            double delta = round12(c - b);
            metrics.put(name, new Metric(b, c, delta));
            String direction = direction(delta, tolerance);
            if ("regression".equals(direction)) {
                regressions.add(name);
            } else if ("improvement".equals(direction)) {
                improvements.add(name);
            }
        }
        List<CaseChange> changes = new ArrayList<>();
        for (int i = 0; i < baseCases.size(); i++) {
            CaseScores b = baseCases.get(i);
            CaseScores c = candCases.get(i);
            if (Boolean.TRUE.equals(b.retrievalHit()) && Boolean.FALSE.equals(c.retrievalHit())) {
                changes.add(new CaseChange(b.query(), "retrieval_hit", "regression", true, false));
            } else if (Boolean.FALSE.equals(b.retrievalHit()) && Boolean.TRUE.equals(c.retrievalHit())) {
                changes.add(new CaseChange(b.query(), "retrieval_hit", "improvement", false, true));
            }
            Map<String, Double> bCase = caseScores(b);
            Map<String, Double> cCase = caseScores(c);
            TreeSet<String> common = new TreeSet<>(bCase.keySet());
            common.retainAll(cCase.keySet());
            for (String name : common) {
                double delta = round12(cCase.get(name) - bCase.get(name));
                String direction = direction(delta, tolerance);
                if (direction != null) {
                    changes.add(new CaseChange(b.query(), name, direction, bCase.get(name), cCase.get(name)));
                }
            }
        }
        boolean has = !regressions.isEmpty();
        for (CaseChange change : changes) {
            has = has || "regression".equals(change.kind());
        }
        LatencyComparison latency = null;
        if (baseline.latency() != null && candidate.latency() != null) {
            latency = new LatencyComparison(baseline.latency().mean(), candidate.latency().mean(),
                    baseline.latency().p95(), candidate.latency().p95());
        }
        return new Comparison(baseCases.size(), tolerance, metrics, regressions, improvements, changes, latency, has);
    }

    private static String direction(double delta, double tolerance) {
        if (delta < -tolerance) {
            return "regression";
        }
        if (delta > tolerance) {
            return "improvement";
        }
        return null;
    }

    /** Python's round(x, 12): the exact binary value rounded half-even to 12 decimals; a rounded zero keeps its sign. */
    static double round12(double x) {
        double r = new BigDecimal(x).setScale(12, RoundingMode.HALF_EVEN).doubleValue();
        return r == 0 ? Math.copySign(0.0, x) : r;
    }

    private static Map<String, Double> runScores(RunScores run) {
        Map<String, Double> scores = new TreeMap<>();
        scores.put("retrieval_hit_rate", run.retrievalHitRate());
        scores.put("keyword_coverage_rate", run.keywordCoverageRate());
        scores.put("groundedness_rate", run.groundednessRate());
        if (run.retrievalMeans() != null) {
            run.retrievalMeans().forEach((metric, mean) -> scores.put("retrieval." + metric, mean));
        }
        if (run.supportMean() != null) {
            scores.put("support", run.supportMean());
        }
        return scores;
    }

    private static Map<String, Double> caseScores(CaseScores c) {
        Map<String, Double> scores = new TreeMap<>();
        if (c.keywordCoverage() != null) {
            scores.put("keyword_coverage", c.keywordCoverage());
        }
        if (c.groundedness() != null) {
            scores.put("groundedness", c.groundedness());
        }
        if (c.support() != null) {
            scores.put("support", c.support());
        }
        if (c.metrics() != null) {
            c.metrics().forEach((name, value) -> scores.put("retrieval." + name, value));
        }
        return scores;
    }
}
