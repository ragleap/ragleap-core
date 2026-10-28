import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { GraphException, GraphTransientException } from "../src/errors.ts";
import { GraphIndex } from "../src/graphIndex.ts";
import type { GraphLogger } from "../src/graphIndex.ts";
import { Neo4jFloat } from "../src/neo4jJson.ts";
import { cpCompare, pyStrip } from "../src/pyText.ts";
import type { CypherRunner, Row } from "../src/queryApiRunner.ts";
import { DEFAULT_MAX_ENTITIES, DEFAULT_MAX_PAIRS, MAX_ATTEMPTS, NOT_AVAILABLE, formatSeconds, mostCommon } from "../src/upsert.ts";

interface Call {
  cypher: string;
  params: string;
}

// Canonical text of a parameter value, telling FLOAT (Neo4jFloat) from INTEGER (plain number).
function canon(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (v instanceof Neo4jFloat) return "f" + String(v.value);
  if (typeof v === "bigint") return "i" + v.toString();
  if (typeof v === "number") return "i" + String(v);
  if (typeof v === "boolean") return "b" + String(v);
  if (typeof v === "string") return "s" + v.length + ":" + v;
  if (Array.isArray(v)) return "[" + v.map(canon).join(",") + "]";
  if (typeof v === "object") {
    const keys = Object.keys(v as object).sort();
    return "{" + keys.map((k) => k.length + ":" + k + "=" + canon((v as Record<string, unknown>)[k])).join(",") + "}";
  }
  throw new Error("canon: unsupported value");
}

// Python iterates a set when recomputing edges, so statements in each FOREACH block come in arbitrary order.
function normalize(calls: Call[]): Call[] {
  const out: Call[] = [];
  let block: Call[] = [];
  const flush = (): void => {
    block.sort((x, y) => (x.cypher + x.params < y.cypher + y.params ? -1 : x.cypher + x.params > y.cypher + y.params ? 1 : 0));
    out.push(...block);
    block = [];
  };
  for (const c of calls) {
    if (c.cypher.includes("FOREACH")) block.push(c);
    else {
      flush();
      out.push(c);
    }
  }
  flush();
  return out;
}

class RecordingRunner implements CypherRunner {
  calls: Call[] = [];
  oldPairs: any[];
  oldRelations: any[];
  fail: any;
  attempt = 0;
  n = 0;

  constructor(oldPairs: any[], oldRelations: any[], fail: any) {
    this.oldPairs = oldPairs;
    this.oldRelations = oldRelations;
    this.fail = fail;
  }

  reset(): void {
    this.calls = [];
    this.attempt = 0;
    this.n = 0;
  }

  async run(cypher: string, params?: Record<string, unknown> | null): Promise<Row[]> {
    if (cypher.includes("MERGE (d:Document {composite_key: $composite_key})")) {
      this.attempt++;
      this.n = 0;
    }
    this.n++;
    this.calls.push({ cypher: cypher.replace(/\s+/g, ""), params: canon(params ?? {}) });
    const f = this.fail;
    if (f && this.attempt >= 1 && this.attempt <= f.attempts_failing && this.n === f.at_call) {
      throw f.kind === "transient" ? new GraphTransientException("boom: transient") : new GraphException("boom: hard");
    }
    if (cypher.includes("RETURN pw.entity_a AS a")) return this.oldPairs.map((p: any) => ({ a: p[0], b: p[1] }));
    if (cypher.includes("RETURN rw.subject AS subject")) {
      return this.oldRelations.map((p: any) => ({ subject: p[0], relation_type: p[1], object: p[2] }));
    }
    return [];
  }

  close(): void {}
}

const QUIET: GraphLogger = { info() {}, warn() {} };

// ---- unit tests (no fixtures needed) ----

test("constants match the Python defaults", () => {
  assert.equal(DEFAULT_MAX_ENTITIES, 80);
  assert.equal(DEFAULT_MAX_PAIRS, 150);
  assert.equal(MAX_ATTEMPTS, 3);
  assert.equal(NOT_AVAILABLE, "Neo4j driver not available");
});

test("formatSeconds rounds like Python (half to even on exact ties)", () => {
  assert.equal(formatSeconds(0.125), "0.12");
  assert.equal(formatSeconds(0.375), "0.38");
  assert.equal(formatSeconds(0.625), "0.62");
  assert.equal(formatSeconds(0.875), "0.88");
  assert.equal(formatSeconds(0.225), "0.23");
  assert.equal(formatSeconds(0.1), "0.10");
  assert.equal(formatSeconds(0.12), "0.12");
});

test("mostCommon keeps first-seen order for ties and gives nothing for n <= 0", () => {
  const entries: Array<[string, number]> = [["a", 1], ["b", 2], ["c", 2], ["d", 1]];
  assert.deepStrictEqual(mostCommon(entries, 3), [["b", 2], ["c", 2], ["a", 1]]);
  assert.deepStrictEqual(mostCommon(entries, 10).map((x) => x[0]), ["b", "c", "a", "d"]);
  assert.deepStrictEqual(mostCommon(entries, 0), []);
});

test("cpCompare orders by code point, not by UTF-16 unit", () => {
  assert.ok("\u{1F600}" < "\uff5e");
  assert.equal(cpCompare("\u{1F600}", "\uff5e"), 1);
  assert.equal(cpCompare("\uff5e", "\u{1F600}"), -1);
  assert.equal(cpCompare("ab", "ab"), 0);
  assert.equal(cpCompare("a", "ab"), -1);
  assert.equal(cpCompare("ab", "a"), 1);
});

test("pyStrip removes Unicode whitespace at both ends", () => {
  assert.equal(pyStrip(" \t\n\u00a0\u3000x y\u2003\x1c"), "x y");
  assert.equal(pyStrip("   "), "");
});

