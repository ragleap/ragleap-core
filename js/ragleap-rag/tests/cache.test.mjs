import test from "node:test";
import assert from "node:assert/strict";
import { QueryEmbeddingCache } from "../dist/cache.js";

test("cache miss followed by hit", () => {
  const cache = new QueryEmbeddingCache(10);

  assert.equal(cache.get("what is x?", "model-a"), null);
  cache.set("what is x?", "model-a", [0.1, 0.2, 0.3]);

  assert.deepEqual(cache.get("what is x?", "model-a"), [0.1, 0.2, 0.3]);
  assert.deepEqual(cache.stats(), {
    hits: 1, misses: 1, hit_rate: 0.5, size: 1,
  });
});

test("cache key includes the embedding model", () => {
  const cache = new QueryEmbeddingCache();

  cache.set("same query", "model-a", [0.1]);

  assert.equal(cache.get("same query", "model-b"), null);
});

test("cache evicts the least recently used entry", () => {
  const cache = new QueryEmbeddingCache(2);

  cache.set("q1", "m", [1]);
  cache.set("q2", "m", [2]);

  // Refresh q1, making q2 the least recently used entry.
  assert.deepEqual(cache.get("q1", "m"), [1]);

  cache.set("q3", "m", [3]);

  assert.equal(cache.get("q2", "m"), null);
  assert.deepEqual(cache.get("q1", "m"), [1]);
  assert.deepEqual(cache.get("q3", "m"), [3]);
});

test("updating an entry refreshes it without duplicating it", () => {
  const cache = new QueryEmbeddingCache(2);

  cache.set("q1", "m", [1]);
  cache.set("q2", "m", [2]);
  cache.set("q1", "m", [10]);

  assert.deepEqual(cache.stats(), {
    hits: 0, misses: 0, hit_rate: 0, size: 2,
  });
  assert.deepEqual(cache.get("q1", "m"), [10]);
});

test("clear resets entries and statistics", () => {
  const cache = new QueryEmbeddingCache();

  cache.get("missing", "m");
  cache.set("q1", "m", [1]);
  cache.get("q1", "m");
  cache.clear();

  assert.deepEqual(cache.stats(), {
    hits: 0, misses: 0, hit_rate: 0, size: 0,
  });
});

test("empty cache has a zero hit rate", () => {
  assert.deepEqual(new QueryEmbeddingCache().stats(), {
    hits: 0, misses: 0, hit_rate: 0, size: 0,
  });
});
