package com.ragleap.rag;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.ragleap.rag.embedding.EmbeddingConfig;
import com.ragleap.rag.generation.ProviderConfig;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * Builds the facade through its public constructor against the test Postgres. The embedder points at a dead port,
 * so ask and retrieve stop at the failed embedding; no schema is created or changed.
 */
@EnabledIfEnvironmentVariable(named = "RAGLEAP_JAVA_TEST_DATABASE_URL", matches = ".+")
class RagLeapWiringTest {
    @Test
    void publicConstructorWiresTheRealCollaborators() throws Exception {
        RagLeapConfig config = RagLeapConfig.builder(System.getenv("RAGLEAP_JAVA_TEST_DATABASE_URL"),
                new ProviderConfig("openai", "key", "gpt-test", "http://localhost:1/v1"),
                new EmbeddingConfig("openai", "key", "text-embedding-test", 3, "http://localhost:1/v1"))
                .cacheMaxSize(5).build();
        try (RagLeap rag = new RagLeap(config)) {
            CacheInfo info = rag.cacheStats();
            assertTrue(info.enabled());
            assertEquals(0, info.size());
            assertEquals(List.of(), rag.retrieve("anything"));
            assertEquals("Sorry, I couldn't process your question (embedding failed).", rag.ask("hello").answer());
        }
    }

    @Test
    void requireTenantFlowsThroughTheConfigAndForTenantStillWorks() throws Exception {
        RagLeapConfig config = RagLeapConfig.builder(System.getenv("RAGLEAP_JAVA_TEST_DATABASE_URL"),
                new ProviderConfig("openai", "key", "gpt-test", "http://localhost:1/v1"),
                new EmbeddingConfig("openai", "key", "text-embedding-test", 3, "http://localhost:1/v1"))
                .requireTenant(true).build();
        try (RagLeap rag = new RagLeap(config)) {
            TenantRequiredException ex = assertThrows(TenantRequiredException.class, () -> rag.ask("hello"));
            assertEquals("ask() needs a tenant scope: use rag.forTenant(...) (RagLeap was built with requireTenant(true))",
                    ex.getMessage());
            assertThrows(TenantRequiredException.class, () -> rag.retrieve("anything"));
            TenantRagLeap view = rag.forTenant("acme");
            assertEquals(List.of(), view.retrieve("anything"));
            assertEquals("Sorry, I couldn't process your question (embedding failed).", view.ask("hello").answer());
        }
    }
}
