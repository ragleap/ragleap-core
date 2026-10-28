import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { GraphConfig } from "../src/config.ts";
import { GraphException } from "../src/errors.ts";
import { CONSTRAINTS, GraphIndex } from "../src/graphIndex.ts";
import type { GraphLogger } from "../src/graphIndex.ts";
import type { CypherRunner, Row } from "../src/queryApiRunner.ts";

class FakeRunner implements CypherRunner {
  statements: string[] = [];
  closeCalls = 0;
  returnOneCalls = 0;
  constraintCalls = 0;
  connectFails: boolean;
  constraintFailAt: number | null;
  healthFails: boolean;

  constructor(connectFails = false, constraintFailAt: number | null = null, healthFails = false) {
    this.connectFails = connectFails;
    this.constraintFailAt = constraintFailAt;
    this.healthFails = healthFails;
  }

  async run(cypher: string, _params?: Record<string, unknown> | null): Promise<Row[]> {
    this.statements.push(cypher);
    if (cypher === "RETURN 1") {
      this.returnOneCalls++;
      if (this.connectFails && this.returnOneCalls === 1) throw new GraphException("boom: connect");
      if (this.healthFails && this.returnOneCalls >= 2) throw new GraphException("boom: health");
    }
    if (cypher.startsWith("CREATE CONSTRAINT")) {
      this.constraintCalls++;
      if (this.constraintFailAt === this.constraintCalls) throw new GraphException("boom: constraint");
    }
    return [];
  }

  close(): void {
    this.closeCalls++;
  }
}

function recorder(): { logger: GraphLogger; warns: string[]; infos: string[] } {
  const warns: string[] = [];
  const infos: string[] = [];
  return {
    logger: {
      info(m: string) {
        infos.push(m);
      },
      warn(m: string) {
        warns.push(m);
      },
    },
    warns,
    infos,
  };
}

const QUIET: GraphLogger = { info() {}, warn() {} };

test("connect runs RETURN 1, then the six constraints in order", async () => {
  const r = new FakeRunner();
  const rec = recorder();
  const idx = await GraphIndex.connect(r, { logger: rec.logger });
  assert.equal(idx.isAvailable(), true);
  assert.equal(CONSTRAINTS.length, 6);
  assert.equal(r.statements[0], "RETURN 1");
  assert.deepStrictEqual(r.statements.slice(1), [...CONSTRAINTS]);
  assert.ok(CONSTRAINTS.every((s) => s.includes("IF NOT EXISTS") && s.includes("composite_key")));
  assert.deepStrictEqual(rec.warns, []);
  assert.equal(rec.infos.length, 1);
});

test("a failing first query makes the index unavailable, warns once and closes the runner", async () => {
  const r = new FakeRunner(true);
  const rec = recorder();
  const idx = await GraphIndex.connect(r, { logger: rec.logger });
  assert.equal(idx.isAvailable(), false);
  assert.equal(await idx.healthCheck(), false);
  assert.equal(rec.warns.length, 1);
  assert.ok(rec.warns[0].includes("Failed to initialize Neo4j connection"));
  assert.ok(rec.warns[0].includes("boom: connect"));
  assert.equal(r.closeCalls, 1);
  assert.deepStrictEqual(r.statements, ["RETURN 1"]);
  idx.close();
  assert.equal(r.closeCalls, 1);
});

test("a failing constraint keeps the index available, warns once and stops at that constraint", async () => {
  const r = new FakeRunner(false, 3);
  const rec = recorder();
  const idx = await GraphIndex.connect(r, { logger: rec.logger });
  assert.equal(idx.isAvailable(), true);
  assert.equal(r.statements.filter((s) => s.startsWith("CREATE CONSTRAINT")).length, 3);
  assert.equal(rec.warns.length, 1);
  assert.ok(rec.warns[0].includes("Failed to create composite_key constraints"));
  assert.ok(rec.warns[0].includes("boom: constraint"));
  assert.equal(r.closeCalls, 0);
});

test("health check: true when the server answers, false and a warning when it does not", async () => {
  const good = new FakeRunner();
  const goodIdx = await GraphIndex.connect(good, { logger: QUIET });
  assert.equal(await goodIdx.healthCheck(), true);
  assert.equal(good.statements[good.statements.length - 1], "RETURN 1");

  const bad = new FakeRunner(false, null, true);
  const rec = recorder();
  const badIdx = await GraphIndex.connect(bad, { logger: rec.logger });
  assert.equal(badIdx.isAvailable(), true);
  assert.equal(await badIdx.healthCheck(), false);
  assert.equal(rec.warns.length, 1);
  assert.ok(rec.warns[0].includes("Graph health check failed"));
  assert.ok(rec.warns[0].includes("boom: health"));
});

