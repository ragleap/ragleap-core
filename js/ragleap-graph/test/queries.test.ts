import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { GraphException } from "../src/errors.ts";
import { GraphIndex } from "../src/graphIndex.ts";
import { pyRepr } from "../src/pyText.ts";
import { MAX_ALLOWED_DEPTH, normalizeEntityList } from "../src/queries.ts";
import type { CypherRunner, Row } from "../src/queryApiRunner.ts";

interface Call {
  cypher: string;
  params: Record<string, unknown>;
}

class ScriptRunner implements CypherRunner {
  calls: Call[] = [];
  rows: Record<string, Row[]>;
  failWith: Error | null = null;

  constructor(rows: Record<string, Row[]> = {}) {
    this.rows = rows;
  }

  async run(cypher: string, params?: Record<string, unknown> | null): Promise<Row[]> {
    this.calls.push({ cypher: cypher.replace(/\s+/g, ""), params: { ...(params ?? {}) } });
    if (this.failWith !== null) throw this.failWith;
    const key = cypher.includes("PairWeight") ? "pair" : cypher.includes("RelationWeight") ? "relation" : "default";
    return (this.rows[key] ?? []).map((r) => ({ ...r }));
  }

  close(): void {}
}

function recorder(): { logger: { info(m: string): void; warn(m: string): void; error(m: string): void }; warns: string[]; errors: string[] } {
  const warns: string[] = [];
  const errors: string[] = [];
  return {
    logger: {
      info() {},
      warn(m: string) {
        warns.push(m);
      },
      error(m: string) {
        errors.push(m);
      },
    },
    warns,
    errors,
  };
}

async function make(rows: Record<string, Row[]> = {}) {
  const runner = new ScriptRunner(rows);
  const rec = recorder();
  const idx = await GraphIndex.connect(runner, { logger: rec.logger });
  runner.calls.length = 0;
  rec.warns.length = 0;
  rec.errors.length = 0;
  return { idx, runner, rec };
}

async function makeUnavailable() {
  const rec = recorder();
  const idx = await GraphIndex.connect(null, { logger: rec.logger });
  return { idx, rec };
}

// ---- unit tests (no fixtures needed) ----

test("normalizeEntityList: normalizes, lowercases, deduplicates, drops short and blank names", () => {
  assert.deepStrictEqual(normalizeEntityList(["Acme Corp", "acme corp", "ACME CORP", "  ", "ab", "Globex"]), ["acme corp", "globex"]);
  assert.deepStrictEqual(normalizeEntityList(null), []);
  assert.deepStrictEqual(normalizeEntityList([]), []);
});

test("pyRepr follows Python repr for common strings", () => {
  assert.equal(pyRepr("sideways"), "'sideways'");
  assert.equal(pyRepr("it's"), `"it's"`);
  assert.equal(pyRepr(`a"b'c`), `'a"b\\'c'`);
  assert.equal(pyRepr("a\nb"), "'a\\nb'");
  assert.equal(pyRepr("a\rb\tc"), "'a\\rb\\tc'");
  assert.equal(pyRepr("\x01"), "'\\x01'");
  assert.equal(pyRepr("\x7f"), "'\\x7f'");
  assert.equal(pyRepr("back\\slash"), "'back\\\\slash'");
});

test("defaults: limit 25, related limit 10, depth 2, relation type null, outgoing direction", async () => {
  const { idx, runner } = await make();
  await idx.findDocumentsByEntities(["Acme Corp"]);
  assert.deepStrictEqual(runner.calls[0].params, { namespace: "", user_id: "", entity_names: ["acme corp"], limit: 25 });
  runner.calls.length = 0;
  await idx.searchRelatedEntities(["Acme Corp"]);
  assert.deepStrictEqual(runner.calls[0].params, { entity_names: ["acme corp"], namespace: "", user_id: "", limit: 10 });
  assert.ok(runner.calls[0].cypher.includes("[*1..2]"));
  runner.calls.length = 0;
  await idx.findRelations("Acme Corp");
  assert.deepStrictEqual(runner.calls[0].params, { subject: "acme corp", namespace: "", user_id: "", relation_type: null, limit: 25 });
  assert.ok(runner.calls[0].cypher.includes("-[r:RELATES_AS]->(o:Entity"));
  runner.calls.length = 0;
  await idx.findLineage("Acme Corp", "Globex Corp");
  assert.equal(runner.calls.length, 2);
  for (const c of runner.calls) assert.equal(c.params["limit"], 25);
  assert.equal(runner.calls[1].params["relation_type"], null);
  runner.calls.length = 0;
  await idx.findEntitiesByType("ORG");
  assert.deepStrictEqual(runner.calls[0].params, { namespace: "", user_id: "", entity_type: "ORG", limit: 25 });
});

