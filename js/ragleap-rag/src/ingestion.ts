import { TextChunker } from "./chunker.js";
import type { TextChunkerOptions } from "./chunker.js";
import { EmbeddingService } from "./embedding.js";
import type { Metadata, VectorBackend } from "./vectorstores.js";
import { sanitize_text, detect_injection_risk } from "./sanitization.js";
import { fire_event } from "./observability.js";
import type { EventHandler } from "./observability.js";

export interface IngestResult {
  document_id: string;
  chunks_stored: number;
}

export interface IngestEvent {
  document_id: string;
  filename: string;
  chunks_stored: number;
  chunks_attempted: number;
}

export type TextGuardrail = (text: string) => string;

export interface IngestionServiceOptions {
  chunker?: TextChunker;
  chunker_options?: TextChunkerOptions;
  embedder: Pick<EmbeddingService, "embed_text">;
  vector_backend: Pick<
    VectorBackend,
    | "insert_document"
    | "insert_chunk"
    | "delete_document"
    | "get_document_filename"
  >;
  input_guardrails?: readonly TextGuardrail[] | null;
  on_ingest?: readonly EventHandler<IngestEvent>[] | null;
}

export class IngestionService {
  private readonly chunker: TextChunker;
  private readonly embedder: Pick<EmbeddingService, "embed_text">;
  private readonly vector_backend: IngestionServiceOptions["vector_backend"];
  private readonly input_guardrails: readonly TextGuardrail[];
  private readonly on_ingest: readonly EventHandler<IngestEvent>[] | null | undefined;

  constructor(options: IngestionServiceOptions) {
    this.chunker = options.chunker ?? new TextChunker(options.chunker_options);
    this.embedder = options.embedder;
    this.vector_backend = options.vector_backend;
    this.input_guardrails = options.input_guardrails ?? [];
    this.on_ingest = options.on_ingest;
  }

  async update_document(
    document_id: string,
    text: string,
    filename?: string | null,
  ): Promise<IngestResult> {
    let existing_filename: string;

    if (filename !== undefined && filename !== null) {
      existing_filename = filename;
    } else {
      const stored_filename =
        await this.vector_backend.get_document_filename(document_id);

      if (stored_filename === null || stored_filename === undefined) {
        throw new Error(`No document found with id ${document_id}`);
      }

      existing_filename = stored_filename;
    }

    await this.vector_backend.delete_document(document_id);
    return this.ingest_text(existing_filename, text);
  }

  async ingest_text(
    filename: string,
    text: string,
    metadata: Metadata | null = {},
    sanitize = true,
    warn_on_injection_risk = true,
  ): Promise<IngestResult> {
    if (sanitize) text = sanitize_text(text);

    if (warn_on_injection_risk) {
      const matches = detect_injection_risk(text);
      if (matches.length) {
        console.warn(
          `Possible prompt-injection content in '${filename}': matched phrase(s) ${matches.join(", ")}. This is a heuristic signal, not a guarantee the content is safe.`,
        );
      }
    }

    for (const guardrail of this.input_guardrails) {
      text = guardrail(text);
      if (typeof text !== "string") {
        throw new TypeError("Input guardrails must return a string");
      }
    }

    const chunks = this.chunker.chunk_text(text);
    if (!chunks.length) {
      throw new Error("No chunks produced from input text — is it empty?");
    }

    const document_id = globalThis.crypto.randomUUID();
    const document_metadata = metadata ?? {};
    await this.vector_backend.insert_document(
      document_id,
      filename,
      document_metadata,
    );

    let stored = 0;
    try {
      for (const chunk of chunks) {
        const embedding = await this.embedder.embed_text(chunk.text);
        if (embedding === null) {
          console.warn(`Skipping chunk ${chunk.chunk_index} — embedding failed`);
          continue;
        }

        await this.vector_backend.insert_chunk({
          document_id,
          document_name: filename,
          chunk_index: chunk.chunk_index,
          text: chunk.text,
          token_count: chunk.token_count,
          embedding,
          metadata: document_metadata,
        });
        stored += 1;
      }

      if (stored === 0) {
        throw new Error(
          `All ${chunks.length} chunk(s) failed to embed — nothing was stored.`,
        );
      }
    } catch (error) {
      try {
        await this.vector_backend.delete_document(document_id);
      } catch (cleanupError) {
        console.warn("Failed to clean up incomplete ingestion:", cleanupError);
      }
      throw error;
    }

    fire_event(
      {
        document_id,
        filename,
        chunks_stored: stored,
        chunks_attempted: chunks.length,
      },
      this.on_ingest,
      "on_ingest",
    );

    return { document_id, chunks_stored: stored };
  }
}
