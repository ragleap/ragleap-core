import assert from "node:assert/strict";
import { GraphConfig } from "../src/config.ts";
import { GraphIndex } from "../src/graphIndex.ts";
import { QueryApiRunner } from "../src/queryApiRunner.ts";
import type { Summarizer } from "../src/communityStore.ts";

// Shared helpers of the tenant and user isolation tests (not a test file). Real Neo4j, own namespaces, removed afterwards.
export const url = process.env.NEO4J_HTTP_URL;
export const skip = url ? false : "NEO4J_HTTP_URL not set";
export const FOREIGN = ["leaked", "umbrella", "gamma", "bait", "sued_by"];
export const TEXT = "Acme Corp and Globex Corp are rivals. Acme Corp works with Initech.";
export const EXTRA = "Umbrella Corp and Acme Corp are partners. Umbrella Corp works with Initech.";
export const MUST = ["documentEntities", "findEntitiesByType", "findLineage", "searchRelated1", "searchRelated3",
  "findDocumentsByEntities", "relations_outgoing_Acme Corp", "relations_incoming_Globex Corp"];

export function config(): GraphConfig {
  return new GraphConfig({ baseUrl: url as string, user: process.env.NEO4J_USER ?? "neo4j", password: process.env.NEO4J_PASSWORD ?? "" });
}
export function runId(prefix: string): string {
  return prefix + Date.now().toString(36) + Math.floor(Math.random() * 1e9).toString(36);
}
export async function open(): Promise<{ direct: QueryApiRunner; idx: GraphIndex }> {
  const direct = new QueryApiRunner(config());
  const idx = await GraphIndex.connect(config(), { logger: { info() {}, warn() {} } });
  assert.equal(idx.isAvailable(), true);
  return { direct, idx };
}
export function close(direct: QueryApiRunner, idx: GraphIndex): void {
  idx.close();
  direct.close();
}
export async function wipe(direct: QueryApiRunner, prefix: string): Promise<void> {
  await direct.run("MATCH (n) WHERE n.namespace STARTS WITH $p DETACH DELETE n", { p: prefix });
}
export function norm(rows: readonly unknown[]): string {
  return rows.map((r) => JSON.stringify(r).replace(/[0-9a-f]{64}/g, "<key>")).sort().join("\n");
}
export async function countOf(direct: QueryApiRunner, cypher: string, params: Record<string, unknown>): Promise<number> {
  const rows = await direct.run(cypher, params);
  return Number(rows[0].c);
}
export function foreignWords(text: string): string[] {
  const low = text.toLowerCase();
  return FOREIGN.filter((w) => low.includes(w));
}

// One tenant: a real upsert plus hand-written nodes and edges (zeta, an optional gamma, one RELATES_AS edge, one RelationWeight).
export async function seedTenant(direct: QueryApiRunner, idx: GraphIndex, ns: string, uid: string, extra: boolean): Promise<void> {
  await idx.upsertDocument("doc-1", "Acme news", [{ text: TEXT }], { namespace: ns, userId: uid });
  if (extra) await idx.upsertDocument("doc-2", "Umbrella news", [{ text: EXTRA }], { namespace: ns, userId: uid });
  const type = extra ? "SUED_BY" : "PARTNERED_WITH";
  const p = { ns, uid, k: ns + "|" + uid, t: type };
  await direct.run("CREATE (:Entity {composite_key: $k + '|zeta', name: 'zeta corp', display_name: 'Zeta Corp', namespace: $ns, user_id: $uid, entity_type: 'unknown'})", p);
  if (extra) {
    await direct.run("CREATE (:Entity {composite_key: $k + '|gamma', name: 'gamma corp', display_name: 'Gamma Corp', namespace: $ns, user_id: $uid, entity_type: 'unknown'})", p);
  }
  await direct.run("MATCH (a:Entity {name: 'acme corp', namespace: $ns, user_id: $uid}), (b:Entity {name: 'globex corp', namespace: $ns, user_id: $uid}) " +
    "MERGE (a)-[r:RELATES_AS {composite_key: $k + '|rel|' + $t}]->(b) SET r.relation_type = $t, r.weight = 2.0, r.namespace = $ns, r.user_id = $uid", p);
  await direct.run("CREATE (:RelationWeight {composite_key: $k + '|rw|' + $t, namespace: $ns, user_id: $uid, subject: 'acme corp', object: 'globex corp', " +
    "relation_type: $t, document_id: 'doc-1', weight: 2.0})", p);
}