test("limits are truncated to integers and never below 1", async () => {
  const { idx, runner } = await make();
  await idx.findEntitiesByType("ORG", { limit: 2.9 });
  await idx.findEntitiesByType("ORG", { limit: -5 });
  await idx.findEntitiesByType("ORG", { limit: 0 });
  assert.deepStrictEqual(runner.calls.map((c) => c.params["limit"]), [2, 1, 1]);
});

test("maxDepth: truncated, bounded to 1..10, written into the query text", async () => {
  assert.equal(MAX_ALLOWED_DEPTH, 10);
  const { idx, runner } = await make();
  await idx.searchRelatedEntities(["Acme Corp"], { maxDepth: 2.7 });
  assert.ok(runner.calls[0].cypher.includes("[*1..2]"));
  await idx.searchRelatedEntities(["Acme Corp"], { maxDepth: 10 });
  assert.ok(runner.calls[1].cypher.includes("[*1..10]"));
  await assert.rejects(() => idx.searchRelatedEntities(["Acme Corp"], { maxDepth: 11 }), (e: unknown) => e instanceof RangeError && e.message === "maxDepth must be between 1 and 10, got 11");
  await assert.rejects(() => idx.searchRelatedEntities(["Acme Corp"], { maxDepth: 0 }), RangeError);
  await assert.rejects(() => idx.searchRelatedEntities(["Acme Corp"], { maxDepth: NaN }), (e: unknown) => e instanceof RangeError && e.message === "maxDepth must be an integer, got NaN");
  await assert.rejects(() => idx.searchRelatedEntities(["Acme Corp"], { maxDepth: null as unknown as number }), (e: unknown) => e instanceof RangeError && e.message === "maxDepth must be an integer, got None");
  assert.equal(runner.calls.length, 2);
});

test("an invalid direction throws with the Python message", async () => {
  const { idx } = await make();
  await assert.rejects(
    () => idx.findRelations("Acme Corp", { direction: "sideways" as "both" }),
    (e: unknown) => e instanceof RangeError && e.message === `direction must be "outgoing", "incoming", or "both", got 'sideways'`,
  );
});

test("without a connection every method returns [] and warns once (except findDocumentsByEntities), bad arguments still throw", async () => {
  const { idx, rec } = await makeUnavailable();
  assert.deepStrictEqual(await idx.findDocumentsByEntities(["Acme Corp"]), []);
  assert.deepStrictEqual(rec.warns, []);
  assert.deepStrictEqual(await idx.searchRelatedEntities(["Acme Corp"]), []);
  assert.deepStrictEqual(await idx.findRelations("Acme Corp"), []);
  assert.deepStrictEqual(await idx.findLineage("Acme Corp", "Globex Corp"), []);
  assert.deepStrictEqual(await idx.findEntitiesByType("ORG"), []);
  assert.deepStrictEqual(await idx.documentEntities("d1"), []);
  assert.deepStrictEqual(rec.warns, Array(5).fill("Neo4j driver not available"));
  await assert.rejects(() => idx.searchRelatedEntities(["Acme Corp"], { maxDepth: 0 }), RangeError);
  await assert.rejects(() => idx.findRelations("Acme Corp", { direction: "x" as "both" }), RangeError);
});

test("a failing server gives [] and one error message per method", async () => {
  const { idx, runner, rec } = await make();
  runner.failWith = new GraphException("boom");
  const cases: Array<[string, () => Promise<unknown[]>]> = [
    ["Graph document lookup error: boom", () => idx.findDocumentsByEntities(["Acme Corp"])],
    ["Related-entity search error: boom", () => idx.searchRelatedEntities(["Acme Corp"])],
    ["Relation search error: boom", () => idx.findRelations("Acme Corp")],
    ["Document entity lookup error: boom", () => idx.documentEntities("d1")],
    ["Entity-by-type search error: boom", () => idx.findEntitiesByType("ORG")],
    ["Lineage lookup error: boom", () => idx.findLineage("Acme Corp", "Globex Corp")],
  ];
  for (const [message, call] of cases) {
    rec.errors.length = 0;
    assert.deepStrictEqual(await call(), []);
    assert.deepStrictEqual(rec.errors, [message]);
  }
});

test("a relation row without a weight makes the whole lookup return [] (Python float(None) fails)", async () => {
  const { idx, rec } = await make({ default: [{ subject: "Acme Corp", relation_type: "R", object: "Globex Corp", weight: null }] });
  assert.deepStrictEqual(await idx.findRelations("Acme Corp"), []);
  assert.equal(rec.errors.length, 1);
  assert.ok(rec.errors[0].startsWith("Relation search error: "));
});

