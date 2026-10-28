import test from "node:test";
import assert from "node:assert/strict";
import { IngestionService } from "../dist/ingestion.js";
import { TextChunker } from "../dist/chunker.js";

function makeBackend({ failChunkAt = -1 } = {}) {
  const calls = { documents: [], chunks: [], deleted: [] };
  return {
    calls,
    async insert_document(...args) {
      calls.documents.push(args);
    },
    async insert_chunk(args) {
      if (calls.chunks.length === failChunkAt) {
        throw new Error("simulated chunk insert failure");
      }
      calls.chunks.push(args);
    },
    async delete_document(id) {
      calls.deleted.push(id);
      return true;
    },
    async get_document_filename(id) {
      const document = calls.documents.find(([documentId]) => documentId === id);
      return document ? document[1] : null;
    },
  };
}

function makeService(backend, embed_text, extra = {}) {
  return new IngestionService({
    chunker: new TextChunker({ chunk_size: 3, chunk_overlap: 1 }),
    embedder: { embed_text },
    vector_backend: backend,
    ...extra,
  });
}

test("ingests chunks and emits the success event", async () => {
  const backend = makeBackend();
  const events = [];
  const service = makeService(
    backend,
    async () => [0.1, 0.2],
    { on_ingest: [(event) => events.push(event)] },
  );

  const result = await service.ingest_text("notes.txt", "one two three four");

  assert.match(result.document_id, /^[0-9a-f-]{36}$/i);
  assert.equal(result.chunks_stored, 2);
  assert.equal(backend.calls.documents.length, 1);
  assert.equal(backend.calls.chunks.length, 2);
  assert.deepEqual(events, [{
    document_id: result.document_id,
    filename: "notes.txt",
    chunks_stored: 2,
    chunks_attempted: 2,
  }]);
  assert.deepEqual(backend.calls.deleted, []);
});

test("rejects empty input before writing to storage", async () => {
  const backend = makeBackend();
  const service = makeService(backend, async () => [0.1]);

  await assert.rejects(
    service.ingest_text("empty.txt", "   "),
    /No chunks produced/,
  );
  assert.equal(backend.calls.documents.length, 0);
});

test("cleans up when all embeddings fail", async () => {
  const backend = makeBackend();
  const service = makeService(backend, async () => null);

  await assert.rejects(
    service.ingest_text("notes.txt", "one two three four"),
    /All 2 chunk\(s\) failed to embed/,
  );
  assert.equal(backend.calls.deleted.length, 1);
  assert.equal(backend.calls.deleted[0], backend.calls.documents[0][0]);
});

test("cleans up after a chunk storage error and preserves the original error", async () => {
  const backend = makeBackend({ failChunkAt: 1 });
  const service = makeService(backend, async () => [0.1, 0.2]);

  await assert.rejects(
    service.ingest_text("notes.txt", "one two three four"),
    /simulated chunk insert failure/,
  );
  assert.equal(backend.calls.deleted.length, 1);
});

test("runs guardrails in order and supports disabling sanitization", async () => {
  const backend = makeBackend();
  const service = makeService(
    backend,
    async () => [0.1],
    {
      input_guardrails: [
        (text) => `${text} first`,
        (text) => `${text} second`,
      ],
    },
  );

  await service.ingest_text("notes.txt", "hello\u0000", {}, false, false);
  assert.equal(backend.calls.chunks[0].text, "hello\u0000 first second");
});


test("update_document preserves the existing filename and creates a new ID", async () => {
  const backend = makeBackend();
  backend.calls.documents.push(["old-id", "original.txt", {}]);
  const service = makeService(backend, async () => [0.1, 0.2]);

  const result = await service.update_document("old-id", "replacement text");

  assert.notEqual(result.document_id, "old-id");
  assert.equal(result.chunks_stored, 1);
  assert.deepEqual(backend.calls.deleted, ["old-id"]);
  assert.equal(backend.calls.documents[1][1], "original.txt");
});

test("update_document uses an explicitly supplied replacement filename", async () => {
  const backend = makeBackend();
  backend.calls.documents.push(["old-id", "original.txt", {}]);
  const service = makeService(backend, async () => [0.1, 0.2]);

  const result = await service.update_document(
    "old-id",
    "replacement text",
    "renamed.txt",
  );

  assert.notEqual(result.document_id, "old-id");
  assert.deepEqual(backend.calls.deleted, ["old-id"]);
  assert.equal(backend.calls.documents[1][1], "renamed.txt");
});

test("update_document rejects a missing document before deleting anything", async () => {
  const backend = makeBackend();
  const service = makeService(backend, async () => [0.1]);

  await assert.rejects(
    service.update_document("missing-id", "replacement text"),
    /No document found with id missing-id/,
  );

  assert.deepEqual(backend.calls.deleted, []);
  assert.equal(backend.calls.documents.length, 0);
});

test("update_document deletes the old document before replacement ingestion", async () => {
  const backend = makeBackend();
  backend.calls.documents.push(["old-id", "original.txt", {}]);
  const service = makeService(backend, async () => [0.1]);

  await assert.rejects(
    service.update_document("old-id", "   "),
    /No chunks produced/,
  );

  assert.deepEqual(backend.calls.deleted, ["old-id"]);
  assert.equal(backend.calls.documents.length, 1);
});


test("a throwing guardrail aborts before any storage writes", async () => {
  const backend = makeBackend();
  const service = makeService(
    backend,
    async () => [0.1, 0.2],
    {
      input_guardrails: [
        (text) => text,
        () => {
          throw new Error("content rejected by guardrail");
        },
      ],
    },
  );

  await assert.rejects(
    service.ingest_text("blocked.txt", "sensitive content"),
    /content rejected by guardrail/,
  );

  assert.equal(backend.calls.documents.length, 0);
  assert.equal(backend.calls.chunks.length, 0);
  assert.deepEqual(backend.calls.deleted, []);
});

test("null metadata is normalized for document and chunk storage", async () => {
  const backend = makeBackend();
  const service = makeService(backend, async () => [0.1, 0.2]);

  await service.ingest_text(
    "metadata.txt",
    "one two three",
    null,
  );

  assert.deepEqual(backend.calls.documents[0][2], {});
  assert.ok(backend.calls.chunks.length > 0);

  for (const chunk of backend.calls.chunks) {
    assert.deepEqual(chunk.metadata, {});
  }
});
