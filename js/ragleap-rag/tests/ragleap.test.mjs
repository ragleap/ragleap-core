import test from "node:test";
import assert from "node:assert/strict";
import { RagLeap } from "../dist/ragleap.js";

function makeChunk(overrides = {}) {
  return {
    chunk_id: "chunk-1",
    text: "Paris is the capital of France.",
    similarity_score: 0.95,
    document_id: "doc-1",
    document_name: "facts.txt",
    chunk_index: 0,
    token_count: 6,
    metadata: {},
    ...overrides,
  };
}

function makeBackend(overrides = {}) {
  const calls = {
    init: [],
    documents: [],
    insertedChunks: [],
    dense: [],
    hybrid: [],
    deleted: [],
    list: [],
    closed: 0,
  };

  const backend = {
    calls,
    async init_schema(dimensions) {
      calls.init.push(dimensions);
    },
    async insert_document(...args) {
      calls.documents.push(args);
    },
    async insert_chunk(args) {
      calls.insertedChunks.push(args);
    },
    async search_dense(args) {
      calls.dense.push(args);
      return [makeChunk()];
    },
    async search_hybrid(args) {
      calls.hybrid.push(args);
      return [makeChunk()];
    },
    async list_documents(args) {
      calls.list.push(args);
      return [];
    },
    async delete_document(id) {
      calls.deleted.push(id);
      return true;
    },
    async get_document_filename(id) {
      const row = calls.documents.find(([documentId]) => documentId === id);
      return row?.[1] ?? null;
    },
    async close() {
      calls.closed += 1;
    },
  };

  return Object.assign(backend, overrides);
}

function makeGeneration(overrides = {}) {
  const calls = [];

  const generation = {
    calls,
    async generate_answer(query, chunks, options) {
      calls.push({ query, chunks, options });
      return {
        answer: "Paris is the capital of France.",
        sources: ["facts.txt"],
        citations: [{
          source_number: 1,
          document_name: "facts.txt",
          document_id: "doc-1",
          chunk_id: "chunk-1",
          chunk_index: 0,
          text_preview: "Paris is the capital of France.",
        }],
        provider_used: "test-provider",
        model_used: "test-model",
        usage: {
          prompt_tokens: 10,
          completion_tokens: 5,
          total_tokens: 15,
        },
        cost_usd: 0.001,
        chunks_sent: chunks.length,
      };
    },
    ...overrides,
  };

  return generation;
}

function makeRagLeap(overrides = {}) {
  const embeddingCalls = [];
  const embedder = {
    model: "test-embedding-model",
    dimensions: 2,
    async embed_text(text) {
      embeddingCalls.push(text);
      return [0.1, 0.2];
    },
  };

  const backend = makeBackend();
  const generation = makeGeneration();
  const rag = new RagLeap({
    embedder,
    generation,
    vector_backend: backend,
    ...overrides,
  });

  return { rag, embedder, embeddingCalls, backend, generation };
}

test("initializes the vector backend using the configured embedding dimensions", async () => {
  const { rag, backend } = makeRagLeap();

  await rag.init_schema();

  assert.deepEqual(backend.calls.init, [2]);
});

test("retrieves with hybrid search and reuses cached query embeddings", async () => {
  const { rag, embeddingCalls, backend } = makeRagLeap();

  const first = await rag.retrieve("capital of France", { top_k: 3 });
  const second = await rag.retrieve("capital of France", { top_k: 3 });

  assert.equal(first.length, 1);
  assert.equal(second.length, 1);
  assert.equal(embeddingCalls.length, 1);
  assert.equal(backend.calls.hybrid.length, 2);
  assert.equal(backend.calls.dense.length, 0);
  assert.deepEqual(backend.calls.hybrid[0].embedding, [0.1, 0.2]);
  assert.equal(backend.calls.hybrid[0].top_k, 3);
  assert.equal(rag.cache_stats().hits, 1);
  assert.equal(rag.cache_stats().misses, 1);
});

test("uses dense search when hybrid search is disabled", async () => {
  const { rag, backend } = makeRagLeap();

  const chunks = await rag.retrieve("capital of France", {
    hybrid: false,
    document_id: "doc-1",
    metadata_filter: { category: "geography" },
  });

  assert.equal(chunks.length, 1);
  assert.equal(backend.calls.dense.length, 1);
  assert.equal(backend.calls.hybrid.length, 0);
  assert.equal(backend.calls.dense[0].document_id, "doc-1");
  assert.deepEqual(
    backend.calls.dense[0].metadata_filter,
    { category: "geography" },
  );
});

test("uses dense search when the backend has no hybrid implementation", async () => {
  const backend = makeBackend();
  delete backend.search_hybrid;

  const { rag } = makeRagLeap({ vector_backend: backend });
  const chunks = await rag.retrieve("capital of France");

  assert.equal(chunks.length, 1);
  assert.equal(backend.calls.dense.length, 1);
});

