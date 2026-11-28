import { test } from "node:test";
import assert from "node:assert/strict";
import { compositeKey } from "../src/compositeKey.ts";
import { COMMUNITY_STATEMENTS, detectIndexCommunities, refreshIndexCommunities } from "../src/communityStore.ts";
import { GraphIndex } from "../src/graphIndex.ts";
import type { CypherRunner, Row } from "../src/queryApiRunner.ts";
import { asOfParameter, findRelations } from "../src/queries.ts";
import { RECOMPUTE_RELATES_AS, RECOMPUTE_RELATES_AS_TEMPORAL, upsertDocumentWithRetry } from "../src/upsert.ts";

// Offline tests for temporal relation edges (GraphIndex with temporal true): which statement runs when, the validity
// filter of findRelations, and the open-edge statements of the community methods. The texts and the 36 findRelations call
// shapes are compared with the real Python code in temporalParity.test.ts; live behaviour is in liveTemporal.test.ts.

interface Call {
  cypher: string;
  params: Record<string, unknown>;
}

class Recorder implements CypherRunner {
  calls: Call[] = [];
  script: (cypher: string) => Row[];

  constructor(script: (cypher: string) => Row[] = () => []) {
    this.script = script;
  }

  async run(cypher: string, params?: Record<string, unknown> | null): Promise<Row[]> {
    this.calls.push({ cypher, params: { ...(params ?? {}) } });
    return this.script(cypher).map((r) => ({ ...r }));
  }

  close(): void {}
}

const silent = { info() {}, warn() {}, error() {} };

async function make(temporal: boolean | undefined, script?: (cypher: string) => Row[]) {
  const runner = new Recorder(script);
  const index = await GraphIndex.connect(runner, { logger: silent, temporal });
  runner.calls.length = 0;
  return { index, runner };
}

const compact = (s: string): string => s.replace(/\s+/g, " ").trim();

// ---------------------------------------------------------------- the flag

test("temporal is off by default and can be switched on", async () => {
  assert.equal((await make(undefined)).index.temporal, false);
  assert.equal((await make(false)).index.temporal, false);
  assert.equal((await make(true)).index.temporal, true);
  assert.equal((await GraphIndex.connect(null, { logger: silent, temporal: true })).temporal, true);
});

test("a plain GraphIndex built without the flag is not temporal", () => {
  assert.equal(new GraphIndex(null, silent).temporal, false);
  assert.equal(new GraphIndex(null, silent, undefined).temporal, false);
});

test("a bad temporal flag is refused before anything is sent to the server", async () => {
  for (const bad of ["yes", 1, null]) {
    const runner = new Recorder();
    await assert.rejects(GraphIndex.connect(runner, { logger: silent, temporal: bad as unknown as boolean }), { name: "RangeError", message: "temporal must be a bool" });
    assert.equal(runner.calls.length, 0, "no statement may reach the server");
  }
});

test("the module functions are not temporal unless told so", async () => {
  const quiet = { info() {}, warn() {}, error() {} };
  // findRelations
  const r1 = new Recorder();
  await findRelations(r1, quiet, "Acme Corp", { namespace: "ns" });
  assert.ok(!r1.calls[0].cypher.includes("valid_to"));
  // community detection and refresh
  const r2 = new Recorder(edgesScript);
  await detectIndexCommunities(r2, quiet, { includeRelations: true });
  assert.equal(r2.calls[1].cypher, COMMUNITY_STATEMENTS.relates_edges);
  const r3 = new Recorder(edgesScript);
  await refreshIndexCommunities(r3, quiet, { includeRelations: true, summarizer: () => "s" });
  assert.ok(r3.calls.some((c) => c.cypher === COMMUNITY_STATEMENTS.relates_edges));
  assert.ok(r3.calls.some((c) => c.cypher === COMMUNITY_STATEMENTS.relations));
  assert.ok(!r3.calls.some((c) => c.cypher === COMMUNITY_STATEMENTS.relates_edges_open || c.cypher === COMMUNITY_STATEMENTS.relations_open));
  // upsert
  const r4 = new Recorder(oldRelations);
  const summary = await upsertDocumentWithRetry(r4, quiet, { sleep: async () => {}, random: () => 0 }, "d1", "T", [{ text: "Acme Corp and Globex Industries." }], { namespace: "ns" });
  assert.equal(summary.success, true);
  assert.ok(r4.calls.some((c) => c.cypher === RECOMPUTE_RELATES_AS));
  assert.ok(!r4.calls.some((c) => c.cypher === RECOMPUTE_RELATES_AS_TEMPORAL));
});

