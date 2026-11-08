import { cpCompare, detectCommunities } from "./communities.ts";
import type { Community, Edge } from "./communities.ts";
import { compositeKey } from "./compositeKey.ts";
import { neo4jFloat } from "./neo4jJson.ts";
import { pyStrip } from "./pyText.ts";
import { normalizeEntityList } from "./queries.ts";
import type { CypherRunner, Row } from "./queryApiRunner.ts";
import { NOT_AVAILABLE } from "./upsert.ts";

/**
 * Community methods of GraphIndex (port of Python's detect_communities, refresh_communities and find_communities): reads the
 * co-occurrence edges, groups them with detectCommunities, stores the groups as :Community nodes with :IN_COMMUNITY edges, and
 * looks them up again. Do not refresh one namespace from two places at once. Not ported: the audit hook (the TypeScript port has none yet).
 */

export const CONSTRAINT =
  "CREATE CONSTRAINT community_composite_key IF NOT EXISTS FOR (c:Community) REQUIRE c.composite_key IS UNIQUE";
const CO_OCCURS_EDGES = `
MATCH (a:Entity {namespace: $namespace})-[r:CO_OCCURS_WITH]-(b:Entity {namespace: $namespace})
WHERE a.name < b.name
  AND coalesce(a.user_id, '') = $user_id
  AND coalesce(b.user_id, '') = $user_id
RETURN a.name AS a, b.name AS b, coalesce(r.weight, 1.0) AS weight
LIMIT $limit
`;
const RELATES_EDGES = `
MATCH (a:Entity {namespace: $namespace})-[r:RELATES_AS]-(b:Entity {namespace: $namespace})
WHERE a.name < b.name
  AND coalesce(a.user_id, '') = $user_id
  AND coalesce(b.user_id, '') = $user_id
RETURN a.name AS a, b.name AS b, coalesce(r.weight, 1.0) AS weight
LIMIT $limit
`;
const EXISTING = `
MATCH (c:Community {namespace: $namespace})
WHERE coalesce(c.user_id, '') = $user_id
RETURN c.composite_key AS key, c.summary IS NOT NULL AS has_summary
`;
const MERGE = `
MERGE (c:Community {composite_key: $composite_key})
ON CREATE SET c.created_at = datetime(), c.namespace = $namespace, c.user_id = $user_id,
    c.members_hash = $members_hash
SET c.size = $size, c.internal_weight = $internal_weight, c.resolution = $resolution,
    c.updated_at = datetime()
`;
const MEMBERS = `
MATCH (c:Community {composite_key: $composite_key})
UNWIND $members AS member_name
MATCH (e:Entity {name: member_name, namespace: $namespace})
WHERE coalesce(e.user_id, '') = $user_id
MERGE (e)-[:IN_COMMUNITY]->(c)
`;
const DELETE = `
MATCH (c:Community {composite_key: $composite_key})
DETACH DELETE c
`;
const DISPLAY_NAMES = `
MATCH (e:Entity {namespace: $namespace})
WHERE e.name IN $members AND coalesce(e.user_id, '') = $user_id
RETURN e.name AS name, e.display_name AS display_name
`;
const RELATIONS = `
MATCH (s:Entity {namespace: $namespace})-[r:RELATES_AS]->(o:Entity {namespace: $namespace})
WHERE s.name IN $members AND o.name IN $members
  AND coalesce(s.user_id, '') = $user_id AND coalesce(o.user_id, '') = $user_id
RETURN s.display_name AS subject, r.relation_type AS relation_type, o.display_name AS object,
       coalesce(r.weight, 1.0) AS weight
ORDER BY weight DESC
LIMIT $limit
`;
const SET_SUMMARY = `
MATCH (c:Community {composite_key: $composite_key})
SET c.summary = $summary, c.summary_updated_at = datetime()
`;
const LIST = `
MATCH (c:Community {namespace: $namespace})
WHERE coalesce(c.user_id, '') = $user_id
RETURN c.composite_key AS community_id, c.size AS member_count, c.internal_weight AS internal_weight,
       c.summary AS summary, 0 AS matched_entities
ORDER BY member_count DESC, community_id ASC
LIMIT $limit
`;
const BY_ENTITY = `
MATCH (e:Entity)-[:IN_COMMUNITY]->(c:Community)
WHERE e.namespace = $namespace
  AND c.namespace = $namespace
  AND coalesce(e.user_id, '') = $user_id
  AND coalesce(c.user_id, '') = $user_id
  AND e.name IN $entity_names
RETURN c.composite_key AS community_id, c.size AS member_count, c.internal_weight AS internal_weight,
       c.summary AS summary, count(DISTINCT e) AS matched_entities
ORDER BY matched_entities DESC, member_count DESC, community_id ASC
LIMIT $limit
`;
const MEMBER_NAMES = `
MATCH (e:Entity)-[:IN_COMMUNITY]->(c:Community {composite_key: $community_id})
WHERE coalesce(e.user_id, '') = $user_id
RETURN e.display_name AS entity_name
ORDER BY entity_name
LIMIT $limit
`;

