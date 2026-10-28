import { compositeKey } from "./compositeKey.ts";
import { extractEntityCandidates } from "./entities.ts";
import { GraphTransientException } from "./errors.ts";
import { neo4jFloat } from "./neo4jJson.ts";
import { cpCompare, pyLower, pyStrip } from "./pyText.ts";
import type { CypherRunner } from "./queryApiRunner.ts";

export const DEFAULT_MAX_ENTITIES = 80;
export const DEFAULT_MAX_PAIRS = 150;
export const MAX_ATTEMPTS = 3;
export const NOT_AVAILABLE = "Neo4j driver not available";
const CHUNK_ENTITY_LIMIT = 12;

export interface UpsertChunk {
  text?: unknown;
  [key: string]: unknown;
}

export interface UpsertOptions {
  namespace?: string | null;
  userId?: string | null;
  maxEntities?: number;
  maxPairs?: number;
  domainTerms?: readonly string[] | null;
}

export interface UpsertSummary {
  success: boolean;
  documentId: string;
  entitiesIndexed: number;
  relationshipsIndexed: number;
  relationsIndexed: number;
  error: string | null;
}

export interface UpsertLogger {
  warn(message: string): void;
  error?(message: string): void;
}

export interface UpsertTiming {
  sleep(seconds: number): Promise<void>;
  random(): number;
}

const MERGE_DOCUMENT = `
MERGE (d:Document {composite_key: $composite_key})
ON CREATE SET d.created_at = datetime(), d.id = $document_id,
    d.namespace = $namespace, d.user_id = $user_id
SET d.title = $title
`;
const DELETE_CONTAINS = `
MATCH (d:Document {id: $document_id, namespace: $namespace, user_id: $user_id})
      -[r:CONTAINS]->()
DELETE r
`;
const MERGE_ENTITY = `
MATCH (d:Document {composite_key: $document_composite_key})
MERGE (e:Entity {composite_key: $entity_composite_key})
ON CREATE SET e.display_name = $name, e.name = $name_lower,
    e.namespace = $namespace, e.user_id = $user_id
SET e.entity_type = coalesce(NULLIF($entity_type, "UNKNOWN"), e.entity_type, "UNKNOWN")
MERGE (d)-[r:CONTAINS]->(e)
SET r.weight = $weight
`;
const OLD_PAIRS = `
MATCH (pw:PairWeight {namespace: $namespace, document_id: $document_id, user_id: $user_id})
RETURN pw.entity_a AS a, pw.entity_b AS b
`;
const DELETE_PAIRWEIGHTS = `
MATCH (pw:PairWeight {namespace: $namespace, document_id: $document_id, user_id: $user_id})
DELETE pw
`;
const MERGE_PAIRWEIGHT = `
MERGE (pw:PairWeight {composite_key: $composite_key})
ON CREATE SET pw.namespace = $namespace, pw.document_id = $document_id,
    pw.entity_a = $a, pw.entity_b = $b, pw.user_id = $user_id
SET pw.weight = $weight
`;
const RECOMPUTE_CO_OCCURS = `
MATCH (pw:PairWeight {namespace: $namespace, entity_a: $a, entity_b: $b, user_id: $user_id})
WITH sum(pw.weight) AS total
MATCH (ea:Entity {name: $a, namespace: $namespace, user_id: $user_id})
MATCH (eb:Entity {name: $b, namespace: $namespace, user_id: $user_id})
FOREACH (_ IN CASE WHEN total > 0 THEN [1] ELSE [] END |
    MERGE (ea)-[r:CO_OCCURS_WITH {composite_key: $co_occurs_composite_key}]-(eb)
    ON CREATE SET r.namespace = $namespace, r.user_id = $user_id
    SET r.weight = total
)
FOREACH (_ IN CASE WHEN total = 0 THEN [1] ELSE [] END |
    MERGE (ea)-[r2:CO_OCCURS_WITH {composite_key: $co_occurs_composite_key}]-(eb)
    DELETE r2
)
`;
const OLD_RELATIONS = `
MATCH (rw:RelationWeight {namespace: $namespace, document_id: $document_id, user_id: $user_id})
RETURN rw.subject AS subject, rw.relation_type AS relation_type, rw.object AS object
`;
const DELETE_RELATIONWEIGHTS = `
MATCH (rw:RelationWeight {namespace: $namespace, document_id: $document_id, user_id: $user_id})
DELETE rw
`;
const RECOMPUTE_RELATES_AS = `
MATCH (rw:RelationWeight {namespace: $namespace, subject: $subject, relation_type: $relation_type, object: $object, user_id: $user_id})
WITH sum(rw.weight) AS total
MATCH (es:Entity {name: $subject, namespace: $namespace, user_id: $user_id})
MATCH (eo:Entity {name: $object, namespace: $namespace, user_id: $user_id})
FOREACH (_ IN CASE WHEN total > 0 THEN [1] ELSE [] END |
    MERGE (es)-[r:RELATES_AS {composite_key: $relates_as_composite_key}]->(eo)
    ON CREATE SET r.relation_type = $relation_type, r.namespace = $namespace, r.user_id = $user_id
    SET r.weight = total
)
FOREACH (_ IN CASE WHEN total = 0 THEN [1] ELSE [] END |
    MERGE (es)-[r2:RELATES_AS {composite_key: $relates_as_composite_key}]->(eo)
    DELETE r2
)
`;

