export const OPENAI_COMPATIBLE_BASE_URLS: Readonly<Record<string, string>> = {
  openai: "https://api.openai.com/v1",
  mistral: "https://api.mistral.ai/v1",
  together: "https://api.together.xyz/v1",
  ollama: "http://localhost:11434/v1",
};

export const CUSTOM_SHAPE_DEFAULT_BASE_URLS: Readonly<Record<string, string>> = {
  cohere: "https://api.cohere.ai/v1",
  voyage: "https://api.voyageai.com/v1",
};

export const CUSTOM_SHAPE_PROVIDERS = ["cohere", "voyage"] as const;

export const ALL_EMBEDDING_PROVIDERS = [
  "gemini", "openai", "mistral", "together",
  "ollama", "cohere", "voyage", "custom",
] as const;

export type EmbeddingProvider = (typeof ALL_EMBEDDING_PROVIDERS)[number];
export type EmbeddingVector = number[];
export type OptionalEmbedding = EmbeddingVector | null;

export interface EmbeddingConfigOptions {
  provider: string;
  api_key?: string | null;
  model?: string | null;
  dimensions?: number | null;
  base_url?: string | null;
}

function hasOwnProvider(
  providers: Readonly<Record<string, string>>,
  provider: string,
): boolean {
  return Object.hasOwn(providers, provider);
}

function env(name: string): string | undefined {
  const processLike = (
    globalThis as typeof globalThis & {
      process?: { env?: Record<string, string | undefined> };
    }
  ).process;

  return processLike?.env?.[name];
}

function envDimensions(...names: string[]): number | undefined {
  for (const name of names) {
    const value = env(name);
    if (value) {
      const parsed = Number(value);
      if (!Number.isInteger(parsed)) {
        throw new Error(`Invalid integer value for ${name}: ${value}`);
      }
      return parsed;
    }
  }
  return undefined;
}

export class EmbeddingConfig {
  provider: string;
  api_key: string | null;
  model: string | null;
  dimensions: number | null;
  base_url: string | null;

  constructor(options: EmbeddingConfigOptions) {
    this.provider = options.provider.toLowerCase();
    this.api_key = options.api_key || null;
    this.model = options.model || null;
    this.dimensions = options.dimensions || null;
    this.base_url = options.base_url || null;

    const provider = this.provider;

    if (provider === "gemini") {
      this.api_key ||= env("GEMINI_API_KEY") || null;
      this.model ||= env("GEMINI_EMBEDDING_MODEL") || null;
      this.dimensions ||= envDimensions("EMBEDDING_DIMENSIONS") ?? null;
    } else if (hasOwnProvider(OPENAI_COMPATIBLE_BASE_URLS, provider)) {
      const prefix = provider.toUpperCase();
      this.api_key ||= env(`${prefix}_API_KEY`) || null;
      this.model ||= env(`${prefix}_EMBEDDING_MODEL`) || null;
      this.dimensions ||=
        envDimensions(`${prefix}_EMBEDDING_DIMENSIONS`, "EMBEDDING_DIMENSIONS") ?? null;
      this.base_url ||= OPENAI_COMPATIBLE_BASE_URLS[provider];
    } else if (hasOwnProvider(CUSTOM_SHAPE_DEFAULT_BASE_URLS, provider)) {
      const prefix = provider.toUpperCase();
      this.api_key ||= env(`${prefix}_API_KEY`) || null;
      this.model ||= env(`${prefix}_EMBEDDING_MODEL`) || null;
      this.dimensions ||=
        envDimensions(`${prefix}_EMBEDDING_DIMENSIONS`, "EMBEDDING_DIMENSIONS") ?? null;
      this.base_url ||= CUSTOM_SHAPE_DEFAULT_BASE_URLS[provider];
    } else if (provider === "custom") {
      this.api_key ||= env("CUSTOM_EMBEDDING_API_KEY") || null;
      this.model ||= env("CUSTOM_EMBEDDING_MODEL") || null;
      this.dimensions ||=
        envDimensions("CUSTOM_EMBEDDING_DIMENSIONS", "EMBEDDING_DIMENSIONS") ?? null;
      this.base_url ||= env("CUSTOM_EMBEDDING_BASE_URL") || null;

      if (!this.base_url) {
        throw new Error(
          "No base_url for provider 'custom'. Pass base_url explicitly to EmbeddingConfig(), or set CUSTOM_EMBEDDING_BASE_URL in your environment - it must point to an OpenAI-compatible /embeddings endpoint.",
        );
      }
    } else {
      throw new Error(
        `Unknown embedding provider '${provider}'. Supported: ${ALL_EMBEDDING_PROVIDERS.join(", ")}.`,
      );
    }

    if (!this.api_key && provider !== "ollama") {
      throw new Error(
        `No API key for embedding provider '${provider}'. Pass api_key explicitly to EmbeddingConfig(), or set ${provider.toUpperCase()}_API_KEY in your environment.`,
      );
    }

    if (!this.model) {
      throw new Error(
        `No embedding model specified for provider '${provider}'. Pass model explicitly to EmbeddingConfig(), or set ${provider.toUpperCase()}_EMBEDDING_MODEL in your environment. ragleap-rag never hardcodes a default embedding model - provider model availability and dimensions change too frequently for a baked-in default to stay reliable.`,
      );
    }

    if (!this.dimensions) {
      throw new Error(
        `No dimensions specified for embedding provider '${provider}' model '${this.model}'. Pass dimensions explicitly to EmbeddingConfig(), or set EMBEDDING_DIMENSIONS (or ${provider.toUpperCase()}_EMBEDDING_DIMENSIONS) in your environment - dimensions must match your chosen model exactly, and ragleap-rag can't safely guess it.`,
      );
    }
  }
}

