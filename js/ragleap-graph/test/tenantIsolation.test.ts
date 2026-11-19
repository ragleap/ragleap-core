import { test } from "node:test";
import assert from "node:assert/strict";
import { GraphIndex } from "../src/graphIndex.ts";
import type { CypherRunner, Row } from "../src/queryApiRunner.ts";

// Offline: every tenant filter is in the statement that is sent, and the namespace reaches every statement (recording runner).
interface Call {
  cypher: string;
  params: Record<string, unknown>;
}

class Recorder implements CypherRunner {
  calls: Call[] = [];
  rowsFor: (cypher: string) => Row[];

  constructor(rowsFor: (cypher: string) => Row[] = () => []) {
    this.rowsFor = rowsFor;
  }

  async run(cypher: string, params?: Record<string, unknown> | null): Promise<Row[]> {
    this.calls.push({ cypher: cypher.replace(/\s+/g, " ").trim(), params: { ...(params ?? {}) } });
    return this.rowsFor(cypher).map((r) => ({ ...r }));
  }

  close(): void {}
}

async function make(rowsFor?: (cypher: string) => Row[]) {
  const runner = new Recorder(rowsFor);
  const idx = await GraphIndex.connect(runner, { logger: { info() {}, warn() {}, error() {} } });
  runner.calls.length = 0;
  return { idx, runner };
}

test("documentEntities filters the entity side by namespace and user", async () => {
  const { idx, runner } = await make();
  await idx.documentEntities("doc-1", { namespace: "ns1", userId: "u1" });
  assert.equal(runner.calls.length, 1);
  const call = runner.calls[0];
  assert.ok(call.cypher.includes("MATCH (d)-[:CONTAINS]->(e:Entity) WHERE e.namespace = $namespace AND coalesce(e.user_id, '') = $user_id RETURN e.name"), call.cypher);
  assert.equal(call.params.namespace, "ns1");
  assert.equal(call.params.user_id, "u1");
});

test("searchRelatedEntities checks every node of the path", async () => {
  const { idx, runner } = await make();
  await idx.searchRelatedEntities(["acme corp"], { namespace: "ns1", userId: "u1", maxDepth: 3 });
  assert.equal(runner.calls.length, 1);
  const call = runner.calls[0];
  assert.ok(call.cypher.includes("AND ALL(n IN nodes(path) WHERE n.namespace = $namespace AND coalesce(n.user_id, '') = $user_id) RETURN DISTINCT related.name"), call.cypher);
  assert.ok(call.cypher.includes("[*1..3]"), call.cypher);
  assert.equal(call.params.namespace, "ns1");
  assert.equal(call.params.user_id, "u1");
});

test("findCommunities with members sends the namespace to the member name lookup", async () => {
  const { idx, runner } = await make((cypher) => {
    if (cypher.includes("entity_names")) return [{ community_id: "c1", member_count: 2, internal_weight: 1.5, summary: null, matched_entities: 1 }];
    if (cypher.includes("$community_id")) return [{ entity_name: "Alpha One" }];
    return [];
  });
  const hits = await idx.findCommunities(["alpha one"], { namespace: "ns1", userId: "u1", includeMembers: true });
  assert.deepEqual(hits[0].members, ["Alpha One"]);
  const call = runner.calls.find((c) => c.cypher.includes("composite_key: $community_id"));
  assert.ok(call !== undefined, "no member name call");
  assert.ok(call.cypher.includes("WHERE e.namespace = $namespace AND coalesce(e.user_id, '') = $user_id RETURN e.display_name AS entity_name"), call.cypher);
  assert.equal(call.params.namespace, "ns1");
  assert.equal(call.params.user_id, "u1");
  assert.equal(call.params.community_id, "c1");
});
