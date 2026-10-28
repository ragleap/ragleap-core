import { test } from "node:test";
import assert from "node:assert/strict";
import { compositeKey } from "../src/compositeKey.ts";
import { GraphConfig } from "../src/config.ts";
import { GraphIndex } from "../src/graphIndex.ts";
import { neo4jFloat } from "../src/neo4jJson.ts";
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

test("live: the query methods on a seeded graph", { skip }, async () => {
  const ns = "ragleap_js_q_" + Date.now() + "_" + Math.floor(Math.random() * 1e9);
  const direct = new QueryApiRunner(config());
  const idx = await GraphIndex.connect(config(), { logger: { info() {}, warn() {} } });
  try {
    assert.equal(idx.isAvailable(), true);
    await idx.upsertDocument("cooccur-doc-a", "Test", [{ text: "Acme Corp and Globex Corp are rivals." }], { namespace: ns });
    await idx.upsertDocument("cooccur-doc-b", "Test2", [{ text: "Acme Corp and Globex Corp compete fiercely." }], { namespace: ns });

    const related = await idx.searchRelatedEntities(["Acme Corp"], { namespace: ns, maxDepth: 2, limit: 10 });
    assert.ok(
      related.some((r) => r.entityName === "Globex Corp" && r.depth === 1 && r.relationship === "CO_OCCURS_WITH"),
      JSON.stringify(related),
    );

    const lineage = await idx.findLineage("Acme Corp", "Globex Corp", { namespace: ns });
    assert.equal(lineage.length, 2, JSON.stringify(lineage));
    for (const e of lineage) {
      assert.equal(e.relationType, "CO_OCCURS_WITH");
      assert.equal(e.weight, 1);
    }
    assert.deepStrictEqual(new Set(lineage.map((e) => e.documentId)), new Set(["cooccur-doc-a", "cooccur-doc-b"]));

    assert.equal((await idx.findEntitiesByType("unknown", { namespace: ns })).length, 2);
    assert.equal((await idx.findEntitiesByType("person", { namespace: ns })).length, 0);

    const names = (await idx.documentEntities("cooccur-doc-a", { namespace: ns })).map((e) => e.entityName).sort();
    assert.deepStrictEqual(names, ["Acme Corp", "Globex Corp"]);

    const docs = await idx.findDocumentsByEntities(["Acme Corp"], { namespace: ns });
    assert.deepStrictEqual(docs.map((d) => d.documentId).sort(), ["cooccur-doc-a", "cooccur-doc-b"]);
    for (const d of docs) {
      assert.equal(d.matchedEntities, 1);
      assert.equal(d.graphScore, 1);
      assert.deepStrictEqual(d.matchedEntityNames, ["Acme Corp"]);
    }

    const ck = compositeKey(ns, "", "acme corp", "PARTNERED_WITH", "globex corp");
    await direct.run(
      "MATCH (a:Entity {name: 'acme corp', namespace: $ns}), (b:Entity {name: 'globex corp', namespace: $ns}) " +
        "MERGE (a)-[r:RELATES_AS {composite_key: $ck}]->(b) " +
        "SET r.relation_type = 'PARTNERED_WITH', r.weight = $w, r.namespace = $ns, r.user_id = ''",
      { ns, ck, w: neo4jFloat(2) },
    );
    const out = await idx.findRelations("Acme Corp", { namespace: ns });
    assert.deepStrictEqual(out, [{ subject: "Acme Corp", relationType: "PARTNERED_WITH", object: "Globex Corp", weight: 2 }]);
    assert.deepStrictEqual(await idx.findRelations("Globex Corp", { namespace: ns }), []);
    assert.deepStrictEqual(await idx.findRelations("Globex Corp", { namespace: ns, direction: "incoming" }), out);
    assert.deepStrictEqual(await idx.findRelations("Acme Corp", { namespace: ns, direction: "both" }), out);
    assert.deepStrictEqual(await idx.findRelations("Acme Corp", { namespace: ns, relationType: "OTHER" }), []);
  } finally {
    await direct.run("MATCH (n {namespace: $ns}) DETACH DELETE n", { ns });
    idx.close();
  }
});
