import type { Queryable } from "./pgvector.js";
import { DEFAULT_PGVECTOR_DIMENSIONS } from "./pgvector.js";

export const MEMORY_SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS conversations (
    session_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id TEXT NOT NULL REFERENCES conversations(session_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS conversation_messages_session_idx
    ON conversation_messages (session_id, created_at);
`;

export function get_memory_schema_sql(): string {
  return MEMORY_SCHEMA_SQL;
}

export function get_core_schema_sql(
  dimensions = DEFAULT_PGVECTOR_DIMENSIONS,
): string {
  if (!Number.isSafeInteger(dimensions) || dimensions < 1 || dimensions > 16000) {
    throw new RangeError("dimensions must be an integer between 1 and 16000");
  }

  return `
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filename TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    document_name TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER,
    embedding vector(${dimensions}),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    text_search_vector tsvector GENERATED ALWAYS AS
      (to_tsvector('english', text)) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE documents ADD COLUMN IF NOT EXISTS
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw
    ((embedding::halfvec(${dimensions})) halfvec_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_text_search_idx
    ON chunks USING GIN (text_search_vector);
CREATE INDEX IF NOT EXISTS chunks_metadata_idx
    ON chunks USING GIN (metadata);
CREATE INDEX IF NOT EXISTS documents_metadata_idx
    ON documents USING GIN (metadata);
`;
}

export function get_schema_sql(
  dimensions = DEFAULT_PGVECTOR_DIMENSIONS,
): string {
  return get_core_schema_sql(dimensions) + get_memory_schema_sql();
}

export async function init_core_schema(
  database: Queryable,
  dimensions = DEFAULT_PGVECTOR_DIMENSIONS,
): Promise<void> {
  await database.query(get_core_schema_sql(dimensions));
}

export async function init_memory_schema(database: Queryable): Promise<void> {
  await database.query(get_memory_schema_sql());
}

export async function init_schema(
  database: Queryable,
  dimensions = DEFAULT_PGVECTOR_DIMENSIONS,
): Promise<void> {
  await init_core_schema(database, dimensions);
  await init_memory_schema(database);
}
