package com.ragleap.rag.evaluation;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.ragleap.rag.generation.Citation;
import com.ragleap.rag.generation.Usage;
import java.sql.SQLException;
import java.util.List;
import java.util.Map;
import java.util.function.DoubleSupplier;
import org.junit.jupiter.api.Test;

/** Hand-checked behaviour of Evaluator (bit-for-bit comparison with Python is in EvaluatorParityTest). */
class EvaluatorTest {
    private static DoubleSupplier clock(double... v) {
        int[] i = {0};
        return () -> v[i[0]++];
    }

    private static Citation cit(int n, String doc, String preview) {
        return new Citation(n, doc, "id" + n, "c" + n, 0, preview);
    }

    @Test
    void scoresOneCase() throws SQLException {
        Evaluator.AnswerView view = new Evaluator.AnswerView("Alpha only", List.of("a.txt"),
                List.of(cit(1, "a.txt", "alpha text")), new Usage(10, 5, 15));
        Evaluator.Case tc = new Evaluator.Case("q1", "a.txt", List.of("alpha", "beta"), List.of("a.txt"));
        Evaluator.Run run = Evaluator.evaluate(q -> view, List.of(tc), List.of(1, 3), null, 0.5, clock(1.0, 1.5));
        Evaluator.CaseOutcome o = run.results().get(0);
        assertEquals(Boolean.TRUE, o.retrievalHit());
        assertEquals(0.5, o.keywordCoverage());
        assertEquals(List.of("alpha"), o.keywordsFound());
        assertEquals(1.0, o.groundedness());
        assertEquals(List.of("a.txt"), o.rankedDocuments());
        assertEquals(1.0, o.metrics().get("reciprocal_rank"));
        assertEquals(1.0, o.metrics().get("hit@1"));
        assertEquals(0.5, o.latencyS());
        assertNull(o.support());
        assertNull(o.supportError());
        assertEquals(1.0, run.retrievalHitRate());
        assertEquals(0.5, run.keywordCoverageRate());
        assertEquals(1, run.summary().cases());
        assertEquals(Map.of("prompt_tokens", 10L, "completion_tokens", 5L, "total_tokens", 15L), run.summary().usageTotals());
        assertEquals(0.5, run.summary().latency().mean());
        assertNull(run.summary().support());
        assertTrue(run.summary().retrieval().containsKey("ndcg@3"));
    }

    @Test
    void notApplicableScoresStayNull() throws SQLException {
        Evaluator.AnswerView view = new Evaluator.AnswerView("x", List.of(), List.of(), null);
        Evaluator.Run run = Evaluator.evaluate(q -> view, List.of(new Evaluator.Case("q")), List.of(1), null, 0.5, clock(0, 1));
        Evaluator.CaseOutcome o = run.results().get(0);
        assertNull(o.retrievalHit());
        assertNull(o.keywordCoverage());
        assertNull(o.groundedness());
        assertNull(o.metrics());
        assertNull(run.retrievalHitRate());
        assertNull(run.summary().usageTotals());
        assertTrue(run.summary().retrieval().isEmpty());
    }

    @Test
    void supportCheckerResultsAndFailuresArePerCase() throws SQLException {
        Evaluator.AnswerView view = new Evaluator.AnswerView("x", List.of(), List.of(), null);
        int[] n = {0};
        Evaluator.SupportChecker checker = (text, cits) -> {
            int k = n[0]++;
            if (k == 1) {
                throw new IllegalStateException("boom");
            }
            return k == 0 ? 0.8 : 1.5;
        };
        List<Evaluator.Case> cases = List.of(new Evaluator.Case("a"), new Evaluator.Case("b"), new Evaluator.Case("c"));
        Evaluator.Run run = Evaluator.evaluate(q -> view, cases, List.of(1), checker, 0.5, clock(0, 1, 2, 3, 4, 5));
        assertEquals(0.8, run.results().get(0).support());
        assertEquals("IllegalStateException: boom", run.results().get(1).supportError());
        assertNull(run.results().get(1).support());
        assertEquals("support_checker must return a number between 0 and 1, got 1.5", run.results().get(2).supportError());
        assertEquals(2, run.summary().supportErrors());
        assertEquals(1, run.summary().support().n());
    }

    @Test
    void errorsAreCheckedInOrder() {
        Evaluator.Asker asker = q -> null;
        assertEquals("evaluate() requires at least one test case.", assertThrows(IllegalArgumentException.class,
                () -> Evaluator.evaluate(asker, List.of(), List.of(), null, 9, null)).getMessage());
        assertEquals("metric_ks must contain at least one cutoff.", assertThrows(IllegalArgumentException.class,
                () -> Evaluator.evaluate(asker, List.of(new Evaluator.Case("q")), List.of(), null, 2.0, null)).getMessage());
        assertEquals("fail_below must be between 0 and 1, got 1.5", assertThrows(IllegalArgumentException.class,
                () -> Evaluator.evaluate(asker, List.of(new Evaluator.Case("q")), List.of(1), null, 1.5, null)).getMessage());
    }

    @Test
    void defaultClockMeasuresRealTime() throws SQLException {
        Evaluator.Run run = Evaluator.evaluate(q -> {
            try {
                Thread.sleep(5);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            return new Evaluator.AnswerView("x", List.of(), List.of(), null);
        }, List.of(new Evaluator.Case("q")));
        double latency = run.results().get(0).latencyS();
        assertTrue(latency > 0.0 && latency < 5.0, String.valueOf(latency));
    }

    @Test
    void nullAnswerIsScoredAsEmptyTextAndCheckerGetsEmptyText() throws SQLException {
        String[] seen = {null};
        Evaluator.SupportChecker checker = (text, cits) -> {
            seen[0] = text;
            return 0.5;
        };
        Evaluator.Run run = Evaluator.evaluate(q -> new Evaluator.AnswerView(null, null, null, null),
                List.of(new Evaluator.Case("q")), List.of(1), checker, 0.5, clock(0, 1));
        assertEquals("", seen[0]);
        Evaluator.CaseOutcome o = run.results().get(0);
        assertNull(o.answer());
        assertEquals(List.of(), o.sources());
        assertEquals(0.5, o.support());
    }

    @Test
    void runsCanBeComparedThroughScores() throws SQLException {
        Evaluator.Case tc = new Evaluator.Case("q", null, List.of("alpha"), List.of());
        Evaluator.Run good = Evaluator.evaluate(q -> new Evaluator.AnswerView("alpha", List.of(), List.of(), null),
                List.of(tc), List.of(1), null, 0.5, clock(0, 1));
        Evaluator.Run bad = Evaluator.evaluate(q -> new Evaluator.AnswerView("nothing", List.of(), List.of(), null),
                List.of(tc), List.of(1), null, 0.5, clock(0, 2));
        EvalCompare.Comparison c = EvalCompare.compareRuns(good.toScores(), bad.toScores());
        assertTrue(c.hasRegressions());
        assertEquals(List.of("keyword_coverage_rate"), c.regressions());
        assertEquals(new EvalCompare.LatencyComparison(1.0, 2.0, 1.0, 2.0), c.latency());
    }
}
