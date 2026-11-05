package com.ragleap.rag.evaluation;

import com.ragleap.rag.generation.Citation;
import com.ragleap.rag.generation.Usage;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.DoubleSupplier;

/**
 * Evaluation 2.0 run (port of Python's evaluate / evaluate_case): runs labeled cases through an asker, scores each
 * answer (retrieval hit, keyword coverage, groundedness, ranking metrics from the citations, optional support score
 * from a checker you supply), and summarizes the run with mean and tail statistics. No judge model is bundled; the
 * support checker is yours. The older EvalScorer / EvalCase / EvaluationResult stay as they are.
 */
public final class Evaluator {
    public static final double DEFAULT_FAIL_BELOW = 0.5;

    private Evaluator() {
    }

    /** One labeled case; expectedDocument may be null; the lists may be null or empty. */
    public record Case(String query, String expectedDocument, List<String> expectedKeywords,
                       List<String> relevantDocuments) {
        public Case {
            expectedKeywords = expectedKeywords == null ? List.of() : expectedKeywords;
            relevantDocuments = relevantDocuments == null ? List.of() : relevantDocuments;
        }

        public Case(String query) {
            this(query, null, List.of(), List.of());
        }
    }

    /** What the scoring reads from an answer. */
    public record AnswerView(String answer, List<String> sources, List<Citation> citations, Usage usage) {
    }

    @FunctionalInterface
    public interface Asker {
        AnswerView ask(String query) throws SQLException;
    }

    /** Scores how well an answer is supported by its citations, from 0 to 1. Failures are recorded per case. */
    @FunctionalInterface
    public interface SupportChecker {
        double score(String answerText, List<Citation> citations);
    }

    /** Per-case detail; null means not applicable. metrics are the ranking metrics (only with relevant documents). */
    public record CaseOutcome(String query, String answer, List<String> sources, List<String> rankedDocuments,
                              Boolean retrievalHit, Double keywordCoverage, List<String> keywordsFound,
                              Double groundedness, Map<String, Double> metrics, Double support, String supportError,
                              double latencyS, Usage usage) {
    }

    /** Mean-and-tail summary of a run; a Stats entry is null when no case had that score. */
    public record Summary(int cases, double failBelow, EvalMetrics.Stats latency, EvalMetrics.Stats keywordCoverage,
                          EvalMetrics.Stats groundedness, EvalMetrics.Stats support,
                          Map<String, EvalMetrics.Stats> retrieval, Map<String, Long> usageTotals, int supportErrors) {
    }

    public record Run(Double retrievalHitRate, Double keywordCoverageRate, Double groundednessRate,
                      List<CaseOutcome> results, Summary summary) {
        /** The score snapshot EvalCompare.compareRuns reads. */
        public EvalCompare.RunScores toScores() {
            Map<String, Double> means = new LinkedHashMap<>();
            summary.retrieval().forEach((name, stats) -> means.put(name, stats.mean()));
            Double supportMean = summary.support() == null ? null : summary.support().mean();
            EvalCompare.Latency latency = summary.latency() == null ? null
                    : new EvalCompare.Latency(summary.latency().mean(), summary.latency().p95());
            List<EvalCompare.CaseScores> cases = new ArrayList<>();
            for (CaseOutcome o : results) {
                cases.add(new EvalCompare.CaseScores(o.query(), o.retrievalHit(), o.keywordCoverage(),
                        o.groundedness(), o.support(), o.metrics()));
            }
            return new EvalCompare.RunScores(retrievalHitRate, keywordCoverageRate, groundednessRate, means,
                    supportMean, latency, cases);
        }
    }

    public static Run evaluate(Asker asker, List<Case> testCases) throws SQLException {
        return evaluate(asker, testCases, EvalMetrics.DEFAULT_METRIC_KS, null, DEFAULT_FAIL_BELOW, null);
    }

    /**
     * clock returns seconds (null = System.nanoTime based). IllegalArgumentException, checked in this order: no cases,
     * bad metric_ks, failBelow outside 0..1.
     */
    public static Run evaluate(Asker asker, List<Case> testCases, List<Integer> metricKs, SupportChecker checker,
                               double failBelow, DoubleSupplier clock) throws SQLException {
        if (testCases == null || testCases.isEmpty()) {
            throw new IllegalArgumentException("evaluate() requires at least one test case.");
        }
        EvalMetrics.validateKs(metricKs);
        if (!(failBelow >= 0 && failBelow <= 1)) {
            throw new IllegalArgumentException("fail_below must be between 0 and 1, got " + failBelow);
        }
        DoubleSupplier c = clock != null ? clock : () -> System.nanoTime() / 1e9;
        List<CaseOutcome> results = new ArrayList<>();
        for (Case tc : testCases) {
            results.add(evaluateCase(asker, tc, metricKs, checker, c));
        }
        List<Double> coverage = new ArrayList<>();
        List<Double> grounded = new ArrayList<>();
        int hits = 0;
        int hitCases = 0;
        for (CaseOutcome o : results) {
            if (o.retrievalHit() != null) {
                hitCases++;
                if (o.retrievalHit()) {
                    hits++;
                }
            }
            if (o.keywordCoverage() != null) {
                coverage.add(o.keywordCoverage());
            }
            if (o.groundedness() != null) {
                grounded.add(o.groundedness());
            }
        }
        Double hitRate = hitCases == 0 ? null : (double) hits / hitCases;
        return new Run(hitRate, mean(coverage), mean(grounded), results, summarizeRun(results, failBelow));
    }

