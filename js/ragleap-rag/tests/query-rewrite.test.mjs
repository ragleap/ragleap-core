import test from "node:test";
import assert from "node:assert/strict";

import { reciprocal_rank_fusion } from "../dist/query_rewrite.js";

const chunk = (id, extra = {}) => ({
  chunk_id: id,
  text: `text-${id}`,
  document_id: "doc-1",
  chunk_index: 0,
  ...extra,
});

test("RRF combines scores using zero-based ranks and default k=60", () => {
  const a = chunk("a");
  const b = chunk("b");
  const c = chunk("c");

  const result = reciprocal_rank_fusion([[a, b], [c, a]]);

  assert.deepEqual(result, [a, c, b]);
  assert.ok(result[0] === a);
});

test("RRF keeps the first object for duplicate chunk IDs", () => {
  const first = chunk("same", { text: "first" });
  const later = chunk("same", { text: "later" });

  const result = reciprocal_rank_fusion([[first], [later]]);

  assert.equal(result.length, 1);
  assert.equal(result[0], first);
  assert.equal(result[0].text, "first");
});

test("RRF falls back to document ID and chunk index", () => {
  const first = { document_id: "d1", chunk_index: 2, text: "first" };
  const duplicate = { document_id: "d1", chunk_index: 2, text: "duplicate" };
  const other = { document_id: "d1", chunk_index: 3, text: "other" };

  const result = reciprocal_rank_fusion([[first, other], [duplicate]]);

  assert.equal(result.length, 2);
  assert.equal(result[0], first);
  assert.ok(result.includes(other));
});

test("RRF preserves encounter order when scores tie", () => {
  const a = chunk("a");
  const b = chunk("b");

  assert.deepEqual(reciprocal_rank_fusion([[a, b]]), [a, b]);
});

test("RRF supports a custom k and empty ranked lists", () => {
  const a = chunk("a");
  const b = chunk("b");

  assert.deepEqual(reciprocal_rank_fusion([]), []);
  assert.deepEqual(reciprocal_rank_fusion([[], []]), []);
  assert.deepEqual(reciprocal_rank_fusion([[a], [b]], 0), [a, b]);
});
