import { normalizeEntityName } from "./entities.ts";
import { pyLower, pyRepr, pyStrip } from "./pyText.ts";
import type { CypherRunner, Row } from "./queryApiRunner.ts";
import { NOT_AVAILABLE } from "./upsert.ts";

export const MAX_ALLOWED_DEPTH = 10;
export const DEFAULT_LIMIT = 25;
export const DEFAULT_RELATED_LIMIT = 10;
export const DEFAULT_MAX_DEPTH = 2;

export interface QueryLogger {
  warn(message: string): void;
  error?(message: string): void;
}
export interface ScopeOptions {
  namespace?: string | null;
  userId?: string | null;
}
export interface LimitOptions extends ScopeOptions {
  limit?: number;
}
export interface RelatedOptions extends LimitOptions {
  maxDepth?: number;
}
export interface RelationsOptions extends LimitOptions {
  relationType?: string | null;
  direction?: "outgoing" | "incoming" | "both";
}
export interface LineageOptions extends LimitOptions {
  relationType?: string | null;
}
export interface DocumentHit {
  documentId: string;
  documentName: string;
  matchedEntities: number;
  graphScore: number;
  matchedEntityNames: string[];
}
export interface RelatedEntity {
  entityId: string | null;
  entityName: string | null;
  relationship: string | null;
  depth: number;
}
export interface RelationHit {
  subject: string | null;
  relationType: string | null;
  object: string | null;
  weight: number;
}
export interface LineageEntry {
  documentId: string | null;
  relationType: "CO_OCCURS_WITH" | "RELATES_AS";
  relationName?: string | null;
  weight: number | null;
}
export interface TypedEntity {
  entityId: string | null;
  entityName: string | null;
  entityType: string | null;
}
export interface EntityRef {
  entityId: string | null;
  entityName: string | null;
}

const FIND_DOCUMENTS = `
MATCH (e:Entity)<-[r:CONTAINS]-(d:Document)
WHERE e.namespace = $namespace
  AND d.namespace = $namespace
  AND coalesce(e.user_id, '') = $user_id
  AND coalesce(d.user_id, '') = $user_id
  AND e.name IN $entity_names
RETURN d.id AS document_id,
       coalesce(d.title, '') AS document_name,
       count(DISTINCT e) AS matched_entities,
       sum(coalesce(r.weight, 1.0)) AS graph_score,
       collect(DISTINCT e.display_name)[0..10] AS matched_entity_names
ORDER BY matched_entities DESC, graph_score DESC
LIMIT $limit
`;
const DOCUMENT_ENTITIES = `
MATCH (d:Document {id: $document_id, namespace: $namespace})
WHERE coalesce(d.user_id, '') = $user_id
MATCH (d)-[:CONTAINS]->(e:Entity)
WHERE coalesce(e.user_id, '') = $user_id
RETURN e.name AS entity_id,
       e.display_name AS entity_name
LIMIT 50
`;
const SEARCH_RELATED = `
MATCH (start:Entity)
WHERE start.name IN $entity_names
  AND start.namespace = $namespace
  AND coalesce(start.user_id, '') = $user_id
MATCH path = (start)-[*1..{max_depth}]-(related:Entity)
WHERE related.namespace = $namespace
  AND coalesce(related.user_id, '') = $user_id
RETURN DISTINCT related.name AS entity_id,
       related.display_name AS entity_name,
       type(relationships(path)[0]) AS relationship,
       length(path) AS depth
ORDER BY depth ASC
LIMIT $limit
`;
const FIND_RELATIONS_OUT = `
MATCH (s:Entity {name: $subject, namespace: $namespace})
      -[r:RELATES_AS]->(o:Entity {namespace: $namespace})
WHERE ($relation_type IS NULL OR r.relation_type = $relation_type)
  AND coalesce(s.user_id, '') = $user_id
  AND coalesce(o.user_id, '') = $user_id
RETURN s.display_name AS subject,
       r.relation_type AS relation_type,
       o.display_name AS object,
       coalesce(r.weight, 1.0) AS weight
ORDER BY weight DESC
LIMIT $limit
`;
const FIND_RELATIONS_IN = `
MATCH (o:Entity {name: $subject, namespace: $namespace})
      <-[r:RELATES_AS]-(s:Entity {namespace: $namespace})
WHERE ($relation_type IS NULL OR r.relation_type = $relation_type)
  AND coalesce(s.user_id, '') = $user_id
  AND coalesce(o.user_id, '') = $user_id
RETURN s.display_name AS subject,
       r.relation_type AS relation_type,
       o.display_name AS object,
       coalesce(r.weight, 1.0) AS weight
ORDER BY weight DESC
LIMIT $limit
`;
const FIND_RELATIONS_BOTH = `
MATCH (a:Entity {namespace: $namespace})
      -[r:RELATES_AS]-(b:Entity {namespace: $namespace})
WHERE (a.name = $subject OR b.name = $subject)
  AND ($relation_type IS NULL OR r.relation_type = $relation_type)
  AND coalesce(a.user_id, '') = $user_id
  AND coalesce(b.user_id, '') = $user_id
WITH DISTINCT startNode(r) AS s, endNode(r) AS o, r
RETURN s.display_name AS subject,
       r.relation_type AS relation_type,
       o.display_name AS object,
       coalesce(r.weight, 1.0) AS weight
ORDER BY weight DESC
LIMIT $limit
`;
const LINEAGE_PAIRS = `
MATCH (pw:PairWeight {namespace: $namespace})
WHERE ((pw.entity_a = $a AND pw.entity_b = $b)
    OR (pw.entity_a = $b AND pw.entity_b = $a))
  AND coalesce(pw.user_id, '') = $user_id
RETURN pw.document_id AS document_id, pw.weight AS weight
LIMIT $limit
`;
const LINEAGE_RELATIONS = `
MATCH (rw:RelationWeight {namespace: $namespace})
WHERE ((rw.subject = $a AND rw.object = $b)
    OR (rw.subject = $b AND rw.object = $a))
  AND ($relation_type IS NULL OR rw.relation_type = $relation_type)
  AND coalesce(rw.user_id, '') = $user_id
RETURN rw.document_id AS document_id,
       rw.relation_type AS relation_name,
       rw.weight AS weight
LIMIT $limit
`;
const ENTITIES_BY_TYPE = `
MATCH (e:Entity {namespace: $namespace})
WHERE toLower(e.entity_type) = toLower($entity_type)
  AND coalesce(e.user_id, '') = $user_id
RETURN e.name AS entity_id,
       e.display_name AS entity_name,
       e.entity_type AS entity_type
LIMIT $limit
`;

function messageOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function logError(logger: QueryLogger, message: string): void {
  if (logger.error) logger.error(message);
  else logger.warn(message);
}

function reprOf(v: unknown): string {
  if (v === null || v === undefined) return "None";
  if (typeof v === "string") return pyRepr(v);
  return String(v);
}

function notAvailable(logger: QueryLogger): never[] {
  logger.warn(NOT_AVAILABLE);
  return [];
}

/** Python truthiness for the values a row can hold. */
function truthy(v: unknown): boolean {
  if (v === null || v === undefined || v === false || v === 0 || v === "" || v === 0n) return false;
  if (Array.isArray(v) && v.length === 0) return false;
  return true;
}

/** Python int(x). */
function pyIntOf(v: unknown): number {
  if (typeof v === "bigint") return Number(v);
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "number" && Number.isFinite(v)) return Math.trunc(v);
  throw new TypeError(`int() argument must be a number, not ${reprOf(v)}`);
}

/** Python float(x). */
function pyFloatOf(v: unknown): number {
  if (typeof v === "bigint") return Number(v);
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "number") return v;
  throw new TypeError(`float() argument must be a number, not ${reprOf(v)}`);
}

/** Python max(1, int(limit)). */
function limitOf(limit: unknown): number {
  return Math.max(1, pyIntOf(limit));
}

function scopeOf(options: ScopeOptions): { ns: string; uid: string } {
  return {
    ns: options.namespace ?? "",
    uid: options.userId ?? "",
  };
}

function validateDepth(value: unknown): number {
  let depth: number;
  if (typeof value === "bigint") depth = Number(value);
  else if (typeof value === "number" && Number.isFinite(value)) depth = Math.trunc(value);
  else throw new RangeError(`maxDepth must be an integer, got ${reprOf(value)}`);
  if (!(depth >= 1 && depth <= MAX_ALLOWED_DEPTH)) {
    throw new RangeError(`maxDepth must be between 1 and ${MAX_ALLOWED_DEPTH}, got ${depth}`);
  }
  return depth;
}