    private static Double mean(List<Double> values) {
        if (values.isEmpty()) {
            return null;
        }
        double sum = 0.0;
        for (double v : values) {
            sum += v;
        }
        return sum / values.size();
    }

    static CaseOutcome evaluateCase(Asker asker, Case tc, List<Integer> ks, SupportChecker checker,
                                    DoubleSupplier clock) throws SQLException {
        double started = clock.getAsDouble();
        AnswerView a = asker.ask(tc.query());
        double latency = clock.getAsDouble() - started;
        String text = a.answer() == null ? "" : a.answer();
        List<String> sources = a.sources() == null ? List.of() : a.sources();
        List<Citation> citations = a.citations() == null ? List.of() : a.citations();
        Boolean hit = tc.expectedDocument() == null ? null : sources.contains(tc.expectedDocument());
        List<String> keywordHits = EvalScorer.keywordHits(text, tc.expectedKeywords());
        Double coverage = tc.expectedKeywords().isEmpty() ? null
                : (double) keywordHits.size() / tc.expectedKeywords().size();
        Double groundedness = null;
        if (!keywordHits.isEmpty()) {
            List<String> previews = new ArrayList<>();
            for (Citation ci : citations) {
                previews.add(ci.textPreview() == null ? "" : ci.textPreview());
            }
            List<String> groundedHits = EvalScorer.keywordHits(String.join(" ", previews), keywordHits);
            groundedness = (double) groundedHits.size() / keywordHits.size();
        }
        List<String> names = new ArrayList<>();
        for (Citation ci : citations) {
            names.add(ci.documentName());
        }
        List<String> ranked = EvalMetrics.rankedDocuments(names, sources);
        Map<String, Double> metrics = tc.relevantDocuments().isEmpty() ? null
                : EvalMetrics.retrievalMetrics(ranked, tc.relevantDocuments(), ks);
        Double support = null;
        String supportError = null;
        if (checker != null) {
            try {
                double s = checker.score(text, new ArrayList<>(citations));
                if (!(s >= 0 && s <= 1)) {
                    supportError = "support_checker must return a number between 0 and 1, got " + s;
                } else {
                    support = s;
                }
            } catch (RuntimeException e) {
                supportError = e.getClass().getSimpleName() + ": " + e.getMessage();
            }
        }
        return new CaseOutcome(tc.query(), a.answer(), sources, ranked, hit, coverage, keywordHits, groundedness,
                metrics, support, supportError, latency, a.usage());
    }

    private static Summary summarizeRun(List<CaseOutcome> results, double failBelow) {
        List<String> metricNames = new ArrayList<>();
        for (CaseOutcome o : results) {
            if (o.metrics() != null) {
                for (String name : o.metrics().keySet()) {
                    if (!metricNames.contains(name)) {
                        metricNames.add(name);
                    }
                }
            }
        }
        Map<String, EvalMetrics.Stats> retrieval = new LinkedHashMap<>();
        for (String name : metricNames) {
            List<Double> values = new ArrayList<>();
            for (CaseOutcome o : results) {
                values.add(o.metrics() == null ? null : o.metrics().get(name));
            }
            retrieval.put(name, EvalMetrics.summarize(values, null));
        }
        long prompt = 0;
        long completion = 0;
        long total = 0;
        boolean anyUsage = false;
        int supportErrors = 0;
        List<Double> latency = new ArrayList<>();
        List<Double> coverage = new ArrayList<>();
        List<Double> grounded = new ArrayList<>();
        List<Double> support = new ArrayList<>();
        for (CaseOutcome o : results) {
            latency.add(o.latencyS());
            coverage.add(o.keywordCoverage());
            grounded.add(o.groundedness());
            support.add(o.support());
            if (o.supportError() != null) {
                supportErrors++;
            }
            if (o.usage() != null) {
                anyUsage = true;
                prompt += o.usage().promptTokens();
                completion += o.usage().completionTokens();
                total += o.usage().totalTokens();
            }
        }
        Map<String, Long> usageTotals = null;
        if (anyUsage) {
            usageTotals = new LinkedHashMap<>();
            usageTotals.put("prompt_tokens", prompt);
            usageTotals.put("completion_tokens", completion);
            usageTotals.put("total_tokens", total);
        }
        return new Summary(results.size(), failBelow, EvalMetrics.summarize(latency, null),
                EvalMetrics.summarize(coverage, failBelow), EvalMetrics.summarize(grounded, failBelow),
                EvalMetrics.summarize(support, failBelow), retrieval, usageTotals, supportErrors);
    }
}
