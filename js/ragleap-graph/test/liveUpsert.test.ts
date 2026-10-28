import { test } from "node:test";
import assert from "node:assert/strict";
import { GraphConfig } from "../src/config.ts";
import { GraphIndex } from "../src/graphIndex.ts";
import { QueryApiRunner } from "../src/queryApiRunner.ts";

// Runs against a real Neo4j test server in a unique namespace that is removed afterwards. Gated.
const url = process.env.NEO4J_HTTP_URL;
const skip = url ? false : "NEO4J_HTTP_URL not set";

function config(): GraphConfig {
  return new GraphConfig({
    baseUrl: url as string,
    user: process.env.NEO4J_USER ?? "neo4j",
    password: process.env.NEO4J_PASSWORD ?? "",
  });
}

test("live: upsert writes, is idempotent and drops old edges when the content changes", { skip }, async () => {
  const ns = "ragleap_js_" + Date.now() + "_" + Math.floor(Math.random() * 1e9);
  const direct = new QueryApiRunner(config());
  const idx = await GraphIndex.connect(config(), { logger: { info() {}, warn() {} } });
  const edges = "MATCH (:Entity {namespace: $ns})-[r:CO_OCCURS_WITH]-(:Entity {namespace: $ns}) RETURN count(DISTINCT r) AS n, collect(DISTINCT r.weight) AS w";
  try {
    assert.equal(idx.isAvailable(), true);
    const chunks = [{ text: "Acme Corp and Globex Industries." }];
    const first = await idx.upsertDocument("doc-live", "Live", chunks, { namespace: ns });
    assert.deepStrictEqual({ ...first }, {
      success: true, documentId: "doc-live", entitiesIndexed: 2, relationshipsIndexed: 1, relationsIndexed: 0, error: null,
    });
    assert.deepStrictEqual(await direct.run(edges, { ns }), [{ n: 1, w: [1] }]);

    const again = await idx.upsertDocument("doc-live", "Live", chunks, { namespace: ns });
    assert.equal(again.success, true);
    assert.deepStrictEqual(await direct.run(edges, { ns }), [{ n: 1, w: [1] }]);
    const contains = "MATCH (:Document {id: $id, namespace: $ns})-[r:CONTAINS]->(:Entity) RETURN count(r) AS n";
    assert.deepStrictEqual(await direct.run(contains, { id: "doc-live", ns }), [{ n: 2 }]);

    const changed = await idx.upsertDocument("doc-live", "Live", [{ text: "Initech Systems and Hooli Group." }], { namespace: ns });
    assert.equal(changed.success, true);
    const names = "MATCH (:Document {id: $id, namespace: $ns})-[:CONTAINS]->(e:Entity) RETURN e.display_name AS d ORDER BY d";
    assert.deepStrictEqual(await direct.run(names, { id: "doc-live", ns }), [{ d: "Hooli Group" }, { d: "Initech Systems" }]);
    assert.deepStrictEqual(
      await direct.run("MATCH (:Entity {namespace: $ns, name: 'acme corp'})-[r:CO_OCCURS_WITH]-() RETURN count(r) AS n", { ns }),
      [{ n: 0 }],
    );
    assert.deepStrictEqual(await direct.run(edges, { ns }), [{ n: 1, w: [1] }]);
  } finally {
    await direct.run("MATCH (n {namespace: $ns}) DETACH DELETE n", { ns });
    idx.close();
  }
});
