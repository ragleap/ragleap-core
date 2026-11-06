package com.ragleap.graph;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** Configuration for GraphRetriever (port of ragleap_graph.retrieval.GraphRetrievalConfig). */
public final class GraphRetrievalConfig {
    public enum Mode {
        HYBRID("hybrid"), GRAPH_ONLY("graph_only");

        private final String label;

        Mode(String label) {
            this.label = label;
        }

        public String label() {
            return label;
        }
    }

    private final Mode mode;
    private final int graphExpandDepth;
    private final int maxGraphResults;
    private final List<String> domainTerms;
    private final int maxQueryEntities;
    private final boolean includeCommunities;
    private final int maxCommunities;

    private GraphRetrievalConfig(Builder b) {
        Mode resolved = null;
        for (Mode m : Mode.values()) {
            if (m.label.equals(b.mode)) {
                resolved = m;
            }
        }
        if (resolved == null) {
            throw new IllegalArgumentException("mode must be 'hybrid' or 'graph_only', got "
                    + LlmParsing.reprOrNone(b.mode));
        }
        if (b.graphExpandDepth < 1) {
            throw new IllegalArgumentException("graph_expand_depth must be >= 1");
        }
        if (b.maxGraphResults < 1) {
            throw new IllegalArgumentException("max_graph_results must be >= 1");
        }
        if (b.maxQueryEntities < 1) {
            throw new IllegalArgumentException("max_query_entities must be >= 1");
        }
        if (b.maxCommunities < 1) {
            throw new IllegalArgumentException("max_communities must be >= 1");
        }
        this.mode = resolved;
        this.graphExpandDepth = b.graphExpandDepth;
        this.maxGraphResults = b.maxGraphResults;
        this.domainTerms = b.domainTerms == null ? null
                : Collections.unmodifiableList(new ArrayList<>(b.domainTerms));
        this.maxQueryEntities = b.maxQueryEntities;
        this.includeCommunities = b.includeCommunities;
        this.maxCommunities = b.maxCommunities;
    }

    public static Builder builder() {
        return new Builder();
    }

    public static GraphRetrievalConfig defaults() {
        return builder().build();
    }

    public Mode mode() {
        return mode;
    }

    public String modeLabel() {
        return mode.label;
    }

    /** Traversal depth for related-entity search; GraphIndex itself rejects values outside 1..10. */
    public int graphExpandDepth() {
        return graphExpandDepth;
    }

    public int maxGraphResults() {
        return maxGraphResults;
    }

    /** Null when not set. */
    public List<String> domainTerms() {
        return domainTerms;
    }

    public int maxQueryEntities() {
        return maxQueryEntities;
    }

    /** Opt-in (default false): also look up the stored communities that contain the query entities. */
    public boolean includeCommunities() {
        return includeCommunities;
    }

    /** Limit for the community lookup (default 3, at least 1); only used when includeCommunities is on. */
    public int maxCommunities() {
        return maxCommunities;
    }

    public static final class Builder {
        private String mode = "hybrid";
        private int graphExpandDepth = 2;
        private int maxGraphResults = 10;
        private List<String> domainTerms;
        private int maxQueryEntities = 10;
        private boolean includeCommunities = false;
        private int maxCommunities = 3;

        public Builder mode(Mode m) {
            this.mode = m.label;
            return this;
        }

        /** Accepts the Python spellings "hybrid" and "graph_only"; anything else fails in build() with Python's message. */
        public Builder mode(String m) {
            this.mode = m;
            return this;
        }

        public Builder graphExpandDepth(int v) {
            this.graphExpandDepth = v;
            return this;
        }

        public Builder maxGraphResults(int v) {
            this.maxGraphResults = v;
            return this;
        }

        public Builder domainTerms(List<String> v) {
            this.domainTerms = v;
            return this;
        }

        public Builder maxQueryEntities(int v) {
            this.maxQueryEntities = v;
            return this;
        }

        public Builder includeCommunities(boolean v) {
            this.includeCommunities = v;
            return this;
        }

        public Builder maxCommunities(int v) {
            this.maxCommunities = v;
            return this;
        }

        public GraphRetrievalConfig build() {
            return new GraphRetrievalConfig(this);
        }
    }
}
