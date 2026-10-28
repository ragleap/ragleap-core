import { parse_and_validate, parse_and_validate_object } from "./structured.js";
import { CostTracker, compute_cost, SEED_PRICING_TABLE } from "./cost.js";
import type { PricingTable } from "./cost.js";

export const PROVIDER_BASE_URLS: Readonly<Record<string, string>> = {
  openai: "https://api.openai.com/v1",
  mistral: "https://api.mistral.ai/v1",
  groq: "https://api.groq.com/openai/v1",
  together: "https://api.together.xyz/v1",
  openrouter: "https://openrouter.ai/api/v1",
  ollama: "http://localhost:11434/v1",
  deepseek: "https://api.deepseek.com/v1",
  xai: "https://api.x.ai/v1",
  cohere: "https://api.cohere.ai/v1",
  perplexity: "https://api.perplexity.ai",
};

export const DEFAULT_SYSTEM_PROMPT = `You are a helpful assistant that answers questions using ONLY the provided context.
If the answer is not in the context, say clearly that you don't have that information — do not make things up.
Always be concise and cite which document your answer came from when possible.`;

export interface ProviderConfigOptions {
  provider: string;
  api_key?: string | null;
  model?: string | null;
  base_url?: string | null;
}

function env(name: string): string | undefined {
  const processLike = (globalThis as typeof globalThis & {
    process?: { env?: Record<string, string | undefined> };
  }).process;
  return processLike?.env?.[name];
}

export class ProviderConfig {
  provider: string;
  api_key: string | null;
  model: string | null;
  base_url: string | null;

  constructor(options: ProviderConfigOptions) {
    this.provider = options.provider.toLowerCase();
    this.api_key = options.api_key || null;
    this.model = options.model || null;
    this.base_url = options.base_url || null;

    if (this.provider === "gemini") {
      this.api_key ||= env("GEMINI_API_KEY") || null;
      this.model ||= env("GEMINI_CHAT_MODEL") || null;
    } else if (this.provider === "anthropic") {
      this.api_key ||= env("ANTHROPIC_API_KEY") || null;
      this.model ||= env("ANTHROPIC_MODEL") || null;
    } else if (this.provider in PROVIDER_BASE_URLS) {
      this.api_key ||= env(`${this.provider.toUpperCase()}_API_KEY`) || null;
      this.model ||= env(`${this.provider.toUpperCase()}_MODEL`) || null;
      this.base_url ||= PROVIDER_BASE_URLS[this.provider];
    } else if (this.provider === "custom") {
      this.base_url ||= env("CUSTOM_BASE_URL") || null;
    } else {
      throw new Error(
        `Unknown provider '${this.provider}'. Supported: gemini, anthropic, custom, ${Object.keys(PROVIDER_BASE_URLS).join(", ")}.`,
      );
    }

    if (!this.api_key && this.provider !== "ollama") {
      throw new Error(`No API key for provider '${this.provider}'. Pass api_key explicitly.`);
    }
    if (!["gemini", "anthropic"].includes(this.provider) && !this.base_url) {
      throw new Error(`No base_url for provider '${this.provider}'. Pass base_url explicitly.`);
    }
    if (!this.model && this.provider !== "custom") {
      const variable = this.provider === "gemini"
        ? "GEMINI_CHAT_MODEL"
        : this.provider === "anthropic"
          ? "ANTHROPIC_MODEL"
          : `${this.provider.toUpperCase()}_MODEL`;
      throw new Error(`No model specified for provider '${this.provider}'. Pass model explicitly or set ${variable}.`);
    }
  }
}

export interface GenerationChunk {
  text?: string;
  document_name?: string;
  document_id?: string | null;
  chunk_id?: string | null;
  chunk_index?: number | string | null;
  [key: string]: unknown;
}

export interface GenerationUsage {
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  total_tokens?: number | null;
}

export interface GenerateAnswerOptions {
  temperature?: number | null;
  system_prompt?: string | null;
  max_tokens?: number | null;
  history_prefix?: string;
  override_provider?: ProviderConfig | null;
  response_format?: Record<string, unknown> | null;
}

export interface GenerationServiceOptions {
  primary: ProviderConfig;
  fallbacks?: ProviderConfig[];
  default_temperature?: number;
  default_max_tokens?: number;
  max_context_chars?: number;
  system_prompt?: string;
  fetch_impl?: typeof fetch;
  pricing_table?: PricingTable;
  cost_tracker?: CostTracker;
}