test("temporal must be a boolean", async () => {
  for (const bad of ["yes", 1, 0, null, {}]) {
    await assert.rejects(GraphIndex.connect(null, { logger: silent, temporal: bad as unknown as boolean }), {
      name: "RangeError",
      message: "temporal must be a bool",
    });
    assert.throws(() => new GraphIndex(null, silent, undefined, bad as unknown as boolean), { name: "RangeError", message: "temporal must be a bool" });
  }
});

// ---------------------------------------------------------------- upsert

const OLD = [{ subject: "acme corp", relation_type: "PARTNERED_WITH", object: "globex industries" }];
const oldRelations = (cypher: string): Row[] => (cypher.includes("RETURN rw.subject AS subject") ? OLD : []);

async function upsertCalls(temporal: boolean): Promise<Call[]> {
  const { index, runner } = await make(temporal, oldRelations);
  const summary = await index.upsertDocument("d1", "T", [{ text: "Acme Corp and Globex Industries." }], { namespace: "ns", userId: "u1" });
  assert.equal(summary.success, true, String(summary.error));
  return runner.calls;
}

test("upsert recomputes relation edges with the closing statement only on a temporal index", async () => {
  const plain = await upsertCalls(false);
  const temporal = await upsertCalls(true);
  const isRecompute = (c: Call): boolean => c.cypher === RECOMPUTE_RELATES_AS || c.cypher === RECOMPUTE_RELATES_AS_TEMPORAL;
  assert.equal(plain.filter(isRecompute).length, 1);
  assert.equal(temporal.filter(isRecompute).length, 1);
  assert.equal(plain.find(isRecompute)?.cypher, RECOMPUTE_RELATES_AS);
  assert.equal(temporal.find(isRecompute)?.cypher, RECOMPUTE_RELATES_AS_TEMPORAL);
  const expected = {
    namespace: "ns",
    subject: "acme corp",
    relation_type: "PARTNERED_WITH",
    object: "globex industries",
    user_id: "u1",
    relates_as_composite_key: compositeKey("ns", "u1", "acme corp", "PARTNERED_WITH", "globex industries"),
  };
  assert.deepEqual(plain.find(isRecompute)?.params, expected);
  assert.deepEqual(temporal.find(isRecompute)?.params, expected);
  // nothing else differs
  assert.equal(plain.length, temporal.length);
  plain.forEach((c, i) => {
    if (!isRecompute(c)) assert.deepEqual(temporal[i], c, `statement ${i}`);
  });
});

test("the temporal statement closes instead of deleting and reopens a closed edge", () => {
  const t = compact(RECOMPUTE_RELATES_AS_TEMPORAL);
  assert.ok(!t.includes("DELETE"), "a temporal index must never delete the edge");
  assert.ok(compact(RECOMPUTE_RELATES_AS).includes("DELETE r2"));
  assert.ok(t.includes("SET r.valid_from = CASE WHEN r.valid_to IS NOT NULL THEN datetime() ELSE r.valid_from END"));
  assert.ok(t.includes("SET r.valid_to = null, r.weight = total"));
  assert.ok(t.includes("SET r2.valid_to = coalesce(r2.valid_to, datetime())"));
  assert.ok(t.includes("r.valid_from = datetime()"));
  assert.ok(t.includes("r2.valid_from = datetime(), r2.weight = 0.0"));
});