/** Normalized, lowercased, deduplicated entity names (Python _normalize_entity_list). */
export function normalizeEntityList(entityNames: readonly string[] | null | undefined): string[] {
  const normalized: string[] = [];
  const seen = new Set<string>();
  for (const name of entityNames ?? []) {
    const norm = normalizeEntityName(name);
    if (!norm) continue;
    const key = pyLower(norm);
    if (seen.has(key)) continue;
    seen.add(key);
    normalized.push(key);
  }
  return normalized;
}

function toDocumentHit(row: Row): DocumentHit {
  const names = row["matched_entity_names"];
  return {
    documentId: row["document_id"] === null || row["document_id"] === undefined ? "" : String(row["document_id"]),
    documentName: truthy(row["document_name"]) ? String(row["document_name"]) : "",
    matchedEntities: pyIntOf(truthy(row["matched_entities"]) ? row["matched_entities"] : 0),
    graphScore: pyFloatOf(truthy(row["graph_score"]) ? row["graph_score"] : 0),
    matchedEntityNames: Array.isArray(names) ? [...(names as string[])] : [],
  };
}

function toRelatedEntity(row: Row): RelatedEntity {
  return {
    entityId: row["entity_id"] as string | null,
    entityName: row["entity_name"] as string | null,
    relationship: row["relationship"] as string | null,
    depth: pyIntOf(row["depth"]),
  };
}

function toRelationHit(row: Row): RelationHit {
  return {
    subject: row["subject"] as string | null,
    relationType: row["relation_type"] as string | null,
    object: row["object"] as string | null,
    weight: pyFloatOf(row["weight"]),
  };
}

function toTypedEntity(row: Row): TypedEntity {
  return {
    entityId: row["entity_id"] as string | null,
    entityName: row["entity_name"] as string | null,
    entityType: row["entity_type"] as string | null,
  };
}

function toEntityRef(row: Row): EntityRef {
  return {
    entityId: row["entity_id"] as string | null,
    entityName: row["entity_name"] as string | null,
  };
}

/** Documents connected to the given entity names, ranked by matched-entity count and summed edge weight. */
export async function findDocumentsByEntities(
  runner: CypherRunner | null,
  logger: QueryLogger,
  entityNames: readonly string[] | null | undefined,
  options: LimitOptions = {},
): Promise<DocumentHit[]> {
  const { limit = DEFAULT_LIMIT } = options;
  if (runner === null) return [];
  const wanted = normalizeEntityList(entityNames);
  if (wanted.length === 0) return [];
  const { ns, uid } = scopeOf(options);
  try {
    const rows = await runner.run(FIND_DOCUMENTS, { namespace: ns, user_id: uid, entity_names: wanted, limit: limitOf(limit) });
    return rows.map(toDocumentHit);
  } catch (e) {
    logError(logger, `Graph document lookup error: ${messageOf(e)}`);
    return [];
  }
}

/** Entities related to the given entity names by graph traversal, up to maxDepth hops (1 to 10). */
export async function searchRelatedEntities(
  runner: CypherRunner | null,
  logger: QueryLogger,
  entityNames: readonly string[] | null | undefined,
  options: RelatedOptions = {},
): Promise<RelatedEntity[]> {
  const { maxDepth = DEFAULT_MAX_DEPTH, limit = DEFAULT_RELATED_LIMIT } = options;
  const depth = validateDepth(maxDepth);
  if (runner === null) return notAvailable(logger);
  const starts = normalizeEntityList(entityNames);
  if (starts.length === 0) return [];
  const { ns, uid } = scopeOf(options);
  // The depth bound cannot be a Cypher parameter, so the validated integer goes into the query text.
  const query = SEARCH_RELATED.replace("{max_depth}", String(depth));
  try {
    const rows = await runner.run(query, { entity_names: starts, namespace: ns, user_id: uid, limit: limitOf(limit) });
    return rows.map(toRelatedEntity);
  } catch (e) {
    logError(logger, `Related-entity search error: ${messageOf(e)}`);
    return [];
  }
}

