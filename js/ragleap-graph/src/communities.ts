import { createHash } from "node:crypto";

/**
 * Deterministic community detection for the entity co-occurrence graph: a port of Python's
 * ragleap_graph.communities. Louvain on an undirected weighted graph, then every community is split into its
 * connected components. No random numbers: nodes are visited in code point order, ties go to the smaller label, and a
 * node moves only for a gain above EPSILON. Pure: reads and writes nothing. Not Leiden, one flat level; modularity has
 * a resolution limit.
 */

/** One undirected weighted edge [nameA, nameB, weight]. Self loops and weights that are not finite positive numbers are ignored. */
export type Edge = readonly [string, string, unknown];

export interface Community {
  /** Sorted by code point. */
  readonly members: string[];
  readonly size: number;
  /** Total weight of the edges inside the community. */
  readonly internalWeight: number;
  /** SHA-256 (hex) of the members joined with NUL characters. */
  readonly membersHash: string;
}

export const EPSILON = 1e-12;
export const MAX_PASSES = 1000;

type Adj = Map<number, number>;
type Pair = { low: string; high: string; weight: number };

/** Python-compatible string order: by Unicode code point, not by UTF-16 unit. */
export function cpCompare(a: string, b: string): number {
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    const ca = a.codePointAt(i) as number;
    const cb = b.codePointAt(j) as number;
    if (ca !== cb) {
      return ca < cb ? -1 : 1;
    }
    i += ca > 0xffff ? 2 : 1;
    j += cb > 0xffff ? 2 : 1;
  }
  return a.length - i - (b.length - j);
}

function sortedKeys(m: Map<number, number>): number[] {
  return [...m.keys()].sort((x, y) => x - y);
}

/** Undirected weights keyed by (smaller, larger) name; duplicates summed in sorted order. */
function prepareEdges(edges: Iterable<Edge>): Map<string, Pair> {
  const cleaned: Pair[] = [];
  for (const [a, b, w] of edges) {
    if (a === b) {
      continue;
    }
    if (typeof w !== "number" || !Number.isFinite(w) || w <= 0) {
      continue;
    }
    cleaned.push(cpCompare(a, b) < 0 ? { low: a, high: b, weight: w } : { low: b, high: a, weight: w });
  }
  cleaned.sort((x, y) => cpCompare(x.low, y.low) || cpCompare(x.high, y.high) || x.weight - y.weight);
  const merged = new Map<string, Pair>();
  for (const c of cleaned) {
    const key = JSON.stringify([c.low, c.high]);
    const prev = merged.get(key);
    merged.set(key, { low: c.low, high: c.high, weight: (prev ? prev.weight : 0.0) + c.weight });
  }
  return merged;
}

/** SHA-256 (hex) of the members joined with NUL characters; pass them sorted by code point. */
export function membersHash(members: readonly string[]): string {
  return createHash("sha256").update(members.join("\u0000"), "utf8").digest("hex");
}

/**
 * Modularity of a partition. Every node of an edge must belong to a community (RangeError otherwise); a node listed in
 * several communities counts for the last one.
 */
export function modularity(
  edges: Iterable<Edge>,
  communities: readonly (readonly string[])[],
  resolution: number = 1.0,
): number {
  const weights = prepareEdges(edges);
  let total = 0.0;
  for (const p of weights.values()) {
    total += p.weight;
  }
  if (total === 0) {
    return 0.0;
  }
  const label = new Map<string, number>();
  communities.forEach((members, index) => {
    for (const name of members) {
      label.set(name, index);
    }
  });
  const degreeSum = new Map<number, number>();
  let inside = 0.0;
  for (const p of weights.values()) {
    const la = label.get(p.low);
    const lb = label.get(p.high);
    if (la === undefined || lb === undefined) {
      throw new RangeError("every node of an edge must belong to a community");
    }
    degreeSum.set(la, (degreeSum.get(la) ?? 0.0) + p.weight);
    degreeSum.set(lb, (degreeSum.get(lb) ?? 0.0) + p.weight);
    if (la === lb) {
      inside += p.weight;
    }
  }
  let squares = 0.0;
  for (const d of degreeSum.values()) {
    squares += d * d;
  }
  const penalty = squares / (4.0 * total * total);
  return inside / total - resolution * penalty;
}

function localMoving(adj: Adj[], loops: number[], resolution: number): { community: number[]; movedAny: boolean } {
  const n = adj.length;
  const degree: number[] = [];
  for (let i = 0; i < n; i++) {
    let s = 0;
    for (const j of sortedKeys(adj[i])) {
      s += adj[i].get(j) as number;
    }
    degree.push(s + 2.0 * loops[i]);
  }
  let sum = 0;
  for (const d of degree) {
    sum += d;
  }
  const total = sum / 2.0;
  const community: number[] = [];
  for (let i = 0; i < n; i++) {
    community.push(i);
  }
  const communityDegree = degree.slice();
  let movedAny = false;
  for (let pass = 0; pass < MAX_PASSES; pass++) {
    let movedPass = false;
    for (let i = 0; i < n; i++) {
      const own = community[i];
      const links = new Map<number, number>();
      for (const j of sortedKeys(adj[i])) {
        const label = community[j];
        links.set(label, (links.get(label) ?? 0.0) + (adj[i].get(j) as number));
      }
      communityDegree[own] -= degree[i];
      const scale = (resolution * degree[i]) / (2.0 * total);
      let best = own;
      let bestGain = (links.get(own) ?? 0.0) - communityDegree[own] * scale;
      for (const label of sortedKeys(links)) {
        if (label === own) {
          continue;
        }
        const gain = (links.get(label) as number) - communityDegree[label] * scale;
        if (gain > bestGain + EPSILON) {
          best = label;
          bestGain = gain;
        }
      }
      communityDegree[best] += degree[i];
      if (best !== own) {
        community[i] = best;
        movedPass = true;
      }
    }
    if (!movedPass) {
      break;
    }
    movedAny = true;
  }
  return { community, movedAny };
}