test("an unavailable index returns the not-available summary instead of throwing", async () => {
  const idx = await GraphIndex.connect(null, { logger: QUIET });
  const summary = await idx.upsertDocument("d1", "T", [{ text: "Acme Corp and Globex Industries." }]);
  assert.deepStrictEqual({ ...summary }, {
    success: false, documentId: "d1", entitiesIndexed: 0, relationshipsIndexed: 0, relationsIndexed: 0, error: NOT_AVAILABLE,
  });
});

test("a transient error that never stops is retried 3 times with backoff and then reported", async () => {
  class AlwaysTransient implements CypherRunner {
    async run(cypher: string): Promise<Row[]> {
      if (cypher.includes("MERGE (d:Document")) throw new GraphTransientException("boom");
      return [];
    }
    close(): void {}
  }
  const sleeps: number[] = [];
  const idx = await GraphIndex.connect(new AlwaysTransient(), {
    logger: QUIET,
    sleep: async (s: number) => {
      sleeps.push(s);
    },
    random: () => 0.4,
  });
  const summary = await idx.upsertDocument("d1", "T", [{ text: "Acme Corp and Globex Industries." }]);
  assert.equal(summary.success, false);
  assert.equal(summary.error, "Transient Neo4j error persisted after 3 attempts: boom");
  assert.equal(sleeps.length, 2);
  assert.ok(Math.abs(sleeps[0] - 0.12) < 1e-9, String(sleeps[0]));
  assert.ok(Math.abs(sleeps[1] - 0.22) < 1e-9, String(sleeps[1]));
});

test("a non-transient error is not retried and is reported in the summary", async () => {
  class Hard implements CypherRunner {
    async run(cypher: string): Promise<Row[]> {
      if (cypher.includes("MERGE (d:Document")) throw new GraphException("boom");
      return [];
    }
    close(): void {}
  }
  const sleeps: number[] = [];
  const idx = await GraphIndex.connect(new Hard(), {
    logger: QUIET,
    sleep: async (s: number) => {
      sleeps.push(s);
    },
  });
  const summary = await idx.upsertDocument("d1", "T", [{ text: "Acme Corp and Globex Industries." }]);
  assert.equal(summary.success, false);
  assert.equal(summary.error, "boom");
  assert.deepStrictEqual(sleeps, []);
});

// ---- parity with the real Python (fixtures) ----

const dir = process.env.RAGLEAP_GRAPH_PARITY_DIR;
const skip = dir ? false : "RAGLEAP_GRAPH_PARITY_DIR not set";

function load(name: string): any[] {
  const text = readFileSync(join(dir as string, name), "utf8");
  return JSON.parse(text, (_key: string, value: unknown, context?: { source?: string }) => {
    const src = context?.source;
    if (typeof value === "number" && src !== undefined && /[.eE]/.test(src)) return new Neo4jFloat(value);
    return value;
  });
}

function numOf(x: unknown): number {
  return x instanceof Neo4jFloat ? x.value : (x as number);
}

async function execute(c: any): Promise<{ summary: any; calls: Call[]; sleeps: number[]; logs: Array<[string, string]> }> {
  const runner = new RecordingRunner(c.old_pairs ?? [], c.old_relations ?? [], c.fail ?? null);
  const sleeps: number[] = [];
  const logs: Array<[string, string]> = [];
  const logger: GraphLogger = {
    info() {},
    warn(m: string) {
      logs.push(["WARNING", m]);
    },
    error(m: string) {
      logs.push(["ERROR", m]);
    },
  };
  const idx = await GraphIndex.connect(c.no_driver ? null : runner, {
    logger,
    sleep: async (s: number) => {
      sleeps.push(s);
    },
    random: () => 0.5,
  });
  runner.reset();
  logs.length = 0;
  const a = c.args;
  const s = await idx.upsertDocument(a.document_id, a.title, a.chunks, {
    namespace: a.namespace,
    userId: a.user_id,
    maxEntities: a.max_entities,
    maxPairs: a.max_pairs,
    domainTerms: a.domain_terms,
  });
  const summary = {
    success: s.success,
    document_id: s.documentId,
    entities_indexed: s.entitiesIndexed,
    relationships_indexed: s.relationshipsIndexed,
    relations_indexed: s.relationsIndexed,
    error: s.error,
  };
  return { summary, calls: runner.calls, sleeps, logs };
}

async function compareAll(label: string, cases: any[]): Promise<void> {
  assert.ok(cases.length > 0, `${label}: no cases`);
  const bad: unknown[] = [];
  for (const c of cases) {
    const got = await execute(c);
    const wantCalls = normalize(c.calls.map((x: any) => ({ cypher: x.cypher, params: canon(x.params) })));
    try {
      assert.deepStrictEqual(got.summary, c.summary);
      assert.deepStrictEqual(normalize(got.calls), wantCalls);
      assert.deepStrictEqual(got.sleeps, (c.sleeps ?? []).map(numOf));
      assert.deepStrictEqual(got.logs, c.logs ?? []);
    } catch (e) {
      bad.push({ name: c.name, error: String((e as Error).message).slice(0, 700) });
    }
  }
  console.log(`${label}: ${cases.length} cases, ${bad.length} differ`);
  assert.equal(bad.length, 0, JSON.stringify(bad.slice(0, 2)).slice(0, 3000));
}

test("parity: upsert statements and summaries (recordings of the real Python upsert)", { skip }, async () => {
  await compareAll("upsert", load("upsert_cases.json"));
});

test("parity: upsert retries, failures, sleeps and log messages (real Python)", { skip }, async () => {
  await compareAll("upsert_retry", load("upsert_retry_cases.json"));
});
