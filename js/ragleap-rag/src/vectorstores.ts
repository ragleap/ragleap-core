/**
 * Vector storage contract for @ragleap/rag.
 *
 * Mirrors the Python VectorBackend interface. Concrete backends should
 * implement these methods; sparse search is optional.
 */

export type Metadata = Record<string, unknown>;

export interface VectorChunk {
  chunk_id: string;
  text: string;
  similarity_score: number;
  document_id: string;
  document_name: string;
  chunk_index: number;
  token_count?: number;
  metadata?: Metadata;
  [key: string]: unknown;
}

export interface DocumentRecord {
  document_id: string;
  filename: string;
  uploaded_at?: string | Date;
  chunk_count?: number;
  metadata?: Metadata;
  [key: string]: unknown;
}

export interface VectorBackend {
  /** Create or verify the backend's storage structures. Must be idempotent. */
  init_schema(dimensions: number): Promise<void> | void;

  /** Register a parent document before inserting its chunks. */
  insert_document(
    document_id: string,
    filename: string,
    metadata: Metadata,
  ): Promise<void> | void;

  /** Store one chunk and its embedding. */
  insert_chunk(args: {
    document_id: string;
    document_name: string;
    chunk_index: number;
    text: string;
    token_count: number;
    embedding: number[];
    metadata: Metadata;
  }): Promise<void> | void;

  /** Dense vector similarity search. */
  search_dense(args: {
    embedding: number[];
    top_k: number;
    document_id?: string | null;
    metadata_filter?: Metadata | null;
  }): Promise<VectorChunk[]> | VectorChunk[];

  /** Keyword/full-text search. Unsupported by default. */
  search_sparse?(args: {
    query_text: string;
    top_k: number;
    document_id?: string | null;
    metadata_filter?: Metadata | null;
  }): Promise<VectorChunk[]> | VectorChunk[];

  /** Combined search. Implementations may fall back to dense search. */
  search_hybrid?(args: {
    query_text: string;
    embedding: number[];
    top_k: number;
    document_id?: string | null;
    metadata_filter?: Metadata | null;
  }): Promise<VectorChunk[]> | VectorChunk[];

  /** Explicit capability flag; defaults to false at the helper level. */
  supports_sparse?(): boolean;

  /** List documents, most recent first. */
  list_documents(args: {
    limit: number;
    offset: number;
  }): Promise<DocumentRecord[]> | DocumentRecord[];

  /** Delete a document and its chunks. */
  delete_document(document_id: string): Promise<boolean> | boolean;

  /** Return the stored filename, or null when the document is missing. */
  get_document_filename(
    document_id: string,
  ): Promise<string | null> | string | null;
}

/**
 * Dense-only fallback matching Python's default search_hybrid behavior.
 * Use this when a backend has no hybrid-search implementation.
 */
export async function search_hybrid_fallback(
  backend: Pick<VectorBackend, "search_dense">,
  args: {
    query_text: string;
    embedding: number[];
    top_k: number;
    document_id?: string | null;
    metadata_filter?: Metadata | null;
  },
): Promise<VectorChunk[]> {
  return await backend.search_dense({
    embedding: args.embedding,
    top_k: args.top_k,
    document_id: args.document_id,
    metadata_filter: args.metadata_filter,
  });
}

/** True only when the backend explicitly advertises sparse-search support. */
export function backend_supports_sparse(
  backend: Pick<VectorBackend, "supports_sparse">,
): boolean {
  return backend.supports_sparse?.() ?? false;
}
