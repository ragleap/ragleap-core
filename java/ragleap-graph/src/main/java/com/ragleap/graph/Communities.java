package com.ragleap.graph;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;

/**
 * Deterministic community detection for the entity co-occurrence graph: a port of Python's
 * ragleap_graph.communities. Louvain on an undirected weighted graph, then every community is split into its
 * connected components. No random numbers: nodes are visited in code point order, ties go to the smaller label, and a
 * node moves only for a gain above {@link #EPSILON}. Pure: reads and writes nothing. Not Leiden, one flat level;
 * modularity has a resolution limit.
 */
public final class Communities {
    static final double EPSILON = 1e-12;
    static final int MAX_PASSES = 1000;

    private Communities() {
    }

    /** One undirected weighted edge. Self loops and weights that are not finite and positive are ignored. */
    public record Edge(String a, String b, double weight) {
    }

    /** One community: members sorted by code point, total weight of the edges inside, SHA-256 of the members. */
    public record Community(List<String> members, int size, double internalWeight, String membersHash) {
    }

    private record Pair(String low, String high) {
    }

    private record Cleaned(String low, String high, double weight) {
    }

    private record Moving(int[] community, boolean movedAny) {
    }

    private record Aggregated(List<TreeMap<Integer, Double>> adj, double[] loops, int[] mapping) {
    }

    private record Louvain(List<String> names, int[] membership) {
    }

    public static List<Community> detect(Iterable<Edge> edges) {
        return detect(edges, 1.0, 2);
    }

