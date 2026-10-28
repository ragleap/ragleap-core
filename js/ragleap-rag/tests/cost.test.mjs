import test from "node:test";
import assert from "node:assert/strict";
import {
  compute_cost,
  CostTracker,
  SEED_PRICING_TABLE,
  PRICING_TABLE_VERIFIED_DATE,
} from "../dist/index.js";

const usage = { prompt_tokens: 10, completion_tokens: 5 };

test("computes cost from known model rates", () => {
  assert.equal(compute_cost(SEED_PRICING_TABLE, "gemini", "gemini-3.6-flash", usage), 0.000053);
});

test("unknown provider, unknown model, or missing usage returns null", () => {
  assert.equal(compute_cost(SEED_PRICING_TABLE, "unknown", "model", usage), null);
  assert.equal(compute_cost(SEED_PRICING_TABLE, "gemini", "unpriced-model", usage), null);
  assert.equal(compute_cost(SEED_PRICING_TABLE, "gemini", "gemini-3.6-flash", null), null);
  assert.equal(compute_cost(SEED_PRICING_TABLE, null, "gemini-3.6-flash", usage), null);
});

test("Ollama wildcard rates calculate zero cost", () => {
  assert.equal(compute_cost(SEED_PRICING_TABLE, "ollama", "local-model", {
    prompt_tokens: 1000, completion_tokens: 1000,
  }), 0);
});

test("custom rates override one model without deleting other seed rates", () => {
  const tracker = new CostTracker({
    pricing_table: {
      gemini: {
        "gemini-2.5-flash": { input: 0.05, output: 0.1 },
      },
    },
  });

  assert.equal(tracker.record("gemini", "gemini-2.5-flash", {
    prompt_tokens: 1_000_000, completion_tokens: 1_000_000,
  }), 0.15);
  assert.equal(tracker.record("gemini", "gemini-3.6-flash", usage), 0.000053);
});

test("tracker accumulates known costs but ignores unknown prices", () => {
  const tracker = new CostTracker();
  tracker.record("gemini", "gemini-3.6-flash", usage);
  tracker.record("gemini", "gemini-3.6-flash", usage);
  tracker.record("gemini", "unpriced-model", usage);

  assert.equal(tracker.cumulative_cost_usd, 0.000106);
});

test("budget status changes when cumulative cost reaches threshold", () => {
  const tracker = new CostTracker({ budget_usd_per_month: 0.00001 });
  assert.equal(tracker.is_over_budget(), false);
  tracker.record("gemini", "gemini-3.6-flash", usage);
  assert.equal(tracker.is_over_budget(), true);
});

test("no budget configured means never over budget", () => {
  const tracker = new CostTracker();
  tracker.record("gemini", "gemini-3.6-flash", {
    prompt_tokens: 999_999_999,
    completion_tokens: 999_999_999,
  });
  assert.equal(tracker.is_over_budget(), false);
});

test("exports pricing-table verification date", () => {
  assert.equal(PRICING_TABLE_VERIFIED_DATE, "2026-07-28");
});