// Tenants in different namespaces: edges and documents of tenant a and b point at each other.
export async function namespaceBait(direct: QueryApiRunner, a: string, b: string): Promise<void> {
  const p = { a, b };
  await direct.run("MATCH (y:Entity {namespace: $b}) SET y.display_name = 'LEAKED ' + y.name", p);
  await direct.run("MATCH (d:Document {id: 'doc-1', namespace: $a}) MATCH (y:Entity {namespace: $b}) MERGE (d)-[:CONTAINS]->(y)", p);
  await direct.run("MATCH (d:Document {namespace: $b}) MATCH (x:Entity {namespace: $a}) MERGE (d)-[:CONTAINS]->(x)", p);
  await direct.run("MATCH (x:Entity {name: 'acme corp', namespace: $a}) MATCH (g:Entity {name: 'gamma corp', namespace: $b}) " +
    "MATCH (z:Entity {name: 'zeta corp', namespace: $a}) MERGE (x)-[:BAIT_LINK]->(g) MERGE (g)-[:BAIT_LINK]->(z)", p);
  await direct.run("MATCH (x:Entity {namespace: $a}) MATCH (y:Entity {namespace: $b}) " +
    "MERGE (x)-[r:RELATES_AS {composite_key: 'bo|' + x.composite_key + '|' + y.composite_key}]->(y) " +
    "SET r.relation_type = 'BAIT', r.weight = 1.0, r.namespace = $a, r.user_id = ''", p);
  await direct.run("MATCH (x:Entity {namespace: $a}) MATCH (y:Entity {namespace: $b}) " +
    "MERGE (x)-[r:CO_OCCURS_WITH {composite_key: 'bc|' + x.composite_key + '|' + y.composite_key}]->(y) SET r.weight = 1.0, r.namespace = $a, r.user_id = ''", p);
  assert.ok(await countOf(direct, "MATCH (:Document {namespace: $b})-[r:CONTAINS]->(:Entity {namespace: $a}) RETURN count(r) AS c", p) > 0, "bait missing: document edge");
  assert.ok(await countOf(direct, "MATCH (:Entity {namespace: $a})-[r:BAIT_LINK]->(:Entity {namespace: $b}) RETURN count(r) AS c", p) > 0, "bait missing: path edge");
  assert.ok(await countOf(direct, "MATCH (:Entity {namespace: $a})-[r:RELATES_AS {relation_type: 'BAIT'}]->(:Entity {namespace: $b}) RETURN count(r) AS c", p) > 0, "bait missing: relation edge");
  assert.ok(await countOf(direct, "MATCH (:Entity {namespace: $a})-[r:CO_OCCURS_WITH]->(:Entity {namespace: $b}) RETURN count(r) AS c", p) > 0, "bait missing: co-occurrence edge");
}

// Decoy entities with the same names as the own ones (another user or another namespace).
export async function decoyEntities(direct: QueryApiRunner, ns: string, uid: string, tag: string): Promise<void> {
  const decoys: [string, string][] = [["acme corp", "Leaked Acme Corp"], ["globex corp", "Leaked Globex Corp"], ["gamma corp", "Gamma Corp"], ["umbrella corp", "Umbrella Corp"]];
  for (const [name, display] of decoys) {
    await direct.run("CREATE (:Entity {composite_key: $k, name: $name, display_name: $dn, namespace: $ns, user_id: $uid, entity_type: 'unknown'})",
      { k: [ns, uid, tag, name].join("|"), name, dn: display, ns, uid });
  }
}

