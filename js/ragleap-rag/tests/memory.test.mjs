import test from "node:test";
import assert from "node:assert/strict";
import {
  ConversationMemory,
  DEFAULT_MAX_HISTORY_MESSAGES,
} from "../dist/memory.js";

function createMockPool(responses = []) {
  const calls = [];

  async function query(text, values = []) {
    calls.push({ text, values });
    return responses.shift() ?? { rows: [], rowCount: 0 };
  }

  return {
    calls,
    query,
    async connect() {
      return {
        query,
        release() {},
      };
    },
  };
}

test("exports the default history limit", () => {
  assert.equal(DEFAULT_MAX_HISTORY_MESSAGES, 10);
});

test("constructor validates the history limit", () => {
  const pool = createMockPool();

  assert.equal(new ConversationMemory(pool).max_history_messages, 10);
  assert.equal(new ConversationMemory(pool, 0).max_history_messages, 0);
  assert.throws(() => new ConversationMemory(pool, -1), /non-negative integer/);
  assert.throws(() => new ConversationMemory(pool, 1.5), /non-negative integer/);
});

test("add_message ensures a session and inserts the message", async () => {
  const pool = createMockPool();
  const memory = new ConversationMemory(pool);

  await memory.add_message("session-1", "user", "Hello");

  assert.equal(pool.calls[0].text.trim(), "BEGIN");
  assert.match(pool.calls[1].text, /INSERT INTO conversations/);
  assert.match(pool.calls[2].text, /INSERT INTO conversation_messages/);
  assert.equal(pool.calls[3].text.trim(), "COMMIT");
  assert.match(pool.calls[1].text, /INSERT INTO conversations/);
  assert.match(pool.calls[1].text, /ON CONFLICT/);
  assert.deepEqual(pool.calls[1].values, ["session-1"]);
  assert.match(pool.calls[2].text, /INSERT INTO conversation_messages/);
  assert.deepEqual(pool.calls[2].values, ["session-1", "user", "Hello"]);
});

test("add_message rolls back on insert failure and releases the client", async () => {
  const calls = [];
  const originalError = new Error("message insert failed");
  let released = false;

  const client = {
    async query(text, values = []) {
      calls.push({ text, values });

      if (/INSERT INTO conversation_messages/i.test(text)) {
        throw originalError;
      }

      return { rows: [], rowCount: 0 };
    },
    release() {
      released = true;
    },
  };

  const pool = {
    async query() {
      throw new Error("Expected transaction client to handle queries");
    },
    async connect() {
      return client;
    },
  };

  const memory = new ConversationMemory(pool);

  await assert.rejects(
    memory.add_message("session-1", "user", "Hello"),
    (error) => error === originalError,
  );

  assert.equal(calls[0].text.trim(), "BEGIN");
  assert.match(calls[1].text, /INSERT INTO conversations/);
  assert.match(calls[2].text, /INSERT INTO conversation_messages/);
  assert.equal(calls[3].text.trim(), "ROLLBACK");
  assert.equal(released, true);
});

test("add_message rejects invalid roles", async () => {
  const pool = createMockPool();
  const memory = new ConversationMemory(pool);

  await assert.rejects(
    memory.add_message("session-1", "system", "Hello"),
    /role must be/,
  );
  assert.equal(pool.calls.length, 0);
});

test("get_history returns recent messages in chronological order", async () => {
  const newer = { role: "assistant", content: "Hi", created_at: "2026-01-02" };
  const older = { role: "user", content: "Hello", created_at: "2026-01-01" };
  const pool = createMockPool([{ rows: [newer, older], rowCount: 2 }]);
  const memory = new ConversationMemory(pool, 5);

  const history = await memory.get_history("session-1");

  assert.deepEqual(history, [older, newer]);
  assert.match(pool.calls[0].text, /ORDER BY created_at DESC/);
  assert.deepEqual(pool.calls[0].values, ["session-1", 5]);
});

test("get_history supports an explicit limit and zero limit", async () => {
  const pool = createMockPool([{ rows: [], rowCount: 0 }]);
  const memory = new ConversationMemory(pool);

  await memory.get_history("session-1", 3);
  assert.deepEqual(pool.calls[0].values, ["session-1", 3]);

  const before = pool.calls.length;
  assert.deepEqual(await memory.get_history("session-1", 0), []);
  assert.equal(pool.calls.length, before);
});

test("get_history rejects invalid limits", async () => {
  const memory = new ConversationMemory(createMockPool());

  await assert.rejects(memory.get_history("s", -1), /non-negative integer/);
  await assert.rejects(memory.get_history("s", 1.5), /non-negative integer/);
});

test("clear_session deletes the conversation row", async () => {
  const pool = createMockPool();
  const memory = new ConversationMemory(pool);

  await memory.clear_session("session-1");

  assert.match(pool.calls[0].text, /DELETE FROM conversations/);
  assert.deepEqual(pool.calls[0].values, ["session-1"]);
});

test("build_history_prompt formats prior messages", async () => {
  const pool = createMockPool([{
    rows: [
      { role: "assistant", content: "Hi there", created_at: "2026-01-02" },
      { role: "user", content: "Hello", created_at: "2026-01-01" },
    ],
    rowCount: 2,
  }]);
  const memory = new ConversationMemory(pool);

  assert.equal(
    await memory.build_history_prompt("session-1"),
    "Previous conversation:\nUser: Hello\nAssistant: Hi there\n\n",
  );
});

test("build_history_prompt returns empty text when history is empty", async () => {
  const memory = new ConversationMemory(createMockPool());

  assert.equal(await memory.build_history_prompt("missing-session"), "");
});