/** Every statement by its Python name (the parity test compares them with the recorded Python texts). */
export const COMMUNITY_STATEMENTS: Readonly<Record<string, string>> = Object.freeze({
  constraint: CONSTRAINT,
  co_occurs_edges: CO_OCCURS_EDGES,
  relates_edges: RELATES_EDGES,
  existing: EXISTING,
  merge: MERGE,
  members: MEMBERS,
  delete: DELETE,
  display_names: DISPLAY_NAMES,
  relations: RELATIONS,
  set_summary: SET_SUMMARY,
  list: LIST,
  by_entity: BY_ENTITY,
  member_names: MEMBER_NAMES,
});

const MEMBER_LIMIT = 25;
const SUMMARY_LIMIT = 20000;

/** Bad arguments or too many edges (Python: ValueError). */
export class CommunityArgumentError extends RangeError {}

export interface StoreLogger {
  warn(message: string): void;
  error?(message: string): void;
}
export interface DetectOptions {
  namespace?: string | null;
  userId?: string | null;
  resolution?: number;
  minSize?: number;
  includeRelations?: boolean;
  maxEdges?: number;
}
export interface CommunityInfo {
  communityId: string;
  members: string[];
  displayNames: string[];
  size: number;
  internalWeight: number;
  topPairs: { a: string; b: string; weight: number }[];
  relations: { subject: unknown; relationType: unknown; object: unknown; weight: number }[];
}
/** Returns the summary text for one community (a failing or empty answer is counted, never thrown). */
export type Summarizer = (info: CommunityInfo) => string | Promise<string>;
export interface RefreshOptions extends DetectOptions {
  summarizer?: Summarizer | null;
  force?: boolean;
  maxSummaryItems?: number;
}
export interface RefreshResult {
  success: boolean;
  communities: number;
  created: number;
  kept: number;
  removed: number;
  summarized: number;
  summaryErrors: number;
  error: string | null;
}
export interface FindOptions {
  namespace?: string | null;
  userId?: string | null;
  limit?: number;
  includeMembers?: boolean;
}
export interface CommunityHit {
  communityId: string;
  size: number;
  internalWeight: number;
  summary: string | null;
  matchedEntities: number;
  members?: string[];
}

function messageOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
function toInt(v: unknown): number {
  return Math.trunc(Number(v || 0));
}
function toFloat(v: unknown): number {
  return Number(v || 0.0);
}
function positiveInt(v: unknown): boolean {
  return typeof v === "number" && Number.isInteger(v) && v >= 1;
}

function checkArgs(resolution: number, minSize: number, maxEdges: number): void {
  detectCommunities([], resolution, minSize);
  if (!positiveInt(maxEdges)) {
    throw new RangeError(`max_edges must be a positive integer, got ${String(maxEdges)}`);
  }
}

async function fetchEdges(runner: CypherRunner, ns: string, uid: string, includeRelations: boolean, maxEdges: number): Promise<Edge[]> {
  const queries = includeRelations ? [CO_OCCURS_EDGES, RELATES_EDGES] : [CO_OCCURS_EDGES];
  const edges: Edge[] = [];
  for (const query of queries) {
    const rows = await runner.run(query, { namespace: ns, user_id: uid, limit: maxEdges + 1 });
    if (rows.length > maxEdges) {
      throw new CommunityArgumentError(
        `more than max_edges=${maxEdges} edges of one kind in this namespace; raise max_edges or use a smaller namespace`,
      );
    }
    for (const row of rows) {
      edges.push([row.a as string, row.b as string, row.weight]);
    }
  }
  return edges;
}

function clip(s: string, maxCodePoints: number): string {
  const cps = Array.from(s);
  return cps.length > maxCodePoints ? cps.slice(0, maxCodePoints).join("") : s;
}

