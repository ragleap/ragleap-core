import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { isDeepStrictEqual } from "node:util";
import { GraphIndex } from "../src/graphIndex.ts";
import { COMMUNITY_STATEMENTS } from "../src/communityStore.ts";
import type { CommunityHit, CommunityInfo, RefreshResult } from "../src/communityStore.ts";
import type { Community } from "../src/communities.ts";
import { Neo4jFloat, encodeNeo4jJson } from "../src/neo4jJson.ts";
import type { CypherRunner, Row } from "../src/queryApiRunner.ts";

// Compares the community methods of GraphIndex with the real Python methods (recorded by
// java/ragleap-graph/parity/gen_fixtures_community_index.py): results, every statement with its exact parameters, every
// summarizer input and every error. Not compared: Python's audit events (the TypeScript port has no audit hook yet).
// One known difference: Python prints a float resolution as "0.0" in its error message, JavaScript has no float type ("0").
const dir = process.env.RAGLEAP_GRAPH_PARITY_DIR;
const skip = dir ? false : "RAGLEAP_GRAPH_PARITY_DIR not set";
const silent = { info() {}, warn() {}, error() {} };

function norm(s: string): string {
  return s.trim().replace(/\s+/g, " ");
}
function load(): any {
  return JSON.parse(readFileSync(join(dir as string, "community_index_cases.json"), "utf8"));
}

function snakeInfo(i: CommunityInfo): unknown {
  return {
    community_id: i.communityId,
    members: i.members,
    display_names: i.displayNames,
    size: i.size,
    internal_weight: i.internalWeight,
    top_pairs: i.topPairs,
    relations: i.relations.map((r) => ({ subject: r.subject, relation_type: r.relationType, object: r.object, weight: r.weight })),
  };
}
function snakeCommunity(c: Community): unknown {
  return { members: c.members, size: c.size, internal_weight: c.internalWeight, members_hash: c.membersHash };
}
function snakeRefresh(r: RefreshResult): unknown {
  return { success: r.success, communities: r.communities, created: r.created, kept: r.kept, removed: r.removed, summarized: r.summarized, summary_errors: r.summaryErrors, error: r.error };
}
function snakeHit(h: CommunityHit): unknown {
  const o: Record<string, unknown> = { community_id: h.communityId, size: h.size, internal_weight: h.internalWeight, summary: h.summary, matched_entities: h.matchedEntities };
  if (h.members !== undefined) o.members = h.members;
  return o;
}

function makeSummarizer(kind: string, events: unknown[]): (info: CommunityInfo) => string {
  let n = 0;
  return (info) => {
    n++;
    events.push(["summarizer", snakeInfo(info)]);
    switch (kind) {
      case "text":
      case "fail_second":
        if (kind === "fail_second" && n === 2) throw new Error("nope");
        return `  S:${info.size}:${info.displayNames.join(",")}  `;
      case "blank":
        return "   ";
      case "raise":
        throw new Error("nope");
      case "none":
        return null as unknown as string;
      case "long":
        return "x".repeat(20005);
      case "astral":
        return "\u{1F600}".repeat(20001);
      default:
        throw new Error("unknown summarizer kind " + kind);
    }
  };
}

test("the Cypher texts match Python", { skip }, () => {
  const texts = load().texts as Record<string, string>;
  assert.equal(Object.keys(texts).length, Object.keys(COMMUNITY_STATEMENTS).length);
  for (const [name, text] of Object.entries(texts)) {
    assert.equal(norm(COMMUNITY_STATEMENTS[name]), norm(text), name);
  }
});

test("the community methods match the recorded Python behaviour", { skip }, async () => {
  const root = load();
  const nameByNorm = new Map<string, string>(Object.entries(root.texts as Record<string, string>).map(([k, v]) => [norm(v), k]));
  const bad: string[] = [];
  let compared = 0;
  let raised = 0;
  let eventsTotal = 0;
  for (const sc of root.scenarios) {
    const events: unknown[] = [];
    const counts = new Map<string, number>();
    const fail = sc.fail as [string, number] | null;
    let armed = false;
    const runner: CypherRunner | null = sc.driver
      ? {
          async run(cypher: string, params?: Record<string, unknown> | null): Promise<Row[]> {
            if (!armed) return [];
            const name = nameByNorm.get(norm(cypher));
            if (name === undefined) {
              bad.push(`${sc.name}: statement not in the Python fixtures: ${cypher}`);
              throw new Error("unknown statement");
            }
            const p = params ?? {};
            if (name === "merge" && (!(p.internal_weight instanceof Neo4jFloat) || !(p.resolution instanceof Neo4jFloat) || typeof p.size !== "number")) {
              bad.push(`${sc.name}: merge parameter types are wrong`);
            }
            events.push(["run", name, JSON.parse(encodeNeo4jJson(p))]);
            const n = (counts.get(name) ?? 0) + 1;
            counts.set(name, n);
            if (fail && fail[0] === name && fail[1] === n) throw new Error("boom");
            const lists = sc.script[name];
            if (!lists || lists.length === 0) return [];
            return JSON.parse(JSON.stringify(lists[Math.min(n - 1, lists.length - 1)]));
          },
          close(): void {},
        }
      : null;
    const index = await GraphIndex.connect(runner, { logger: silent });
    armed = true;
    const k = sc.kwargs;
    let actual: unknown = null;
    let message: string | null = null;
    try {
      if (sc.op === "detect") {
        const r = await index.detectCommunities({ namespace: k.namespace, userId: k.user_id, resolution: k.resolution, minSize: k.min_size, includeRelations: k.include_relations, maxEdges: k.max_edges });
        actual = r.map(snakeCommunity);
      } else if (sc.op === "refresh") {
        const r = await index.refreshCommunities({
          namespace: k.namespace,
          userId: k.user_id,
          resolution: k.resolution,
          minSize: k.min_size,
          includeRelations: k.include_relations,
          maxEdges: k.max_edges,
          summarizer: sc.summarizer ? makeSummarizer(sc.summarizer, events) : null,
          force: k.force,
          maxSummaryItems: k.max_summary_items,
        });
        actual = snakeRefresh(r);
      } else {
        const r = await index.findCommunities(k.entity_names, { namespace: k.namespace, userId: k.user_id, limit: k.limit, includeMembers: k.include_members });
        actual = r.map(snakeHit);
      }
    } catch (e) {
      if (e instanceof RangeError) message = e.message;
      else throw e;
    }
    compared++;
    if (sc.raises) {
      raised++;
      const expected = String(sc.message).replace(/(got -?\d+)\.0$/, "$1");
      if (expected !== message) bad.push(`${sc.name}: error '${message}', Python '${expected}'`);
    } else if (message !== null) {
      bad.push(`${sc.name}: unexpected error ${message}`);
    } else if (!isDeepStrictEqual(actual, sc.return)) {
      bad.push(`${sc.name}: result ${JSON.stringify(actual)}, Python ${JSON.stringify(sc.return)}`);
    }
    const want = (sc.events as unknown[][]).filter((e) => e[0] !== "audit");
    eventsTotal += events.length;
    if (!isDeepStrictEqual(events, want)) {
      bad.push(`${sc.name}: events differ, TypeScript ${JSON.stringify(events).slice(0, 300)}`);
    }
  }
  console.log(`community index parity: scenarios compared ${compared}, errors matched ${raised}, events compared ${eventsTotal}, differing ${bad.length}`);
  assert.ok(compared > 0 && raised > 0 && eventsTotal > 0, "nothing compared");
  assert.deepEqual(bad.slice(0, 3), []);
});