function aggregate(adj: Adj[], loops: number[], community: number[]): { adj: Adj[]; loops: number[]; mapping: number[] } {
  const order = new Map<number, number>();
  for (let i = 0; i < adj.length; i++) {
    if (!order.has(community[i])) {
      order.set(community[i], order.size);
    }
  }
  const size = order.size;
  const newAdj: Adj[] = [];
  const newLoops: number[] = [];
  for (let i = 0; i < size; i++) {
    newAdj.push(new Map());
    newLoops.push(0.0);
  }
  for (let i = 0; i < adj.length; i++) {
    const ci = order.get(community[i]) as number;
    newLoops[ci] += loops[i];
    for (const j of sortedKeys(adj[i])) {
      if (j < i) {
        continue;
      }
      const cj = order.get(community[j]) as number;
      const weight = adj[i].get(j) as number;
      if (ci === cj) {
        newLoops[ci] += weight;
      } else {
        newAdj[ci].set(cj, (newAdj[ci].get(cj) ?? 0.0) + weight);
        newAdj[cj].set(ci, (newAdj[cj].get(ci) ?? 0.0) + weight);
      }
    }
  }
  const mapping = community.map((c) => order.get(c) as number);
  return { adj: newAdj, loops: newLoops, mapping };
}

function louvain(weights: Map<string, Pair>, resolution: number): { names: string[]; membership: number[] } {
  const nameSet = new Set<string>();
  for (const p of weights.values()) {
    nameSet.add(p.low);
    nameSet.add(p.high);
  }
  const names = [...nameSet].sort(cpCompare);
  const index = new Map<string, number>();
  names.forEach((n, i) => index.set(n, i));
  let adj: Adj[] = names.map(() => new Map<number, number>());
  for (const p of weights.values()) {
    const a = index.get(p.low) as number;
    const b = index.get(p.high) as number;
    adj[a].set(b, p.weight);
    adj[b].set(a, p.weight);
  }
  let loops: number[] = names.map(() => 0.0);
  let membership: number[] = names.map((_, i) => i);
  while (adj.length > 0) {
    const m = localMoving(adj, loops, resolution);
    if (!m.movedAny) {
      break;
    }
    const g = aggregate(adj, loops, m.community);
    adj = g.adj;
    loops = g.loops;
    membership = membership.map((x) => g.mapping[x]);
  }
  return { names, membership };
}

/** Connected parts of every community; edges between different communities do not connect. */
function components(neighbors: number[][], membership: number[]): number[][] {
  const seen: boolean[] = membership.map(() => false);
  const out: number[][] = [];
  for (let start = 0; start < membership.length; start++) {
    if (seen[start]) {
      continue;
    }
    seen[start] = true;
    const stack = [start];
    const part: number[] = [];
    while (stack.length > 0) {
      const node = stack.pop() as number;
      part.push(node);
      for (const other of neighbors[node]) {
        if (!seen[other] && membership[other] === membership[start]) {
          seen[other] = true;
          stack.push(other);
        }
      }
    }
    part.sort((x, y) => x - y);
    out.push(part);
  }
  return out;
}

/**
 * Groups the nodes of a weighted undirected graph into communities, largest first (ties by first member).
 * RangeError for a bad resolution (checked first) or minSize. Communities with fewer than minSize members are left out.
 */
export function detectCommunities(edges: Iterable<Edge>, resolution: number = 1.0, minSize: number = 2): Community[] {
  if (typeof resolution !== "number" || !Number.isFinite(resolution) || resolution <= 0) {
    throw new RangeError(`resolution must be a positive finite number, got ${String(resolution)}`);
  }
  if (typeof minSize !== "number" || !Number.isInteger(minSize) || minSize < 1) {
    throw new RangeError(`min_size must be a positive integer, got ${String(minSize)}`);
  }
  const weights = prepareEdges(edges);
  const { names, membership } = louvain(weights, resolution);
  const index = new Map<string, number>();
  names.forEach((n, i) => index.set(n, i));
  const neighbors: number[][] = names.map(() => []);
  for (const p of weights.values()) {
    const a = index.get(p.low) as number;
    const b = index.get(p.high) as number;
    neighbors[a].push(b);
    neighbors[b].push(a);
  }
  for (const row of neighbors) {
    row.sort((x, y) => x - y);
  }
  const parts = components(neighbors, membership);
  const label: number[] = names.map(() => 0);
  parts.forEach((part, partNumber) => {
    for (const node of part) {
      label[node] = partNumber;
    }
  });
  const internal: number[] = parts.map(() => 0.0);
  for (const p of weights.values()) {
    const la = label[index.get(p.low) as number];
    const lb = label[index.get(p.high) as number];
    if (la === lb) {
      internal[la] += p.weight;
    }
  }
  const out: Community[] = [];
  parts.forEach((part, partNumber) => {
    if (part.length < minSize) {
      return;
    }
    const members = part.map((node) => names[node]);
    out.push({ members, size: members.length, internalWeight: internal[partNumber], membersHash: membersHash(members) });
  });
  out.sort((x, y) => y.size - x.size || cpCompare(x.members[0], y.members[0]));
  return out;
}
