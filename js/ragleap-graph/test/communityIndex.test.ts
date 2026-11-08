import { test } from "node:test";
import assert from "node:assert/strict";
import { detectCommunities, membersHash } from "../src/communities.ts";
import { compositeKey } from "../src/compositeKey.ts";
import { GraphIndex } from "../src/graphIndex.ts";
import { Neo4jFloat, encodeNeo4jJson } from "../src/neo4jJson.ts";
import type { CypherRunner, Row } from "../src/queryApiRunner.ts";

const silent = { info() {}, warn() {}, error() {} };
const TRIANGLE: Row[] = [
  { a: "a1", b: "a2", weight: 1.0 },
  { a: "a2", b: "a3", weight: 1.0 },
  { a: "a1", b: "a3", weight: 1.0 },
];

class Scripted implements CypherRunner {
  armed = false;
  names: string[] = [];
  params: Record<string, unknown>[] = [];
  edges: Row[];
  existing: Row[];

  constructor(edges: Row[], existing: Row[] = []) {
    this.edges = edges;
    this.existing = existing;
  }

  async run(cypher: string, params?: Record<string, unknown> | null): Promise<Row[]> {
    if (!this.armed) return [];
    const n = cypher.replace(/\s+/g, " ");
    let name = "other";
    let out: Row[] = [];
    if (n.includes("[r:CO_OCCURS_WITH]")) {
      name = "edges";
      out = this.edges;
    } else if (n.includes("AS has_summary")) {
      name = "existing";
      out = this.existing;
    } else if (n.includes("MERGE (c:Community")) name = "merge";
    else if (n.includes("DETACH DELETE")) name = "delete";
    else if (n.includes("e.display_name AS display_name")) {
      name = "display";
      out = [{ name: "a1", display_name: "A1" }];
    } else if (n.includes("SET c.summary")) name = "set_summary";
    else if (n.includes("count(DISTINCT e)")) name = "by_entity";
    this.names.push(name);
    this.params.push({ ...(params ?? {}) });
    return out.map((r) => ({ ...r }));
  }

  close(): void {}
}

async function indexOf(runner: Scripted): Promise<GraphIndex> {
  const g = await GraphIndex.connect(runner, { logger: silent });
  runner.armed = true;
  return g;
}
function paramsOf(r: Scripted, name: string): Record<string, unknown> {
  const i = r.names.indexOf(name);
  assert.ok(i >= 0, `no ${name} statement`);
  return r.params[i];
}

test("an unavailable index returns empty results and reports the error", async () => {
  const g = await GraphIndex.connect(null, { logger: silent });
  assert.deepEqual(await g.detectCommunities(), []);
  const r = await g.refreshCommunities();
  assert.equal(r.success, false);
  assert.equal(r.error, "Neo4j driver not available");
  assert.deepEqual(await g.findCommunities(null), []);
});

test("arguments are validated before availability", async () => {
  const g = await GraphIndex.connect(null, { logger: silent });
  await assert.rejects(g.detectCommunities({ resolution: 0 }), (e: Error) => e instanceof RangeError && e.message.startsWith("resolution must be"));
  await assert.rejects(g.detectCommunities({ maxEdges: 0 }), { name: "RangeError", message: "max_edges must be a positive integer, got 0" });
  await assert.rejects(g.refreshCommunities({ maxSummaryItems: 0 }), { name: "RangeError", message: "max_summary_items must be a positive integer, got 0" });
});

test("the edge limit is maxEdges + 1 and too many edges reject", async () => {
  const r = new Scripted(TRIANGLE);
  const g = await indexOf(r);
  await assert.rejects(g.detectCommunities({ maxEdges: 2 }), (e: Error) => e.message.startsWith("more than max_edges=2 edges of one kind"));
  r.names.length = 0;
  r.params.length = 0;
  assert.equal((await g.detectCommunities({ maxEdges: 3 })).length, 1);
  assert.equal(paramsOf(r, "edges").limit, 4);
});

test("refresh keeps unchanged, removes vanished and sends float types", async () => {
  const hash = membersHash(["a1", "a2", "a3"]);
  const r = new Scripted(TRIANGLE, [
    { key: compositeKey("", "", "community", hash), has_summary: true },
    { key: "stale", has_summary: false },
  ]);
  const res = await (await indexOf(r)).refreshCommunities();
  assert.deepEqual(res, { success: true, communities: 1, created: 0, kept: 1, removed: 1, summarized: 0, summaryErrors: 0, error: null });
  assert.equal(paramsOf(r, "delete").composite_key, "stale");
  const merge = paramsOf(r, "merge");
  assert.equal(merge.size, 3);
  assert.ok(merge.internal_weight instanceof Neo4jFloat && merge.resolution instanceof Neo4jFloat);
  assert.equal(JSON.parse(encodeNeo4jJson(merge.internal_weight)), 3);
});

test("a blank summary is counted and a good one is stripped and stored", async () => {
  const bad = new Scripted(TRIANGLE);
  const rb = await (await indexOf(bad)).refreshCommunities({ summarizer: () => "  " });
  assert.equal(rb.success, true);
  assert.equal(rb.summaryErrors, 1);
  assert.equal(rb.summarized, 0);
  assert.ok(!bad.names.includes("set_summary"));
  const ok = new Scripted(TRIANGLE);
  const ro = await (await indexOf(ok)).refreshCommunities({ summarizer: () => "  hello  " });
  assert.equal(ro.summarized, 1);
  assert.equal(paramsOf(ok, "set_summary").summary, "hello");
});

test("the summary is cut by code points and a failing summarizer never rejects", async () => {
  const r = new Scripted(TRIANGLE);
  await (await indexOf(r)).refreshCommunities({ summarizer: () => "\u{1F600}".repeat(20001) });
  assert.equal(Array.from(paramsOf(r, "set_summary").summary as string).length, 20000);
  const r2 = new Scripted(TRIANGLE);
  const res = await (await indexOf(r2)).refreshCommunities({
    summarizer: async () => {
      throw new Error("nope");
    },
  });
  assert.equal(res.success, true);
  assert.equal(res.summaryErrors, 1);
});

test("a lookup with only blank names does not query", async () => {
  const r = new Scripted([]);
  const g = await indexOf(r);
  assert.deepEqual(await g.findCommunities(["  ", "ab"]), []);
  assert.deepEqual(await g.findCommunities([]), []);
  assert.deepEqual(r.names, []);
});

test("a lookup by names normalizes them and uses the entity query", async () => {
  const r = new Scripted([]);
  const g = await indexOf(r);
  await g.findCommunities(["Helio Grid", "HELIO GRID"], { namespace: "ns1", limit: 0 });
  assert.deepEqual(r.names, ["by_entity"]);
  assert.deepEqual(r.params[0].entity_names, ["helio grid"]);
  assert.equal(r.params[0].limit, 1);
  assert.equal(r.params[0].namespace, "ns1");
});

test("detection groups the stored edges like the pure function", async () => {
  const g = await indexOf(new Scripted(TRIANGLE));
  assert.deepEqual(await g.detectCommunities(), detectCommunities([["a1", "a2", 1], ["a2", "a3", 1], ["a1", "a3", 1]]));
});