export interface GenerationResult {
  answer: string;
  sources: string[];
  citations: Array<{
    source_number: number;
    document_name: string;
    document_id: string | null;
    chunk_id: string | null;
    chunk_index: number | string | null;
    text_preview: string;
  }>;
  provider_used: string | null;
  model_used: string | null;
  usage: GenerationUsage | null;
  cost_usd: number | null;
  chunks_sent: number;
  structured?: unknown;
  structured_valid?: boolean;
  structured_enforcement?: string | null;
  structured_validation_method?: string | null;
}

type ProviderResponse = {
  text: string;
  usage: GenerationUsage | null;
  enforcement: string | null;
  structuredObject?: unknown;
};

type JsonObject = Record<string, unknown>;

function record(value: unknown): JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : {};
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export class GenerationService {
  readonly primary: ProviderConfig;
  readonly fallbacks: ProviderConfig[];
  readonly default_temperature: number;
  readonly default_max_tokens: number;
  readonly max_context_chars: number;
  readonly system_prompt: string;
  readonly pricing_table: PricingTable;
  readonly cost_tracker: CostTracker | null;
  private readonly fetchImpl: typeof fetch;

  constructor(options: GenerationServiceOptions) {
    this.primary = options.primary;
    this.fallbacks = options.fallbacks ?? [];
    this.default_temperature = options.default_temperature ?? 0.3;
    this.default_max_tokens = options.default_max_tokens ?? 1024;
    this.max_context_chars = options.max_context_chars ?? 12000;
    this.system_prompt = options.system_prompt ?? DEFAULT_SYSTEM_PROMPT;
    this.fetchImpl = options.fetch_impl ?? fetch;
    this.pricing_table = options.pricing_table ?? SEED_PRICING_TABLE;
    this.cost_tracker = options.cost_tracker ?? null;
  }

  _chain(override_provider?: ProviderConfig | null): ProviderConfig[] {
    if (override_provider) return [override_provider];
    return [
      this.primary,
      ...this.fallbacks.filter((item) => item.provider !== this.primary.provider),
    ];
  }

  _trim_chunks_to_budget(chunks: GenerationChunk[]): GenerationChunk[] {
    if (this.max_context_chars <= 0 || chunks.length === 0) return chunks;
    const kept: GenerationChunk[] = [];
    let total = 0;
    for (const chunk of chunks) {
      const length = (chunk.text ?? "").length;
      if (total + length > this.max_context_chars && kept.length > 0) break;
      kept.push(chunk);
      total += length;
    }
    return kept;
  }

  _build_context(chunks: GenerationChunk[]): string {
    if (!chunks.length) return "No relevant context was found.";
    return chunks.map((chunk, index) =>
      `[Source ${index + 1}: ${chunk.document_name ?? "unknown document"}, chunk ${chunk.chunk_index ?? "?"}]\n${chunk.text ?? ""}`,
    ).join("\n\n");
  }

  _build_citations(chunks: GenerationChunk[]): GenerationResult["citations"] {
    return chunks.map((chunk, index) => {
      const text = chunk.text ?? "";
      return {
        source_number: index + 1,
        document_name: chunk.document_name ?? "unknown document",
        document_id: chunk.document_id ?? null,
        chunk_id: chunk.chunk_id ?? null,
        chunk_index: chunk.chunk_index ?? null,
        text_preview: text.slice(0, 150) + (text.length > 150 ? "..." : ""),
      };
    });
  }

  _build_prompt(
    query: string,
    chunks: GenerationChunk[],
    system_prompt?: string | null,
    history_prefix = "",
  ): string {
    return `${system_prompt || this.system_prompt}\n\n${history_prefix}Context:\n${this._build_context(chunks)}\n\nQuestion: ${query}\nAnswer:`;
  }

  async generate_answer(
    query: string,
    chunks: GenerationChunk[],
    options: GenerateAnswerOptions = {},
  ): Promise<GenerationResult> {
    const temperature = options.temperature ?? this.default_temperature;
    const maxTokens = options.max_tokens ?? this.default_max_tokens;
    const trimmed = this._trim_chunks_to_budget(chunks);
    const sources = [...new Set(trimmed.map((chunk) => chunk.document_name ?? "unknown"))];
    const citations = this._build_citations(trimmed);
    const prompt = this._build_prompt(query, trimmed, options.system_prompt, options.history_prefix);

    let lastError: unknown = "No providers configured";
    for (const config of this._chain(options.override_provider)) {
      try {
        const response = await this.callProvider(config, prompt, temperature, maxTokens, options.response_format ?? null);
        const result: GenerationResult = {
          answer: response.text,
          sources,
          citations,
          provider_used: config.provider,
          model_used: config.model,
          usage: response.usage,
          cost_usd: null,
          chunks_sent: trimmed.length,
        };
        if (options.response_format) {
          const validation = response.structuredObject !== undefined
            ? parse_and_validate_object(response.structuredObject, options.response_format)
            : parse_and_validate(response.text, options.response_format);
          result.structured = validation.parsed;
          result.structured_valid = validation.is_valid;
          result.structured_enforcement = response.enforcement;
          result.structured_validation_method = validation.validation_method;
        }

        result.cost_usd = this.cost_tracker
          ? this.cost_tracker.record(config.provider, config.model, response.usage)
          : compute_cost(this.pricing_table, config.provider, config.model, response.usage);

        return result;
      } catch (error) {
        lastError = error;
        console.warn(`Provider '${config.provider}' failed: ${errorMessage(error)}`);
      }
    }

    const failure: GenerationResult = {
      answer: `Sorry, all configured providers failed. Last error: ${errorMessage(lastError)}`,
      sources: [],
      citations: [],
      provider_used: null,
      model_used: null,
      usage: null,
      cost_usd: null,
      chunks_sent: 0,
    };
    if (options.response_format) {
      failure.structured = null;
      failure.structured_valid = false;
      failure.structured_enforcement = null;
      failure.structured_validation_method = null;
    }
    return failure;
  }

  async *generate_answer_stream(
    query: string,
    chunks: GenerationChunk[],
    options: Omit<GenerateAnswerOptions, "response_format"> = {},
  ): AsyncGenerator<string> {
    const trimmed = this._trim_chunks_to_budget(chunks);
    const prompt = this._build_prompt(query, trimmed, options.system_prompt, options.history_prefix);
    const temperature = options.temperature ?? this.default_temperature;
    const maxTokens = options.max_tokens ?? this.default_max_tokens;
    let lastError: unknown = "No providers configured";

    for (const config of this._chain(options.override_provider)) {
      let yielded = false;
      try {
        for await (const part of this.streamProvider(config, prompt, temperature, maxTokens)) {
          yielded = true;
          yield part;
        }
        return;
      } catch (error) {
        lastError = error;
        console.warn(`Provider '${config.provider}' failed during streaming: ${errorMessage(error)}`);
        if (yielded) {
          yield `\n[Error: generation interrupted — ${errorMessage(error)}]`;
          return;
        }
      }
    }
    yield `Sorry, all configured providers failed. Last error: ${errorMessage(lastError)}`;
  }

  private async post(url: string, body: unknown, headers: Record<string, string>): Promise<JsonObject> {
    const response = await this.fetchImpl(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status} ${response.statusText}: ${await response.text().catch(() => "")}`);
    }
    return record(await response.json());
  }

  private auth(config: ProviderConfig): Record<string, string> {
    return config.api_key ? { Authorization: `Bearer ${config.api_key}` } : {};
  }

  private async callProvider(
    config: ProviderConfig,
    prompt: string,
    temperature: number,
    maxTokens: number,
    responseFormat: Record<string, unknown> | null,
  ): Promise<ProviderResponse> {
    if (config.provider === "gemini") {
      return this.callGemini(config, prompt, temperature, maxTokens, responseFormat);
    }
    if (config.provider === "anthropic") {
      return this.callAnthropic(config, prompt, temperature, maxTokens, responseFormat);
    }
    return this.callOpenAICompatible(config, prompt, temperature, maxTokens, responseFormat);
  }

  private async callGemini(
    config: ProviderConfig, prompt: string, temperature: number, maxTokens: number,
    responseFormat: Record<string, unknown> | null,
  ): Promise<ProviderResponse> {
    const model = encodeURIComponent(config.model!);
    const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${encodeURIComponent(config.api_key!)}`;
    const generationConfig: JsonObject = { temperature, maxOutputTokens: maxTokens };
    if (responseFormat) {
      generationConfig.responseMimeType = "application/json";
      generationConfig.responseSchema = responseFormat;
    }
    const data = await this.post(url, {
      contents: [{ parts: [{ text: prompt }] }],
      generationConfig,
    }, {});
    const candidates = Array.isArray(data.candidates) ? data.candidates : [];
    const first = record(candidates[0]);
    const content = record(first.content);
    const parts = Array.isArray(content.parts) ? content.parts : [];
    const text = parts.map((part) => record(part).text).filter((item): item is string => typeof item === "string").join("").trim();
    const answerText = text || "No answer generated.";
    const metadata = record(data.usageMetadata);
    const usage = Object.keys(metadata).length ? {
      prompt_tokens: typeof metadata.promptTokenCount === "number" ? metadata.promptTokenCount : null,
      completion_tokens: typeof metadata.candidatesTokenCount === "number" ? metadata.candidatesTokenCount : null,
      total_tokens: typeof metadata.totalTokenCount === "number" ? metadata.totalTokenCount : null,
    } : null;
    return { text: answerText, usage, enforcement: responseFormat ? "native" : null };
  }

  private async callAnthropic(
    config: ProviderConfig, prompt: string, temperature: number, maxTokens: number,
    responseFormat: Record<string, unknown> | null,
  ): Promise<ProviderResponse> {
    const body: JsonObject = {
      model: config.model, max_tokens: maxTokens, temperature,
      messages: [{ role: "user", content: prompt }],
    };
    if (responseFormat) {
      body.tools = [{
        name: "structured_response",
        description: "Return the answer in the required structure.",
        input_schema: responseFormat,
      }];
      body.tool_choice = { type: "tool", name: "structured_response" };
    }
    const data = await this.post(`${"https://api.anthropic.com"}/v1/messages`, body, {
      "x-api-key": config.api_key!,
      "anthropic-version": "2023-06-01",
    });
    const content = Array.isArray(data.content) ? data.content : [];
    const toolBlock = content.map(record).find((item) => item.type === "tool_use");
    const textBlock = content.map(record).find((item) => item.type === "text");
    const text = responseFormat
      ? toolBlock ? JSON.stringify(toolBlock.input) : "No answer generated."
      : typeof textBlock?.text === "string" && textBlock.text.trim()
        ? textBlock.text.trim()
        : "No answer generated.";
    const usageData = record(data.usage);
    const inputTokens = typeof usageData.input_tokens === "number" ? usageData.input_tokens : null;
    const outputTokens = typeof usageData.output_tokens === "number" ? usageData.output_tokens : null;
    const usage = inputTokens === null && outputTokens === null ? null : {
      prompt_tokens: inputTokens, completion_tokens: outputTokens,
      total_tokens: (inputTokens ?? 0) + (outputTokens ?? 0),
    };
    return {
      text,
      usage,
      enforcement: responseFormat ? "native" : null,
      ...(responseFormat && toolBlock ? { structuredObject: toolBlock.input } : {}),
    };
  }

  private async callOpenAICompatible(
    config: ProviderConfig, prompt: string, temperature: number, maxTokens: number,
    responseFormat: Record<string, unknown> | null,
  ): Promise<ProviderResponse> {
    const base = config.base_url!.replace(/\/+$/, "");
    const body: JsonObject = {
      model: config.model,
      messages: [{ role: "user", content: prompt }],
      temperature,
      max_tokens: maxTokens,
    };
    let enforcement: string | null = null;
    if (responseFormat) {
      body.response_format = {
        type: "json_schema",
        json_schema: { name: "structured_response", schema: responseFormat, strict: true },
      };
    }

    let data: JsonObject;
    try {
      data = await this.post(`${base}/chat/completions`, body, this.auth(config));
      if (responseFormat) enforcement = "native";
    } catch (error) {
      if (!responseFormat) throw error;
      console.warn(`Provider does not support strict json_schema mode; retrying json_object: ${errorMessage(error)}`);
      body.response_format = { type: "json_object" };
      data = await this.post(`${base}/chat/completions`, body, this.auth(config));
      enforcement = "json_object_fallback";
    }

    const choices = Array.isArray(data.choices) ? data.choices : [];
    const message = record(record(choices[0]).message);
    const text = typeof message.content === "string" && message.content.trim()
      ? message.content.trim()
      : "No answer generated.";
    const usageData = record(data.usage);
    const promptTokens = typeof usageData.prompt_tokens === "number" ? usageData.prompt_tokens : null;
    const completionTokens = typeof usageData.completion_tokens === "number" ? usageData.completion_tokens : null;
    const totalTokens = typeof usageData.total_tokens === "number" ? usageData.total_tokens : null;
    const usage = promptTokens === null && completionTokens === null && totalTokens === null
      ? null
      : { prompt_tokens: promptTokens, completion_tokens: completionTokens, total_tokens: totalTokens };
    return { text, usage, enforcement };
  }

  private async *streamProvider(
    config: ProviderConfig, prompt: string, temperature: number, maxTokens: number,
  ): AsyncGenerator<string> {
    let url: string;
    let headers: Record<string, string>;
    let body: JsonObject;
    if (config.provider === "gemini") {
      url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(config.model!)}:streamGenerateContent?alt=sse&key=${encodeURIComponent(config.api_key!)}`;
      headers = { "Content-Type": "application/json" };
      body = { contents: [{ parts: [{ text: prompt }] }], generationConfig: { temperature, maxOutputTokens: maxTokens } };
    } else if (config.provider === "anthropic") {
      url = "https://api.anthropic.com/v1/messages";
      headers = { "Content-Type": "application/json", "x-api-key": config.api_key!, "anthropic-version": "2023-06-01" };
      body = { model: config.model, max_tokens: maxTokens, temperature, stream: true, messages: [{ role: "user", content: prompt }] };
    } else {
      url = `${config.base_url!.replace(/\/+$/, "")}/chat/completions`;
      headers = { "Content-Type": "application/json", ...this.auth(config) };
      body = { model: config.model, messages: [{ role: "user", content: prompt }], temperature, max_tokens: maxTokens, stream: true };
    }

    const response = await this.fetchImpl(url, {
      method: "POST", headers, body: JSON.stringify(body),
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status} ${response.statusText}: ${await response.text().catch(() => "")}`);
    }
    if (!response.body) throw new Error("Provider response has no streaming body");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const extractPiece = (payload: string): string => {
      if (!payload || payload === "[DONE]") return "";
      const event = record(JSON.parse(payload));
      if (config.provider === "gemini") {
        const candidates = Array.isArray(event.candidates) ? event.candidates : [];
        const content = record(record(candidates[0]).content);
        const parts = Array.isArray(content.parts) ? content.parts : [];
        return parts.map((part) => record(part).text)
          .filter((item): item is string => typeof item === "string").join("");
      }
      if (config.provider === "anthropic") {
        const delta = record(event.delta);
        return event.type === "content_block_delta" && typeof delta.text === "string"
          ? delta.text : "";
      }
      const choices = Array.isArray(event.choices) ? event.choices : [];
      const delta = record(record(choices[0]).delta);
      return typeof delta.content === "string" ? delta.content : "";
    };

    const processLine = (line: string): string => {
      if (!line.startsWith("data:")) return "";
      const payload = line.slice(5).trim();
      if (!payload || payload === "[DONE]") return "";
      try {
        return extractPiece(payload);
      } catch {
        // Ignore malformed or non-JSON SSE data lines, as before.
        return "";
      }
    };

    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          const piece = processLine(line);
          if (piece) yield piece;
        }
      }

      buffer += decoder.decode();
      if (buffer) {
        // Process the final line even when the stream ends without a newline.
        const piece = processLine(buffer);
        if (piece) yield piece;
      }
    } finally {
      reader.releaseLock();
    }
  }

  async describe_image(
    image_bytes: Uint8Array,
    mime_type = "image/jpeg",
    prompt?: string | null,
  ): Promise<string> {
    const config = this._chain().find((item) => item.provider === "gemini");
    if (!config) {
      throw new Error("Vision captioning currently requires a Gemini provider configured as primary or a fallback.");
    }
    const model = encodeURIComponent(config.model!);
    const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${encodeURIComponent(config.api_key!)}`;
    let binary = "";
    for (const byte of image_bytes) binary += String.fromCharCode(byte);
    const data = await this.post(url, {
      contents: [{ parts: [
        { inline_data: { mime_type, data: btoa(binary) } },
        { text: prompt || "Describe this image in detail, including any visible text, objects, people, charts, or diagrams." },
      ] }],
    }, {});
    const candidates = Array.isArray(data.candidates) ? data.candidates : [];
    const content = record(record(candidates[0]).content);
    const parts = Array.isArray(content.parts) ? content.parts : [];
    const text = parts.map((part) => record(part).text).filter((item): item is string => typeof item === "string").join("").trim();
    if (!text) throw new Error("Vision model returned no description for this image.");
    return text;
  }
}
