import test from "node:test";
import assert from "node:assert/strict";
import {
  MEMORY_SCHEMA_SQL,
  get_core_schema_sql,
  get_memory_schema_sql,
  get_schema_sql,
  init_core_schema,
  init_memory_schema,
  init_schema,
} from "../dist/schema.js";

function createMockPool() {
  const calls = [];
  return {
    calls,
    async query(text, values = []) {
      calls.push({ text, values });
      return { rows: [], rowCount: 0 };
    },
  };
}

test("core schema contains document, chunk, vector, and search indexes", () => {
  const sql = get_core_schema_sql(768);

  assert.match(sql, /CREATE EXTENSION IF NOT EXISTS vector/);
  assert.match(sql, /embedding vector\(768\)/);
  assert.match(sql, /chunks_embedding_idx/);
  assert.match(sql, /chunks_text_search_idx/);
  assert.match(sql, /documents_metadata_idx/);
});

test("core schema rejects invalid dimensions", () => {
  for (const dimensions of [0, -1, 1.5, 16001, NaN]) {
    assert.throws(() => get_core_schema_sql(dimensions), /dimensions/);
  }
});

test("memory schema matches the required conversation tables", () => {
  const sql = get_memory_schema_sql();

  assert.equal(sql, MEMORY_SCHEMA_SQL);
  assert.match(sql, /CREATE TABLE IF NOT EXISTS conversations/);
  assert.match(sql, /CREATE TABLE IF NOT EXISTS conversation_messages/);
  assert.match(sql, /ON DELETE CASCADE/);
  assert.match(sql, /role IN \('user', 'assistant'\)/);
  assert.match(sql, /conversation_messages_session_idx/);
});

test("combined schema includes both core and memory schema", () => {
  const sql = get_schema_sql(1024);

  assert.match(sql, /embedding vector\(1024\)/);
  assert.match(sql, /CREATE TABLE IF NOT EXISTS conversations/);
  assert.match(sql, /CREATE TABLE IF NOT EXISTS conversation_messages/);
});

test("init_core_schema runs only core schema", async () => {
  const pool = createMockPool();

  await init_core_schema(pool, 768);

  assert.equal(pool.calls.length, 1);
  assert.match(pool.calls[0].text, /embedding vector\(768\)/);
  assert.doesNotMatch(pool.calls[0].text, /CREATE TABLE IF NOT EXISTS conversations/);
});

test("init_memory_schema runs only memory schema", async () => {
  const pool = createMockPool();

  await init_memory_schema(pool);

  assert.equal(pool.calls.length, 1);
  assert.match(pool.calls[0].text, /CREATE TABLE IF NOT EXISTS conversations/);
  assert.doesNotMatch(pool.calls[0].text, /CREATE EXTENSION IF NOT EXISTS vector/);
});

test("init_schema initializes core and memory schema separately", async () => {
  const pool = createMockPool();

  await init_schema(pool, 512);

  assert.equal(pool.calls.length, 2);
  assert.match(pool.calls[0].text, /embedding vector\(512\)/);
  assert.match(pool.calls[1].text, /CREATE TABLE IF NOT EXISTS conversations/);
});
