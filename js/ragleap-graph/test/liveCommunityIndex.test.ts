import { test } from "node:test";
import assert from "node:assert/strict";
import { detectCommunities } from "../src/communities.ts";
import type { Edge } from "../src/communities.ts";
import { compositeKey } from "../src/compositeKey.ts";
import { GraphConfig } from "../src/config.ts";
import { GraphIndex } from "../src/graphIndex.ts";
import { neo4jFloat } from "../src/neo4jJson.ts";
import { QueryApiRunner } from "../src/queryApiRunner.ts";

// Runs against a real Neo4j test server in a unique namespace that is removed afterwards. Gated.
const url = process.env.NEO4J_HTTP_URL;
const skip = url ? false : "NEO4J_HTTP_URL not set";

function config(): GraphConfig {
  return new GraphConfig({ baseUrl: url as string, user: process.env.NEO4J_USER ?? "neo4j", password: process.env.NEO4J_PASSWORD ?? "" });
}
function display(lower: string): string {
  return lower.split(" ").map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");
}

test("live: community methods on a seeded graph", { skip }, async () => {
  const ns = "ragleap_js_comm_" + Date.now() + "_" + Math.floor(Math.random() * 1e9);
  const pairs: [string, string][] = [
    ["alpha one", "alpha two"], ["alpha two", "alpha three"], ["alpha one", "alpha three"],
    ["beta one", "beta two"], ["beta two", "beta three"], ["beta one", "beta three"], ["alpha three", "beta one"],
  ];
  const weights = [2.0, 2.0, 2.0, 3.0, 3.0, 3.0, 0.5];
  const direct = new QueryApiRunner(config());
  const idx = await GraphIndex.connect(config(), { logger: { info() {}, warn() {} } });
  try {
    assert.equal(idx.isAvailable(), true);
    for (const name of ["alpha one", "alpha two", "alpha three", "beta one", "beta two", "beta three"]) {
      await direct.run("MERGE (e:Entity {composite_key: $ck}) SET e.name = $n, e.display_name = $d, e.namespace = $ns, e.user_id = ''",
        { ck: compositeKey(ns, "", name), n: name, d: display(name), ns });
    }
    const edges: Edge[] = [];
    for (let i = 0; i < pairs.length; i++) {
      await direct.run("MATCH (a:Entity {name: $a, namespace: $ns}), (b:Entity {name: $b, namespace: $ns}) " +
        "MERGE (a)-[r:CO_OCCURS_WITH {composite_key: $ck}]->(b) SET r.weight = $w, r.namespace = $ns, r.user_id = ''",
        { a: pairs[i][0], b: pairs[i][1], ns, w: neo4jFloat(weights[i]), ck: compositeKey(ns, "", pairs[i][0], pairs[i][1]) });
      edges.push([pairs[i][0], pairs[i][1], weights[i]]);
    }
    const expected = detectCommunities(edges);
    assert.ok(expected.length >= 1);
    assert.deepEqual(await idx.detectCommunities({ namespace: ns, maxEdges: 1000 }), expected);

    const summarizer = (info: { size: number }) => "S " + info.size;
    const first = await idx.refreshCommunities({ namespace: ns, maxEdges: 1000, summarizer });
    assert.equal(first.success, true, String(first.error));
    assert.equal(first.communities, expected.length);
    assert.equal(first.created, expected.length);
    assert.equal(first.kept, 0);
    assert.equal(first.summarized, expected.length);

    const second = await idx.refreshCommunities({ namespace: ns, maxEdges: 1000, summarizer });
    assert.equal(second.success, true, String(second.error));
    assert.equal(second.kept, expected.length);
    assert.equal(second.created, 0);
    assert.equal(second.removed, 0);
    assert.equal(second.summarized, 0);

    const all = await idx.findCommunities(null, { namespace: ns, includeMembers: true });
    assert.equal(all.length, expected.length);
    for (const hit of all) {
      const want = expected.find((c) => compositeKey(ns, "", "community", c.membersHash) === hit.communityId);
      assert.ok(want, "stored community not in the expected set");
      assert.equal(hit.size, want.size);
      assert.equal(hit.internalWeight, want.internalWeight);
      assert.equal(hit.summary, "S " + want.size);
      assert.equal(hit.members?.length, want.size);
    }
    const byEntity = await idx.findCommunities(["Alpha One", "alpha two"], { namespace: ns, limit: 5, includeMembers: true });
    assert.equal(byEntity.length, 1);
    assert.equal(byEntity[0].matchedEntities, 2);
    assert.ok(byEntity[0].members?.includes("Alpha One"));
    assert.equal((await idx.findCommunities(["Alpha One"], { namespace: "other_" + ns })).length, 0);

    const types = await direct.run("MATCH (c:Community {namespace: $ns}) RETURN valueType(c.internal_weight) AS w, valueType(c.size) AS s, valueType(c.resolution) AS r LIMIT 1", { ns });
    console.log("live community index (ts): " + expected.length + " communities, stored types " + JSON.stringify(types[0]));
    assert.match(String(types[0].w), /^FLOAT/);
    assert.match(String(types[0].r), /^FLOAT/);
    assert.match(String(types[0].s), /^INTEGER/);
  } finally {
    await direct.run("MATCH (n) WHERE n.namespace = $ns DETACH DELETE n", { ns });
  }
});
