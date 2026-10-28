import test from "node:test";
import assert from "node:assert/strict";
import {
  PgVectorBackend,
  RRF_K,
  DEFAULT_MIN_SIMILARITY,
} from "../dist/pgvector.js";

function createMockPool(responses = []) {
  const calls = [];
  return {
    calls,
    async query(text, values = []) {
      calls.push({ text, values });
      return responses.shift() ?? { rows: [], rowCount: 0 };
    },
  };
}

const chunk = (id, score = 0.8) => ({
  chunk_id: id,
  text: `text-${id}`,
  document_id: "doc-1",
  document_name: "file.txt",
  chunk_index: 0,
  similarity_score: score,
});

test("exports expected defaults", () => {
  assert.equal(RRF_K, 60);
  assert.equal(DEFAULT_MIN_SIMILARITY, 0.05);
});

test("constructor requires a database URL or injected pool", () => {
  assert.throws(() => new PgVectorBackend(), /Provide databaseUrl or an injected pool/);
});

test("schema initialization validates dimensions and records successful setup", async () => {
  const pool = createMockPool();
  const backend = new PgVectorBackend({ pool });

  await assert.rejects(() => backend.init_schema(0), /dimensions/);
  assert.equal(pool.calls.length, 0);

  await backend.init_schema(3);
  assert.match(pool.calls[0].text, /vector\(3\)/);
  assert.match(pool.calls[0].text, /text_search_vector/);
});

test("dense search uses valid placeholders without metadata filter", async () => {
  const pool = createMockPool([
    { rows: [], rowCount: null },
    { rows: [chunk("c1", 0.812345)], rowCount: 1 },
  ]);
  const backend = new PgVectorBackend({ pool });
  await backend.init_schema(3);

  const result = await backend.search_dense({
    embedding: [0.1, 0.2, 0.3],
    top_k: 5,
  });

  assert.equal(result.length, 1);
  assert.equal(result[0].similarity_score, 0.8123);
  const call = pool.calls[1];
  assert.match(call.text, /LIMIT \$7/);
  assert.deepEqual(call.values, [
    3, "[0.1,0.2,0.3]", 3, 3, "[0.1,0.2,0.3]", 3, 5,
  ]);
});

test("dense search uses valid placeholders with metadata filter", async () => {
  const pool = createMockPool([{ rows: [], rowCount: 0 }]);
  const backend = new PgVectorBackend({ pool });
  await backend.init_schema(3);

  await backend.search_dense({
    embedding: [0.1, 0.2, 0.3],
    top_k: 4,
    metadata_filter: { department: "sales" },
  });

  const call = pool.calls[1];
  assert.match(call.text, /metadata @> \$4::jsonb/);
  assert.match(call.text, /LIMIT \$8/);
  assert.deepEqual(call.values, [
    3, "[0.1,0.2,0.3]", 3, '{"department":"sales"}',
    3, "[0.1,0.2,0.3]", 3, 4,
  ]);
});

test("dense search filters by document_id and metadata with correct placeholders", async () => {
  const pool = createMockPool([
    { rows: [], rowCount: null },
    { rows: [], rowCount: 0 },
  ]);
  const backend = new PgVectorBackend({ pool });
  await backend.init_schema(3);

  await backend.search_dense({
    embedding: [0.1, 0.2, 0.3],
    top_k: 4,
    document_id: "doc-42",
    metadata_filter: { department: "sales" },
  });

  const call = pool.calls[1];
  assert.ok(call.text.includes("document_id = $4"));
  assert.ok(call.text.includes("metadata @> $5::jsonb"));
  assert.ok(call.text.includes("LIMIT $9"));
  assert.deepEqual(call.values, [
    3, "[0.1,0.2,0.3]", 3, "doc-42",
    '{"department":"sales"}', 3, "[0.1,0.2,0.3]", 3, 4,
  ]);
});

test("dense search rejects mismatched dimensions and non-finite vectors", async () => {
  const pool = createMockPool();
  const backend = new PgVectorBackend({ pool });
  await backend.init_schema(3);

  await assert.rejects(
    () => backend.search_dense({ embedding: [1, 2], top_k: 3 }),
    /dimension mismatch/i,
  );
  await assert.rejects(
    () => backend.search_dense({ embedding: [1, Infinity, 3], top_k: 3 }),
    /finite numbers/i,
  );
});

test("dense search filters scores below minimum similarity", async () => {
  const pool = createMockPool([
    { rows: [], rowCount: null },
    { rows: [chunk("low", 0.01), chunk("high", 0.8)], rowCount: 2 },
  ]);
  const backend = new PgVectorBackend({ pool });
  await backend.init_schema(3);

  const result = await backend.search_dense({
    embedding: [1, 2, 3],
    top_k: 2,
  });
  assert.deepEqual(result.map((item) => item.chunk_id), ["high"]);
});

