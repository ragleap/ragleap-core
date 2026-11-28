import { test } from "node:test";
import assert from "node:assert/strict";
import { compositeKey } from "../src/compositeKey.ts";
import { GraphIndex } from "../src/graphIndex.ts";
import { QueryApiRunner } from "../src/queryApiRunner.ts";
import { RECOMPUTE_RELATES_AS_TEMPORAL } from "../src/upsert.ts";
import { config, runId, skip, wipe } from "./isolationSupport.ts";

// Temporal relation edges against a real Neo4j (own namespaces, removed afterwards). Mirrors the Java live test: the edge is
// opened, closed (never deleted), reopened, asked about at several instants, and a phantom edge is closed at birth. Also the
// real upsert path (a temporal index closes, a plain index deletes) and the community methods. Gated by NEO4J_HTTP_URL.
const silent = { info() {}, warn() {}, error() {} };
const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

async function open(temporal: boolean): Promise<{ direct: QueryApiRunner; idx: GraphIndex }> {
  const direct = new QueryApiRunner(config());
  const idx = await GraphIndex.connect(config(), { logger: silent, temporal });
  assert.equal(idx.isAvailable(), true);
  assert.equal(idx.temporal, temporal);
  return { direct, idx };
}

async function entities(direct: QueryApiRunner, ns: string, names: Record<string, string>): Promise<void> {
  for (const [name, display] of Object.entries(names)) {
    await direct.run("CREATE (:Entity {composite_key: $k, name: $n, display_name: $d, namespace: $ns, user_id: ''})", { k: ns + "|" + name, n: name, d: display, ns });
  }
}

async function edgeRow(direct: QueryApiRunner, key: string): Promise<{ vf: string | null; vt: string | null; w: number }> {
  const rows = await direct.run("MATCH ()-[r:RELATES_AS {composite_key: $k}]->() RETURN toString(r.valid_from) AS vf, toString(r.valid_to) AS vt, r.weight AS w", { k: key });
  assert.equal(rows.length, 1, "exactly one edge " + key);
  return { vf: (rows[0].vf as string | null) ?? null, vt: (rows[0].vt as string | null) ?? null, w: Number(rows[0].w) };
}

test("live: temporal edge opens, closes, reopens and answers asOf questions", { skip }, async () => {
  const ns = runId("ragleap_js_temporal_");
  const ck = "edge-" + ns;
  const ck2 = "edge2-" + ns;
  const { direct, idx } = await open(true);
  try {
    await wipe(direct, ns);
    await entities(direct, ns, { "acme corp": "Acme Corp", "globex corp": "Globex Corp" });
    const supportOn = (): Promise<unknown> =>
      direct.run("CREATE (:RelationWeight {composite_key: $k, namespace: $ns, document_id: 'd1', subject: 'acme corp', relation_type: 'PARTNERED_WITH', object: 'globex corp', user_id: '', weight: 2.0})", { k: "rw-" + ns, ns });
    const supportOff = (): Promise<unknown> => direct.run("MATCH (rw:RelationWeight {namespace: $ns}) DELETE rw", { ns });
    const refresh = (key: string, type: string): Promise<unknown> =>
      direct.run(RECOMPUTE_RELATES_AS_TEMPORAL, { namespace: ns, subject: "acme corp", relation_type: type, object: "globex corp", user_id: "", relates_as_composite_key: key });
    const found = async (at: string | null, type: string | null = null): Promise<number> => (await idx.findRelations("Acme Corp", { namespace: ns, relationType: type, direction: "outgoing", asOf: at })).length;

    await supportOn();
    await refresh(ck, "PARTNERED_WITH");
    let e = await edgeRow(direct, ck);
    assert.ok(e.vf !== null);
    assert.equal(e.vt, null);
    assert.equal(e.w, 2);
    assert.equal(await found(null), 1);
    const firstFrom = e.vf as string;

    await sleep(80);
    await supportOff();
    await refresh(ck, "PARTNERED_WITH");
    e = await edgeRow(direct, ck);
    assert.equal(e.vf, firstFrom, "closing keeps valid_from");
    assert.ok(e.vt !== null, "closing sets valid_to");
    const vf = e.vf as string;
    const vt = e.vt as string;
    assert.ok(Date.parse(vt) > Date.parse(vf));
    assert.equal(await found(null), 0, "a closed edge is left out by default");
    assert.equal(await found(new Date((Date.parse(vf) + Date.parse(vt)) / 2).toISOString()), 1, "valid in between");
    assert.equal(await found(vt), 0, "valid_to itself is already closed");
    assert.equal(await found(vf), 1, "valid_from itself is valid");
    assert.equal(await found(new Date(Date.parse(vf) - 1000).toISOString()), 0, "before the edge existed");
    await refresh(ck, "PARTNERED_WITH");
    assert.equal((await edgeRow(direct, ck)).vt, vt, "closing twice keeps the first valid_to");

    await sleep(80);
    await supportOn();
    await refresh(ck, "PARTNERED_WITH");
    e = await edgeRow(direct, ck);
    assert.equal(e.vt, null, "reopened");
    assert.ok(Date.parse(e.vf as string) >= Date.parse(vt), "reopening restarts valid_from");
    assert.equal(await found(null), 1);
    assert.equal(await found(new Date((Date.parse(vf) + Date.parse(vt)) / 2).toISOString()), 0, "the old interval is a gap now");

    await supportOff();
    await refresh(ck2, "OTHER");
    e = await edgeRow(direct, ck2);
    assert.equal(e.vf, e.vt, "a phantom edge is closed at birth");
    assert.equal(e.w, 0);
    assert.equal(await found(null, "OTHER"), 0);
  } finally {
    await wipe(direct, ns);
    idx.close();
    direct.close();
  }
});