/** Groups the entities of a namespace into communities without writing anything. Bad arguments reject with a RangeError. */
export async function detectIndexCommunities(runner: CypherRunner | null, logger: StoreLogger, options: DetectOptions = {}): Promise<Community[]> {
  const { resolution = 1.0, minSize = 2, includeRelations = false, maxEdges = 200_000 } = options;
  checkArgs(resolution, minSize, maxEdges);
  if (runner === null) {
    logger.warn(NOT_AVAILABLE);
    return [];
  }
  const ns = options.namespace || "";
  const uid = options.userId || "";
  try {
    const edges = await fetchEdges(runner, ns, uid, includeRelations, maxEdges);
    return detectCommunities(edges, resolution, minSize);
  } catch (e) {
    if (e instanceof CommunityArgumentError) throw e;
    logger.error?.(`Community detection error: ${messageOf(e)}`);
    return [];
  }
}

/**
 * Detects the communities and stores them. A community is identified by a hash of its members: unchanged ones are kept (with
 * their summary), changed ones are replaced, vanished ones are deleted. The summarizer gets one object and returns text; a
 * failing or empty answer is counted in summaryErrors and the refresh carries on.
 */
export async function refreshIndexCommunities(runner: CypherRunner | null, logger: StoreLogger, options: RefreshOptions = {}): Promise<RefreshResult> {
  const { resolution = 1.0, minSize = 2, includeRelations = false, maxEdges = 200_000, summarizer = null, force = false, maxSummaryItems = 20 } = options;
  checkArgs(resolution, minSize, maxEdges);
  if (!positiveInt(maxSummaryItems)) {
    throw new RangeError(`max_summary_items must be a positive integer, got ${String(maxSummaryItems)}`);
  }
  if (summarizer !== null && typeof summarizer !== "function") {
    throw new RangeError("summarizer must be a function taking one dict and returning text");
  }
  const result: RefreshResult = { success: false, communities: 0, created: 0, kept: 0, removed: 0, summarized: 0, summaryErrors: 0, error: null };
  if (runner === null) {
    result.error = NOT_AVAILABLE;
    return result;
  }
  const ns = options.namespace || "";
  const uid = options.userId || "";
  try {
    const edges = await fetchEdges(runner, ns, uid, includeRelations, maxEdges);
    const found = detectCommunities(edges, resolution, minSize);
    await runner.run(CONSTRAINT, {});
    const existing = new Map<string, boolean>();
    for (const row of await runner.run(EXISTING, { namespace: ns, user_id: uid })) {
      existing.set(String(row.key), Boolean(row.has_summary));
    }
    const keys: string[] = [];
    for (const c of found) {
      const key = compositeKey(ns, uid, "community", c.membersHash);
      keys.push(key);
      await runner.run(MERGE, {
        composite_key: key,
        namespace: ns,
        user_id: uid,
        members_hash: c.membersHash,
        size: c.size,
        internal_weight: neo4jFloat(c.internalWeight),
        resolution: neo4jFloat(resolution),
      });
      await runner.run(MEMBERS, { composite_key: key, namespace: ns, user_id: uid, members: c.members });
    }
    const keep = new Set(keys);
    const created = keys.filter((k) => !existing.has(k));
    const kept = keys.filter((k) => existing.has(k));
    const removed = [...existing.keys()].filter((k) => !keep.has(k)).sort(cpCompare);
    for (const key of removed) {
      await runner.run(DELETE, { composite_key: key });
    }
    result.communities = found.length;
    result.created = created.length;
    result.kept = kept.length;
    result.removed = removed.length;
    if (summarizer !== null) {
      await summarize(runner, logger, found, keys, existing, edges, ns, uid, summarizer, force, maxSummaryItems, result);
    }
    result.success = true;
    return result;
  } catch (e) {
    if (e instanceof CommunityArgumentError) throw e;
    logger.error?.(`Community refresh error: ${messageOf(e)}`);
    result.success = false;
    result.error = messageOf(e);
    return result;
  }
}

