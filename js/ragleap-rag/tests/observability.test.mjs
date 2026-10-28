import test from "node:test";
import assert from "node:assert/strict";
import { fire_event } from "../dist/observability.js";

test("handlers run in order and receive the event", () => {
  const calls = [];
  const event = { action: "ingest" };

  fire_event(event, [
    (value) => calls.push(["first", value]),
    (value) => calls.push(["second", value]),
  ], "on_ingest");

  assert.deepEqual(calls, [
    ["first", event],
    ["second", event],
  ]);
});

test("a broken handler does not stop later handlers", () => {
  const calls = [];
  const originalWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args);

  try {
    fire_event({}, [
      () => { throw new Error("hook failed"); },
      () => calls.push("second"),
    ], "on_answer");
  } finally {
    console.warn = originalWarn;
  }

  assert.deepEqual(calls, ["second"]);
  assert.equal(warnings.length, 1);
  assert.match(String(warnings[0][0]), /on_answer/);
  assert.match(String(warnings[0][0]), /swallowed/);
});

test("missing or empty handlers are a no-op", () => {
  assert.doesNotThrow(() => fire_event({}, undefined, "on_query"));
  assert.doesNotThrow(() => fire_event({}, [], "on_query"));
});