type JsonObject = Record<string, unknown>;

async function postJson(
  url: string,
  body: unknown,
  headers: Record<string, string> = {},
): Promise<JsonObject> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(30_000),
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(`HTTP ${response.status} ${response.statusText}: ${detail}`);
  }

  return (await response.json()) as JsonObject;
}

function asVector(value: unknown): EmbeddingVector {
  if (
    !Array.isArray(value) ||
    !value.every((item) => typeof item === "number" && Number.isFinite(item))
  ) {
    throw new Error("Embedding provider returned an invalid vector");
  }
  return value as EmbeddingVector;
}

function getRecord(value: unknown, label: string): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`Embedding provider returned an invalid ${label}`);
  }
  return value as JsonObject;
}

function getArray(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) {
    throw new Error(`Embedding provider response is missing ${label}`);
  }
  return value;
}

export class EmbeddingService {
  readonly config: EmbeddingConfig;
  readonly model: string | null;
  readonly dimensions: number | null;

  constructor(config: EmbeddingConfig) {
    this.config = config;
    this.model = config.model;
    this.dimensions = config.dimensions;
  }

  async embed_text(text: string): Promise<OptionalEmbedding> {
    if (!text || !text.trim()) {
      console.warn("Empty text provided for embedding");
      return null;
    }

    try {
      if (this.config.provider === "gemini") {
        return await this.embedGemini(text);
      }

      if (
        hasOwnProvider(OPENAI_COMPATIBLE_BASE_URLS, this.config.provider) ||
        this.config.provider === "custom"
      ) {
        return await this.embedOpenAICompatible(text);
      }

      if (this.config.provider === "cohere") {
        return (await this.embedCohere([text]))[0] ?? null;
      }

      if (this.config.provider === "voyage") {
        return (await this.embedVoyage([text]))[0] ?? null;
      }

      return null;
    } catch (error) {
      console.error(
        `Embedding generation failed (${this.config.provider}):`,
        error,
      );
      return null;
    }
  }

