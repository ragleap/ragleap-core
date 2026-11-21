package com.ragleap.vectorstores;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * The tenant filter contract, offline (no server): only the indexed fields can be filtered, any other key is rejected
 * instead of being ignored or matching nothing, and tenant_id reaches the query. The recorded-command parity with the real
 * Python is in the *ParityTest classes (they need the fixtures).
 */
class TenantFilterContractTest {
    private static Map<String, Object> filter(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    @Test
    void pyReprFollowsPython() {
        assertEquals("'a'", PyRepr.str("a"));
        assertEquals("\"it's\"", PyRepr.str("it's"));
        assertEquals("'say \"hi\"'", PyRepr.str("say \"hi\""));
        assertEquals("'a\\'b\"c'", PyRepr.str("a'b\"c"));
        assertEquals("'a\\\\b'", PyRepr.str("a\\b"));
        assertEquals("'a\\nb\\tc\\x01'", PyRepr.str("a\nb\tc\u0001"));
        assertEquals("'cl\u00e9'", PyRepr.str("cl\u00e9"));
        assertEquals("[]", PyRepr.list(List.of()));
        assertEquals("['x', 'y z']", PyRepr.list(List.of("x", "y z")));
    }

    @Test
    void redisFilterClause() {
        assertEquals("*", RedisBackend.filterClause(null, true, "idx"));
        assertEquals("*", RedisBackend.filterClause(Map.of(), true, "idx"));
        assertEquals("(@document_id:{a\\-b})", RedisBackend.filterClause(filter("document_id", "a-b"), true, "idx"));
        assertEquals("(@tenant_id:{acme})", RedisBackend.filterClause(filter("tenant_id", "acme"), true, "idx"));
        // both keys go into ONE group, document_id first, whatever the order of the map
        assertEquals("(@document_id:{d1} @tenant_id:{acme})",
                RedisBackend.filterClause(filter("tenant_id", "acme", "document_id", "d1"), true, "idx"));
        assertEquals("(@tenant_id:{5})", RedisBackend.filterClause(filter("tenant_id", 5), true, "idx"));
    }

    @Test
    void redisRejectsUnknownKeysBeforeAnythingElse() {
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> RedisBackend.filterClause(filter("tenant_id", "acme", "other", "x", "b", 1), true, "idx"));
        assertEquals("RedisBackend can only filter by document_id and tenant_id, got ['b', 'other']; "
                + "other keys used to be ignored silently, which is unsafe for tenant isolation", e.getMessage());
        // the unknown key wins over the missing tenant field (same order as Python)
        assertThrows(IllegalArgumentException.class, () -> RedisBackend.filterClause(filter("other", 1, "tenant_id", "a"), false, "idx"));
    }

    @Test
    void redisTenantFilterNeedsTheField() {
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> RedisBackend.filterClause(filter("tenant_id", "acme"), false, "my_idx"));
        assertEquals("RedisBackend: index 'my_idx' was created without a tenant_id field; "
                + "use a new index_name to enable tenant filtering", e.getMessage());
        assertEquals("(@document_id:{d})", RedisBackend.filterClause(filter("document_id", "d"), false, "my_idx"));
    }

    @Test
    void redisFindsTheTenantFieldAnywhereInTheInfoReply() {
        byte[] tenant = "tenant_id".getBytes(StandardCharsets.UTF_8);
        byte[] other = "document_id".getBytes(StandardCharsets.UTF_8);
        assertTrue(RedisBackend.containsText(List.of(other, List.of(List.of(other, tenant))), "tenant_id"));
        assertFalse(RedisBackend.containsText(List.of(other, List.of(List.of(other))), "tenant_id"));
        assertFalse(RedisBackend.containsText(null, "tenant_id"));
        assertTrue(RedisBackend.containsText("a tenant_id b", "tenant_id"));
        assertTrue(RedisBackend.containsText(List.of(1L, "tenant_id"), "tenant_id"));
        assertFalse(RedisBackend.containsText(42L, "tenant_id"));
    }

    @Test
    void upstashRejectsFilterKeysThatAreNotIdentifiers() {
        assertEquals("a.b_c = 1 AND _x9 = 'y'", UpstashBackend.buildFilter(filter("a.b_c", 1, "_x9", "y")));
        assertEquals("", UpstashBackend.buildFilter(Map.of()));
        for (String bad : List.of("bad key", "1abc", "x;drop", "", "a-b", "cl\u00e9")) {
            IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                    () -> UpstashBackend.buildFilter(filter("ok", 1, bad, 2)), bad);
            assertEquals("Invalid metadata filter key: " + PyRepr.str(bad), e.getMessage());
        }
        assertEquals("Invalid metadata filter key: \"it's\"",
                assertThrows(IllegalArgumentException.class, () -> UpstashBackend.buildFilter(filter("it's", 1))).getMessage());
    }

    @Test
    void openSearchRejectsFilterKeysThatAreNotIndexed() throws Exception {
        OpenSearchBackend be = new OpenSearchBackend("http://127.0.0.1:9");
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> be.searchDense(List.of(1.0), 2, filter("tenant_id", "acme", "flag", true, "a", 1)));
        assertEquals("OpenSearchBackend can only filter by ['chunk_index', 'document_id', 'document_name', 'tenant_id', "
                + "'token_count'], got ['a', 'flag']: other metadata is stored unindexed, so such a filter would silently "
                + "match nothing", e.getMessage());
        // no embedding: nothing is sent and nothing is validated (same order as Python)
        assertEquals(List.of(), be.searchDense(List.of(), 2, filter("flag", true)));
    }
}
