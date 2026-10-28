/**
 * Reciprocal Rank Fusion for combining ranked retrieval results.
 * Matches Python ragleap.rag.query_rewrite.reciprocal_rank_fusion.
 */

export interface RankedChunk {
  chunk_id?: string | null;
  document_id?: string | null;
  chunk_index?: number | null;
  [key: string]: unknown;
}

function fusionKey(chunk: RankedChunk): string {
  if (chunk.chunk_id) {
    return `chunk:${JSON.stringify(chunk.chunk_id)}`;
  }

  return `fallback:${JSON.stringify([
    chunk.document_id ?? null,
    chunk.chunk_index ?? null,
  ])}`;
}

/**
 * Merge ranked lists, deduplicating by chunk_id or by
 * (document_id, chunk_index). The first chunk encountered for a key wins.
 * Scores are used only for ordering; returned objects are not modified.
 */
export function reciprocal_rank_fusion<T extends RankedChunk>(
  ranked_lists: readonly (readonly T[])[],
  k = 60,
): T[] {
  const scores = new Map<string, number>();
  const firstChunk = new Map<string, T>();

  for (const rankedList of ranked_lists) {
    rankedList.forEach((chunk, rank) => {
      const key = fusionKey(chunk);
      scores.set(key, (scores.get(key) ?? 0) + 1 / (k + rank + 1));

      if (!firstChunk.has(key)) {
        firstChunk.set(key, chunk);
      }
    });
  }

  return [...scores.keys()]
    .sort((a, b) => scores.get(b)! - scores.get(a)!)
    .map((key) => firstChunk.get(key)!);
}


/**
 * Generate alternative phrasings for retrieval, preserving the original query
 * as the first variant. Mirrors Python ragleap.rag.query_rewrite.
 */
export async function multi_query_variants(
  generator: {
    generate_answer: (
      query: string,
      chunks: import("./generation.js").GenerationChunk[],
      options?: import("./generation.js").GenerateAnswerOptions,
    ) => Promise<import("./generation.js").GenerationResult>;
  },
  query: string,
  n = 3,
  override_provider?: import("./generation.js").ProviderConfig | null,
): Promise<[string[], import("./generation.js").GenerationResult | null]> {
  const prompt =
    `Generate ${n} different phrasings of the following question, capturing different angles or wordings a user might use to ask about the same underlying information need. Respond with ONLY the alternative questions, one per line, no numbering or extra text.\n\n` +
    `Original question: ${query}\n\n` +
    `Alternative phrasings:`;

  try {
    const result = await generator.generate_answer(prompt, [], {
      history_prefix: "",
      override_provider,
      system_prompt:
        "Follow the instructions in the question below exactly. Do not add commentary, explanation, or a preamble - respond with only what was asked for.",
    });

    const lines = (result.answer || "")
      .split(/\r?\n/)
      .map((line) => line.replace(/^[\s-*]+|[\s-*]+$/g, ""))
      .filter((line) => line.length > 0);

    const variants = [
      query,
      ...lines.filter((line) => line.toLowerCase() !== query.toLowerCase()),
    ];

    return [variants.slice(0, n), result];
  } catch (error) {
    console.warn(
      `Multi-query generation failed, using original query only: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
    return [[query], null];
  }
}


/**
 * Generate a hypothetical passage for retrieval embedding.
 * Mirrors Python ragleap.rag.query_rewrite.hyde_document.
 */
export async function hyde_document(
  generator: {
    generate_answer: (
      query: string,
      chunks: import("./generation.js").GenerationChunk[],
      options?: import("./generation.js").GenerateAnswerOptions,
    ) => Promise<import("./generation.js").GenerationResult>;
  },
  query: string,
  override_provider?: import("./generation.js").ProviderConfig | null,
): Promise<[string, import("./generation.js").GenerationResult | null]> {
  const prompt =
    "Write a short, plausible passage that would answer the following question, as if it were an excerpt from a real document. Write confidently and specifically, even if you're not certain of the real answer - this passage is used only to improve document retrieval matching, it is never shown to the user.\n\n" +
    `Question: ${query}\n\nPassage:`;

  try {
    const result = await generator.generate_answer(prompt, [], {
      history_prefix: "",
      override_provider,
      system_prompt:
        "Follow the instructions in the question below exactly. Do not add commentary, explanation, or a preamble - respond with only what was asked for.",
    });

    const hypothetical = (result.answer || "").trim();
    return [hypothetical || query, result];
  } catch (error) {
    console.warn(
      `HyDE passage generation failed, using original query for retrieval: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
    return [query, null];
  }
}


/**
 * Rewrite a follow-up into a standalone retrieval query using conversation
 * history. Mirrors Python ragleap.rag.query_rewrite.contextual_rewrite.
 */
export async function contextual_rewrite(
  generator: {
    generate_answer: (
      query: string,
      chunks: import("./generation.js").GenerationChunk[],
      options?: import("./generation.js").GenerateAnswerOptions,
    ) => Promise<import("./generation.js").GenerationResult>;
  },
  query: string,
  history_prefix: string,
  override_provider?: import("./generation.js").ProviderConfig | null,
): Promise<[string, import("./generation.js").GenerationResult | null]> {
  if (!history_prefix) {
    return [query, null];
  }

  const prompt =
    "Given the conversation history and a follow-up question, rewrite the follow-up question to be a standalone question that includes all necessary context from the history. Do not answer the question - only rewrite it. If the question is already standalone, return it unchanged. Respond with ONLY the rewritten question, nothing else.\n\n" +
    `${history_prefix}\n` +
    `Follow-up question: ${query}\n\n` +
    "Standalone question:";

  try {
    const result = await generator.generate_answer(prompt, [], {
      history_prefix: "",
      override_provider,
      system_prompt:
        "Follow the instructions in the question below exactly. Do not add commentary, explanation, or a preamble - respond with only what was asked for.",
    });

    const rewritten = (result.answer || "").trim();
    return [rewritten || query, result];
  } catch (error) {
    console.warn(
      `Contextual query rewrite failed, using original query for retrieval: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
    return [query, null];
  }
}