test("live: upsert closes the edge on a temporal index and deletes it on a plain one", { skip }, async () => {
  const run = runId("ragleap_js_temporal_up_");
  for (const temporal of [true, false]) {
    const ns = run + (temporal ? "_t" : "_p");
    const { direct, idx } = await open(temporal);
    try {
      await wipe(direct, ns);
      await entities(direct, ns, { "acme corp": "Acme Corp", "globex corp": "Globex Corp" });
      const key = compositeKey(ns, "", "acme corp", "PARTNERED_WITH", "globex corp");
      await direct.run("CREATE (:RelationWeight {composite_key: $k, namespace: $ns, document_id: 'd1', subject: 'acme corp', relation_type: 'PARTNERED_WITH', object: 'globex corp', user_id: '', weight: 2.0})", { k: "rw-" + ns, ns });
      await direct.run(RECOMPUTE_RELATES_AS_TEMPORAL, { namespace: ns, subject: "acme corp", relation_type: "PARTNERED_WITH", object: "globex corp", user_id: "", relates_as_composite_key: key });
      assert.equal((await edgeRow(direct, key)).vt, null, "the edge starts open");
      // Re-indexing the document drops its old relation support (relation extraction is not ported, so nothing replaces it).
      const summary = await idx.upsertDocument("d1", "Acme news", [{ text: "Zeta Corp and Omega Corp are here." }], { namespace: ns });
      assert.equal(summary.success, true, String(summary.error));
      const rows = await direct.run("MATCH ()-[r:RELATES_AS {composite_key: $k}]->() RETURN toString(r.valid_to) AS vt", { k: key });
      const edges = await direct.run("MATCH (:Entity {namespace: $ns})-[r:RELATES_AS]->() RETURN count(r) AS c", { ns });
      assert.equal(Number(edges[0].c), temporal ? 1 : 0, "no second edge may appear");
      if (temporal) {
        assert.equal(rows.length, 1, "a temporal index keeps the edge");
        assert.ok(rows[0].vt !== null && rows[0].vt !== undefined, "and closes it");
        assert.equal((await idx.findRelations("Acme Corp", { namespace: ns })).length, 0);
        assert.equal((await idx.findRelations("Acme Corp", { namespace: ns, asOf: new Date(Date.now() - 3_600_000) })).length, 0, "an hour ago it did not exist yet");
      } else {
        assert.equal(rows.length, 0, "a plain index deletes the edge");
      }
    } finally {
      await wipe(direct, ns);
      idx.close();
      direct.close();
    }
  }
});

test("live: communities use open relation edges only on a temporal index", { skip }, async () => {
  const run = runId("ragleap_js_temporal_c_");
  const ns = run + "_ns";
  const temporalSide = await open(true);
  const plainSide = await open(false);
  const { direct } = temporalSide;
  try {
    await wipe(direct, ns);
    await entities(direct, ns, { x1: "X1", x2: "X2", y1: "Y1", y2: "Y2" });
    const edge = (key: string, a: string, b: string, closed: boolean): Promise<unknown> =>
      direct.run(
        "MATCH (a:Entity {name: $a, namespace: $ns}), (b:Entity {name: $b, namespace: $ns}) MERGE (a)-[r:RELATES_AS {composite_key: $k}]->(b) " +
          "SET r.relation_type = 'T', r.weight = 1.0, r.namespace = $ns, r.user_id = '', r.valid_from = datetime(), r.valid_to = CASE WHEN $closed THEN datetime() ELSE null END",
        { k: key + ns, a, b, ns, closed },
      );
    await edge("closed-", "x1", "x2", true);
    await edge("open-", "y1", "y2", false);
    const names = (cs: Array<{ members: string[] }>): string[] => cs.map((c) => c.members.join("+")).sort();
    assert.deepEqual(names(await temporalSide.idx.detectCommunities({ namespace: ns, includeRelations: true })), ["y1+y2"]);
    assert.deepEqual(names(await plainSide.idx.detectCommunities({ namespace: ns, includeRelations: true })), ["x1+x2", "y1+y2"]);
    assert.deepEqual(names(await temporalSide.idx.detectCommunities({ namespace: ns })), [], "relations are off by default");
  } finally {
    await wipe(direct, ns);
    temporalSide.idx.close();
    plainSide.idx.close();
    temporalSide.direct.close();
    plainSide.direct.close();
  }
});
