export const RAGLEAP_TS_VERSION = "0.1.0";

export {
  DEFAULT_CHUNK_SIZE,
  DEFAULT_CHUNK_OVERLAP,
  TextChunker,
  create_chunker,
  realTokenCount,
} from "./chunker.js";

export type { TextChunk, TextChunkerOptions } from "./chunker.js";

export {
  DEFAULT_MAX_CHUNK_LENGTH,
  INJECTION_PATTERNS,
  sanitize_text,
  detect_injection_risk,
  check_length,
} from "./sanitization.js";

export {
  configure_json_schema_validator,
  parse_and_validate,
  parse_and_validate_object,
} from "./structured.js";

export type {
  JsonSchemaValidator,
  ValidationMethod,
  ValidationResult,
} from "./structured.js";

export { fire_event } from "./observability.js";

export { IngestionService } from "./ingestion.js";
export type { IngestResult, IngestEvent, IngestionServiceOptions, TextGuardrail } from "./ingestion.js";
export type { EventHandler } from "./observability.js";

export {
  ALL_EMBEDDING_PROVIDERS,
  CUSTOM_SHAPE_DEFAULT_BASE_URLS,
  CUSTOM_SHAPE_PROVIDERS,
  OPENAI_COMPATIBLE_BASE_URLS,
  EmbeddingConfig,
  EmbeddingService,
} from "./embedding.js";
export type {
  EmbeddingConfigOptions,
  EmbeddingProvider,
  EmbeddingVector,
  OptionalEmbedding,
} from "./embedding.js";

export {
  DEFAULT_SYSTEM_PROMPT,
  PROVIDER_BASE_URLS,
  GenerationService,
  ProviderConfig,
} from "./generation.js";
export type {
  GenerateAnswerOptions,
  GenerationChunk,
  GenerationResult,
  GenerationServiceOptions,
  GenerationUsage,
  ProviderConfigOptions,
} from "./generation.js";

export {
  PRICING_TABLE_VERIFIED_DATE,
  SEED_PRICING_TABLE,
  compute_cost,
  CostTracker,
} from "./cost.js";
export type { CostTrackerOptions, ModelRates, PricingTable, TokenUsage } from "./cost.js";

export { QueryEmbeddingCache } from "./cache.js";
export type { CacheStats } from "./cache.js";

export {
  backend_supports_sparse,
  search_hybrid_fallback,
} from "./vectorstores.js";
export type {
  DocumentRecord,
  Metadata,
  VectorBackend,
  VectorChunk,
} from "./vectorstores.js";

export {
  DEFAULT_MIN_SIMILARITY,
  DEFAULT_PGVECTOR_DIMENSIONS,
  PgVectorBackend,
  RRF_K,
} from "./pgvector.js";
export type { PgVectorOptions, Queryable } from "./pgvector.js";

export {
  DEFAULT_MAX_HISTORY_MESSAGES,
  ConversationMemory,
} from "./memory.js";
export type {
  ConversationMessage,
  MessageRole,
  MemoryPool,
  MemoryClient,
} from "./memory.js";

export {
  MEMORY_SCHEMA_SQL,
  get_core_schema_sql,
  get_memory_schema_sql,
  get_schema_sql,
  init_core_schema,
  init_memory_schema,
  init_schema,
} from "./schema.js";

export { reciprocal_rank_fusion } from "./query_rewrite.js";

export { multi_query_variants } from "./query_rewrite.js";

export { hyde_document } from "./query_rewrite.js";

export { contextual_rewrite } from './query_rewrite.js';

export { DEFAULT_RERANK_MODEL_REPO, RerankerService } from "./reranking.js";
export type { RerankChunk, RerankScorePair, RerankerServiceOptions } from "./reranking.js";

export { RagLeap } from "./ragleap.js";
export type {
  RagLeapOptions,
  RetrieveOptions,
  AskOptions,
  RagLeapAnswer,
} from "./ragleap.js";
