package com.ragleap.rag.generation;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

/** Cheaper Inference was added to the Python provider table (PR 1173); Java keeps the same table. */
class ProviderConfigCheaperInferenceTest {

    @Test
    void cheaperinferenceHasItsDefaultBaseUrl() {
        ProviderConfig config = new ProviderConfig("cheaperinference", "example-key", "example-model", null);
        assertEquals("https://api.cheaperinference.com/v1", config.getBaseUrl());
        assertEquals("https://api.cheaperinference.com/v1", ProviderConfig.PROVIDER_BASE_URLS.get("cheaperinference"));
    }
}
