package com.ragleap.rag.evaluation;

import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Pure ranking and summary functions of evaluation 2.0 (port of ragleap.evaluation: retrieval_metrics, percentile,
 * summarize and the ranking read from citations). Binary relevance, document level; a document that appears several
 * times counts once, at its first position; precision@k always divides by k. No LLM and no I/O.
 */
public final class EvalMetrics {
    public static final List<Integer> DEFAULT_METRIC_KS = List.of(1, 3, 5, 10);

    private EvalMetrics() {
    }

    /** n, mean, min, p10, p50, p95 and, when a threshold was given, the share of scores below it (else null). */
    public record Stats(int n, double mean, double min, double p10, double p50, double p95, Double failRate) {
    }

    private static final MathContext MC = new MathContext(60);
    private static final BigDecimal LN2 = lnNear1(BigDecimal.valueOf(2));

    /** ln(m) for m in [1, 2] by 2 * atanh((m - 1) / (m + 1)); the series converges fast because z <= 1/3. */
    private static BigDecimal lnNear1(BigDecimal m) {
        BigDecimal z = m.subtract(BigDecimal.ONE).divide(m.add(BigDecimal.ONE), MC);
        BigDecimal z2 = z.multiply(z, MC);
        BigDecimal term = z;
        BigDecimal sum = BigDecimal.ZERO;
        for (int k = 0; k < 120; k++) {
            sum = sum.add(term.divide(BigDecimal.valueOf(2L * k + 1), MC), MC);
            term = term.multiply(z2, MC);
        }
        return sum.multiply(BigDecimal.valueOf(2), MC);
    }

    /**
     * log2 for a positive whole number, correctly rounded (Java has no log2; Math.log(x) / Math.log(2) differs from
     * Python's math.log2 in the last bit for some values, which showed up in nDCG).
     */
    private static double log2(double x) {
        int e = Math.getExponent(x);
        if (x == Math.scalb(1.0, e)) {
            return e;
        }
        BigDecimal m = new BigDecimal(x).divide(new BigDecimal(2).pow(e), MC);
        return BigDecimal.valueOf(e).add(lnNear1(m).divide(LN2, MC), MC).doubleValue();
    }

    private static List<String> dedupeKeepOrder(Collection<String> items) {
        return new ArrayList<>(new LinkedHashSet<>(items));
    }

    public static void validateKs(List<Integer> ks) {
        if (ks == null || ks.isEmpty()) {
            throw new IllegalArgumentException("metric_ks must contain at least one cutoff.");
        }
        for (Integer k : ks) {
            if (k == null || k < 1) {
                throw new IllegalArgumentException("metric_ks must contain positive integers, got " + k);
            }
        }
    }

    /**
     * Document names in rank order, from the citations (one per chunk sent to the model, in retrieval or rerank
     * order). Citations without a document name are skipped; the sources list is only the fallback when no citation has
     * one.
     */
    public static List<String> rankedDocuments(List<String> citationDocumentNames, List<String> sources) {
        List<String> names = new ArrayList<>();
        if (citationDocumentNames != null) {
            for (String n : citationDocumentNames) {
                if (n != null && !n.isEmpty()) {
                    names.add(n);
                }
            }
        }
        if (!names.isEmpty()) {
            return dedupeKeepOrder(names);
        }
        return sources == null ? new ArrayList<>() : new ArrayList<>(sources);
    }

    /**
     * Ranking metrics for one query: reciprocal_rank, then for every cutoff k in order hit@k, recall@k, precision@k and
     * ndcg@k. IllegalArgumentException when relevant is empty (checked first) or ks is empty or has a value below 1.
     */
    public static Map<String, Double> retrievalMetrics(List<String> ranked, Collection<String> relevant, List<Integer> ks) {
        Set<String> relevantSet = new HashSet<>(relevant);
        if (relevantSet.isEmpty()) {
            throw new IllegalArgumentException("retrieval_metrics() requires at least one relevant document.");
        }
        validateKs(ks);
        List<String> ranking = dedupeKeepOrder(ranked);
        int[] gains = new int[ranking.size()];
        int first = -1;
        for (int i = 0; i < gains.length; i++) {
            gains[i] = relevantSet.contains(ranking.get(i)) ? 1 : 0;
            if (gains[i] == 1 && first < 0) {
                first = i;
            }
        }
        Map<String, Double> metrics = new LinkedHashMap<>();
        metrics.put("reciprocal_rank", first >= 0 ? 1.0 / (first + 1) : 0.0);
        for (int k : ks) {
            int limit = Math.min(k, gains.length);
            int found = 0;
            double dcg = 0.0;
            for (int i = 0; i < limit; i++) {
                found += gains[i];
                dcg += gains[i] / log2(i + 2);
            }
            metrics.put("hit@" + k, found > 0 ? 1.0 : 0.0);
            metrics.put("recall@" + k, (double) found / relevantSet.size());
            metrics.put("precision@" + k, (double) found / k);
            int ideal = Math.min(relevantSet.size(), k);
            double idcg = 0.0;
            for (int i = 0; i < ideal; i++) {
                idcg += 1 / log2(i + 2);
            }
            metrics.put("ndcg@" + k, dcg / idcg);
        }
        return metrics;
    }

    /** Nearest-rank percentile (q from 0 to 100), exact for decimal q; the smallest value for q = 0. */
    public static double percentile(List<Double> values, double q) {
        if (values == null || values.isEmpty()) {
            throw new IllegalArgumentException("percentile() requires at least one value.");
        }
        if (!(q >= 0 && q <= 100)) {
            throw new IllegalArgumentException("q must be between 0 and 100, got " + q);
        }
        List<Double> ordered = new ArrayList<>(values);
        Collections.sort(ordered);
        BigDecimal rank = new BigDecimal(Double.toString(q)).multiply(BigDecimal.valueOf(ordered.size()))
                .divide(BigDecimal.valueOf(100));
        int position = Math.max(1, rank.setScale(0, RoundingMode.CEILING).intValueExact());
        return ordered.get(position - 1);
    }

    /** Mean and tail of the scores (null entries are skipped), or null when there is nothing to summarize. */
    public static Stats summarize(List<Double> values, Double failBelow) {
        List<Double> nums = new ArrayList<>();
        for (Double v : values) {
            if (v != null) {
                nums.add(v);
            }
        }
        if (nums.isEmpty()) {
            return null;
        }
        double sum = 0.0;
        double min = nums.get(0);
        for (double v : nums) {
            sum += v;
            if (v < min) {
                min = v;
            }
        }
        Double failRate = null;
        if (failBelow != null) {
            int below = 0;
            for (double v : nums) {
                if (v < failBelow) {
                    below++;
                }
            }
            failRate = (double) below / nums.size();
        }
        return new Stats(nums.size(), sum / nums.size(), min, percentile(nums, 10), percentile(nums, 50),
                percentile(nums, 95), failRate);
    }
}
