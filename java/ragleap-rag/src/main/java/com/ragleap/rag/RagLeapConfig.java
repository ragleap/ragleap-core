package com.ragleap.rag;

import com.ragleap.rag.cost.Rate;
import com.ragleap.rag.embedding.EmbeddingConfig;
import com.ragleap.rag.generation.ProviderConfig;
import com.ragleap.rag.vectorstore.VectorBackend;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.function.Consumer;
import java.util.function.UnaryOperator;

/**
 * Settings for {@link RagLeap}, with the Python defaults. Create with {@link #builder}. The Redis query cache and
 * reranking of the Python package are not available in Java. Passing null for a list means no entries.
 */
public record RagLeapConfig(String databaseUrl, ProviderConfig primary, EmbeddingConfig embedder,
                            VectorBackend vectorBackend, List<ProviderConfig> fallbacks, double defaultTemperature,
                            int defaultMaxTokens, int maxContextChars, Integer chunkSize, Integer chunkOverlap,
                            boolean cacheEnabled, int cacheMaxSize, List<UnaryOperator<String>> inputGuardrails,
                            List<UnaryOperator<String>> outputGuardrails,
                            List<Consumer<Map<String, Object>>> onIngest,
                            List<Consumer<Map<String, Object>>> onQuery,
                            List<Consumer<Map<String, Object>>> onAnswer,
                            Map<String, Map<String, Rate>> pricingTable, Double budgetUsdPerMonth,
                            ProviderConfig budgetFallback, boolean requireTenant) {

    /** The layout before requireTenant existed: no tenant scope is required. */
    public RagLeapConfig(String databaseUrl, ProviderConfig primary, EmbeddingConfig embedder,
                         VectorBackend vectorBackend, List<ProviderConfig> fallbacks, double defaultTemperature,
                         int defaultMaxTokens, int maxContextChars, Integer chunkSize, Integer chunkOverlap,
                         boolean cacheEnabled, int cacheMaxSize, List<UnaryOperator<String>> inputGuardrails,
                         List<UnaryOperator<String>> outputGuardrails,
                         List<Consumer<Map<String, Object>>> onIngest,
                         List<Consumer<Map<String, Object>>> onQuery,
                         List<Consumer<Map<String, Object>>> onAnswer,
                         Map<String, Map<String, Rate>> pricingTable, Double budgetUsdPerMonth,
                         ProviderConfig budgetFallback) {
        this(databaseUrl, primary, embedder, vectorBackend, fallbacks, defaultTemperature, defaultMaxTokens,
                maxContextChars, chunkSize, chunkOverlap, cacheEnabled, cacheMaxSize, inputGuardrails,
                outputGuardrails, onIngest, onQuery, onAnswer, pricingTable, budgetUsdPerMonth, budgetFallback,
                false);
    }

    public RagLeapConfig {
        Objects.requireNonNull(databaseUrl, "databaseUrl");
        Objects.requireNonNull(primary, "primary");
        Objects.requireNonNull(embedder, "embedder");
        fallbacks = fallbacks == null ? List.of() : List.copyOf(fallbacks);
        inputGuardrails = inputGuardrails == null ? List.of() : List.copyOf(inputGuardrails);
        outputGuardrails = outputGuardrails == null ? List.of() : List.copyOf(outputGuardrails);
        onIngest = onIngest == null ? List.of() : List.copyOf(onIngest);
        onQuery = onQuery == null ? List.of() : List.copyOf(onQuery);
        onAnswer = onAnswer == null ? List.of() : List.copyOf(onAnswer);
    }

    public static Builder builder(String databaseUrl, ProviderConfig primary, EmbeddingConfig embedder) {
        return new Builder(databaseUrl, primary, embedder);
    }

    public static final class Builder {
        private final String databaseUrl;
        private final ProviderConfig primary;
        private final EmbeddingConfig embedder;
        private VectorBackend vectorBackend;
        private List<ProviderConfig> fallbacks = List.of();
        private double defaultTemperature = 0.3;
        private int defaultMaxTokens = 1024;
        private int maxContextChars = 12000;
        private Integer chunkSize;
        private Integer chunkOverlap;
        private boolean cacheEnabled = true;
        private int cacheMaxSize = 1000;
        private List<UnaryOperator<String>> inputGuardrails = List.of();
        private List<UnaryOperator<String>> outputGuardrails = List.of();
        private List<Consumer<Map<String, Object>>> onIngest = List.of();
        private List<Consumer<Map<String, Object>>> onQuery = List.of();
        private List<Consumer<Map<String, Object>>> onAnswer = List.of();
        private Map<String, Map<String, Rate>> pricingTable;
        private Double budgetUsdPerMonth;
        private ProviderConfig budgetFallback;
        private boolean requireTenant;

        private Builder(String databaseUrl, ProviderConfig primary, EmbeddingConfig embedder) {
            this.databaseUrl = databaseUrl;
            this.primary = primary;
            this.embedder = embedder;
        }

        public Builder vectorBackend(VectorBackend v) {
            this.vectorBackend = v;
            return this;
        }

        public Builder fallbacks(List<ProviderConfig> v) {
            this.fallbacks = v;
            return this;
        }

        public Builder defaultTemperature(double v) {
            this.defaultTemperature = v;
            return this;
        }

        public Builder defaultMaxTokens(int v) {
            this.defaultMaxTokens = v;
            return this;
        }

        public Builder maxContextChars(int v) {
            this.maxContextChars = v;
            return this;
        }

        public Builder chunkSize(Integer v) {
            this.chunkSize = v;
            return this;
        }

        public Builder chunkOverlap(Integer v) {
            this.chunkOverlap = v;
            return this;
        }

        public Builder cacheEnabled(boolean v) {
            this.cacheEnabled = v;
            return this;
        }

        public Builder cacheMaxSize(int v) {
            this.cacheMaxSize = v;
            return this;
        }

        public Builder inputGuardrails(List<UnaryOperator<String>> v) {
            this.inputGuardrails = v;
            return this;
        }

        public Builder outputGuardrails(List<UnaryOperator<String>> v) {
            this.outputGuardrails = v;
            return this;
        }

        public Builder onIngest(List<Consumer<Map<String, Object>>> v) {
            this.onIngest = v;
            return this;
        }

        public Builder onQuery(List<Consumer<Map<String, Object>>> v) {
            this.onQuery = v;
            return this;
        }

        public Builder onAnswer(List<Consumer<Map<String, Object>>> v) {
            this.onAnswer = v;
            return this;
        }

        public Builder pricingTable(Map<String, Map<String, Rate>> v) {
            this.pricingTable = v;
            return this;
        }

        public Builder budgetUsdPerMonth(Double v) {
            this.budgetUsdPerMonth = v;
            return this;
        }

        public Builder budgetFallback(ProviderConfig v) {
            this.budgetFallback = v;
            return this;
        }

        /**
         * When true, every unscoped call (ingest, ask, retrieve, history, document management) throws
         * TenantRequiredException; use {@link RagLeap#forTenant} instead. Default false.
         */
        public Builder requireTenant(boolean v) {
            this.requireTenant = v;
            return this;
        }

        public RagLeapConfig build() {
            return new RagLeapConfig(databaseUrl, primary, embedder, vectorBackend, fallbacks, defaultTemperature,
                    defaultMaxTokens, maxContextChars, chunkSize, chunkOverlap, cacheEnabled, cacheMaxSize,
                    inputGuardrails, outputGuardrails, onIngest, onQuery, onAnswer, pricingTable,
                    budgetUsdPerMonth, budgetFallback, requireTenant);
        }
    }
}
