import { QueryEmbeddingCache } from "./cache.js";
import { ConversationMemory } from "./memory.js";
import type { MemoryPool } from "./memory.js";
import type { CacheStats } from "./cache.js";
import { EmbeddingService } from "./embedding.js";
import { GenerationService } from "./generation.js";
import type {
  GenerateAnswerOptions,
  GenerationResult,
} from "./generation.js";
import { IngestionService } from "./ingestion.js";
import type {
  IngestResult,
  IngestEvent,
  TextGuardrail,
} from "./ingestion.js";
import { fire_event } from "./observability.js";
import type { EventHandler } from "./observability.js";
import { PgVectorBackend, DEFAULT_PGVECTOR_DIMENSIONS } from "./pgvector.js";
import type { Queryable } from "./pgvector.js";
import { init_schema } from "./schema.js";
import type {
  Metadata,
  VectorBackend,
  VectorChunk,
} from "./vectorstores.js";

export interface RagLeapOptions {
  embedder: Pick<EmbeddingService, "embed_text" | "model" | "dimensions">;
  generation: Pick<GenerationService, "generate_answer">;
  vector_backend: VectorBackend;
  conversation_memory?: Pick<
    ConversationMemory,
    "get_history" | "build_history_prompt" | "add_message"
  > | null;
  dimensions?: number;
  cache_enabled?: boolean;
  cache_max_size?: number;
  input_guardrails?: readonly TextGuardrail[] | null;
  output_guardrails?: readonly TextGuardrail[] | null;
  on_ingest?: readonly EventHandler<IngestEvent>[] | null;
  on_query?: readonly EventHandler<Record<string, unknown>>[] | null;
  on_answer?: readonly EventHandler<Record<string, unknown>>[] | null;
}

export interface RetrieveOptions {
  top_k?: number;
  hybrid?: boolean;
  document_id?: string | null;
  metadata_filter?: Metadata | null;
}

export interface AskOptions extends RetrieveOptions, GenerateAnswerOptions {
  session_id?: string | null;
}

export interface RagLeapAnswer extends GenerationResult {
  chunks: VectorChunk[];
  query: string;
}

export class RagLeap {
  readonly embedder: RagLeapOptions["embedder"];
  readonly generation: RagLeapOptions["generation"];
  readonly vector_backend: VectorBackend;
  readonly conversation_memory: RagLeapOptions["conversation_memory"];
  readonly ingestion: IngestionService;
  readonly cache: QueryEmbeddingCache | null;
  readonly dimensions: number;

  private readonly input_guardrails: readonly TextGuardrail[];
  private readonly output_guardrails: readonly TextGuardrail[];
  private readonly on_query: RagLeapOptions["on_query"];
  private readonly on_answer: RagLeapOptions["on_answer"];

  constructor(options: RagLeapOptions) {
    this.embedder = options.embedder;
    this.generation = options.generation;
    this.vector_backend = options.vector_backend;
    this.conversation_memory = options.conversation_memory ?? null;
    this.dimensions =
      options.dimensions ??
      options.embedder.dimensions ??
      DEFAULT_PGVECTOR_DIMENSIONS;

    if (
      !Number.isSafeInteger(this.dimensions) ||
      this.dimensions < 1 ||
      this.dimensions > 16000
    ) {
      throw new RangeError("dimensions must be an integer between 1 and 16000");
    }

    this.cache = options.cache_enabled === false
      ? null
      : new QueryEmbeddingCache(options.cache_max_size ?? 1000);

    this.input_guardrails = options.input_guardrails ?? [];
    this.output_guardrails = options.output_guardrails ?? [];
    this.on_query = options.on_query;
    this.on_answer = options.on_answer;

    this.ingestion = new IngestionService({
      embedder: this.embedder,
      vector_backend: this.vector_backend,
      input_guardrails: this.input_guardrails,
      on_ingest: options.on_ingest,
    });
  }

  async init_schema(): Promise<void> {
    await this.vector_backend.init_schema(this.dimensions);
  }

  async ingest_text(
    filename: string,
    text: string,
    metadata: Metadata | null = {},
    sanitize = true,
    warn_on_injection_risk = true,
  ): Promise<IngestResult> {
    return this.ingestion.ingest_text(
      filename,
      text,
      metadata,
      sanitize,
      warn_on_injection_risk,
    );
  }