// ---------------------------------------------------------------- findRelations

const DIRECTIONS = ["outgoing", "incoming", "both"] as const;
const marker = (direction: string): string => (direction === "both" ? "WITH DISTINCT" : "RETURN s.display_name");
const OPEN = "AND r.valid_to IS NULL\n";
const AS_OF = "AND (r.valid_from IS NULL OR r.valid_from <= datetime($as_of))\nAND (r.valid_to IS NULL OR r.valid_to > datetime($as_of))\n";

async function statementOf(temporal: boolean, direction: (typeof DIRECTIONS)[number], asOf?: Date | string | null): Promise<Call> {
  const { index, runner } = await make(temporal);
  await index.findRelations("Acme Corp", { namespace: "ns", direction, asOf });
  assert.equal(runner.calls.length, 1);
  return runner.calls[0];
}

test("findRelations adds the validity filter right before RETURN (or WITH DISTINCT) and nothing else", async () => {
  for (const direction of DIRECTIONS) {
    const base = await statementOf(false, direction);
    assert.ok(!base.cypher.includes("valid_to") && !base.cypher.includes("valid_from"), `${direction}: plain statement`);
    assert.ok(!("as_of" in base.params));
    const open = await statementOf(true, direction);
    const m = marker(direction);
    assert.equal(open.cypher, base.cypher.replace(m, OPEN + m), `${direction}: temporal`);
    assert.ok(!("as_of" in open.params));
    for (const temporal of [false, true]) {
      const at = await statementOf(temporal, direction, "2026-01-01T12:00:00Z");
      assert.equal(at.cypher, base.cypher.replace(m, AS_OF + m), `${direction}: as_of, temporal ${temporal}`);
    }
    assert.equal(open.cypher.split("valid_to IS NULL").length, 2, "the open clause appears once");
  }
});

test("asOf goes in as an ISO string parameter, only when given", async () => {
  const a = await statementOf(false, "outgoing", "2026-03-04T10:15:30.250000+05:30");
  assert.equal(a.params.as_of, "2026-03-04T10:15:30.250000+05:30");
  const b = await statementOf(true, "both", new Date(Date.UTC(2026, 0, 1, 12, 0, 0, 7)));
  assert.equal(b.params.as_of, "2026-01-01T12:00:00.007Z");
  const c = await statementOf(true, "incoming", null);
  assert.ok(!("as_of" in c.params));
  assert.deepEqual(Object.keys(a.params).sort(), ["as_of", "limit", "namespace", "relation_type", "subject", "user_id"]);
  assert.deepEqual(Object.keys(c.params).sort(), ["limit", "namespace", "relation_type", "subject", "user_id"]);
  assert.equal(a.params.subject, "acme corp");
  assert.equal(a.params.relation_type, null);
});

test("asOf must be a valid Date or an ISO string with a time zone", async () => {
  const bad: unknown[] = [new Date(Number.NaN), "2026-01-01", "2026-01-01T12:00:00", "yesterday", "", "2026-13-45T12:00:00Z", 1767268800000, {}];
  for (const value of bad) {
    const { index, runner } = await make(true);
    await assert.rejects(index.findRelations("Acme Corp", { asOf: value as Date }), { name: "RangeError" }, String(value));
    assert.equal(runner.calls.length, 0, "nothing may be sent");
    assert.throws(() => asOfParameter(value as Date), { name: "RangeError" });
  }
  assert.throws(() => asOfParameter(new Date(Number.NaN)), { name: "RangeError", message: "asOf must be a valid date" });
  assert.equal(asOfParameter(undefined), null);
  assert.equal(asOfParameter(null), null);
  assert.equal(asOfParameter("2026-01-01T12:00Z"), "2026-01-01T12:00Z");
  assert.equal(asOfParameter("2026-01-01T12:00:00-08:00"), "2026-01-01T12:00:00-08:00");
  const unavailable = await GraphIndex.connect(null, { logger: silent, temporal: true });
  await assert.rejects(unavailable.findRelations("Acme Corp", { asOf: "nope" }), { name: "RangeError" });
});