/** Typed relations involving entityName (direction: outgoing, incoming or both). */
export async function findRelations(
  runner: CypherRunner | null,
  logger: QueryLogger,
  entityName: string,
  options: RelationsOptions = {},
): Promise<RelationHit[]> {
  const { relationType = null, limit = DEFAULT_LIMIT, direction = "outgoing" } = options;
  if (direction !== "outgoing" && direction !== "incoming" && direction !== "both") {
    throw new RangeError(`direction must be "outgoing", "incoming", or "both", got ${reprOf(direction)}`);
  }
  if (runner === null) return notAvailable(logger);
  const subject = normalizeEntityName(entityName);
  if (!subject) return [];
  const { ns, uid } = scopeOf(options);
  const query = direction === "outgoing" ? FIND_RELATIONS_OUT : direction === "incoming" ? FIND_RELATIONS_IN : FIND_RELATIONS_BOTH;
  try {
    const rows = await runner.run(query, {
      subject: pyLower(subject),
      namespace: ns,
      user_id: uid,
      relation_type: relationType,
      limit: limitOf(limit),
    });
    return rows.map(toRelationHit);
  } catch (e) {
    logError(logger, `Relation search error: ${messageOf(e)}`);
    return [];
  }
}

/** Per-document contributions to the edges between entityA and entityB (either order). */
export async function findLineage(
  runner: CypherRunner | null,
  logger: QueryLogger,
  entityA: string,
  entityB: string,
  options: LineageOptions = {},
): Promise<LineageEntry[]> {
  const { relationType = null, limit = DEFAULT_LIMIT } = options;
  if (runner === null) return notAvailable(logger);
  if (!entityA || pyStrip(entityA) === "" || !entityB || pyStrip(entityB) === "") return [];
  const { ns, uid } = scopeOf(options);
  // Lowercased but not stripped or normalized, like Python.
  const aLower = pyLower(entityA);
  const bLower = pyLower(entityB);
  try {
    const lim = limitOf(limit);
    const contributions: LineageEntry[] = [];
    const pairRows = await runner.run(LINEAGE_PAIRS, { namespace: ns, user_id: uid, a: aLower, b: bLower, limit: lim });
    for (const row of pairRows) {
      contributions.push({
        documentId: row["document_id"] as string | null,
        relationType: "CO_OCCURS_WITH",
        weight: row["weight"] as number | null,
      });
    }
    const relationRows = await runner.run(LINEAGE_RELATIONS, {
      namespace: ns,
      user_id: uid,
      a: aLower,
      b: bLower,
      relation_type: relationType,
      limit: lim,
    });
    for (const row of relationRows) {
      contributions.push({
        documentId: row["document_id"] as string | null,
        relationType: "RELATES_AS",
        relationName: row["relation_name"] as string | null,
        weight: row["weight"] as number | null,
      });
    }
    return contributions.slice(0, lim);
  } catch (e) {
    logError(logger, `Lineage lookup error: ${messageOf(e)}`);
    return [];
  }
}

/** Entities of a given type, matched case-insensitively. */
export async function findEntitiesByType(
  runner: CypherRunner | null,
  logger: QueryLogger,
  entityType: string,
  options: LimitOptions = {},
): Promise<TypedEntity[]> {
  const { limit = DEFAULT_LIMIT } = options;
  if (runner === null) return notAvailable(logger);
  if (!entityType || pyStrip(entityType) === "") return [];
  const { ns, uid } = scopeOf(options);
  try {
    const rows = await runner.run(ENTITIES_BY_TYPE, { namespace: ns, user_id: uid, entity_type: entityType, limit: limitOf(limit) });
    return rows.map(toTypedEntity);
  } catch (e) {
    logError(logger, `Entity-by-type search error: ${messageOf(e)}`);
    return [];
  }
}

/** All entities linked to one document (at most 50). */
export async function documentEntities(
  runner: CypherRunner | null,
  logger: QueryLogger,
  documentId: string | number,
  options: ScopeOptions = {},
): Promise<EntityRef[]> {
  if (runner === null) return notAvailable(logger);
  const { ns, uid } = scopeOf(options);
  try {
    const rows = await runner.run(DOCUMENT_ENTITIES, { document_id: String(documentId), namespace: ns, user_id: uid });
    return rows.map(toEntityRef);
  } catch (e) {
    logError(logger, `Document entity lookup error: ${messageOf(e)}`);
    return [];
  }
}