async function summarize(
  runner: CypherRunner,
  logger: StoreLogger,
  found: Community[],
  keys: string[],
  existing: Map<string, boolean>,
  edges: Edge[],
  ns: string,
  uid: string,
  summarizer: Summarizer,
  force: boolean,
  limit: number,
  result: RefreshResult,
): Promise<void> {
  type Pair = { a: string; b: string; weight: number };
  const owner = new Map<string, string>();
  found.forEach((c, i) => {
    for (const name of c.members) owner.set(name, keys[i]);
  });
  const pairs = new Map<string, Pair>();
  for (const [a, b, w] of edges) {
    const [low, high] = cpCompare(a, b) < 0 ? [a, b] : [b, a];
    const k = JSON.stringify([low, high]);
    const prev = pairs.get(k);
    pairs.set(k, { a: low, b: high, weight: (prev ? prev.weight : 0.0) + Number(w) });
  }
  const byKey = new Map<string, Pair[]>();
  for (const p of pairs.values()) {
    if (p.a !== p.b && owner.has(p.a) && owner.get(p.a) === owner.get(p.b)) {
      const k = owner.get(p.a) as string;
      const list = byKey.get(k);
      if (list) list.push(p);
      else byKey.set(k, [p]);
    }
  }
  for (let i = 0; i < found.length; i++) {
    const key = keys[i];
    const community = found[i];
    const needs = force || !existing.has(key) || !existing.get(key);
    if (!needs) continue;
    const names = new Map<string, unknown>();
    for (const row of await runner.run(DISPLAY_NAMES, { namespace: ns, user_id: uid, members: community.members })) {
      names.set(String(row.name), row.display_name);
    }
    const relations: CommunityInfo["relations"] = [];
    for (const row of await runner.run(RELATIONS, { namespace: ns, user_id: uid, members: community.members, limit })) {
      relations.push({ subject: row.subject ?? null, relationType: row.relation_type ?? null, object: row.object ?? null, weight: Number(row.weight) });
    }
    const top = (byKey.get(key) ?? [])
      .slice()
      .sort((x, y) => y.weight - x.weight || cpCompare(x.a, y.a) || cpCompare(x.b, y.b))
      .slice(0, limit);
    const info: CommunityInfo = {
      communityId: key,
      members: [...community.members],
      displayNames: community.members.map((m) => (names.get(m) as string | null) || m),
      size: community.size,
      internalWeight: community.internalWeight,
      topPairs: top.map((p) => ({ a: p.a, b: p.b, weight: p.weight })),
      relations,
    };
    let text: string;
    try {
      text = await summarizer(info);
      if (typeof text !== "string" || pyStrip(text) === "") {
        throw new RangeError("summarizer must return a non-empty string");
      }
    } catch (e) {
      result.summaryErrors += 1;
      logger.warn(`Community summary failed: ${messageOf(e)}`);
      continue;
    }
    await runner.run(SET_SUMMARY, { composite_key: key, summary: clip(pyStrip(text), SUMMARY_LIMIT) });
    result.summarized += 1;
  }
}

/**
 * Stored communities. With entity names (normalized like the other lookups): the communities that contain any of them, most
 * matched entities first; with null or nothing: the largest communities.
 */
export async function findIndexCommunities(
  runner: CypherRunner | null,
  logger: StoreLogger,
  entityNames?: readonly string[] | null,
  options: FindOptions = {},
): Promise<CommunityHit[]> {
  if (runner === null) {
    logger.warn(NOT_AVAILABLE);
    return [];
  }
  let names: string[] | null = null;
  if (entityNames !== null && entityNames !== undefined) {
    names = normalizeEntityList(entityNames);
    if (names.length === 0) return [];
  }
  const ns = options.namespace || "";
  const uid = options.userId || "";
  const lim = Math.max(1, Math.trunc(options.limit ?? 25));
  try {
    const rows: Row[] =
      names === null
        ? await runner.run(LIST, { namespace: ns, user_id: uid, limit: lim })
        : await runner.run(BY_ENTITY, { namespace: ns, user_id: uid, entity_names: names, limit: lim });
    const out: CommunityHit[] = [];
    for (const row of rows) {
      const hit: CommunityHit = {
        communityId: row.community_id as string,
        size: toInt(row.member_count),
        internalWeight: toFloat(row.internal_weight),
        summary: (row.summary ?? null) as string | null,
        matchedEntities: toInt(row.matched_entities),
      };
      if (options.includeMembers) {
        const members: string[] = [];
        for (const m of await runner.run(MEMBER_NAMES, { community_id: row.community_id, user_id: uid, limit: MEMBER_LIMIT })) {
          members.push(m.entity_name as string);
        }
        hit.members = members;
      }
      out.push(hit);
    }
    return out;
  } catch (e) {
    logger.error?.(`Community lookup error: ${messageOf(e)}`);
    return [];
  }
}
