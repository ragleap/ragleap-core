import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { detectCommunities, modularity } from "../src/communities.ts";
import type { Edge } from "../src/communities.ts";

// Compares the port with the real Python code (recorded by java/ragleap-graph/parity/gen_fixtures_communities.py):
// every community, every float (exact), every hash, every error. Needs RAGLEAP_GRAPH_PARITY_DIR.
const dir = process.env.RAGLEAP_GRAPH_PARITY_DIR;
const skip = dir ? false : "RAGLEAP_GRAPH_PARITY_DIR not set";

function dec(t: any): unknown {
  switch (t.k) {
    case "num":
    case "bool":
    case "str":
      return t.v;
    case "none":
      return null;
    case "nan":
      return NaN;
    case "inf":
      return Infinity;
    case "ninf":
      return -Infinity;
    default:
      throw new Error("unknown tag " + t.k);
  }
}

function edgesOf(list: any[]): Edge[] {
  return list.map(([a, b, w]: [string, string, any]) => [a, b, dec(w)] as Edge);
}

test("detectCommunities and modularity match the recorded Python results", { skip }, () => {
  const root = JSON.parse(readFileSync(join(dir as string, "communities_cases.json"), "utf8"));
  const bad: string[] = [];
  let compared = 0;
  let errors = 0;
  for (const sc of root.detect) {
    if (sc.name === "bad_min_size_2.0") {
      continue; // JavaScript cannot tell 2.0 from 2
    }
    const edges = edgesOf(sc.edges);
    const resolution = dec(sc.resolution) as number;
    const minSize = dec(sc.min_size) as number;
    compared++;
    if (sc.ok) {
      let got;
      try {
        got = detectCommunities(edges, resolution, minSize);
      } catch (e) {
        bad.push(`${sc.name}: unexpected ${e}`);
        continue;
      }
      if (got.length !== sc.result.length) {
        bad.push(`${sc.name}: ${got.length} communities, Python ${sc.result.length}`);
        continue;
      }
      got.forEach((g, i) => {
        const w = sc.result[i];
        if (JSON.stringify(g.members) !== JSON.stringify(w.members) || g.size !== w.size ||
            !Object.is(g.internalWeight, w.internal_weight) || g.membersHash !== w.members_hash) {
          bad.push(`${sc.name}: community ${i} differs`);
        }
      });
      const mod = modularity(edges, sc.partition, resolution);
      if (!Object.is(mod, sc.modularity)) {
        bad.push(`${sc.name}: modularity ${mod}, Python ${sc.modularity}`);
      }
    } else {
      try {
        detectCommunities(edges, resolution, minSize);
        bad.push(`${sc.name}: expected an error`);
      } catch (e: any) {
        errors++;
        if (!(e instanceof RangeError) || !e.message.startsWith(`${sc.error_param} must be`)) {
          bad.push(`${sc.name}: error ${e}`);
        }
      }
    }
  }
  let modChecked = 0;
  for (const m of root.modularity) {
    const edges = edgesOf(m.edges);
    modChecked++;
    if (m.ok) {
      const got = modularity(edges, m.communities, m.resolution);
      if (!Object.is(got, m.value)) {
        bad.push(`${m.name}: modularity ${got}, Python ${m.value}`);
      }
    } else {
      try {
        modularity(edges, m.communities, m.resolution);
        bad.push(`${m.name}: expected an error`);
      } catch {
        errors++;
      }
    }
  }
  console.log(`communities parity: detect scenarios ${compared}, modularity cases ${modChecked}, errors matched ${errors}, differing ${bad.length}`);
  assert.ok(compared > 0 && errors > 0, "nothing compared");
  assert.deepEqual(bad, []);
});