// Another user (u2) in the same namespace, plus a decoy weight in another namespace for the own user (u1).
export async function foreignStructure(direct: QueryApiRunner, ns: string, other: string, u1: string, u2: string): Promise<void> {
  const p = { ns, u1, u2, other };
  await direct.run("CREATE (:Document {composite_key: $ns + '|' + $u2 + '|doc-1', id: 'doc-1', title: 'Gamma news', namespace: $ns, user_id: $u2})", p);
  await direct.run("MATCH (d:Document {id: 'doc-1', namespace: $ns, user_id: $u2}) MATCH (y:Entity {namespace: $ns, user_id: $u2}) MERGE (d)-[:CONTAINS]->(y)", p);
  await direct.run("MATCH (x:Entity {name: 'acme corp', namespace: $ns, user_id: $u2}) MATCH (y:Entity {name: 'globex corp', namespace: $ns, user_id: $u2}) " +
    "MERGE (x)-[r:RELATES_AS {composite_key: x.composite_key + '|' + y.composite_key + '|sued'}]->(y) " +
    "SET r.relation_type = 'SUED_BY', r.weight = 3.0, r.namespace = $ns, r.user_id = $u2", p);
  await direct.run("MATCH (x:Entity {namespace: $ns, user_id: $u2}) MATCH (y:Entity {namespace: $ns, user_id: $u2}) WHERE x.composite_key < y.composite_key " +
    "MERGE (x)-[r:CO_OCCURS_WITH {composite_key: 'cu|' + x.composite_key + '|' + y.composite_key}]->(y) SET r.weight = 3.0, r.namespace = $ns, r.user_id = $u2", p);
  await direct.run("CREATE (:PairWeight {composite_key: $ns + '|' + $u2 + '|pw', namespace: $ns, user_id: $u2, entity_a: 'acme corp', entity_b: 'globex corp', document_id: 'gamma-doc', weight: 5.0})", p);
  await direct.run("CREATE (:RelationWeight {composite_key: $ns + '|' + $u2 + '|rw', namespace: $ns, user_id: $u2, subject: 'acme corp', object: 'globex corp', " +
    "relation_type: 'SUED_BY', document_id: 'gamma-doc', weight: 5.0})", p);
  await direct.run("CREATE (:RelationWeight {composite_key: $other + '|' + $u1 + '|rw', namespace: $other, user_id: $u1, subject: 'acme corp', object: 'globex corp', " +
    "relation_type: 'SUED_BY', document_id: 'gamma-doc', weight: 5.0})", p);
}

// Edges between u1 and u2 (same namespace) and between the namespaces (same user), in both directions where it matters.
export async function userBait(direct: QueryApiRunner, ns: string, other: string, u1: string, u2: string): Promise<void> {
  const p = { ns, u1, u2, other };
  await direct.run("MATCH (x:Entity {namespace: $ns, user_id: $u1}) MATCH (y:Entity {namespace: $ns, user_id: $u2}) " +
    "MERGE (x)-[r:RELATES_AS {composite_key: 'bo|' + x.composite_key + '|' + y.composite_key}]->(y) " +
    "SET r.relation_type = 'BAIT', r.weight = 1.0, r.namespace = $ns, r.user_id = $u1 " +
    "MERGE (y)-[q:RELATES_AS {composite_key: 'bi|' + x.composite_key + '|' + y.composite_key}]->(x) " +
    "SET q.relation_type = 'BAIT', q.weight = 1.0, q.namespace = $ns, q.user_id = $u2", p);
  await direct.run("MATCH (x:Entity {namespace: $ns, user_id: $u1}) MATCH (z:Entity {namespace: $other, user_id: $u1}) " +
    "MERGE (x)-[r:RELATES_AS {composite_key: 'oo|' + x.composite_key + '|' + z.composite_key}]->(z) " +
    "SET r.relation_type = 'BAIT', r.weight = 1.0, r.namespace = $ns, r.user_id = $u1 " +
    "MERGE (z)-[q:RELATES_AS {composite_key: 'oi|' + x.composite_key + '|' + z.composite_key}]->(x) " +
    "SET q.relation_type = 'BAIT', q.weight = 1.0, q.namespace = $other, q.user_id = $u1", p);
  await direct.run("MATCH (x:Entity {namespace: $ns, user_id: $u1}) MATCH (y:Entity {namespace: $ns, user_id: $u2}) WHERE y.name IN ['gamma corp', 'umbrella corp'] " +
    "MERGE (x)-[r:CO_OCCURS_WITH {composite_key: 'bc|' + x.composite_key + '|' + y.composite_key}]->(y) SET r.weight = 5.0, r.namespace = $ns, r.user_id = $u1", p);
  await direct.run("MATCH (d:Document {id: 'doc-1', namespace: $ns, user_id: $u1}) MATCH (y:Entity {namespace: $ns, user_id: $u2}) MERGE (d)-[:CONTAINS]->(y)", p);
  await direct.run("MATCH (d:Document {id: 'doc-1', namespace: $ns, user_id: $u2}) MATCH (x:Entity {namespace: $ns, user_id: $u1}) MERGE (d)-[:CONTAINS]->(x)", p);
  assert.ok(await countOf(direct, "MATCH (:Entity {namespace: $ns, user_id: $u2})-[r:RELATES_AS {relation_type: 'BAIT'}]->(:Entity {namespace: $ns, user_id: $u1}) RETURN count(r) AS c", p) > 0, "bait missing: user relation edge");
  assert.ok(await countOf(direct, "MATCH (:Entity {namespace: $other, user_id: $u1})-[r:RELATES_AS {relation_type: 'BAIT'}]->(:Entity {namespace: $ns, user_id: $u1}) RETURN count(r) AS c", p) > 0, "bait missing: namespace relation edge");
  assert.ok(await countOf(direct, "MATCH (:Document {namespace: $ns, user_id: $u2})-[r:CONTAINS]->(:Entity {namespace: $ns, user_id: $u1}) RETURN count(r) AS c", p) > 0, "bait missing: document edge");
  assert.ok(await countOf(direct, "MATCH (:Entity {namespace: $ns, user_id: $u1})-[r:CO_OCCURS_WITH]->(:Entity {namespace: $ns, user_id: $u2}) RETURN count(r) AS c", p) > 0, "bait missing: co-occurrence edge");
}

