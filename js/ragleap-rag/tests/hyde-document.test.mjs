import test from "node:test";
import assert from "node:assert/strict";
import { hyde_document } from "../dist/query_rewrite.js";

const generated = (answer) => ({
  answer,
  sources: [],
  citations: [],
  provider_used: "test",
  model_used: "test-model",
  usage: null,
  chunks_sent: 0,
});

test("uses the exact Python prompt and returns the trimmed passage", async () => {
  let call;
  const result = generated("  A hypothetical passage about retrieval. \n");
  const generator = {
    async generate_answer(...args) {
      call = args;
      return result;
    },
  };

  const [passage, raw] = await hyde_document(
    generator, "How does RAG work?"
  );

  assert.equal(passage, "A hypothetical passage about retrieval.");
  assert.equal(raw, result);
  assert.equal(call[0],
    "Write a short, plausible passage that would answer the following question, as if it were an excerpt from a real document. Write confidently and specifically, even if you're not certain of the real answer - this passage is used only to improve document retrieval matching, it is never shown to the user.\n\nQuestion: How does RAG work?\n\nPassage:");
  assert.deepEqual(call[1], []);
  assert.equal(call[2].history_prefix, "");
  assert.equal(call[2].system_prompt,
    "Follow the instructions in the question below exactly. Do not add commentary, explanation, or a preamble - respond with only what was asked for.");
});

test("forwards the optional provider override", async () => {
  const override = { provider: "test", model: "test-model" };
  let options;
  const generator = {
    async generate_answer(_prompt, _chunks, opts) {
      options = opts;
      return generated("passage");
    },
  };

  const [passage, result] = await hyde_document(
    generator, "question", override
  );

  assert.equal(passage, "passage");
  assert.ok(result);
  assert.equal(options.override_provider, override);
});

test("falls back to the original query when generated answer is empty", async () => {
  const result = generated(" \n\t ");
  const generator = { async generate_answer() { return result; } };

  const [passage, raw] = await hyde_document(generator, "original query");

  assert.equal(passage, "original query");
  assert.equal(raw, result);
});

test("falls back to original query and null result on generation exception", async () => {
  const generator = {
    async generate_answer() {
      throw new Error("provider unavailable");
    },
  };

  const [passage, result] = await hyde_document(generator, "original query");

  assert.equal(passage, "original query");
  assert.equal(result, null);
});
