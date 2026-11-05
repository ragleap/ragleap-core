package com.ragleap.rag.evaluation;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** Hand-checked behaviour of EvalCompare (bit-for-bit comparison with Python is in EvalCompareParityTest). */
class EvalCompareTest {
    private static EvalCompare.CaseScores c(String q, Boolean hit, Double kw) {
        return new EvalCompare.CaseScores(q, hit, kw, null, null, null);
    }

    private static EvalCompare.RunScores run(Double kwRate, EvalCompare.CaseScores... cases) {
        return new EvalCompare.RunScores(null, kwRate, null, Map.of(), null, null, List.of(cases));
    }

    @Test
    void identicalRunsHaveNoChanges() {
        EvalCompare.RunScores r = run(0.5, c("a", true, 0.5));
        EvalCompare.Comparison x = EvalCompare.compareRuns(r, r);
        assertEquals(1, x.cases());
        assertFalse(x.hasRegressions());
        assertTrue(x.regressions().isEmpty() && x.improvements().isEmpty() && x.caseChanges().isEmpty());
        assertEquals(0.0, x.metrics().get("keyword_coverage_rate").delta());
        assertNull(x.metrics().get("retrieval_hit_rate").delta());
        assertNull(x.latency());
    }

    @Test
    void dropBeyondToleranceIsARegressionAndRiseAnImprovement() {
        EvalCompare.RunScores base = run(0.8, c("a", true, 0.8), c("b", false, 0.2));
        EvalCompare.RunScores cand = run(0.5, c("a", false, 0.5), c("b", true, 0.9));
        EvalCompare.Comparison x = EvalCompare.compareRuns(base, cand, 0.1);
        assertEquals(List.of("keyword_coverage_rate"), x.regressions());
        assertTrue(x.hasRegressions());
        assertEquals(-0.3, x.metrics().get("keyword_coverage_rate").delta());
        List<String> kinds = x.caseChanges().stream().map(ch -> ch.query() + ":" + ch.metric() + ":" + ch.kind()).toList();
        assertEquals(List.of("a:retrieval_hit:regression", "a:keyword_coverage:regression",
                "b:retrieval_hit:improvement", "b:keyword_coverage:improvement"), kinds);
    }

    @Test
    void floatNoiseBelowOneTrillionthIsNotAChange() {
        EvalCompare.Comparison x = EvalCompare.compareRuns(run(0.3), run(0.1 + 0.2), 0.0);
        assertEquals(0.0, x.metrics().get("keyword_coverage_rate").delta());
        assertTrue(x.regressions().isEmpty() && x.improvements().isEmpty());
        assertEquals(0, Double.compare(-0.0, EvalCompare.round12(-1e-13)));
        assertEquals(0, Double.compare(0.0, EvalCompare.round12(1e-13)));
    }

    @Test
    void errorsAreCheckedInOrder() {
        EvalCompare.RunScores one = run(0.5, c("a", null, null));
        EvalCompare.RunScores two = run(0.5, c("a", null, null), c("b", null, null));
        assertEquals("tolerance must not be negative, got -0.1", assertThrows(IllegalArgumentException.class,
                () -> EvalCompare.compareRuns(one, two, -0.1)).getMessage());
        assertEquals("runs are not comparable: 1 cases against 2", assertThrows(IllegalArgumentException.class,
                () -> EvalCompare.compareRuns(one, two)).getMessage());
        assertEquals("runs are not comparable: case 0 has a different query", assertThrows(IllegalArgumentException.class,
                () -> EvalCompare.compareRuns(one, run(0.5, c("z", null, null)))).getMessage());
    }

    @Test
    void longerBaselineIsAlsoNotComparable() {
        EvalCompare.RunScores one = run(0.5, c("a", null, null));
        EvalCompare.RunScores two = run(0.5, c("a", null, null), c("b", null, null));
        assertEquals("runs are not comparable: 2 cases against 1", assertThrows(IllegalArgumentException.class,
                () -> EvalCompare.compareRuns(two, one)).getMessage());
    }

    @Test
    void round12IsHalfEvenOnExactBinaryTies() {
        // 1 * 2^-13 = 0.0001220703125 sits exactly between 0.000122070312 and 0.000122070313: half-even gives ...312
        assertEquals(0.000122070312, EvalCompare.round12(1 * Math.pow(2, -13)));
        // 3 * 2^-13 = 0.0003662109375: half-even gives ...376 (even), half-up would give ...375 + 1 = ...376 as well
        // 5 * 2^-13 = 0.0006103515625: half-even gives ...352, half-up gives ...353
        assertEquals(0.000610351562, EvalCompare.round12(5 * Math.pow(2, -13)));
    }

    @Test
    void retrievalMeansSupportAndLatencyAreCompared() {
        EvalCompare.RunScores base = new EvalCompare.RunScores(null, null, null, Map.of("ndcg@3", 0.9), 0.8,
                new EvalCompare.Latency(1.0, 2.0), List.of());
        EvalCompare.RunScores cand = new EvalCompare.RunScores(null, null, null, Map.of("ndcg@3", 0.95, "hit@1", 1.0), null,
                new EvalCompare.Latency(1.5, 3.0), List.of());
        EvalCompare.Comparison x = EvalCompare.compareRuns(base, cand);
        assertEquals(List.of("retrieval.ndcg@3"), x.improvements());
        assertNull(x.metrics().get("retrieval.hit@1").delta());
        assertNull(x.metrics().get("support").delta());
        assertEquals(new EvalCompare.LatencyComparison(1.0, 1.5, 2.0, 3.0), x.latency());
        assertFalse(x.hasRegressions());
    }
}