/** Python Counter.most_common(n): highest counts first, ties in first-seen order. n <= 0 gives nothing. */
export function mostCommon<T>(entries: ReadonlyArray<readonly [T, number]>, n: number): Array<[T, number]> {
  if (!(n > 0)) return [];
  return entries
    .map((entry, index) => ({ entry, index }))
    .sort((x, y) => y.entry[1] - x.entry[1] || x.index - y.index)
    .slice(0, n)
    .map((x) => [x.entry[0], x.entry[1]] as [T, number]);
}

/** Python f"{x:.2f}": rounds half to even on the exact binary value (toFixed rounds exact ties up). */
export function formatSeconds(x: number): string {
  const s = x.toFixed(30);
  const dot = s.indexOf(".");
  const intPart = s.slice(0, dot);
  const frac = s.slice(dot + 1);
  const head = frac.slice(0, 2);
  const rest = frac.slice(2);
  let up = rest[0] >= "5";
  if (/^50*$/.test(rest)) up = "13579".includes(head[1]);
  const scaled = BigInt(intPart + head) + (up ? 1n : 0n);
  const digits = scaled.toString().padStart(3, "0");
  return digits.slice(0, -2) + "." + digits.slice(-2);
}

function emptySummary(documentId: string): UpsertSummary {
  return {
    success: false,
    documentId: String(documentId),
    entitiesIndexed: 0,
    relationshipsIndexed: 0,
    relationsIndexed: 0,
    error: null,
  };
}

function messageOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function logError(logger: UpsertLogger, message: string): void {
  if (logger.error) logger.error(message);
  else logger.warn(message);
}

interface PairEntry {
  a: string;
  b: string;
  count: number;
}

