import { test } from "node:test";
import assert from "node:assert/strict";
import { compositeKey } from "../src/compositeKey.ts";
import { detectCommunities, membersHash, modularity } from "../src/communities.ts";
import type { Edge } from "../src/communities.ts";

function tri(p: string): Edge[] {
  return [[p + "1", p + "2", 1], [p + "2", p + "3", 1], [p + "1", p + "3", 1]];
}
function twoTrianglesBridge(): Edge[] {
  return [...tri("a"), ...tri("b"), ["a3", "b1", 1]];
}

test("no edges and only invalid edges give no communities", () => {
  assert.deepEqual(detectCommunities([]), []);
  const bad: Edge[] = [["s", "s", 1], ["a", "b", 0], ["a", "c", -1], ["a", "d", NaN], ["a", "f", Infinity],
    ["a", "g", -Infinity], ["a", "h", true], ["a", "i", "2"], ["a", "j", null]];
  assert.deepEqual(detectCommunities(bad, 1.0, 1), []);
});

test("invalid edges are ignored", () => {
  const withBad: Edge[] = [...twoTrianglesBridge(), ["s", "s", 1], ["a1", "z", 0], ["a1", "y", NaN], ["a1", "x", true]];
  assert.deepEqual(detectCommunities(withBad, 1.0, 1), detectCommunities(twoTrianglesBridge(), 1.0, 1));
});

test("edge order and direction do not matter", () => {
  const base = twoTrianglesBridge();
  const mixed: Edge[] = base.map(([a, b, w]) => [b, a, w] as Edge).reverse();
  assert.deepEqual(detectCommunities(mixed, 1.0, 1), detectCommunities(base, 1.0, 1));
});

test("duplicate edges are summed", () => {
  assert.deepEqual(detectCommunities([["a", "b", 1], ["b", "a", 2]], 1.0, 1), detectCommunities([["a", "b", 3]], 1.0, 1));
  assert.equal(detectCommunities([["a", "b", 1], ["b", "a", 2]], 1.0, 1)[0].internalWeight, 3);
});

test("disconnected parts are never merged", () => {
  for (const c of detectCommunities([...tri("a"), ...tri("b")], 1.0, 1)) {
    assert.equal(new Set(c.members.map((m) => m[0])).size, 1, `community mixes both triangles: ${c.members}`);
  }
});

test("members are sorted by code point, not UTF-16 unit", () => {
  const bmp = "\uffff";
  const astral = "\u{10000}";
  const flat = detectCommunities([[astral, bmp, 1]], 1.0, 1).flatMap((c) => c.members);
  assert.deepEqual(flat, [bmp, astral]);
});

test("min size filters and size matches members", () => {
  for (const c of detectCommunities([...twoTrianglesBridge(), ["x", "y", 1]], 1.0, 3)) {
    assert.ok(c.size >= 3);
    assert.equal(c.members.length, c.size);
  }
});

test("members hash matches the composite key of the members", () => {
  assert.equal(membersHash(["a", "b"]), compositeKey("a", "b"));
  for (const c of detectCommunities(twoTrianglesBridge(), 1.0, 1)) {
    assert.equal(c.membersHash, membersHash(c.members));
  }
});

test("invalid arguments are rejected, resolution first", () => {
  const g = twoTrianglesBridge();
  for (const bad of [0, -1, NaN, Infinity, true, "1", null]) {
    assert.throws(() => detectCommunities(g, bad as unknown as number, 2), (e: Error) => e instanceof RangeError && e.message.startsWith("resolution must be"));
  }
  assert.throws(() => detectCommunities(g, 1.0, 0), (e: Error) => e.message.startsWith("min_size must be"));
  assert.throws(() => detectCommunities(g, 1.0, 1.5), (e: Error) => e.message.startsWith("min_size must be"));
  assert.throws(() => detectCommunities(g, 0, 0), (e: Error) => e.message.startsWith("resolution must be"));
});

test("modularity basics", () => {
  const g = twoTrianglesBridge();
  const all = ["a1", "a2", "a3", "b1", "b2", "b3"];
  assert.ok(Math.abs(modularity(g, [all], 1.0)) < 1e-12);
  assert.equal(modularity([], [], 1.0), 0);
  assert.throws(() => modularity(g, [["a1", "a2", "a3"]], 1.0), RangeError);
});
