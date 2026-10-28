import test from "node:test";
import assert from "node:assert/strict";
import { RAGLEAP_TS_VERSION } from "../dist/index.js";

test("TypeScript package builds and exports its version", () => {
  assert.equal(RAGLEAP_TS_VERSION, "0.1.0");
});