async function upsertOnce(
  runner: CypherRunner | null,
  logger: UpsertLogger,
  documentId: string,
  title: string | null | undefined,
  chunks: readonly UpsertChunk[] | null | undefined,
  options: UpsertOptions,
): Promise<UpsertSummary> {
  const summary = emptySummary(documentId);
  if (runner === null) {
    summary.error = NOT_AVAILABLE;
    return summary;
  }
  const maxEntities = options.maxEntities ?? DEFAULT_MAX_ENTITIES;
  const maxPairs = options.maxPairs ?? DEFAULT_MAX_PAIRS;
  const domainTerms = options.domainTerms ?? null;
  const entityCounter = new Map<string, number>();
  const pairCounter = new Map<string, PairEntry>();
  const entityTypeMap = new Map<string, string>();

  for (const chunk of chunks ?? []) {
    const raw = chunk?.text;
    const text = typeof raw === "string" ? pyStrip(raw) : "";
    if (!text) continue;
    const names = extractEntityCandidates(text, CHUNK_ENTITY_LIMIT, domainTerms);
    if (names.length === 0) continue;
    const unique: string[] = [];
    const seenLocal = new Set<string>();
    for (const name of names) {
      const key = pyLower(name);
      if (seenLocal.has(key)) continue;
      seenLocal.add(key);
      unique.push(name);
      entityCounter.set(name, (entityCounter.get(name) ?? 0) + 1);
      if (!entityTypeMap.has(key)) entityTypeMap.set(key, "UNKNOWN");
    }
    for (let i = 0; i < unique.length; i++) {
      for (let j = i + 1; j < unique.length; j++) {
        const a = unique[i];
        const b = unique[j];
        if (a === b) continue;
        const swap = cpCompare(pyLower(b), pyLower(a)) < 0;
        const first = swap ? b : a;
        const second = swap ? a : b;
        const pairKey = JSON.stringify([first, second]);
        const existing = pairCounter.get(pairKey);
        if (existing) existing.count += 1;
        else pairCounter.set(pairKey, { a: first, b: second, count: 1 });
      }
    }
  }

  const topEntities = mostCommon([...entityCounter.entries()], maxEntities);
  const topPairs = mostCommon([...pairCounter.values()].map((p) => [p, p.count] as const), maxPairs);
  const ns = options.namespace ?? "";
  const uid = options.userId ?? "";
  const docId = String(documentId);

  try {
    const docKey = compositeKey(ns, uid, docId);
    await runner.run(MERGE_DOCUMENT, { composite_key: docKey, document_id: docId, namespace: ns, user_id: uid, title: title ?? "" });
    // The document's CONTAINS edges are rebuilt from scratch, so re-running is idempotent and stale links go away.
    await runner.run(DELETE_CONTAINS, { document_id: docId, namespace: ns, user_id: uid });

    for (const [name, weight] of topEntities) {
      const entityType = entityTypeMap.get(pyLower(name)) ?? "UNKNOWN";
      await runner.run(MERGE_ENTITY, {
        document_composite_key: docKey,
        entity_composite_key: compositeKey(ns, uid, pyLower(name)),
        namespace: ns,
        user_id: uid,
        name_lower: pyLower(name),
        name,
        entity_type: entityType,
        weight: neo4jFloat(weight),
      });
      summary.entitiesIndexed += 1;
    }

    // Per-document pair contributions are separate PairWeight nodes; the shared CO_OCCURS_WITH weight is their sum.
    const oldPairRows = await runner.run(OLD_PAIRS, { namespace: ns, document_id: docId, user_id: uid });
    const oldPairs = new Map<string, [string, string]>();
    for (const row of oldPairRows) {
      const pair: [string, string] = [String(row["a"]), String(row["b"])];
      oldPairs.set(JSON.stringify(pair), pair);
    }
    await runner.run(DELETE_PAIRWEIGHTS, { namespace: ns, document_id: docId, user_id: uid });
    const currentPairs = new Map<string, [string, string]>();
    for (const [pair, count] of topPairs) {
      const aLower = pyLower(pair.a);
      const bLower = pyLower(pair.b);
      const lowered: [string, string] = [aLower, bLower];
      currentPairs.set(JSON.stringify(lowered), lowered);
      await runner.run(MERGE_PAIRWEIGHT, {
        composite_key: compositeKey(ns, uid, docId, aLower, bLower),
        namespace: ns,
        document_id: docId,
        a: aLower,
        b: bLower,
        user_id: uid,
        weight: neo4jFloat(count),
      });
    }
    for (const [a, b] of new Map([...oldPairs, ...currentPairs]).values()) {
      const [aCanon, bCanon] = cpCompare(a, b) <= 0 ? [a, b] : [b, a];
      await runner.run(RECOMPUTE_CO_OCCURS, { namespace: ns, a, b, user_id: uid, co_occurs_composite_key: compositeKey(ns, uid, aCanon, bCanon) });
      summary.relationshipsIndexed += 1;
    }

    // Same bookkeeping for relations. Relation extraction is not ported yet, so only old contributions are recomputed.
    const oldRelationRows = await runner.run(OLD_RELATIONS, { namespace: ns, document_id: docId, user_id: uid });
    const oldRelations = new Map<string, [string, string, string]>();
    for (const row of oldRelationRows) {
      const t: [string, string, string] = [String(row["subject"]), String(row["relation_type"]), String(row["object"])];
      oldRelations.set(JSON.stringify(t), t);
    }
    await runner.run(DELETE_RELATIONWEIGHTS, { namespace: ns, document_id: docId, user_id: uid });
    for (const [subject, relationType, obj] of oldRelations.values()) {
      await runner.run(RECOMPUTE_RELATES_AS, {
        namespace: ns,
        subject,
        relation_type: relationType,
        object: obj,
        user_id: uid,
        relates_as_composite_key: compositeKey(ns, uid, subject, relationType, obj),
      });
      summary.relationsIndexed += 1;
    }

    summary.success = true;
    return summary;
  } catch (e) {
    if (e instanceof GraphTransientException) throw e;
    const message = messageOf(e);
    logError(logger, `Graph upsert error: ${message}`);
    summary.error = message;
    return summary;
  }
}

/** Up to MAX_ATTEMPTS tries; only transient Neo4j errors are retried, with exponential backoff plus jitter. */
export async function upsertDocumentWithRetry(
  runner: CypherRunner | null,
  logger: UpsertLogger,
  timing: UpsertTiming,
  documentId: string,
  title: string | null | undefined,
  chunks: readonly UpsertChunk[] | null | undefined,
  options: UpsertOptions,
): Promise<UpsertSummary> {
  let summary = emptySummary(documentId);
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    try {
      summary = await upsertOnce(runner, logger, documentId, title, chunks, options);
      break;
    } catch (e) {
      if (!(e instanceof GraphTransientException)) throw e;
      const message = messageOf(e);
      if (attempt === MAX_ATTEMPTS - 1) {
        logError(logger, `Graph upsert error: transient Neo4j error persisted after ${MAX_ATTEMPTS} attempts: ${message}`);
        summary.error = `Transient Neo4j error persisted after ${MAX_ATTEMPTS} attempts: ${message}`;
        break;
      }
      const backoff = 0.1 * 2 ** attempt + 0.05 * timing.random();
      logger.warn(`Transient Neo4j error on attempt ${attempt + 1}/${MAX_ATTEMPTS}, retrying in ${formatSeconds(backoff)}s: ${message}`);
      await timing.sleep(backoff);
    }
  }
  return summary;
}
