import test from "node:test";
import assert from "node:assert/strict";
import { TextChunker, create_chunker } from "../dist/chunker.js";

test("chunks use the expected overlapping windows", () => {
  const chunks = new TextChunker({
    chunk_size: 5,
    chunk_overlap: 1,
  }).chunk_text("the quick brown fox jumps over the lazy dog today");

  assert.equal(chunks[0].text, "the quick brown fox jumps");
  assert.equal(chunks[1].text, "jumps over the lazy dog");
  assert.deepEqual(
    chunks.map((chunk) => chunk.chunk_index),
    chunks.map((_, index) => index),
  );
});

test("empty and whitespace-only inputs return no chunks", () => {
  const chunker = new TextChunker();
  assert.deepEqual(chunker.chunk_text(""), []);
  assert.deepEqual(chunker.chunk_text("   "), []);
});

test("overlap equal to chunk size throws", () => {
  assert.throws(
    () => new TextChunker({ chunk_size: 10, chunk_overlap: 10 }),
    /Chunk overlap must be less than chunk size/,
  );
});

test("fallback counts whitespace-separated words and is inexact", () => {
  const chunks = new TextChunker({
    chunk_size: 10,
    chunk_overlap: 2,
  }).chunk_text("some real text to chunk here for this test");

  for (const chunk of chunks) {
    assert.equal(chunk.token_count, chunk.text.split(/\s+/u).length);
    assert.equal(chunk.token_count_is_exact, false);
  }
});

test("factory creates a configured chunker", () => {
  const chunker = create_chunker(10, 2);
  assert.equal(chunker.chunk_size, 10);
  assert.equal(chunker.chunk_overlap, 2);
});
