/**
 * Per-call USD cost estimates from provider-reported token usage.
 * Pricing is only estimated for explicitly configured provider/model pairs.
 * Unknown rates or missing usage return null rather than guessing.
 */

export const PRICING_TABLE_VERIFIED_DATE = "2026-07-28";

export interface ModelRates {
  input: number;
  output: number;
}

export type PricingTable = Record<string, Record<string, ModelRates>>;

export interface TokenUsage {
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
}

export const SEED_PRICING_TABLE: PricingTable = {
  gemini: {
    "gemini-3.6-flash": { input: 1.5, output: 7.5 },
    "gemini-3.1-pro": { input: 2, output: 12 },
    "gemini-2.5-flash-lite": { input: 0.1, output: 0.4 },
  },
  anthropic: {
    "claude-haiku-4-5-20251001": { input: 1, output: 5 },
    "claude-sonnet-5": { input: 2, output: 10 },
    "claude-opus-5": { input: 5, output: 25 },
    "claude-fable-5": { input: 10, output: 50 },
  },
  openai: {
    "gpt-5.6-luna": { input: 1, output: 6 },
    "gpt-5.6-terra": { input: 2.5, output: 15 },
    "gpt-5.6-sol": { input: 5, output: 30 },
  },
  ollama: {
    "*": { input: 0, output: 0 },
  },
};

function lookupRate(
  pricingTable: PricingTable,
  provider: string,
  model?: string | null,
): ModelRates | undefined {
  const providerRates = pricingTable[provider];
  if (!providerRates) return undefined;
  if (model && providerRates[model]) return providerRates[model];
  return providerRates["*"];
}

export function compute_cost(
  pricing_table: PricingTable,
  provider: string | null | undefined,
  model: string | null | undefined,
  usage: TokenUsage | null | undefined,
): number | null {
  if (!provider || !usage) return null;

  const rate = lookupRate(pricing_table, provider, model);
  if (!rate) return null;

  const promptTokens = usage.prompt_tokens || 0;
  const completionTokens = usage.completion_tokens || 0;

  const cost =
    (promptTokens / 1_000_000) * rate.input +
    (completionTokens / 1_000_000) * rate.output;

  return Math.round((cost + Number.EPSILON) * 1_000_000) / 1_000_000;
}

export interface CostTrackerOptions {
  pricing_table?: PricingTable;
  budget_usd_per_month?: number | null;
}

export class CostTracker {
  readonly pricing_table: PricingTable;
  readonly budget_usd_per_month: number | null;
  cumulative_cost_usd = 0;

  constructor(options: CostTrackerOptions = {}) {
    this.pricing_table = Object.fromEntries(
      Object.entries(SEED_PRICING_TABLE).map(([provider, models]) => [
        provider,
        { ...models },
      ]),
    );

    for (const [provider, models] of Object.entries(options.pricing_table ?? {})) {
      this.pricing_table[provider] = {
        ...(this.pricing_table[provider] ?? {}),
        ...models,
      };
    }

    this.budget_usd_per_month = options.budget_usd_per_month ?? null;
  }

  record(
    provider: string | null | undefined,
    model: string | null | undefined,
    usage: TokenUsage | null | undefined,
  ): number | null {
    const cost = compute_cost(this.pricing_table, provider, model, usage);
    if (cost !== null) this.cumulative_cost_usd += cost;
    return cost;
  }

  is_over_budget(): boolean {
    return this.budget_usd_per_month !== null &&
      this.cumulative_cost_usd >= this.budget_usd_per_month;
  }
}
