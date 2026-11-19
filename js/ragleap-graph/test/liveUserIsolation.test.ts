import { test } from "node:test";
import assert from "node:assert/strict";
import type { GraphIndex } from "../src/graphIndex.ts";
import type { CommunityInfo } from "../src/communityStore.ts";
import { COMMUNITY_STATEMENTS } from "../src/communityStore.ts";
import { MUST, close, communityReads, countOf, decoyEntities, foreignStructure, foreignWords, norm, open, problems, reads, refresh, runId, seedTenant, skip, userBait, wipe } from "./isolationSupport.ts";

// Tenant scope is (namespace, user_id): a user must see exactly what it would see alone, even when another user shares the
// namespace and the same user has decoys in another namespace, with foreign edges (bait) in both directions. Real Neo4j, gated.
const PREFIX = runId("ragleap_js_user_");
const BASE = PREFIX + "_base";
const NS = PREFIX + "_crowd";
const OTHER = PREFIX + "_other";
const U1 = "user-one";
const U2 = "user-two";

async function buildCrowd(direct: Awaited<ReturnType<typeof open>>["direct"], idx: GraphIndex, otherUserCommunities: boolean): Promise<void> {
  await decoyEntities(direct, NS, U2, "pre");
  await decoyEntities(direct, OTHER, U1, "pre");
  await seedTenant(direct, idx, NS, U1, false);
  await decoyEntities(direct, NS, U2, "post");
  await decoyEntities(direct, OTHER, U1, "post");
  await foreignStructure(direct, NS, OTHER, U1, U2);
  if (otherUserCommunities) await refresh(idx, NS, U2);
  await userBait(direct, NS, OTHER, U1, U2);
}

test("live: user-scoped reads match what the user sees alone", { skip }, async () => {
  const { direct, idx } = await open();
  try {
    await wipe(direct, PREFIX);
    await seedTenant(direct, idx, BASE, U1, false);
    const base = await reads(idx, BASE, U1);
    for (const must of MUST) assert.notEqual(base[must], "", "baseline empty, the test would pass trivially: " + must);
    await buildCrowd(direct, idx, false);
    const bad = problems(base, await reads(idx, NS, U1));
    assert.deepEqual(bad, [], bad.join(" || "));
  } finally {
    await wipe(direct, PREFIX);
    close(direct, idx);
  }
});

test("live: community lookups ignore other users and other namespaces", { skip }, async () => {
  const { direct, idx } = await open();
  try {
    await wipe(direct, PREFIX);
    await seedTenant(direct, idx, BASE, U1, false);
    await refresh(idx, BASE, U1, { includeRelations: true });
    const base = await communityReads(idx, BASE, U1);
    assert.ok(!base.startsWith("||"), "baseline empty, the test would pass trivially");
    await buildCrowd(direct, idx, true);
    await refresh(idx, NS, U1, { includeRelations: true });
    const p = { ns: NS, u1: U1, u2: U2 };
    await direct.run("MATCH (c:Community {namespace: $ns, user_id: $u1}) MATCH (y:Entity {name: 'acme corp', namespace: $ns, user_id: $u2}) MERGE (y)-[:IN_COMMUNITY]->(c)", p);
    await direct.run("MATCH (c:Community {namespace: $ns, user_id: $u2}) MATCH (x:Entity {name: 'acme corp', namespace: $ns, user_id: $u1}) MERGE (x)-[:IN_COMMUNITY]->(c)", p);
    assert.ok(await countOf(direct, "MATCH (:Entity {namespace: $ns, user_id: $u2})-[r:IN_COMMUNITY]->(:Community {namespace: $ns, user_id: $u1}) RETURN count(r) AS c", p) > 0, "community bait missing");
    assert.ok(await countOf(direct, "MATCH (:Entity {namespace: $ns, user_id: $u1})-[r:IN_COMMUNITY]->(:Community {namespace: $ns, user_id: $u2}) RETURN count(r) AS c", p) > 0, "community bait missing");
    const crowded = await communityReads(idx, NS, U1);
    assert.deepEqual(foreignWords(crowded), [], crowded);
    assert.equal(crowded, base);
  } finally {
    await wipe(direct, PREFIX);
    close(direct, idx);
  }
});

async function communityCount(direct: Awaited<ReturnType<typeof open>>["direct"], uid: string): Promise<string> {
  return norm(await direct.run("MATCH (c:Community {namespace: $ns, user_id: $uid}) RETURN count(c) AS communities", { ns: NS, uid }));
}

test("live: a pruning refresh keeps the communities of another user", { skip }, async () => {
  const { direct, idx } = await open();
  try {
    await wipe(direct, PREFIX);
    await seedTenant(direct, idx, NS, U1, false);
    await decoyEntities(direct, NS, U2, "pre");
    await foreignStructure(direct, NS, OTHER, U1, U2);
    await refresh(idx, NS, U2);
    const before = await communityCount(direct, U2);
    assert.ok(!before.includes('"communities":0'), "user two has no communities, the test would pass trivially: " + before);
    const r = await refresh(idx, NS, U1, { force: true });
    assert.equal(r.removed, 0);
    assert.equal(await communityCount(direct, U2), before);
  } finally {
    await wipe(direct, PREFIX);
    close(direct, idx);
  }
});

test("live: summarizer inputs ignore other users and other namespaces", { skip }, async () => {
  const { direct, idx } = await open();
  try {
    await wipe(direct, PREFIX);
    await seedTenant(direct, idx, BASE, U1, false);
    const baseInfos: CommunityInfo[] = [];
    await refresh(idx, BASE, U1, { includeRelations: true, force: true, summarizer: (info) => { baseInfos.push(info); return "summary"; } });
    assert.ok(baseInfos.length > 0, "the summarizer was never called");
    await buildCrowd(direct, idx, false);
    const infos: CommunityInfo[] = [];
    await refresh(idx, NS, U1, { includeRelations: true, force: true, summarizer: (info) => { infos.push(info); return "summary"; } });
    assert.deepEqual(foreignWords(JSON.stringify(infos)), [], JSON.stringify(infos));
    assert.equal(norm(infos), norm(baseInfos));
  } finally {
    await wipe(direct, PREFIX);
    close(direct, idx);
  }
});

test("live: the display names statement returns only the own user's entities", { skip }, async () => {
  const { direct, idx } = await open();
  try {
    await wipe(direct, PREFIX);
    await seedTenant(direct, idx, BASE, U1, false);
    const members = ["acme corp", "globex corp", "initech", "zeta corp"];
    const baseRows = await direct.run(COMMUNITY_STATEMENTS.display_names, { namespace: BASE, user_id: U1, members });
    assert.ok(baseRows.length > 0, "baseline empty, the test would pass trivially");
    await buildCrowd(direct, idx, false);
    const crowdRows = await direct.run(COMMUNITY_STATEMENTS.display_names, { namespace: NS, user_id: U1, members });
    assert.deepEqual(foreignWords(JSON.stringify(crowdRows)), [], JSON.stringify(crowdRows));
    assert.equal(norm(crowdRows), norm(baseRows));
  } finally {
    await wipe(direct, PREFIX);
    close(direct, idx);
  }
});