export async function reads(idx: GraphIndex, ns: string, uid: string): Promise<Record<string, string>> {
  const s = { namespace: ns, userId: uid, limit: 100 };
  const out: Record<string, string> = {};
  out.findDocumentsByEntities = norm(await idx.findDocumentsByEntities(["Acme Corp", "Initech"], s));
  out.documentEntities = norm(await idx.documentEntities("doc-1", { namespace: ns, userId: uid }));
  out.findEntitiesByType = norm(await idx.findEntitiesByType("unknown", s));
  out.findLineage = norm(await idx.findLineage("Acme Corp", "Globex Corp", s));
  for (let d = 1; d <= 3; d++) out["searchRelated" + d] = norm(await idx.searchRelatedEntities(["Acme Corp"], { ...s, maxDepth: d }));
  for (const direction of ["outgoing", "incoming", "both"] as const) {
    for (const ent of ["Acme Corp", "Globex Corp"]) {
      out["relations_" + direction + "_" + ent] = norm(await idx.findRelations(ent, { ...s, direction }));
    }
  }
  return out;
}

export function problems(base: Record<string, string>, crowded: Record<string, string>): string[] {
  const out: string[] = [];
  for (const k of Object.keys(base)) {
    if (base[k] !== crowded[k]) {
      const have = new Set(base[k].split("\n"));
      out.push(k + " differs, only in the crowded read: " + JSON.stringify(crowded[k].split("\n").filter((l) => !have.has(l))));
    }
    const words = foreignWords(crowded[k]);
    if (words.length > 0) out.push(k + " contains the foreign word '" + words[0] + "'");
  }
  return out;
}

export async function refresh(idx: GraphIndex, ns: string, uid: string, o: { summarizer?: Summarizer | null; includeRelations?: boolean; force?: boolean } = {}) {
  return idx.refreshCommunities({ namespace: ns, userId: uid, maxEdges: 200000, includeRelations: o.includeRelations ?? false, summarizer: o.summarizer ?? null, force: o.force ?? false });
}
export async function communityReads(idx: GraphIndex, ns: string, uid: string): Promise<string> {
  const byName = await idx.findCommunities(["Acme Corp"], { namespace: ns, userId: uid, limit: 1000, includeMembers: true });
  const all = await idx.findCommunities(null, { namespace: ns, userId: uid, limit: 1000, includeMembers: true });
  return norm(byName) + "||" + norm(all);
}
