import { Pool } from "pg";
import type { QueryResultRow } from "pg";
import type {
  DocumentRecord,
  Metadata,
  VectorBackend,
  VectorChunk,
} from "./vectorstores.js";

export const DEFAULT_PGVECTOR_DIMENSIONS = 3072;
export const DEFAULT_MIN_SIMILARITY = 0.05;
export const RRF_K = 60;

export interface Queryable {
  query(
    text: string,
    values?: unknown[],
  ): Promise<{ rows: QueryResultRow[]; rowCount: number | null }>;
}

export interface PgVectorOptions {
  databaseUrl?: string;
  minSimilarity?: number;
  pool?: Queryable & { end?: () => Promise<void> };
}

function validateDimensions(dimensions: number): void {
  if (!Number.isSafeInteger(dimensions) || dimensions < 1 || dimensions > 16000) {
    throw new RangeError("dimensions must be an integer between 1 and 16000");
  }
}

function vectorLiteral(embedding: number[]): string {
  if (!embedding.every(Number.isFinite)) {
    throw new TypeError("Embedding values must all be finite numbers");
  }
  return `[${embedding.map(Number).join(",")}]`;
}

function mapChunk(row: QueryResultRow): VectorChunk {
  return {
    chunk_id: String(row.chunk_id),
    text: String(row.text),
    similarity_score: Number(Number(row.similarity_score).toFixed(4)),
    document_id: String(row.document_id),
    document_name: String(row.document_name),
    chunk_index: Number(row.chunk_index),
  };
}

export class PgVectorBackend implements VectorBackend {
  private readonly pool: Queryable & { end?: () => Promise<void> };
  private readonly ownsPool: boolean;
  private dimensions: number | null = null;
  readonly minSimilarity: number;

  constructor(options: PgVectorOptions = {}) {
    this.minSimilarity = options.minSimilarity ?? DEFAULT_MIN_SIMILARITY;

    if (!Number.isFinite(this.minSimilarity)) {
      throw new RangeError("minSimilarity must be finite");
    }

    if (options.pool) {
      this.pool = options.pool;
      this.ownsPool = false;
    } else if (options.databaseUrl) {
      this.pool = new Pool({ connectionString: options.databaseUrl });
      this.ownsPool = true;
    } else {
      throw new Error("Provide databaseUrl or an injected pool");
    }
  }

  async close(): Promise<void> {
    if (this.ownsPool && this.pool.end) {
      await this.pool.end();
    }
  }

