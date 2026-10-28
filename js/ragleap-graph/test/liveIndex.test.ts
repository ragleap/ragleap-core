import { test } from "node:test";
import assert from "node:assert/strict";
import { GraphConfig } from "../src/config.ts";
import { CONSTRAINTS, GraphIndex } from "../src/graphIndex.ts";
import { QueryApiRunner } from "../src/queryApiRunner.ts";

// Runs against a real Neo4j test server. Creates the six composite_key constraints (idempotent). Gated.
const url = process.env.NEO4J_HTTP_URL;
const skip = url ? false : "NEO4J_HTTP_URL not set";

function config(secret?: string): GraphConfig {
  return new GraphConfig({
    baseUrl: url as string,
    user: process.env.NEO4J_USER ?? "neo4j",
    password: secret ?? process.env.NEO4J_PASSWORD ?? "",
  });
}

test("live: GraphIndex connects, creates the six constraints and passes the health check", { skip }, async () => {
  const warns: string[] = [];
  const idx = await GraphIndex.connect(config(), { logger: { info() {}, warn: (m: string) => void warns.push(m) } });
  try {
    assert.equal(idx.isAvailable(), true);
    assert.deepStrictEqual(warns, []);
    assert.equal(await idx.healthCheck(), true);
    const rows = await new QueryApiRunner(config()).run("SHOW CONSTRAINTS YIELD name RETURN name", {});
    const names = new Set(rows.map((r) => r.name));
    for (const statement of CONSTRAINTS) {
      const name = statement.split(" ")[2];
      assert.ok(names.has(name), `constraint missing: ${name}`);
    }
  } finally {
    idx.close();
  }
});

test("live: a wrong login gives an unavailable index", { skip }, async () => {
  const warns: string[] = [];
  const idx = await GraphIndex.connect(config("example-wrong-password"), { logger: { info() {}, warn: (m: string) => void warns.push(m) } });
  assert.equal(idx.isAvailable(), false);
  assert.equal(warns.length, 1);
});
