package com.ragleap.rag.evaluation;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** Hand-checked values of EvalMetrics (exact bit-for-bit comparison with Python is in EvalMetricsParityTest). */
class EvalMetricsTest {
    private static final double EPS = 1e-12;

    @Test
    void secondPlaceHit() {
        Map<String, Double> m = EvalMetrics.retrievalMetrics(List.of("a", "b", "c"), List.of("b"), List.of(1, 3));
        assertEquals(List.of("reciprocal_rank", "hit@1", "recall@1", "precision@1", "ndcg@1", "hit@3", "recall@3",
                "precision@3", "ndcg@3"), new ArrayList<>(m.keySet()));
        assertEquals(0.5, m.get("reciprocal_rank"));
        assertEquals(0.0, m.get("hit@1"));
        assertEquals(0.0, m.get("ndcg@1"));
        assertEquals(1.0, m.get("hit@3"));
        assertEquals(1.0, m.get("recall@3"));
        assertEquals(1.0 / 3, m.get("precision@3"), EPS);
        assertEquals(1.0 / (Math.log(3) / Math.log(2)), m.get("ndcg@3"), EPS);
    }

    @Test
    void repeatedDocumentsCountOnceAtFirstPosition() {
        Map<String, Double> m = EvalMetrics.retrievalMetrics(List.of("a", "a", "b"), List.of("b"), List.of(2));
        assertEquals(0.5, m.get("reciprocal_rank"));
        assertEquals(0.5, m.get("precision@2"));
    }

    @Test
    void precisionDividesByKEvenWhenFewerDocumentsCameBack() {
        Map<String, Double> m = EvalMetrics.retrievalMetrics(List.of("a"), List.of("a"), List.of(10));
        assertEquals(0.1, m.get("precision@10"));
        assertEquals(1.0, m.get("ndcg@10"), EPS);
    }

    @Test
    void errorsCheckRelevantBeforeCutoffs() {
        assertEquals("retrieval_metrics() requires at least one relevant document.", assertThrows(
                IllegalArgumentException.class, () -> EvalMetrics.retrievalMetrics(List.of("a"), List.of(), List.of())).getMessage());
        assertEquals("metric_ks must contain at least one cutoff.", assertThrows(IllegalArgumentException.class,
                () -> EvalMetrics.retrievalMetrics(List.of("a"), List.of("a"), List.of())).getMessage());
        assertEquals("metric_ks must contain positive integers, got 0", assertThrows(IllegalArgumentException.class,
                () -> EvalMetrics.retrievalMetrics(List.of("a"), List.of("a"), List.of(1, 0))).getMessage());
    }

    @Test
    void rankedDocumentsPrefersCitationsAndFallsBackToSources() {
        List<String> names = new ArrayList<>();
        names.add("b.txt");
        names.add(null);
        names.add("");
        names.add("a.txt");
        names.add("b.txt");
        assertEquals(List.of("b.txt", "a.txt"), EvalMetrics.rankedDocuments(names, List.of("z.txt")));
        assertEquals(List.of("z.txt", "y.txt"), EvalMetrics.rankedDocuments(new ArrayList<>(), List.of("z.txt", "y.txt")));
        assertEquals(List.of(), EvalMetrics.rankedDocuments(null, null));
    }

    @Test
    void oneCitationNameIsEnoughToIgnoreSources() {
        assertEquals(List.of("only.txt"), EvalMetrics.rankedDocuments(List.of("only.txt"), List.of("z.txt", "y.txt")));
    }

    @Test
    void percentileIsNearestRank() {
        List<Double> v = List.of(4.0, 1.0, 3.0, 2.0);
        assertEquals(1.0, EvalMetrics.percentile(v, 0));
        assertEquals(2.0, EvalMetrics.percentile(v, 50));
        assertEquals(4.0, EvalMetrics.percentile(v, 95));
        assertEquals(3.0, EvalMetrics.percentile(v, 75));
        assertEquals(4.0, EvalMetrics.percentile(v, 100));
        assertTrue(assertThrows(IllegalArgumentException.class, () -> EvalMetrics.percentile(List.of(), 50))
                .getMessage().startsWith("percentile() requires"));
        assertTrue(assertThrows(IllegalArgumentException.class, () -> EvalMetrics.percentile(v, 100.1))
                .getMessage().startsWith("q must be between 0 and 100"));
        assertThrows(IllegalArgumentException.class, () -> EvalMetrics.percentile(v, Double.NaN));
    }

    @Test
    void summarizeSkipsNullsAndComputesFailRate() {
        List<Double> v = new ArrayList<>();
        v.add(1.0);
        v.add(null);
        v.add(0.0);
        v.add(0.5);
        EvalMetrics.Stats s = EvalMetrics.summarize(v, 0.5);
        assertEquals(3, s.n());
        assertEquals(0.5, s.mean());
        assertEquals(0.0, s.min());
        assertEquals(1.0 / 3, s.failRate(), EPS);
        assertNull(EvalMetrics.summarize(v, null).failRate());
        List<Double> none = new ArrayList<>();
        none.add(null);
        assertNull(EvalMetrics.summarize(none, 0.5));
        assertNull(EvalMetrics.summarize(List.of(), null));
    }
}
