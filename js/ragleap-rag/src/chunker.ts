export const DEFAULT_CHUNK_SIZE = 512;
export const DEFAULT_CHUNK_OVERLAP = 50;

export interface TextChunk {
  text: string;
  chunk_index: number;
  token_count: number;
  token_count_is_exact: boolean;
}

export interface TextChunkerOptions {
  chunk_size?: number | null;
  chunk_overlap?: number | null;
}

export function realTokenCount(text: string): {
  token_count: number;
  token_count_is_exact: false;
} {
  // Honest fallback until a compatible tokenizer is integrated.
  return {
    token_count: text.trim() === "" ? 0 : text.trim().split(/\s+/u).length,
    token_count_is_exact: false,
  };
}

export class TextChunker {
  readonly chunk_size: number;
  readonly chunk_overlap: number;

  constructor(options: TextChunkerOptions = {}) {
    this.chunk_size = options.chunk_size || DEFAULT_CHUNK_SIZE;
    this.chunk_overlap = options.chunk_overlap || DEFAULT_CHUNK_OVERLAP;

    if (this.chunk_overlap >= this.chunk_size) {
      throw new RangeError("Chunk overlap must be less than chunk size");
    }
  }

  private tokenize(text: string): string[] {
    return text.trim().split(/\s+/u).filter(Boolean);
  }

  chunk_text(text: string): TextChunk[] {
    if (!text || !text.trim()) return [];

    const tokens = this.tokenize(text);
    if (tokens.length === 0) return [];

    const chunks: TextChunk[] = [];
    const step = this.chunk_size - this.chunk_overlap;

    for (let start = 0; start < tokens.length; start += step) {
      const end = Math.min(start + this.chunk_size, tokens.length);
      const chunkText = tokens.slice(start, end).join(" ");
      const count = realTokenCount(chunkText);

      chunks.push({
        text: chunkText,
        chunk_index: chunks.length,
        token_count: count.token_count,
        token_count_is_exact: count.token_count_is_exact,
      });

      if (end === tokens.length) break;
    }

    return chunks;
  }
}

export function create_chunker(
  chunk_size?: number | null,
  chunk_overlap?: number | null,
): TextChunker {
  return new TextChunker({ chunk_size, chunk_overlap });
}
