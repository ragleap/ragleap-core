package com.ragleap.rag.generation;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.function.Function;

/**
 * Explicit generation provider configuration for ragleap-rag. Java
 * port of ragleap-rag's generation.py — ProviderConfig.
 *
 * No model is ever hardcoded as a silent default - provider model
 * names and deprecations change too frequently for a baked-in default
 * to stay reliable, matching the Python source's documented reasoning
 * exactly (see its CHANGELOG v0.8.1/v0.9.0 reference).
 */
public final class ProviderConfig {

    // Providers with a known OpenAI-compatible /v1 base URL.
    public static final Map<String, String> PROVIDER_BASE_URLS = buildProviderBaseUrls();

    private static Map<String, String> buildProviderBaseUrls() {
        Map<String, String> m = new LinkedHashMap<>();
        m.put("openai", "https://api.openai.com/v1");
        m.put("mistral", "https://api.mistral.ai/v1");
        m.put("groq", "https://api.groq.com/openai/v1");
        m.put("together", "https://api.together.xyz/v1");
        m.put("openrouter", "https://openrouter.ai/api/v1");
        m.put("ollama", "http://localhost:11434/v1");
        m.put("deepseek", "https://api.deepseek.com/v1");
        m.put("demonroute", "https://api.demonroute.com/v1");
        m.put("cheaperinference", "https://api.cheaperinference.com/v1");
        m.put("xai", "https://api.x.ai/v1");
        m.put("cohere", "https://api.cohere.ai/v1");
        m.put("perplexity", "https://api.perplexity.ai");
        return Map.copyOf(m);
    }

    private final String provider;
    private final String apiKey;
    private final String model;
    private final String baseUrl;

    public ProviderConfig(String provider, String apiKey, String model, String baseUrl) {
        this(provider, apiKey, model, baseUrl, System::getenv);
    }

    /**
     * Package-private constructor allowing tests to inject a fake
     * environment lookup instead of real System.getenv() - same
     * pattern as EmbeddingConfig.
     */
    ProviderConfig(String provider, String apiKey, String model, String baseUrl, Function<String, String> env) {
        String resolvedProvider = provider.toLowerCase();
        String resolvedApiKey = apiKey;
        String resolvedModel = model;
        String resolvedBaseUrl = baseUrl;

        if (resolvedProvider.equals("gemini")) {
            resolvedApiKey = firstNonNull(resolvedApiKey, env.apply("GEMINI_API_KEY"));
            resolvedModel = firstNonNull(resolvedModel, env.apply("GEMINI_CHAT_MODEL"));
        } else if (resolvedProvider.equals("anthropic")) {
            resolvedApiKey = firstNonNull(resolvedApiKey, env.apply("ANTHROPIC_API_KEY"));
            resolvedModel = firstNonNull(resolvedModel, env.apply("ANTHROPIC_MODEL"));
        } else if (PROVIDER_BASE_URLS.containsKey(resolvedProvider)) {
            String upper = resolvedProvider.toUpperCase();
            resolvedApiKey = firstNonNull(resolvedApiKey, env.apply(upper + "_API_KEY"));
            resolvedModel = firstNonNull(resolvedModel, env.apply(upper + "_MODEL"));
            resolvedBaseUrl = firstNonNull(resolvedBaseUrl, PROVIDER_BASE_URLS.get(resolvedProvider));
        } else if (resolvedProvider.equals("custom")) {
            resolvedBaseUrl = firstNonNull(resolvedBaseUrl, env.apply("CUSTOM_BASE_URL"));
        } else {
            throw new IllegalArgumentException(
                    "Unknown provider '" + resolvedProvider + "'. Supported: gemini, anthropic, custom, " +
                    String.join(", ", PROVIDER_BASE_URLS.keySet()) + ".");
        }

        if (resolvedApiKey == null && !resolvedProvider.equals("ollama")) {
            throw new IllegalArgumentException(
                    "No API key for provider '" + resolvedProvider + "'. Pass apiKey explicitly.");
        }
        if (!resolvedProvider.equals("gemini") && !resolvedProvider.equals("anthropic") && resolvedBaseUrl == null) {
            throw new IllegalArgumentException(
                    "No baseUrl for provider '" + resolvedProvider + "'. Pass baseUrl explicitly.");
        }
        // First model check: required for every provider except custom,
        // with a detailed provider-specific message.
        if (resolvedModel == null && !resolvedProvider.equals("custom")) {
            String envVarName = resolvedProvider.equals("gemini") ? "GEMINI_CHAT_MODEL"
                    : resolvedProvider.equals("anthropic") ? "ANTHROPIC_MODEL"
                    : resolvedProvider.toUpperCase() + "_MODEL";
            throw new IllegalArgumentException(
                    "No model specified for provider '" + resolvedProvider + "'. Pass model explicitly " +
                    "to ProviderConfig(), or set " + envVarName + " in your environment. ragleap-rag " +
                    "never hardcodes a default model - provider model names and deprecations change too " +
                    "frequently for a baked-in default to stay reliable.");
        }
        // Second model check: catches custom (skipped above), matching the
        // Python source's structure exactly - see the class javadoc for why
        // this isn't dead code despite looking redundant with the check above.
        if (!resolvedProvider.equals("gemini") && !resolvedProvider.equals("anthropic") && resolvedModel == null) {
            throw new IllegalArgumentException(
                    "No model for provider '" + resolvedProvider + "'. Pass model explicitly.");
        }

        this.provider = resolvedProvider;
        this.apiKey = resolvedApiKey;
        this.model = resolvedModel;
        this.baseUrl = resolvedBaseUrl;
    }

    private static String firstNonNull(String a, String b) {
        return a != null ? a : b;
    }

    public String getProvider() {
        return provider;
    }

    public String getApiKey() {
        return apiKey;
    }

    public String getModel() {
        return model;
    }

    public String getBaseUrl() {
        return baseUrl;
    }
}
