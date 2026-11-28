import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { COMMUNITY_STATEMENTS } from "../src/communityStore.ts";
import { GraphIndex } from "../src/graphIndex.ts";
import type { CypherRunner, Row } from "../src/queryApiRunner.ts";
import { RECOMPUTE_RELATES_AS_TEMPORAL } from "../src/upsert.ts";

// Compares the temporal statements with the real Python code (recorded by
// java/ragleap-graph/parity/gen_fixtures_temporal.py): the statement texts (whitespace-normalized) and, for find_relations,
// the exact statement and parameters of 36 call shapes. One known difference: TypeScript reads the as_of parameter with
// datetime($as_of) and sends an ISO string (the HTTP Query API takes no typed parameters), so Python's "$as_of" is replaced
// by "datetime($as_of)" before comparing, and the instants are compared.
const dir = process.env.RAGLEAP_GRAPH_PARITY_DIR;
const skip = dir ? false : "RAGLEAP_GRAPH_PARITY_DIR not set";
const silent = { info() {}, warn() {}, error() {} };

function norm(s: string): string {
  return s.trim().replace(/\s+/g, " ");
}
function load(): any {
  return JSON.parse(readFileSync(join(dir as string, "temporal_cases.json"), "utf8"));
}

test("the temporal statement texts match Python", { skip }, () => {
  const t = load().texts as Record<string, string>;
  assert.deepEqual(Object.keys(t).sort(), ["comm_edges_open", "comm_relations_open", "refresh_temporal"]);
  assert.equal(norm(RECOMPUTE_RELATES_AS_TEMPORAL), norm(t.refresh_temporal));
  assert.equal(norm(COMMUNITY_STATEMENTS.relates_edges_open), norm(t.comm_edges_open));
  assert.equal(norm(COMMUNITY_STATEMENTS.relations_open), norm(t.comm_relations_open));
});

test("findRelations matches the recorded Python statements and parameters", { skip }, async () => {
  const cases = load().cases as any[];
  const bad: string[] = [];
  let withAsOf = 0;
  for (const c of cases) {
    const sent: Array<{ cypher: string; params: Record<string, unknown> }> = [];
    let armed = false;
    const runner: CypherRunner = {
      async run(cypher: string, params?: Record<string, unknown> | null): Promise<Row[]> {
        if (armed) sent.push({ cypher, params: { ...(params ?? {}) } });
        return [];
      },
      close(): void {},
    };
    const index = await GraphIndex.connect(runner, { logger: silent, temporal: c.temporal });
    armed = true;
    await index.findRelations("Acme Corp", { namespace: "ns", relationType: c.relation_type, direction: c.direction, asOf: c.as_of, limit: 25 });
    const name = `${c.temporal}/${c.direction}/${c.relation_type}/${c.as_of}`;
    if (sent.length !== 1) {
      bad.push(`${name}: ${sent.length} statements`);
      continue;
    }
    const [pyQuery, pyParams] = c.calls[0] as [string, Record<string, unknown>];
    const want = norm(pyQuery).replaceAll("$as_of", "datetime($as_of)");
    if (want !== norm(sent[0].cypher)) bad.push(`${name}: statement differs\n  python: ${want}\n  ts:     ${norm(sent[0].cypher)}`);
    const got = sent[0].params;
    if (JSON.stringify(Object.keys(got).sort()) !== JSON.stringify(Object.keys(pyParams).sort())) {
      bad.push(`${name}: parameter names ${Object.keys(got).sort()} vs ${Object.keys(pyParams).sort()}`);
      continue;
    }
    for (const [key, value] of Object.entries(got)) {
      const expected = pyParams[key];
      if (key === "as_of") {
        withAsOf++;
        if (Date.parse(String(value)) !== Date.parse(String(expected))) bad.push(`${name}: as_of ${value} vs ${expected}`);
      } else if (expected === null ? value !== null : String(value) !== String(expected)) {
        bad.push(`${name}: parameter ${key} ${String(value)} vs ${String(expected)}`);
      }
    }
  }
  console.log(`temporal parity: cases ${cases.length}, as_of parameters compared ${withAsOf}, differing ${bad.length}`);
  assert.equal(cases.length, 36);
  assert.equal(withAsOf, 24);
  assert.deepEqual(bad.slice(0, 3), []);
});
