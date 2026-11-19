import { test } from "node:test";
import assert from "node:assert/strict";
import type { CommunityInfo } from "../src/communityStore.ts";
import { MUST, close, communityReads, countOf, foreignWords, namespaceBait, norm, open, problems, reads, refresh, runId, seedTenant, skip, wipe } from "./isolationSupport.ts";

// Tenant isolation against a real Neo4j: tenant A must see exactly what it would see if it were alone, even with deliberate
// cross-tenant edges ("bait"). Own namespaces, removed afterwards. Gated by NEO4J_HTTP_URL.
const PREFIX = runId("ragleap_js_tenant_");
const BASE = PREFIX + "_base";
const A = PREFIX + "_a";
const B = PREFIX + "_b";

test("live: tenant A sees only what it would see alone", { skip }, async () => {
  const { direct, idx } = await open();
  try {
    await wipe(direct, PREFIX);
    await seedTenant(direct, idx, BASE, "", false);
    const base = await reads(idx, BASE, "");
    for (const must of MUST) assert.notEqual(base[must], "", "baseline empty, the test would pass trivially: " + must);
    await seedTenant(direct, idx, A, "", false);
    await seedTenant(direct, idx, B, "", true);
    await namespaceBait(direct, A, B);
    const bad = problems(base, await reads(idx, A, ""));
    assert.deepEqual(bad, [], bad.join(" || "));
  } finally {
    await wipe(direct, PREFIX);
    close(direct, idx);
  }
});

test("live: community lookups ignore other tenants", { skip }, async () => {
  const { direct, idx } = await open();
  try {
    await wipe(direct, PREFIX);
    await seedTenant(direct, idx, BASE, "", false);
    await refresh(idx, BASE, "");
    const base = await communityReads(idx, BASE, "");
    assert.ok(!base.startsWith("||"), "baseline empty, the test would pass trivially");
    await seedTenant(direct, idx, A, "", false);
    await seedTenant(direct, idx, B, "", true);
    await refresh(idx, A, "");
    await refresh(idx, B, "");
    await namespaceBait(direct, A, B);
    const p = { a: A, b: B };
    await direct.run("MATCH (c:Community {namespace: $a}) MATCH (y:Entity {name: 'acme corp', namespace: $b}) MERGE (y)-[:IN_COMMUNITY]->(c)", p);
    await direct.run("MATCH (c:Community {namespace: $b}) MATCH (x:Entity {name: 'acme corp', namespace: $a}) MERGE (x)-[:IN_COMMUNITY]->(c)", p);
    assert.ok(await countOf(direct, "MATCH (:Entity {namespace: $b})-[r:IN_COMMUNITY]->(:Community {namespace: $a}) RETURN count(r) AS c", p) > 0, "community bait missing");
    assert.ok(await countOf(direct, "MATCH (:Entity {namespace: $a})-[r:IN_COMMUNITY]->(:Community {namespace: $b}) RETURN count(r) AS c", p) > 0, "community bait missing");
    const crowded = await communityReads(idx, A, "");
    assert.deepEqual(foreignWords(crowded), [], crowded);
    assert.equal(crowded, base);
  } finally {
    await wipe(direct, PREFIX);
    close(direct, idx);
  }
});

async function communityRows(direct: Awaited<ReturnType<typeof open>>["direct"], ns: string): Promise<string> {
  return norm(await direct.run("MATCH (c:Community {namespace: $ns}) OPTIONAL MATCH (e)-[r:IN_COMMUNITY]->(c) RETURN count(DISTINCT c) AS communities, count(r) AS memberships", { ns }));
}

test("live: refreshing one tenant leaves the communities of another tenant untouched", { skip }, async () => {
  const { direct, idx } = await open();
  try {
    await wipe(direct, PREFIX);
    await seedTenant(direct, idx, A, "", false);
    await seedTenant(direct, idx, B, "", true);
    await refresh(idx, A, "");
    await refresh(idx, B, "");
    const before = await communityRows(direct, B);
    assert.ok(!before.includes('"communities":0'), "tenant B has no communities, the test would pass trivially: " + before);
    await refresh(idx, A, "", { force: true });
    assert.equal(await communityRows(direct, B), before);
  } finally {
    await wipe(direct, PREFIX);
    close(direct, idx);
  }
});

test("live: summarizer inputs contain only the own tenant", { skip }, async () => {
  const { direct, idx } = await open();
  try {
    await wipe(direct, PREFIX);
    await seedTenant(direct, idx, BASE, "", false);
    const baseInfos: CommunityInfo[] = [];
    await refresh(idx, BASE, "", { includeRelations: true, force: true, summarizer: (info) => { baseInfos.push(info); return "summary"; } });
    assert.ok(baseInfos.length > 0, "the summarizer was never called");
    await seedTenant(direct, idx, A, "", false);
    await seedTenant(direct, idx, B, "", true);
    await namespaceBait(direct, A, B);
    const infos: CommunityInfo[] = [];
    await refresh(idx, A, "", { includeRelations: true, force: true, summarizer: (info) => { infos.push(info); return "summary"; } });
    assert.deepEqual(foreignWords(JSON.stringify(infos)), [], JSON.stringify(infos));
    assert.equal(norm(infos), norm(baseInfos));
  } finally {
    await wipe(direct, PREFIX);
    close(direct, idx);
  }
});
