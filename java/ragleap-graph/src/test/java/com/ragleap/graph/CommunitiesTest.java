package com.ragleap.graph;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;
import org.junit.jupiter.api.Test;

/** Properties of Communities that need no recorded Python output (exact values are in CommunitiesParityTest). */
class CommunitiesTest {
    private static Communities.Edge e(String a, String b, double w) {
        return new Communities.Edge(a, b, w);
    }

    private static List<Communities.Edge> tri(String p) {
        return List.of(e(p + "1", p + "2", 1), e(p + "2", p + "3", 1), e(p + "1", p + "3", 1));
    }

    private static List<Communities.Edge> twoTrianglesBridge() {
        List<Communities.Edge> all = new ArrayList<>(tri("a"));
        all.addAll(tri("b"));
        all.add(e("a3", "b1", 1));
        return all;
    }

    @Test
    void noEdgesAndOnlyInvalidEdgesGiveNoCommunities() {
        assertTrue(Communities.detect(List.of()).isEmpty());
        List<Communities.Edge> bad = List.of(e("s", "s", 1), e("a", "b", 0), e("a", "c", -1),
                e("a", "d", Double.NaN), e("a", "f", Double.POSITIVE_INFINITY), e("a", "g", Double.NEGATIVE_INFINITY));
        assertTrue(Communities.detect(bad, 1.0, 1).isEmpty());
    }

    @Test
    void invalidEdgesAreIgnored() {
        List<Communities.Edge> withBad = new ArrayList<>(twoTrianglesBridge());
        withBad.add(e("s", "s", 1));
        withBad.add(e("a1", "z", 0));
        withBad.add(e("a1", "y", Double.NaN));
        assertEquals(Communities.detect(twoTrianglesBridge(), 1.0, 1), Communities.detect(withBad, 1.0, 1));
    }

    @Test
    void edgeOrderAndDirectionDoNotMatter() {
        List<Communities.Edge> base = twoTrianglesBridge();
        List<Communities.Edge> mixed = new ArrayList<>();
        for (Communities.Edge x : base) {
            mixed.add(e(x.b(), x.a(), x.weight()));
        }
        Collections.shuffle(mixed, new Random(7));
        assertEquals(Communities.detect(base, 1.0, 1), Communities.detect(mixed, 1.0, 1));
    }

    @Test
    void duplicateEdgesAreSummed() {
        assertEquals(Communities.detect(List.of(e("a", "b", 3)), 1.0, 1),
                Communities.detect(List.of(e("a", "b", 1), e("b", "a", 2)), 1.0, 1));
    }

    @Test
    void disconnectedPartsAreNeverMerged() {
        List<Communities.Edge> all = new ArrayList<>(tri("a"));
        all.addAll(tri("b"));
        for (Communities.Community c : Communities.detect(all, 1.0, 1)) {
            Set<Character> firsts = new HashSet<>();
            c.members().forEach(m -> firsts.add(m.charAt(0)));
            assertEquals(1, firsts.size(), "community mixes both triangles: " + c.members());
        }
    }

    @Test
    void membersAreSortedByCodePointNotUtf16() {
        String bmp = "\uffff";
        String astral = new String(Character.toChars(0x10000));
        List<String> flat = new ArrayList<>();
        for (Communities.Community c : Communities.detect(List.of(e(astral, bmp, 1)), 1.0, 1)) {
            flat.addAll(c.members());
        }
        assertEquals(List.of(bmp, astral), flat);
    }

    @Test
    void minSizeFiltersAndSizeMatchesMembers() {
        List<Communities.Edge> all = new ArrayList<>(twoTrianglesBridge());
        all.add(e("x", "y", 1));
        for (Communities.Community c : Communities.detect(all, 1.0, 3)) {
            assertTrue(c.size() >= 3);
            assertEquals(c.members().size(), c.size());
        }
    }

    @Test
    void membersHashMatchesCompositeKey() {
        assertEquals(CompositeKey.of("a", "b"), Communities.membersHash(List.of("a", "b")));
        for (Communities.Community c : Communities.detect(twoTrianglesBridge(), 1.0, 1)) {
            assertEquals(Communities.membersHash(c.members()), c.membersHash());
        }
    }

    @Test
    void invalidArgumentsAreRejectedResolutionFirst() {
        List<Communities.Edge> g = twoTrianglesBridge();
        for (double bad : new double[] {0, -1, Double.NaN, Double.POSITIVE_INFINITY}) {
            IllegalArgumentException ex = assertThrows(IllegalArgumentException.class, () -> Communities.detect(g, bad, 2));
            assertTrue(ex.getMessage().startsWith("resolution must be"));
        }
        IllegalArgumentException ex = assertThrows(IllegalArgumentException.class, () -> Communities.detect(g, 1.0, 0));
        assertTrue(ex.getMessage().startsWith("min_size must be"));
        ex = assertThrows(IllegalArgumentException.class, () -> Communities.detect(g, 0, 0));
        assertTrue(ex.getMessage().startsWith("resolution must be"));
    }

    @Test
    void modularityBasics() {
        List<Communities.Edge> g = twoTrianglesBridge();
        List<String> all = List.of("a1", "a2", "a3", "b1", "b2", "b3");
        assertEquals(0.0, Communities.modularity(g, List.of(all), 1.0), 1e-12);
        assertEquals(0.0, Communities.modularity(List.of(), List.of(), 1.0));
        assertThrows(IllegalArgumentException.class,
                () -> Communities.modularity(g, List.of(List.of("a1", "a2", "a3")), 1.0));
    }
}
