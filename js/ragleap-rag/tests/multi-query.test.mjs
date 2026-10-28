import test from "node:test";
import assert from "node:assert/strict";

import { multi_query_variants } from "../dist/query_rewrite.js";

const generated = (answer) => ({
  answer,
  sources: [],
  citations: [],
  provider_used: "test",
  model_used: "test-model",
  usage: null,
  chunks_sent: 0,
});

test("includes original query first and removes blank, duplicate, and marked lines", async () => {
  const query = "How does RAG work?";
  let call;

  const generator = {
    async generate_answer(...args) {
      call = args;
      return generated(
        "How does RAG work?\n" +
        "- How does retrieval augmented generation work?\n" +
        "* Explain RAG systems\n" +
        "   \n" +
        "HOW DOES RAG WORK?\n"
      );
    },
  };

  const [variants, result] = await multi_query_variants(generator, query);

  assert.deepEqual(variants, [
    query,
    "How does retrieval augmented generation work?",
    "Explain RAG systems",
  ]);
  assert.equal(result.answer.includes("Explain RAG systems"), true);
  assert.equal(call[0], [
    "Generate 3 different phrasings of the following question, capturing different angles or wordings a user might use to ask about the same underlying information need. Respond with ONLY the alternative questions, one per line, no numbering or extra text.",
    "",
    "Original question: How does RAG work?",
    "",
    "Alternative phrasings:",
  ].join("\n"));
  assert.deepEqual(call[1], []);
  assert.equal(call[2].history_prefix, "");
  assert.equal(
    call[2].system_prompt,
    "Follow the instructions in the question below exactly. Do not add commentary, explanation, or a preamble - respond with only what was asked for."
  );
});

test("respects n as the maximum total variant count", async () => {
  const generator = {
    async generate_answer() {
      return generated("alternative one\nalternative two\nalternative three");
    },
  };

  const [variants] = await multi_query_variants(generator, "original", 2);
  assert.deepEqual(variants, ["original", "alternative one"]);
});

test("returns only the original query when generation throws", async () => {
  const generator = {
    async generate_answer() {
      throw new Error("provider unavailable");
    },
  };

  const [variants, result] = await multi_query_variants(generator, "original");
  assert.deepEqual(variants, ["original"]);
  assert.equal(result, null);
});

test("handles empty generation output and zero variants", async () => {
  const generator = {
    async generate_answer() {
      return generated("");
    },
  };

  const [variants, result] = await multi_query_variants(generator, "original");
  assert.deepEqual(variants, ["original"]);
  assert.ok(result);

  const [none] = await multi_query_variants(generator, "original", 0);
  assert.deepEqual(none, []);
});