test("does not embed or search when top_k is zero", async () => {
  const { rag, embeddingCalls, backend } = makeRagLeap();

  assert.deepEqual(await rag.retrieve("capital of France", { top_k: 0 }), []);
  assert.equal(embeddingCalls.length, 0);
  assert.equal(backend.calls.hybrid.length, 0);
});

test("rejects empty queries and invalid top_k values", async () => {
  const { rag, embeddingCalls } = makeRagLeap();

  await assert.rejects(rag.retrieve("   "), /query must not be empty/);
  await assert.rejects(
    rag.retrieve("question", { top_k: -1 }),
    /top_k must be a non-negative integer/,
  );
  await assert.rejects(
    rag.retrieve("question", { top_k: 1.5 }),
    /top_k must be a non-negative integer/,
  );
  assert.equal(embeddingCalls.length, 0);
});

test("throws when query embedding fails", async () => {
  const backend = makeBackend();
  const embedder = {
    model: "broken-model",
    dimensions: 2,
    async embed_text() {
      return null;
    },
  };
  const rag = new RagLeap({
    embedder,
    generation: makeGeneration(),
    vector_backend: backend,
  });

  await assert.rejects(
    rag.retrieve("capital of France"),
    /Query embedding failed/,
  );
  assert.equal(backend.calls.hybrid.length, 0);
  assert.equal(backend.calls.dense.length, 0);
});

test("ask passes retrieved chunks and generation options, then applies output guardrails", async () => {
  const { rag, generation } = makeRagLeap({
    output_guardrails: [
      (text) => `${text} Verified.`,
      (text) => text.toUpperCase(),
    ],
  });

  const result = await rag.ask("capital of France", {
    top_k: 4,
    temperature: 0.2,
    max_tokens: 80,
    system_prompt: "Use only retrieved context.",
    history_prefix: "Earlier conversation.",
  });

  assert.equal(result.answer, "PARIS IS THE CAPITAL OF FRANCE. VERIFIED.");
  assert.equal(result.query, "capital of France");
  assert.equal(result.chunks.length, 1);
  assert.equal(result.provider_used, "test-provider");
  assert.equal(result.cost_usd, 0.001);

  assert.equal(generation.calls.length, 1);
  assert.equal(generation.calls[0].query, "capital of France");
  assert.equal(generation.calls[0].chunks.length, 1);
  assert.equal(generation.calls[0].options.temperature, 0.2);
  assert.equal(generation.calls[0].options.max_tokens, 80);
  assert.equal(
    generation.calls[0].options.system_prompt,
    "Use only retrieved context.",
  );

  // The facade should forward all supported generation options.
  assert.equal(
    generation.calls[0].options.history_prefix,
    "Earlier conversation.",
  );
});

test("input guardrails are applied during facade ingestion", async () => {
  const { rag, backend } = makeRagLeap({
    input_guardrails: [(text) => `${text} approved`],
  });

  const result = await rag.ingest_text(
    "notes.txt",
    "alpha beta gamma",
    {},
    false,
    false,
  );

  assert.equal(result.chunks_stored, 1);
  assert.equal(backend.calls.documents.length, 1);
  assert.equal(backend.calls.insertedChunks.length, 1);
  assert.match(backend.calls.insertedChunks[0].text, /approved/);
});

test("fires query and answer callbacks", async () => {
  const queryEvents = [];
  const answerEvents = [];
  const { rag } = makeRagLeap({
    on_query: [(event) => queryEvents.push(event)],
    on_answer: [(event) => answerEvents.push(event)],
  });

  await rag.ask("capital of France");

  assert.equal(queryEvents.length, 1);
  assert.equal(queryEvents[0].query, "capital of France");
  assert.equal(queryEvents[0].result_count, 1);

  assert.equal(answerEvents.length, 1);
  assert.equal(answerEvents[0].query, "capital of France");
  assert.match(answerEvents[0].answer, /Paris/);
  assert.equal(answerEvents[0].provider_used, "test-provider");
});

test("disabled cache reports empty statistics and embeds every query", async () => {
  const { rag, embeddingCalls } = makeRagLeap({ cache_enabled: false });

  assert.equal(rag.cache, null);
  await rag.retrieve("same query");
  await rag.retrieve("same query");

  assert.equal(embeddingCalls.length, 2);
  assert.deepEqual(rag.cache_stats(), {
    hits: 0,
    misses: 0,
    hit_rate: 0,
    size: 0,
  });
});

test("validates configured dimensions", () => {
  const common = {
    embedder: {
      model: "test",
      dimensions: 2,
      async embed_text() {
        return [0.1, 0.2];
      },
    },
    generation: makeGeneration(),
    vector_backend: makeBackend(),
  };

  assert.throws(
    () => new RagLeap({ ...common, dimensions: 0 }),
    /dimensions must be an integer/,
  );
  assert.throws(
    () => new RagLeap({ ...common, dimensions: 1.5 }),
    /dimensions must be an integer/,
  );
});

