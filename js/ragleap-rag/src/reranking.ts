export const DEFAULT_RERANK_MODEL_REPO =
  "Xenova/ms-marco-MiniLM-L-6-v2";

export interface RerankChunk {
  text: string;
  rerank_score?: number;
  [key: string]: unknown;
}

export type RerankScorePair = (
  query: string,
  text: string,
) => number | Promise<number>;

export interface RerankerServiceOptions {
  model_repo?: string;
  score_pair?: RerankScorePair;
}

interface Tokenizer {
  (
    texts: string[],
    options: {
      text_pair: string[];
      padding: boolean;
      truncation: boolean;
      max_length: number;
    },
  ): unknown;
}

interface ModelOutput {
  logits?: { data?: ArrayLike<number> };
}

interface SequenceClassificationModel {
  (features: unknown): Promise<ModelOutput>;
}

interface TransformersModule {
  AutoTokenizer?: {
    from_pretrained(repo: string): Promise<Tokenizer>;
  };
  AutoModelForSequenceClassification?: {
    from_pretrained(repo: string): Promise<SequenceClassificationModel>;
  };
}

export class RerankerService {
  readonly model_repo: string;

  private readonly injectedScorer?: RerankScorePair;
  private scorerPromise?: Promise<RerankScorePair>;

  constructor(options: RerankerServiceOptions = {}) {
    this.model_repo =
      options.model_repo ?? DEFAULT_RERANK_MODEL_REPO;
    this.injectedScorer = options.score_pair;
  }

  private async _load_scorer(): Promise<RerankScorePair> {
    if (this.injectedScorer) return this.injectedScorer;

    const packageName = "@huggingface/transformers";
    let transformers: TransformersModule;

    try {
      transformers = (await import(packageName)) as TransformersModule;
    } catch (error) {
      const detail =
        error instanceof Error ? ` (${error.message})` : "";

      throw new Error(
        "Reranking requires the optional '@huggingface/transformers' " +
        "package. Install it with: npm install @huggingface/transformers" +
        detail,
      );
    }

    const tokenizerFactory = transformers.AutoTokenizer;
    const modelFactory = transformers.AutoModelForSequenceClassification;

    if (!tokenizerFactory || !modelFactory) {
      throw new Error(
        "The installed '@huggingface/transformers' package does not " +
        "provide the required tokenizer and sequence-classification APIs.",
      );
    }

    let tokenizer: Tokenizer;
    let model: SequenceClassificationModel;

    try {
      [tokenizer, model] = await Promise.all([
        tokenizerFactory.from_pretrained(this.model_repo),
        modelFactory.from_pretrained(this.model_repo),
      ]);
    } catch (error) {
      const detail =
        error instanceof Error ? `: ${error.message}` : "";

      throw new Error(
        `Failed to load reranker model '${this.model_repo}'${detail}`,
      );
    }

    return async (query, text) => {
      const features = tokenizer([query], {
        text_pair: [text],
        padding: true,
        truncation: true,
        max_length: 512,
      });

      const output = await model(features);
      const score = output.logits?.data?.[0];

      if (typeof score !== "number" || !Number.isFinite(score)) {
        throw new Error("Reranker model returned invalid logits.");
      }

      return score;
    };
  }

  private async _ensure_model_loaded(): Promise<RerankScorePair> {
    if (!this.scorerPromise) {
      this.scorerPromise = this._load_scorer().catch((error) => {
        this.scorerPromise = undefined;
        throw error;
      });
    }

    return this.scorerPromise;
  }

  async _score_pair(query: string, text: string): Promise<number> {
    const scorer = await this._ensure_model_loaded();
    const score = await scorer(query, text);

    if (typeof score !== "number" || !Number.isFinite(score)) {
      throw new Error("Reranker scorer must return a finite number.");
    }

    return score;
  }

  async rerank(
    query: string,
    chunks: RerankChunk[],
    top_k: number,
  ): Promise<RerankChunk[]> {
    if (!chunks.length) return chunks;

    for (const chunk of chunks) {
      const score = await this._score_pair(query, chunk.text);
      chunk.rerank_score = Math.round(score * 10_000) / 10_000;
    }

    return [...chunks]
      .sort(
        (a, b) =>
          (b.rerank_score ?? 0) - (a.rerank_score ?? 0),
      )
      .slice(0, Math.max(0, Math.trunc(top_k)));
  }
}