  async init_schema(dimensions: number): Promise<void> {
    validateDimensions(dimensions);

    const sql = `
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

    await this.pool.query(sql);
    this.dimensions = dimensions;
  }

  async insert_document(
    document_id: string,
    filename: string,
    metadata: Metadata,
  ): Promise<void> {
    await this.pool.query(
      `INSERT INTO documents (id, filename, metadata)
       VALUES ($1, $2, $3::jsonb)`,
      [document_id, filename, JSON.stringify(metadata ?? {})],
    );
  }

  async insert_chunk(args: {
    document_id: string;
    document_name: string;
    chunk_index: number;
    text: string;
    token_count: number;
    embedding: number[];
    metadata: Metadata;
  }): Promise<void> {
    const embedding = vectorLiteral(args.embedding);

    await this.pool.query(
      `INSERT INTO chunks
        (document_id, document_name, chunk_index, text, token_count, embedding, metadata)
       VALUES ($1, $2, $3, $4, $5, $6::vector, $7::jsonb)`,
      [
        args.document_id,
        args.document_name,
        args.chunk_index,
        args.text,
        args.token_count,
        embedding,
        JSON.stringify(args.metadata ?? {}),
      ],
    );
  }

  async search_dense(args: {
    embedding: number[];
    top_k: number;
    document_id?: string | null;
    metadata_filter?: Metadata | null;
  }): Promise<VectorChunk[]> {
    if (!args.embedding.length || args.top_k <= 0) return [];

    const dimensions = this.dimensions ?? args.embedding.length;
    validateDimensions(dimensions);

    if (args.embedding.length !== dimensions) {
      throw new RangeError(
        `Embedding dimension mismatch: expected ${dimensions}, received ${args.embedding.length}`,
      );
    }

    const literal = vectorLiteral(args.embedding);
    const values: unknown[] = [dimensions, literal, dimensions];
    const filters: string[] = [];

    if (args.document_id) {
      values.push(args.document_id);
      filters.push(`document_id = $${values.length}`);
    }
    if (args.metadata_filter && Object.keys(args.metadata_filter).length > 0) {
      values.push(JSON.stringify(args.metadata_filter));
      filters.push(`metadata @> $${values.length}::jsonb`);
    }

    const where = filters.length ? ` WHERE ${filters.join(" AND ")}` : "";

    const orderDimensionsIndex = values.length + 1;
    const orderVectorIndex = values.length + 2;
    const orderVectorDimensionsIndex = values.length + 3;
    const limitIndex = values.length + 4;

    values.push(dimensions, literal, dimensions, args.top_k);

    const result = await this.pool.query(
      `SELECT id AS chunk_id, text, document_id, document_name, chunk_index,
          1 - (embedding::halfvec($1) <=> $2::halfvec($3)) / 2
            AS similarity_score
       FROM chunks${where}
       ORDER BY embedding::halfvec($${orderDimensionsIndex})
         <=> $${orderVectorIndex}::halfvec($${orderVectorDimensionsIndex})
       LIMIT $${limitIndex}`,
      values,
    );

    return result.rows
      .map(mapChunk)
      .filter((chunk) => chunk.similarity_score >= this.minSimilarity);
  }

  async search_sparse(args: {
    query_text: string;
    top_k: number;
    document_id?: string | null;
    metadata_filter?: Metadata | null;
  }): Promise<VectorChunk[]> {
    if (!args.query_text.trim() || args.top_k <= 0) return [];

    const values: unknown[] = [args.query_text, args.query_text];
    const filters: string[] = [];

    if (args.document_id) {
      values.push(args.document_id);
      filters.push(`document_id = $${values.length}`);
    }
    if (args.metadata_filter && Object.keys(args.metadata_filter).length) {
      values.push(JSON.stringify(args.metadata_filter));
      filters.push(`metadata @> $${values.length}::jsonb`);
    }

    const filter = filters.length ? ` AND ${filters.join(" AND ")}` : "";
    values.push(args.top_k);

    const result = await this.pool.query(
      `SELECT id AS chunk_id, text, document_id, document_name, chunk_index,
          ts_rank(
            text_search_vector,
            websearch_to_tsquery('english', $1)
          ) AS similarity_score
       FROM chunks
       WHERE text_search_vector @@ websearch_to_tsquery('english', $2)
       ${filter}
       ORDER BY similarity_score DESC
       LIMIT $${values.length}`,
      values,
    );

    return result.rows.map(mapChunk);
  }

  async search_hybrid(args: {
    query_text: string;
    embedding: number[];
    top_k: number;
    document_id?: string | null;
    metadata_filter?: Metadata | null;
  }): Promise<VectorChunk[]> {
    if (args.top_k <= 0) return [];

    const [dense, sparse] = await Promise.all([
      this.search_dense({
        embedding: args.embedding,
        top_k: args.top_k * 3,
        document_id: args.document_id,
        metadata_filter: args.metadata_filter,
      }),
      this.search_sparse({
        query_text: args.query_text,
        top_k: args.top_k * 3,
        document_id: args.document_id,
        metadata_filter: args.metadata_filter,
      }),
    ]);

    const denseRanks = new Map(dense.map((chunk, i) => [chunk.chunk_id, i]));
    const sparseRanks = new Map(sparse.map((chunk, i) => [chunk.chunk_id, i]));
    const lookup = new Map<string, VectorChunk>();

    for (const chunk of dense) lookup.set(chunk.chunk_id, chunk);
    for (const chunk of sparse) {
      if (!lookup.has(chunk.chunk_id)) lookup.set(chunk.chunk_id, chunk);
    }

    const ids = new Set([...denseRanks.keys(), ...sparseRanks.keys()]);
    const results: VectorChunk[] = [];

    for (const id of ids) {
      let score = 0;
      const denseRank = denseRanks.get(id);
      const sparseRank = sparseRanks.get(id);

      if (denseRank !== undefined) score += 1 / (RRF_K + denseRank + 1);
      if (sparseRank !== undefined) score += 1 / (RRF_K + sparseRank + 1);

      const chunk = lookup.get(id);
      if (chunk) {
        results.push({
          ...chunk,
          similarity_score: Number(score.toFixed(6)),
          retrieval_method: "hybrid_rrf",
        });
      }
    }

    results.sort((a, b) => b.similarity_score - a.similarity_score);
    return results.slice(0, args.top_k);
  }

  supports_sparse(): boolean {
    return true;
  }

  async list_documents(args: {
    limit: number;
    offset: number;
  }): Promise<DocumentRecord[]> {
    const result = await this.pool.query(
      `SELECT d.id, d.filename, d.uploaded_at, d.metadata,
          COUNT(c.id)::int AS chunk_count
       FROM documents d
       LEFT JOIN chunks c ON c.document_id = d.id
       GROUP BY d.id, d.filename, d.uploaded_at, d.metadata
       ORDER BY d.uploaded_at DESC
       LIMIT $1 OFFSET $2`,
      [args.limit, args.offset],
    );

    return result.rows.map((row) => ({
      document_id: String(row.id),
      filename: String(row.filename),
      uploaded_at: row.uploaded_at,
      metadata: row.metadata ?? {},
      chunk_count: Number(row.chunk_count),
    }));
  }

  async delete_document(document_id: string): Promise<boolean> {
    const result = await this.pool.query(
      "DELETE FROM documents WHERE id = $1",
      [document_id],
    );
    return (result.rowCount ?? 0) > 0;
  }

  async get_document_filename(document_id: string): Promise<string | null> {
    const result = await this.pool.query(
      "SELECT filename FROM documents WHERE id = $1",
      [document_id],
    );
    return result.rows[0] ? String(result.rows[0].filename) : null;
  }
}
