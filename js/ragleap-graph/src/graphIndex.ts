import { GraphConfig } from "./config.ts";
import { extractQueryEntities as extractQueryEntitiesFn } from "./entities.ts";
import { QueryApiRunner } from "./queryApiRunner.ts";
import type { CypherRunner } from "./queryApiRunner.ts";
import { upsertDocumentWithRetry } from "./upsert.ts";
import type { UpsertChunk, UpsertOptions, UpsertSummary, UpsertTiming } from "./upsert.ts";

/** The composite_key constraints created on connect (idempotent: IF NOT EXISTS). */
export const CONSTRAINTS: readonly string[] = Object.freeze([
  "CREATE CONSTRAINT document_composite_key IF NOT EXISTS FOR (d:Document) REQUIRE d.composite_key IS UNIQUE",
  "CREATE CONSTRAINT entity_composite_key IF NOT EXISTS FOR (e:Entity) REQUIRE e.composite_key IS UNIQUE",
  "CREATE CONSTRAINT pairweight_composite_key IF NOT EXISTS FOR (pw:PairWeight) REQUIRE pw.composite_key IS UNIQUE",
  "CREATE CONSTRAINT relationweight_composite_key IF NOT EXISTS FOR (rw:RelationWeight) REQUIRE rw.composite_key IS UNIQUE",
  "CREATE CONSTRAINT co_occurs_with_composite_key IF NOT EXISTS FOR ()-[r:CO_OCCURS_WITH]-() REQUIRE r.composite_key IS UNIQUE",
  "CREATE CONSTRAINT relates_as_composite_key IF NOT EXISTS FOR ()-[r:RELATES_AS]-() REQUIRE r.composite_key IS UNIQUE",
]);

export interface GraphLogger {
  info(message: string): void;
  warn(message: string): void;
  error?(message: string): void;
}

export interface GraphIndexOptions {
  /** Where warnings go. Default: warnings to console.warn, info messages dropped. */
  logger?: GraphLogger;
  /** Waits between upsert retries (seconds). Default: a real timer. */
  sleep?: (seconds: number) => Promise<void>;
  /** Jitter source for the retry backoff, 0 to 1. Default: Math.random. */
  random?: () => number;
}

const DEFAULT_LOGGER: GraphLogger = {
  info() {},
  warn(message: string) {
    console.warn(message);
  },
  error(message: string) {
    console.error(message);
  },
};

const DEFAULT_TIMING: UpsertTiming = {
  sleep: (seconds: number) => new Promise<void>((resolve) => setTimeout(resolve, seconds * 1000)),
  random: () => Math.random(),
};

function messageOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function openRunner(config: GraphConfig, logger: GraphLogger): CypherRunner | null {
  try {
    return new QueryApiRunner(config);
  } catch (e) {
    logger.warn(`Failed to initialize Neo4j client: ${messageOf(e)}`);
    return null;
  }
}

async function connectRunner(candidate: CypherRunner, logger: GraphLogger): Promise<CypherRunner | null> {
  try {
    await candidate.run("RETURN 1", {});
    logger.info("Neo4j connection initialized successfully");
  } catch (e) {
    logger.warn(`Failed to initialize Neo4j connection: ${messageOf(e)}`);
    try {
      candidate.close();
    } catch {
      // closing a failed connection is best effort
    }
    return null;
  }
  try {
    for (const statement of CONSTRAINTS) {
      await candidate.run(statement, {});
    }
  } catch (e) {
    logger.warn(`Failed to create composite_key constraints: ${messageOf(e)}`);
  }
  return candidate;
}

/**
 * Neo4j-backed knowledge graph index (port of ragleap_graph.GraphIndex), over the HTTP Query API.
 * Always safe to create: if the server cannot be reached, isAvailable() is false instead of throwing.
 * Create it with `await GraphIndex.connect(...)`: a JavaScript constructor cannot wait for the network.
 */
export class GraphIndex {
  #runner: CypherRunner | null;
  #logger: GraphLogger;
  #timing: UpsertTiming;

  /** Internal: use GraphIndex.connect(). */
  constructor(runner: CypherRunner | null, logger: GraphLogger, timing: UpsertTiming = DEFAULT_TIMING) {
    this.#runner = runner;
    this.#logger = logger;
    this.#timing = timing;
  }

  /**
   * source: a GraphConfig (opens the HTTP runner), a ready CypherRunner (for tests or custom runners),
   * null (an unavailable index), or nothing (the default local GraphConfig).
   */
  static async connect(source?: GraphConfig | CypherRunner | null, options: GraphIndexOptions = {}): Promise<GraphIndex> {
    const logger = options.logger ?? DEFAULT_LOGGER;
    const resolved = source === undefined ? new GraphConfig() : source;
    const candidate = resolved instanceof GraphConfig ? openRunner(resolved, logger) : resolved;
    const runner = candidate === null ? null : await connectRunner(candidate, logger);
    return new GraphIndex(runner, logger, {
      sleep: options.sleep ?? DEFAULT_TIMING.sleep,
      random: options.random ?? DEFAULT_TIMING.random,
    });
  }

  isAvailable(): boolean {
    return this.#runner !== null;
  }

  /** True if the server answers a trivial query. */
  async healthCheck(): Promise<boolean> {
    if (this.#runner === null) return false;
    try {
      await this.#runner.run("RETURN 1", {});
      return true;
    } catch (e) {
      this.#logger.warn(`Graph health check failed: ${messageOf(e)}`);
      return false;
    }
  }

  close(): void {
    if (this.#runner !== null) this.#runner.close();
  }

  async [Symbol.asyncDispose](): Promise<void> {
    this.close();
  }

  /** Entity candidates from a user query string (regex based, like Python extract_query_entities). */
  extractQueryEntities(
    query: string,
    maxEntities = 10,
    domainTerms: readonly string[] | null = null,
  ): string[] {
    return extractQueryEntitiesFn(query, maxEntities, domainTerms);
  }

  /**
   * Indexes one document (regex entity extraction): the document, its entities, per-document pair weights and the
   * summed CO_OCCURS_WITH edges. Idempotent: re-running replaces this document's earlier contribution.
   * Retries up to 3 times on transient Neo4j errors. Never throws for Neo4j failures: see summary.error.
   */
  async upsertDocument(
    documentId: string,
    title: string | null | undefined,
    chunks: readonly UpsertChunk[] | null | undefined,
    options: UpsertOptions = {},
  ): Promise<UpsertSummary> {
    return upsertDocumentWithRetry(this.#runner, this.#logger, this.#timing, documentId, title, chunks, options);
  }
}