  cache_stats(): CacheStats {
    return this.cache?.stats() ?? {
      hits: 0,
      misses: 0,
      hit_rate: 0,
      size: 0,
    };
  }

  private async embed_query(query: string): Promise<number[]> {
    const model = this.embedder.model ?? "default";
    const cached = this.cache?.get(query, model);

    if (cached !== null && cached !== undefined) {
      return cached;
    }

    const embedding = await this.embedder.embed_text(query);
    if (embedding === null) {
      throw new Error("Query embedding failed; retrieval cannot continue.");
    }

    this.cache?.set(query, model, embedding);
    return embedding;
  }

  async retrieve(
    query: string,
    options: RetrieveOptions = {},
  ): Promise<VectorChunk[]> {
    if (!query.trim()) {
      throw new TypeError("query must not be empty");
    }

    const top_k = options.top_k ?? 5;
    if (!Number.isSafeInteger(top_k) || top_k < 0) {
      throw new RangeError("top_k must be a non-negative integer");
    }
    if (top_k === 0) return [];

    const embedding = await this.embed_query(query);
    const searchArgs = {
      query_text: query,
      embedding,
      top_k,
      document_id: options.document_id,
      metadata_filter: options.metadata_filter,
    };

    const chunks = options.hybrid !== false && this.vector_backend.search_hybrid
      ? await this.vector_backend.search_hybrid(searchArgs)
      : await this.vector_backend.search_dense({
          embedding,
          top_k,
          document_id: options.document_id,
          metadata_filter: options.metadata_filter,
        });

    fire_event(
      { query, top_k, result_count: chunks.length },
      this.on_query,
      "on_query",
    );

    return chunks;
  }

  async ask(
    query: string,
    options: AskOptions = {},
  ): Promise<RagLeapAnswer> {
    const chunks = await this.retrieve(query, options);

    const session_id = options.session_id?.trim();
    let history_prefix = options.history_prefix;

    if (session_id && this.conversation_memory) {
      const prior_history =
        await this.conversation_memory.build_history_prompt(session_id);
      history_prefix = `${prior_history}${history_prefix ?? ""}`;
    }

    const result = await this.generation.generate_answer(query, chunks, {
      temperature: options.temperature,
      system_prompt: options.system_prompt,
      max_tokens: options.max_tokens,
      history_prefix,
      override_provider: options.override_provider,
      response_format: options.response_format,
    });

    let answer = result.answer;
    for (const guardrail of this.output_guardrails) {
      answer = guardrail(answer);
      if (typeof answer !== "string") {
        throw new TypeError("Output guardrails must return a string");
      }
    }

    const response: RagLeapAnswer = { ...result, answer, chunks, query };

    if (session_id && this.conversation_memory) {
      await this.conversation_memory.add_message(session_id, "user", query);
      await this.conversation_memory.add_message(session_id, "assistant", answer);
    }

    fire_event(
      {
        query,
        answer,
        sources: response.sources,
        provider_used: response.provider_used,
        chunks_used: response.chunks_sent,
      },
      this.on_answer,
      "on_answer",
    );

    return response;
  }

  async close(): Promise<void> {
    const backend = this.vector_backend as VectorBackend & {
      close?: () => Promise<void>;
    };
    await backend.close?.();
  }

  /**
   * Initialize the complete core + conversation schema on a supplied database
   * connection. Use init_schema() when only the vector backend's schema is
   * required.
   */
  static async init_database_schema(
    database: Queryable,
    dimensions = DEFAULT_PGVECTOR_DIMENSIONS,
  ): Promise<void> {
    await init_schema(database, dimensions);
  }

  /**
   * Convenience factory for the built-in PostgreSQL/pgvector backend.
   * The returned facade owns the backend pool it creates.
   */
  static with_pgvector(
    options: Omit<RagLeapOptions, "vector_backend"> & {
      database_url?: string;
      pool?: MemoryPool;
    },
  ): RagLeap {
    const { database_url, pool, ...rest } = options;

    if (!pool && !database_url) {
      throw new Error("Provide database_url or an injected pool");
    }

    const vector_backend = new PgVectorBackend(
      pool ? { pool } : { databaseUrl: database_url },
    );

    const conversation_memory =
      rest.conversation_memory ??
      (pool ? new ConversationMemory(pool) : undefined);

    return new RagLeap({
      ...rest,
      vector_backend,
      conversation_memory,
    });
  }
}
