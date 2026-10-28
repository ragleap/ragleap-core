import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_RERANK_MODEL_REPO,
  RerankerService,
} from "../dist/reranking.js";

test("uses the Python-compatible default model repository", () => {
  const service = new RerankerService();

  assert.equal(
    service.model_repo,
    "Xenova/ms-marco-MiniLM-L-6-v2",
  );
  assert.equal(service.model_repo, DEFAULT_RERANK_MODEL_REPO);
});

test("empty input returns the same array without loading a model", async () => {
  const service = new RerankerService({
    score_pair: () => {
      throw new Error("scorer should not run");
    },
  });
  const chunks = [];

  assert.strictEqual(await service.rerank("query", chunks, 5), chunks);
});

test("scores chunks, mutates rerank_score, and sorts descending", async () => {
  const scores = new Map([
    ["low", 0.123456],
    ["high", 0.987654],
    ["middle", 0.555555],
  ]);
  const service = new RerankerService({
    score_pair: (_query, text) => scores.get(text),
  });
  const chunks = [
    { text: "low", original: true },
    { text: "high" },
    { text: "middle" },
  ];

  const result = await service.rerank("query", chunks, 3);

  assert.deepEqual(
    result.map((chunk) => chunk.text),
    ["high", "middle", "low"],
  );
  assert.deepEqual(
    chunks.map((chunk) => chunk.rerank_score),
    [0.1235, 0.9877, 0.5556],
  );
  assert.equal(result[0].original, undefined);
  assert.equal(chunks[0].original, true);
});

test("top_k limits returned chunks after scoring all candidates", async () => {
  const visited = [];
  const service = new RerankerService({
    score_pair: (_query, text) => {
      visited.push(text);
      return { a: 1, b: 3, c: 2 }[text];
    },
  });
  const chunks = [{ text: "a" }, { text: "b" }, { text: "c" }];

  const result = await service.rerank("query", chunks, 2);

  assert.deepEqual(visited, ["a", "b", "c"]);
  assert.deepEqual(result.map((chunk) => chunk.text), ["b", "c"]);
  assert.equal(result.length, 2);
});

test("zero top_k returns no chunks but still scores candidates", async () => {
  let calls = 0;
  const service = new RerankerService({
    score_pair: () => {
      calls += 1;
      return 0.5;
    },
  });

  assert.deepEqual(
    await service.rerank("query", [{ text: "a" }], 0),
    [],
  );
  assert.equal(calls, 1);
});

test("rejects non-finite scores", async () => {
  const service = new RerankerService({
    score_pair: () => Number.NaN,
  });

  await assert.rejects(
    service.rerank("query", [{ text: "a" }], 1),
    /finite number/,
  );
});

test("propagates scorer errors", async () => {
  const service = new RerankerService({
    score_pair: () => {
      throw new Error("inference failed");
    },
  });

  await assert.rejects(
    service.rerank("query", [{ text: "a" }], 1),
    /inference failed/,
  );
});