test("document rows with missing values get Python's defaults", async () => {
  const { idx } = await make({
    default: [{ document_id: null, document_name: null, matched_entities: null, graph_score: null, matched_entity_names: null }],
  });
  assert.deepStrictEqual(await idx.findDocumentsByEntities(["Acme Corp"]), [
    { documentId: "", documentName: "", matchedEntities: 0, graphScore: 0, matchedEntityNames: [] },
  ]);
});

test("whitespace-only arguments return [] without a query", async () => {
  const { idx, runner } = await make();
  assert.deepStrictEqual(await idx.findLineage("Acme Corp", "   "), []);
  assert.deepStrictEqual(await idx.findLineage("\u00a0", "Globex"), []);
  assert.deepStrictEqual(await idx.findEntitiesByType(" \t "), []);
  assert.equal(runner.calls.length, 0);
});

test("a numeric document id is sent as text", async () => {
  const { idx, runner } = await make();
  await idx.documentEntities(5);
  assert.deepStrictEqual(runner.calls[0].params, { document_id: "5", namespace: "", user_id: "" });
});

test("findLineage lowercases both names but does not strip or normalize them (like Python)", async () => {
  const { idx, runner } = await make();
  await idx.findLineage("  ACME Corp ", "  Globex  ");
  assert.equal(runner.calls[0].params["a"], "  acme corp ");
  assert.equal(runner.calls[0].params["b"], "  globex  ");
});

// ---- parity with the real Python (recordings from java/ragleap-graph/parity/gen_fixtures_queries.py) ----

const dir = process.env.RAGLEAP_GRAPH_PARITY_DIR;
const skip = dir ? false : "RAGLEAP_GRAPH_PARITY_DIR not set";

function camel(s: string): string {
  return s.replace(/_([a-z])/g, (_m: string, c: string) => c.toUpperCase());
}

function camelKeys(v: any): any {
  if (Array.isArray(v)) return v.map(camelKeys);
  if (v !== null && typeof v === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, x] of Object.entries(v)) out[camel(k)] = camelKeys(x);
    return out;
  }
  return v;
}

async function invoke(idx: GraphIndex, method: string, a: any): Promise<unknown> {
  const scope = { namespace: a.namespace, userId: a.user_id };
  switch (method) {
    case "search_related_entities":
      return idx.searchRelatedEntities(a.entity_names, { ...scope, maxDepth: a.max_depth, limit: a.limit });
    case "find_relations":
      return idx.findRelations(a.entity_name, { ...scope, relationType: a.relation_type, limit: a.limit, direction: a.direction });
    case "find_lineage":
      return idx.findLineage(a.entity_a, a.entity_b, { ...scope, relationType: a.relation_type, limit: a.limit });
    case "find_entities_by_type":
      return idx.findEntitiesByType(a.entity_type, { ...scope, limit: a.limit });
    case "find_documents_by_entities":
      return idx.findDocumentsByEntities(a.entity_names, { ...scope, limit: a.limit });
    case "document_entities":
      return idx.documentEntities(a.document_id, scope);
    default:
      throw new Error("unknown method " + method);
  }
}

test("parity: all six query methods against recordings of the real Python code", { skip }, async () => {
  const cases: any[] = JSON.parse(readFileSync(join(dir as string, "query_cases.json"), "utf8"));
  assert.ok(cases.length > 0, "no cases");
  const bad: unknown[] = [];
  const perMethod: Record<string, number> = {};
  for (const c of cases) {
    perMethod[c.method] = (perMethod[c.method] ?? 0) + 1;
    const runner = new ScriptRunner(c.rows ?? {});
    const rec = recorder();
    const idx = await GraphIndex.connect(c.no_driver ? null : runner, { logger: rec.logger });
    runner.calls.length = 0;
    let got: unknown = null;
    let raised: string | null = null;
    try {
      got = await invoke(idx, c.method, c.args);
    } catch (e) {
      raised = (e as Error).message.replace("maxDepth", "max_depth");
    }
    const wantCalls = c.calls.map((x: any) => ({ cypher: x.cypher, params: x.params }));
    try {
      assert.equal(raised, c.expected.raises);
      if (c.expected.raises === null) assert.deepStrictEqual(got, camelKeys(c.expected.result));
      assert.deepStrictEqual(runner.calls, wantCalls);
    } catch (e) {
      bad.push({ name: c.name, error: String((e as Error).message).slice(0, 600) });
    }
  }
  console.log(`queries: ${cases.length} cases, ${bad.length} differ; per method ${JSON.stringify(perMethod)}`);
  assert.equal(bad.length, 0, JSON.stringify(bad.slice(0, 2)).slice(0, 3000));
});