  async embed_batch(texts: string[]): Promise<OptionalEmbedding[]> {
    if (!texts.length) return [];

    try {
      if (this.config.provider === "gemini") {
        return await this.embedBatchGemini(texts);
      }

      if (
        hasOwnProvider(OPENAI_COMPATIBLE_BASE_URLS, this.config.provider) ||
        this.config.provider === "custom"
      ) {
        return await this.embedBatchOpenAICompatible(texts);
      }

      if (this.config.provider === "cohere") {
        return await this.embedCohere(texts);
      }

      if (this.config.provider === "voyage") {
        return await this.embedVoyage(texts);
      }

      return Array<null>(texts.length).fill(null);
    } catch (error) {
      console.error(
        `Batch embedding generation failed (${this.config.provider}):`,
        error,
      );
      return Array<null>(texts.length).fill(null);
    }
  }

  private async embedGemini(text: string): Promise<EmbeddingVector> {
    const model = encodeURIComponent(this.config.model!);
    const url =
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:embedContent` +
      `?key=${encodeURIComponent(this.config.api_key!)}`;

    const result = await postJson(url, {
      content: { parts: [{ text }] },
      outputDimensionality: this.config.dimensions,
    });

    const embedding = getRecord(result.embedding, "Gemini embedding");
    return asVector(embedding.values);
  }

  private async embedBatchGemini(texts: string[]): Promise<OptionalEmbedding[]> {
    const model = encodeURIComponent(this.config.model!);
    const url =
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:batchEmbedContents` +
      `?key=${encodeURIComponent(this.config.api_key!)}`;

    const result = await postJson(url, {
      requests: texts.map((text) => ({
        model: `models/${this.config.model}`,
        content: { parts: [{ text }] },
        outputDimensionality: this.config.dimensions,
      })),
    });

    return getArray(result.embeddings, "embeddings").map((item) => {
      const embedding = getRecord(item, "Gemini embedding");
      return asVector(embedding.values);
    });
  }

  private async embedOpenAICompatible(text: string): Promise<EmbeddingVector> {
    const body: JsonObject = {
      model: this.config.model,
      input: text,
    };

    if (this.config.provider === "openai" && this.config.dimensions) {
      body.dimensions = this.config.dimensions;
    }

    const result = await postJson(
      `${this.config.base_url!.replace(/\/+$/u, "")}/embeddings`,
      body,
      { Authorization: `Bearer ${this.config.api_key || "not-needed"}` },
    );

    const data = getArray(result.data, "data");
    const first = getRecord(data[0], "embedding result");
    return asVector(first.embedding);
  }

  private async embedBatchOpenAICompatible(
    texts: string[],
  ): Promise<OptionalEmbedding[]> {
    const body: JsonObject = {
      model: this.config.model,
      input: texts,
    };

    if (this.config.provider === "openai" && this.config.dimensions) {
      body.dimensions = this.config.dimensions;
    }

    const result = await postJson(
      `${this.config.base_url!.replace(/\/+$/u, "")}/embeddings`,
      body,
      { Authorization: `Bearer ${this.config.api_key || "not-needed"}` },
    );

    return getArray(result.data, "data").map((item) => {
      const record = getRecord(item, "embedding result");
      return asVector(record.embedding);
    });
  }

  private async embedCohere(texts: string[]): Promise<OptionalEmbedding[]> {
    const result = await postJson(
      `${this.config.base_url!.replace(/\/+$/u, "")}/embed`,
      {
        texts,
        model: this.config.model,
        input_type: "search_document",
      },
      { Authorization: `Bearer ${this.config.api_key!}` },
    );

    return getArray(result.embeddings, "embeddings").map(asVector);
  }

  private async embedVoyage(texts: string[]): Promise<OptionalEmbedding[]> {
    const result = await postJson(
      `${this.config.base_url!.replace(/\/+$/u, "")}/embeddings`,
      { input: texts, model: this.config.model },
      { Authorization: `Bearer ${this.config.api_key!}` },
    );

    return getArray(result.data, "data").map((item) => {
      const record = getRecord(item, "embedding result");
      return asVector(record.embedding);
    });
  }
}