test("findRelations keeps its filters, direction check and empty-name shortcut", async () => {
  const { index, runner } = await make(true);
  assert.deepEqual(await index.findRelations("", { asOf: "2026-01-01T12:00:00Z" }), []);
  assert.equal(runner.calls.length, 0);
  await assert.rejects(index.findRelations("Acme", { direction: "sideways" as "both" }), { name: "RangeError" });
  const typed = await make(true);
  await typed.index.findRelations("Acme Corp", { relationType: "PARTNERED_WITH", limit: 7, userId: "u9" });
  assert.equal(typed.runner.calls[0].params.relation_type, "PARTNERED_WITH");
  assert.equal(typed.runner.calls[0].params.limit, 7);
  assert.equal(typed.runner.calls[0].params.user_id, "u9");
});

// ---------------------------------------------------------------- communities

test("the open-edge community statements are the plain ones plus the open filter", () => {
  const s = COMMUNITY_STATEMENTS;
  assert.equal(s.relates_edges_open, s.relates_edges.replace("RETURN a.name AS a", "AND r.valid_to IS NULL\nRETURN a.name AS a"));
  assert.equal(s.relations_open, s.relations.replace("RETURN s.display_name AS subject", "AND r.valid_to IS NULL\nRETURN s.display_name AS subject"));
  assert.equal(s.relates_edges_open.split("valid_to IS NULL").length, 2);
  assert.equal(s.relations_open.split("valid_to IS NULL").length, 2);
  assert.notEqual(s.relates_edges_open, s.relates_edges);
  assert.notEqual(s.relations_open, s.relations);
});

const edgesScript = (cypher: string): Row[] => (cypher.includes("CO_OCCURS_WITH") ? [{ a: "a", b: "b", weight: 1.0 }] : []);

test("detectCommunities reads open relation edges only on a temporal index", async () => {
  for (const temporal of [false, true]) {
    const { index, runner } = await make(temporal, edgesScript);
    const found = await index.detectCommunities({ includeRelations: true });
    assert.equal(found.length, 1);
    assert.equal(runner.calls.length, 2);
    assert.equal(runner.calls[0].cypher, COMMUNITY_STATEMENTS.co_occurs_edges);
    assert.equal(runner.calls[1].cypher, temporal ? COMMUNITY_STATEMENTS.relates_edges_open : COMMUNITY_STATEMENTS.relates_edges);
    const without = await make(temporal, edgesScript);
    await without.index.detectCommunities({});
    assert.equal(without.runner.calls.length, 1);
    assert.equal(without.runner.calls[0].cypher, COMMUNITY_STATEMENTS.co_occurs_edges);
  }
});

test("refreshCommunities reads open edges and summarizes with open relations only on a temporal index", async () => {
  for (const temporal of [false, true]) {
    const { index, runner } = await make(temporal, edgesScript);
    const result = await index.refreshCommunities({ includeRelations: true, summarizer: () => "summary" });
    assert.equal(result.success, true, String(result.error));
    assert.equal(result.summarized, 1);
    const edgeCall = runner.calls.filter((c) => c.cypher === COMMUNITY_STATEMENTS.relates_edges || c.cypher === COMMUNITY_STATEMENTS.relates_edges_open);
    assert.equal(edgeCall.length, 1);
    assert.equal(edgeCall[0].cypher, temporal ? COMMUNITY_STATEMENTS.relates_edges_open : COMMUNITY_STATEMENTS.relates_edges);
    const relationCall = runner.calls.filter((c) => c.cypher === COMMUNITY_STATEMENTS.relations || c.cypher === COMMUNITY_STATEMENTS.relations_open);
    assert.equal(relationCall.length, 1);
    assert.equal(relationCall[0].cypher, temporal ? COMMUNITY_STATEMENTS.relations_open : COMMUNITY_STATEMENTS.relations);
  }
});