test("a null source gives an unavailable index, never throws and logs nothing", async () => {
  const rec = recorder();
  const idx = await GraphIndex.connect(null, { logger: rec.logger });
  assert.equal(idx.isAvailable(), false);
  assert.equal(await idx.healthCheck(), false);
  idx.close();
  assert.deepStrictEqual(rec.warns, []);
  assert.deepStrictEqual(rec.infos, []);
});

test("a config the HTTP runner cannot be built from gives an unavailable index and one warning", async () => {
  const rec = recorder();
  const idx = await GraphIndex.connect(new GraphConfig({ database: "\ud800" }), { logger: rec.logger });
  assert.equal(idx.isAvailable(), false);
  assert.equal(rec.warns.length, 1);
  assert.ok(rec.warns[0].includes("Failed to initialize Neo4j client"), rec.warns[0]);
});

test("a GraphConfig for an unreachable server gives an unavailable index", async () => {
  const rec = recorder();
  const idx = await GraphIndex.connect(new GraphConfig({ baseUrl: "http://127.0.0.1:1" }), { logger: rec.logger });
  assert.equal(idx.isAvailable(), false);
  assert.equal(rec.warns.length, 1);
  assert.ok(rec.warns[0].includes("Failed to initialize Neo4j connection"));
  assert.ok(rec.warns[0].includes("Request to Neo4j failed"));
});

test("close and asyncDispose each close the runner", async () => {
  const r = new FakeRunner();
  const idx = await GraphIndex.connect(r, { logger: QUIET });
  idx.close();
  assert.equal(r.closeCalls, 1);
  const r2 = new FakeRunner();
  const idx2 = await GraphIndex.connect(r2, { logger: QUIET });
  await idx2[Symbol.asyncDispose]();
  assert.equal(r2.closeCalls, 1);
});

test("extractQueryEntities: default limit 10, explicit limit, domain terms", async () => {
  const idx = await GraphIndex.connect(new FakeRunner(), { logger: QUIET });
  const text = "AAA BBB CCC DDD EEE FFF GGG HHH III JJJ KKK LLL";
  assert.deepStrictEqual(idx.extractQueryEntities(text), ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ"]);
  assert.equal(idx.extractQueryEntities(text, 12).length, 12);
  assert.deepStrictEqual(idx.extractQueryEntities("tell me about the alara program", 10, ["alara"]), ["Alara"]);
  assert.deepStrictEqual(idx.extractQueryEntities("What did Acme Corp launch?"), ["Acme Corp"]);
});

// Parity with the real Python GraphIndex (fixtures from parity/gen_fixtures_index.py).
const dir = process.env.RAGLEAP_GRAPH_PARITY_DIR;
const skip = dir ? false : "RAGLEAP_GRAPH_PARITY_DIR not set";

function load(name: string): any {
  return JSON.parse(readFileSync(join(dir as string, name), "utf8"));
}

test("parity: construction, constraints, health check and close", { skip }, async () => {
  const cases: any[] = load("index_cases.json").scenarios;
  assert.ok(cases.length > 0, "no scenarios");
  const bad: unknown[] = [];
  for (const c of cases) {
    const runner = new FakeRunner(c.connect_fails, c.constraint_fail_at, c.health_fails);
    const idx = await GraphIndex.connect(runner, { logger: QUIET });
    const available = idx.isAvailable();
    const attempted = runner.statements.filter((s) => s.startsWith("CREATE CONSTRAINT"));
    const afterConstruct = runner.closeCalls;
    const health = await idx.healthCheck();
    idx.close();
    const got = {
      available,
      constraints_attempted: attempted,
      driver_close_calls_after_construct: afterConstruct,
      health_check: health,
      driver_close_calls_after_close: runner.closeCalls,
    };
    try {
      assert.deepStrictEqual(got, c.expected);
    } catch {
      bad.push({ name: c.name, got, expected: c.expected });
    }
  }
  console.log(`index scenarios: ${cases.length} cases, ${bad.length} differ`);
  assert.equal(bad.length, 0, JSON.stringify(bad.slice(0, 2)).slice(0, 2500));
});

test("parity: extractQueryEntities method", { skip }, async () => {
  const cases: any[] = load("index_cases.json").query;
  assert.ok(cases.length > 0, "no query cases");
  const idx = await GraphIndex.connect(new FakeRunner(), { logger: QUIET });
  const bad: unknown[] = [];
  for (const c of cases) {
    const got = idx.extractQueryEntities(c.query, c.max_entities === null ? undefined : c.max_entities, c.domain_terms);
    try {
      assert.deepStrictEqual(got, c.expected);
    } catch {
      bad.push({ case: c, got });
    }
  }
  console.log(`index query: ${cases.length} cases, ${bad.length} differ`);
  assert.equal(bad.length, 0, JSON.stringify(bad.slice(0, 3)).slice(0, 2500));
});
