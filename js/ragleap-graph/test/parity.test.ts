import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { compositeKey } from "../src/compositeKey.ts";
import { extractQueryEntities, normalizeEntityName } from "../src/entities.ts";
import { pyLower } from "../src/pyText.ts";

// Small unit tests (no fixtures needed, run in CI).
test("composite key matches the Python SHA-256 value", () => {
  assert.equal(
    compositeKey("a", "b", "c"),
    "8badde10c760e9b702defb4b5e225de79c515b1d2a5cfb000e140f3c6fbb5629",
  );
});

test("normalize: strips, capitalizes, keeps acronyms, drops short names", () => {
  assert.equal(normalizeEntityName("  acme   corp. "), "Acme Corp");
  assert.equal(normalizeEntityName("ALARA,"), "ALARA");
  assert.equal(normalizeEntityName("ab"), "");
  assert.equal(normalizeEntityName(""), "");
});

test("extract: question words are dropped, acronyms and phrases kept", () => {
  assert.deepEqual(extractQueryEntities("What did Acme Corp launch?"), ["Acme Corp"]);
  assert.deepEqual(extractQueryEntities("IAEA and ALARA"), ["IAEA", "ALARA"]);
  assert.deepEqual(extractQueryEntities(""), []);
});

// Parity tests against fixtures generated from the real Python code.
const dir = process.env.RAGLEAP_GRAPH_PARITY_DIR;
const skip = dir ? false : "RAGLEAP_GRAPH_PARITY_DIR not set";

function load(name: string): any {
  return JSON.parse(readFileSync(join(dir as string, name), "utf8"));
}

function runCases(label: string, cases: any[], fn: (c: any) => unknown, expectedOf: (c: any) => unknown): void {
  assert.ok(cases.length > 0, `${label}: no cases`);
  const bad: unknown[] = [];
  for (const c of cases) {
    const got = fn(c);
    try {
      assert.deepStrictEqual(got, expectedOf(c));
    } catch {
      bad.push({ case: c, got });
    }
  }
  console.log(`${label}: ${cases.length} cases, ${bad.length} differ`);
  assert.equal(
    bad.length,
    0,
    `${label}: ${bad.length} of ${cases.length} differ; first: ${JSON.stringify(bad.slice(0, 3)).slice(0, 1500)}`,
  );
}

test("parity: composite key", { skip }, () => {
  runCases("composite_key", load("cases.json").composite_key, (c) => compositeKey(...c.parts), (c) => c.expected);
});

test("parity: normalize entity name", { skip }, () => {
  runCases("normalize", load("cases.json").normalize, (c) => normalizeEntityName(c.input), (c) => c.expected);
});

test("parity: extract query entities", { skip }, () => {
  runCases(
    "extract",
    load("cases.json").extract,
    (c) => extractQueryEntities(c.text, c.max_entities, c.domain_terms),
    (c) => c.expected,
  );
});

test("parity: str.lower", { skip }, () => {
  runCases("lower", load("lower_cases.json").lower, (c) => pyLower(c.input), (c) => c.expected);
});

// Cases added after mutation checks showed the recorded fixtures never exercise them.
// Expected values were confirmed against the real Python code at the pinned commit.
test("normalize: question mark is stripped at both ends", () => {
  assert.equal(normalizeEntityName("acme corp?"), "Acme Corp");
  assert.equal(normalizeEntityName("?alpha"), "Alpha");
});

test("extract: hyphenated acronym kept, two-letter acronym dropped by the length rule", () => {
  assert.deepEqual(extractQueryEntities("AB-1 report"), ["AB-1"]);
  assert.deepEqual(extractQueryEntities("AI and ML"), []);
});
