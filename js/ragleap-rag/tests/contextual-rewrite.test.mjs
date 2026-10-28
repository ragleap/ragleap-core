import test from "node:test";
import assert from "node:assert/strict";
import { contextual_rewrite } from "../dist/query_rewrite.js";

const generated = (answer) => ({
  answer,
  sources: [],
  citations: [],
  provider_used: "test",
  model_used: "test-model",
  usage: null,
  chunks_sent: 0,
});

test("returns original query without an LLM call when history is empty", async () => {
  let called = false;
  const generator = {
    async generate_answer() {
      called = true;
      return generated("rewritten");
    },
  };

  const result = await contextual_rewrite(generator, "What about it?", "");

  assert.deepEqual(result, ["What about it?", null]);
  assert.equal(called, false);
});

test("uses the Python prompt and returns the trimmed standalone question", async () => {
  const raw = generated("  How does RAGLeap cache query embeddings? \n");
  let call;
  const generator = {
    async generate_answer(...args) {
      call = args;
      return raw;
    },
  };

  const [rewritten, result] = await contextual_rewrite(
    generator,
    "How does it cache them?",
    "Earlier conversation: We discussed RAGLeap query embedding caching."
  );

  assert.equal(rewritten, "How does RAGLeap cache query embeddings?");
  assert.equal(result, raw);
  assert.equal(
    call[0],
    "Given the conversation history and a follow-up question, rewrite the follow-up question to be a standalone question that includes all necessary context from the history. Do not answer the question - only rewrite it. If the question is already standalone, return it unchanged. Respond with ONLY the rewritten question, nothing else.\n\n" +
    "Earlier conversation: We discussed RAGLeap query embedding caching.\n" +
    "Follow-up question: How does it cache them?\n\n" +
    "Standalone question:"
  );
  assert.deepEqual(call[1], []);
  assert.equal(call[2].history_prefix, "");
  assert.equal(
    call[2].system_prompt,
    "Follow the instructions in the question below exactly. Do not add commentary, explanation, or a preamble - respond with only what was asked for."
  );
});

test("forwards the provider override", async () => {
  const override = { provider: "test", model: "test-model" };
  let options;
  const generator = {
    async generate_answer(_prompt, _chunks, opts) {
      options = opts;
      return generated("Standalone question");
    },
  };

  const [rewritten, result] = await contextual_rewrite(
    generator, "Follow-up?", "Some history", override
  );

  assert.equal(rewritten, "Standalone question");
  assert.ok(result);
  assert.equal(options.override_provider, override);
});

test("uses original query when generated answer is empty", async () => {
  const raw = generated(" \n\t ");
  const generator = { async generate_answer() { return raw; } };

  const [rewritten, result] = await contextual_rewrite(
    generator, "Original query", "Some history"
  );

  assert.equal(rewritten, "Original query");
  assert.equal(result, raw);
});

test("falls back to original query and null result on generation failure", async () => {
  const generator = {
    async generate_answer() {
      throw new Error("provider unavailable");
    },
  };

  const [rewritten, result] = await contextual_rewrite(
    generator, "Original query", "Some history"
  );

  assert.equal(rewritten, "Original query");
  assert.equal(result, null);
});