    public static List<Community> detect(Iterable<Edge> edges, double resolution, int minSize) {
        if (!Double.isFinite(resolution) || resolution <= 0) {
            throw new IllegalArgumentException("resolution must be a positive finite number, got " + resolution);
        }
        if (minSize < 1) {
            throw new IllegalArgumentException("min_size must be a positive integer, got " + minSize);
        }
        LinkedHashMap<Pair, Double> weights = prepareEdges(edges);
        Louvain lv = louvain(weights, resolution);
        List<String> names = lv.names();
        int n = names.size();
        Map<String, Integer> index = new HashMap<>();
        for (int i = 0; i < n; i++) {
            index.put(names.get(i), i);
        }
        List<List<Integer>> neighbors = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            neighbors.add(new ArrayList<>());
        }
        for (Pair p : weights.keySet()) {
            int a = index.get(p.low());
            int b = index.get(p.high());
            neighbors.get(a).add(b);
            neighbors.get(b).add(a);
        }
        for (List<Integer> row : neighbors) {
            Collections.sort(row);
        }
        List<List<Integer>> parts = components(neighbors, lv.membership());
        int[] label = new int[n];
        for (int partNumber = 0; partNumber < parts.size(); partNumber++) {
            for (int node : parts.get(partNumber)) {
                label[node] = partNumber;
            }
        }
        double[] internal = new double[parts.size()];
        for (Map.Entry<Pair, Double> e : weights.entrySet()) {
            int la = label[index.get(e.getKey().low())];
            int lb = label[index.get(e.getKey().high())];
            if (la == lb) {
                internal[la] += e.getValue();
            }
        }
        List<Community> out = new ArrayList<>();
        for (int partNumber = 0; partNumber < parts.size(); partNumber++) {
            List<Integer> part = parts.get(partNumber);
            if (part.size() < minSize) {
                continue;
            }
            List<String> members = new ArrayList<>();
            for (int node : part) {
                members.add(names.get(node));
            }
            out.add(new Community(List.copyOf(members), members.size(), internal[partNumber], membersHash(members)));
        }
        out.sort((x, y) -> {
            int c = Integer.compare(y.size(), x.size());
            if (c != 0) {
                return c;
            }
            return CodePoints.compare(x.members().get(0), y.members().get(0));
        });
        return out;
    }

    /** SHA-256 (hex) of the members joined with NUL characters; pass them sorted by code point. */
    public static String membersHash(List<String> members) {
        return CompositeKey.of(members.toArray(new String[0]));
    }

    /**
     * Modularity of a partition. Every node of an edge must belong to a community (IllegalArgumentException
     * otherwise); a node listed in several communities counts for the last one.
     */
    public static double modularity(Iterable<Edge> edges, List<? extends List<String>> communities, double resolution) {
        LinkedHashMap<Pair, Double> weights = prepareEdges(edges);
        double total = 0.0;
        for (double w : weights.values()) {
            total += w;
        }
        if (total == 0) {
            return 0.0;
        }
        Map<String, Integer> label = new HashMap<>();
        for (int index = 0; index < communities.size(); index++) {
            for (String name : communities.get(index)) {
                label.put(name, index);
            }
        }
        Map<Integer, Double> degreeSum = new LinkedHashMap<>();
        double inside = 0.0;
        for (Map.Entry<Pair, Double> e : weights.entrySet()) {
            String a = e.getKey().low();
            String b = e.getKey().high();
            double weight = e.getValue();
            if (!label.containsKey(a) || !label.containsKey(b)) {
                throw new IllegalArgumentException("every node of an edge must belong to a community");
            }
            int la = label.get(a);
            int lb = label.get(b);
            degreeSum.put(la, degreeSum.getOrDefault(la, 0.0) + weight);
            degreeSum.put(lb, degreeSum.getOrDefault(lb, 0.0) + weight);
            if (la == lb) {
                inside += weight;
            }
        }
        double squares = 0.0;
        for (double d : degreeSum.values()) {
            squares += d * d;
        }
        double penalty = squares / (4.0 * total * total);
        return inside / total - resolution * penalty;
    }

    /** Undirected weights keyed by (smaller, larger) name; duplicates summed in sorted order. */
    private static LinkedHashMap<Pair, Double> prepareEdges(Iterable<Edge> edges) {
        List<Cleaned> cleaned = new ArrayList<>();
        for (Edge e : edges) {
            if (e.a().equals(e.b())) {
                continue;
            }
            double w = e.weight();
            if (!Double.isFinite(w) || w <= 0) {
                continue;
            }
            if (CodePoints.compare(e.a(), e.b()) < 0) {
                cleaned.add(new Cleaned(e.a(), e.b(), w));
            } else {
                cleaned.add(new Cleaned(e.b(), e.a(), w));
            }
        }
        cleaned.sort((x, y) -> {
            int c = CodePoints.compare(x.low(), y.low());
            if (c != 0) {
                return c;
            }
            c = CodePoints.compare(x.high(), y.high());
            if (c != 0) {
                return c;
            }
            return Double.compare(x.weight(), y.weight());
        });
        LinkedHashMap<Pair, Double> merged = new LinkedHashMap<>();
        for (Cleaned c : cleaned) {
            Pair key = new Pair(c.low(), c.high());
            merged.put(key, merged.getOrDefault(key, 0.0) + c.weight());
        }
        return merged;
    }

    private static Moving localMoving(List<TreeMap<Integer, Double>> adj, double[] loops, double resolution) {
        int n = adj.size();
        double[] degree = new double[n];
        for (int i = 0; i < n; i++) {
            double s = 0.0;
            for (double w : adj.get(i).values()) {
                s += w;
            }
            degree[i] = s + 2.0 * loops[i];
        }
        double sum = 0.0;
        for (double d : degree) {
            sum += d;
        }
        double total = sum / 2.0;
        int[] community = new int[n];
        for (int i = 0; i < n; i++) {
            community[i] = i;
        }
        double[] communityDegree = degree.clone();
        boolean movedAny = false;
        for (int pass = 0; pass < MAX_PASSES; pass++) {
            boolean movedPass = false;
            for (int i = 0; i < n; i++) {
                int own = community[i];
                TreeMap<Integer, Double> links = new TreeMap<>();
                for (Map.Entry<Integer, Double> e : adj.get(i).entrySet()) {
                    int label = community[e.getKey()];
                    links.put(label, links.getOrDefault(label, 0.0) + e.getValue());
                }
                communityDegree[own] -= degree[i];
                double scale = resolution * degree[i] / (2.0 * total);
                int best = own;
                double bestGain = links.getOrDefault(own, 0.0) - communityDegree[own] * scale;
                for (Map.Entry<Integer, Double> e : links.entrySet()) {
                    int label = e.getKey();
                    if (label == own) {
                        continue;
                    }
                    double gain = e.getValue() - communityDegree[label] * scale;
                    if (gain > bestGain + EPSILON) {
                        best = label;
                        bestGain = gain;
                    }
                }
                communityDegree[best] += degree[i];
                if (best != own) {
                    community[i] = best;
                    movedPass = true;
                }
            }
            if (!movedPass) {
                break;
            }
            movedAny = true;
        }
        return new Moving(community, movedAny);
    }

    private static Aggregated aggregate(List<TreeMap<Integer, Double>> adj, double[] loops, int[] community) {
        Map<Integer, Integer> order = new HashMap<>();
        for (int i = 0; i < adj.size(); i++) {
            if (!order.containsKey(community[i])) {
                order.put(community[i], order.size());
            }
        }
        int size = order.size();
        List<TreeMap<Integer, Double>> newAdj = new ArrayList<>();
        for (int i = 0; i < size; i++) {
            newAdj.add(new TreeMap<>());
        }
        double[] newLoops = new double[size];
        for (int i = 0; i < adj.size(); i++) {
            int ci = order.get(community[i]);
            newLoops[ci] += loops[i];
            for (Map.Entry<Integer, Double> e : adj.get(i).entrySet()) {
                int j = e.getKey();
                if (j < i) {
                    continue;
                }
                int cj = order.get(community[j]);
                double weight = e.getValue();
                if (ci == cj) {
                    newLoops[ci] += weight;
                } else {
                    newAdj.get(ci).put(cj, newAdj.get(ci).getOrDefault(cj, 0.0) + weight);
                    newAdj.get(cj).put(ci, newAdj.get(cj).getOrDefault(ci, 0.0) + weight);
                }
            }
        }
        int[] mapping = new int[adj.size()];
        for (int i = 0; i < adj.size(); i++) {
            mapping[i] = order.get(community[i]);
        }
        return new Aggregated(newAdj, newLoops, mapping);
    }

    private static Louvain louvain(Map<Pair, Double> weights, double resolution) {
        TreeSet<String> nameSet = new TreeSet<>(CodePoints::compare);
        for (Pair p : weights.keySet()) {
            nameSet.add(p.low());
            nameSet.add(p.high());
        }
        List<String> names = new ArrayList<>(nameSet);
        Map<String, Integer> index = new HashMap<>();
        for (int i = 0; i < names.size(); i++) {
            index.put(names.get(i), i);
        }
        List<TreeMap<Integer, Double>> adj = new ArrayList<>();
        for (int i = 0; i < names.size(); i++) {
            adj.add(new TreeMap<>());
        }
        for (Map.Entry<Pair, Double> e : weights.entrySet()) {
            int a = index.get(e.getKey().low());
            int b = index.get(e.getKey().high());
            adj.get(a).put(b, e.getValue());
            adj.get(b).put(a, e.getValue());
        }
        double[] loops = new double[names.size()];
        int[] membership = new int[names.size()];
        for (int i = 0; i < membership.length; i++) {
            membership[i] = i;
        }
        while (!adj.isEmpty()) {
            Moving m = localMoving(adj, loops, resolution);
            if (!m.movedAny()) {
                break;
            }
            Aggregated g = aggregate(adj, loops, m.community());
            adj = g.adj();
            loops = g.loops();
            for (int k = 0; k < membership.length; k++) {
                membership[k] = g.mapping()[membership[k]];
            }
        }
        return new Louvain(names, membership);
    }

    /** Connected parts of every community; edges between different communities do not connect. */
    private static List<List<Integer>> components(List<List<Integer>> neighbors, int[] membership) {
        boolean[] seen = new boolean[membership.length];
        List<List<Integer>> out = new ArrayList<>();
        for (int start = 0; start < membership.length; start++) {
            if (seen[start]) {
                continue;
            }
            seen[start] = true;
            Deque<Integer> stack = new ArrayDeque<>();
            stack.push(start);
            List<Integer> part = new ArrayList<>();
            while (!stack.isEmpty()) {
                int node = stack.pop();
                part.add(node);
                for (int other : neighbors.get(node)) {
                    if (!seen[other] && membership[other] == membership[start]) {
                        seen[other] = true;
                        stack.push(other);
                    }
                }
            }
            Collections.sort(part);
            out.add(part);
        }
        return out;
    }
}