test("sparse search builds full-text query and optional metadata filter", async () => {
  const pool = createMockPool([{ rows: [chunk("s1", 0.5)], rowCount: 1 }]);
  const backend = new PgVectorBackend({ pool });

  const result = await backend.search_sparse({
    query_text: "customer support",
    top_k: 7,
    metadata_filter: { team: "helpdesk" },
  });

  assert.equal(result[0].chunk_id, "s1");
  const call = pool.calls[0];
  assert.match(call.text, /websearch_to_tsquery/);
  assert.match(call.text, /metadata @> \$3::jsonb/);
  assert.match(call.text, /LIMIT \$4/);
  assert.deepEqual(call.values, [
    "customer support", "customer support", '{"team":"helpdesk"}', 7,
  ]);
});

test("sparse search applies document_id and metadata filters", async () => {
  const pool = createMockPool([{ rows: [], rowCount: 0 }]);
  const backend = new PgVectorBackend({ pool });

  await backend.search_sparse({
    query_text: "customer support",
    top_k: 5,
    document_id: "doc-42",
    metadata_filter: { team: "helpdesk" },
  });

  const call = pool.calls[0];
  assert.ok(call.text.includes("document_id = $3"));
  assert.ok(call.text.includes("metadata @> $4::jsonb"));
  assert.ok(call.text.includes("LIMIT $5"));
  assert.deepEqual(call.values, [
    "customer support", "customer support", "doc-42",
    '{"team":"helpdesk"}', 5,
  ]);
});

test("hybrid search combines dense and sparse ranks with reciprocal rank fusion", async () => {
  const pool = createMockPool([
    { rows: [], rowCount: null },
    { rows: [chunk("a"), chunk("b")], rowCount: 2 },
    { rows: [chunk("b"), chunk("c")], rowCount: 2 },
  ]);
  const backend = new PgVectorBackend({ pool });
  await backend.init_schema(3);

  const results = await backend.search_hybrid({
    query_text: "hello",
    embedding: [1, 2, 3],
    top_k: 3,
  });

  assert.equal(results.length, 3);
  assert.equal(results[0].chunk_id, "b");
  assert.equal(results[0].retrieval_method, "hybrid_rrf");
  assert.equal(results[0].similarity_score, Number(
    (1 / (RRF_K + 2) + 1 / (RRF_K + 1)).toFixed(6),
  ));
});

test("hybrid search forwards document_id to dense and sparse searches", async () => {
  const pool = createMockPool([
    { rows: [], rowCount: null },
    { rows: [], rowCount: 0 },
    { rows: [], rowCount: 0 },
  ]);
  const backend = new PgVectorBackend({ pool });
  await backend.init_schema(3);

  await backend.search_hybrid({
    query_text: "customer support",
    embedding: [1, 2, 3],
    top_k: 2,
    document_id: "doc-42",
  });

  const denseCall = pool.calls[1];
  const sparseCall = pool.calls[2];
  assert.ok(denseCall.text.includes("document_id = $4"));
  assert.deepEqual(denseCall.values, [
    3, "[1,2,3]", 3, "doc-42", 3, "[1,2,3]", 3, 6,
  ]);
  assert.ok(sparseCall.text.includes("document_id = $3"));
  assert.deepEqual(sparseCall.values, [
    "customer support", "customer support", "doc-42", 6,
  ]);
});

test("empty searches return without querying the pool", async () => {
  const pool = createMockPool();
  const backend = new PgVectorBackend({ pool });

  assert.deepEqual(await backend.search_dense({ embedding: [], top_k: 5 }), []);
  assert.deepEqual(await backend.search_sparse({ query_text: "  ", top_k: 5 }), []);
  assert.deepEqual(await backend.search_hybrid({
    query_text: "hello", embedding: [1, 2], top_k: 0,
  }), []);
  assert.equal(pool.calls.length, 0);
});

test("document CRUD methods map database rows", async () => {
  const pool = createMockPool([
    { rows: [{ id: "doc-1", filename: "a.txt", uploaded_at: "2026-01-01", metadata: { x: 1 }, chunk_count: 2 }], rowCount: 1 },
    { rows: [], rowCount: 1 },
    { rows: [{ filename: "a.txt" }], rowCount: 1 },
    { rows: [], rowCount: 0 },
  ]);
  const backend = new PgVectorBackend({ pool });

  const docs = await backend.list_documents({ limit: 10, offset: 0 });
  assert.equal(docs[0].document_id, "doc-1");
  assert.equal(docs[0].chunk_count, 2);

  assert.equal(await backend.delete_document("doc-1"), true);
  assert.equal(await backend.get_document_filename("doc-1"), "a.txt");
  assert.equal(await backend.delete_document("missing"), false);
});

test("injected pool is not closed by backend.close()", async () => {
  let ended = false;
  const pool = {
    async query() {
      return { rows: [], rowCount: 0 };
    },
    async end() {
      ended = true;
    },
  };

  const backend = new PgVectorBackend({ pool });
  await backend.close();
  assert.equal(ended, false);
});