test("close forwards to a backend close method", async () => {
  const { rag, backend } = makeRagLeap();

  await rag.close();

  assert.equal(backend.calls.closed, 1);
});

test("static database schema initializer executes the combined schema", async () => {
  const statements = [];
  const database = {
    async query(sql) {
      statements.push(sql);
      return { rows: [], rowCount: 0 };
    },
  };

  await RagLeap.init_database_schema(database, 2);

  assert.equal(statements.length, 2);
  assert.match(statements[0], /CREATE TABLE IF NOT EXISTS documents/i);
  assert.match(statements[1], /CREATE TABLE IF NOT EXISTS conversations/i);
});


test("ask loads session history and persists the successful exchange", async () => {
  const memoryCalls = [];
  const memory = {
    async build_history_prompt(sessionId) {
      memoryCalls.push(["history", sessionId]);
      return "Previous conversation:\\nUser: Hello\\n\\n";
    },
    async add_message(sessionId, role, content) {
      memoryCalls.push(["save", sessionId, role, content]);
    },
    async get_history() {
      return [];
    },
  };

  const { rag, generation } = makeRagLeap({
    conversation_memory: memory,
  });

  await rag.ask("What is the capital?", {
    session_id: "session-1",
    history_prefix: "Extra context.\\n",
  });

  assert.equal(
    generation.calls[0].options.history_prefix,
    "Previous conversation:\\nUser: Hello\\n\\nExtra context.\\n",
  );
  assert.deepEqual(memoryCalls, [
    ["history", "session-1"],
    ["save", "session-1", "user", "What is the capital?"],
    ["save", "session-1", "assistant", "Paris is the capital of France."],
  ]);
});

test("ask does not use or persist memory without a session id", async () => {
  const memoryCalls = [];
  const memory = {
    async build_history_prompt(sessionId) {
      memoryCalls.push(["history", sessionId]);
      return "Should not be used";
    },
    async add_message(...args) {
      memoryCalls.push(["save", ...args]);
    },
    async get_history() {
      return [];
    },
  };

  const { rag, generation } = makeRagLeap({
    conversation_memory: memory,
  });

  await rag.ask("capital of France", {
    history_prefix: "Caller supplied prefix",
  });

  assert.equal(
    generation.calls[0].options.history_prefix,
    "Caller supplied prefix",
  );
  assert.deepEqual(memoryCalls, []);
});

test("ask does not persist an exchange when generation fails", async () => {
  const memoryCalls = [];
  const memory = {
    async build_history_prompt() {
      return "";
    },
    async add_message(...args) {
      memoryCalls.push(args);
    },
    async get_history() {
      return [];
    },
  };

  const generation = makeGeneration({
    async generate_answer() {
      throw new Error("generation failed");
    },
  });

  const { rag } = makeRagLeap({
    conversation_memory: memory,
    generation,
  });

  await assert.rejects(
    rag.ask("capital of France", { session_id: "session-1" }),
    /generation failed/,
  );
  assert.deepEqual(memoryCalls, []);
});


test("with_pgvector shares an injected pool with conversation memory", async () => {
  const { ConversationMemory } = await import("../dist/memory.js");

  const pool = {
    async query() {
      return { rows: [], rowCount: 0 };
    },
    async connect() {
      return {
        async query() {
          return { rows: [], rowCount: 0 };
        },
        release() {},
      };
    },
    async end() {
      throw new Error("Caller-owned pool must not be closed");
    },
  };

  const rag = RagLeap.with_pgvector({
    embedder: {
      model: "test-model",
      dimensions: 3,
      async embed_text() { return [1, 0, 0]; },
    },
    generation: makeGeneration(),
    pool,
    dimensions: 3,
  });

  assert.ok(rag.conversation_memory instanceof ConversationMemory);
  assert.equal(rag.conversation_memory.pool, pool);

  await rag.close();
});

test("with_pgvector preserves explicitly supplied conversation memory", async () => {
  const memory = {
    async get_history() { return []; },
    async build_history_prompt() { return ""; },
    async add_message() {},
  };
  const pool = {
    async query() { return { rows: [], rowCount: 0 }; },
    async connect() {
      return {
        async query() { return { rows: [], rowCount: 0 }; },
        release() {},
      };
    },
  };

  const rag = RagLeap.with_pgvector({
    embedder: {
      model: "test-model",
      dimensions: 3,
      async embed_text() { return [1, 0, 0]; },
    },
    generation: makeGeneration(),
    pool,
    conversation_memory: memory,
    dimensions: 3,
  });

  assert.equal(rag.conversation_memory, memory);
  await rag.close();
});

test("with_pgvector rejects missing database URL and pool", () => {
  assert.throws(
    () => RagLeap.with_pgvector({
      embedder: {
        model: "test-model",
        dimensions: 3,
        async embed_text() { return [1, 0, 0]; },
      },
      generation: makeGeneration(),
      dimensions: 3,
    }),
    /Provide database_url or an injected pool/,
  );
});
