import test from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_SYSTEM_PROMPT,
  GenerationService,
  CostTracker,
  PROVIDER_BASE_URLS,
  ProviderConfig,
} from "../dist/index.js";

function fakeResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function openAIResponse(content, usage = { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 }) {
  return { choices: [{ message: { content } }], usage };
}

test("provider configuration normalizes names and requires explicit models", () => {
  const config = new ProviderConfig({
    provider: "OPENAI", api_key: "test-key", model: "test-model",
  });
  assert.equal(config.provider, "openai");
  assert.equal(config.base_url, PROVIDER_BASE_URLS.openai);
  assert.throws(() => new ProviderConfig({ provider: "openai", api_key: "key" }), /No model/);
  assert.throws(() => new ProviderConfig({ provider: "unknown", api_key: "key", model: "m" }), /Unknown provider/);
  assert.equal(DEFAULT_SYSTEM_PROMPT.includes("ONLY the provided context"), true);
});

test("answer includes source citations, usage, and context budget", async () => {
  let requestBody;
  const service = new GenerationService({
    primary: new ProviderConfig({ provider: "openai", api_key: "key", model: "gpt-test" }),
    max_context_chars: 5,
    fetch_impl: async (_url, init) => {
      requestBody = JSON.parse(init.body);
      return fakeResponse(openAIResponse("Grounded answer"));
    },
  });
  const result = await service.generate_answer("What?", [
    { text: "first", document_name: "A", document_id: "doc-a", chunk_id: "c1", chunk_index: 0 },
    { text: "second", document_name: "B", document_id: "doc-b", chunk_id: "c2", chunk_index: 1 },
  ]);
  assert.equal(result.answer, "Grounded answer");
  assert.deepEqual(result.sources, ["A"]);
  assert.equal(result.citations.length, 1);
  assert.equal(result.citations[0].document_id, "doc-a");
  assert.equal(result.chunks_sent, 1);
  assert.equal(result.usage.total_tokens, 15);
  assert.match(requestBody.messages[0].content, /first/);
  assert.doesNotMatch(requestBody.messages[0].content, /second/);
});

test("provider failure falls back; override bypasses configured fallback", async () => {
  const calls = [];
  const primary = new ProviderConfig({ provider: "openai", api_key: "key", model: "primary" });
  const fallback = new ProviderConfig({ provider: "mistral", api_key: "key", model: "fallback" });
  const override = new ProviderConfig({ provider: "together", api_key: "key", model: "override" });
  const service = new GenerationService({
    primary, fallbacks: [fallback],
    fetch_impl: async (url, init) => {
      const body = JSON.parse(init.body);
      calls.push(body.model);
      if (body.model === "primary") return fakeResponse({ error: { message: "failed" } }, 500);
      return fakeResponse(openAIResponse("ok"));
    },
  });
  const result = await service.generate_answer("q", []);
  assert.equal(result.provider_used, "mistral");
  assert.deepEqual(calls, ["primary", "fallback"]);

  calls.length = 0;
  const overrideResult = await service.generate_answer("q", [], { override_provider: override });
  assert.equal(overrideResult.provider_used, "together");
  assert.deepEqual(calls, ["override"]);
});

test("structured response reports validation and enforcement", async () => {
  const service = new GenerationService({
    primary: new ProviderConfig({ provider: "openai", api_key: "key", model: "test" }),
    fetch_impl: async (_url, init) => {
      const body = JSON.parse(init.body);
      assert.equal(body.response_format.type, "json_schema");
      return fakeResponse(openAIResponse('{"name":"Ada"}'));
    },
  });
  const result = await service.generate_answer("name?", [], {
    response_format: { type: "object", properties: { name: { type: "string" } }, required: ["name"] },
  });
  assert.deepEqual(result.structured, { name: "Ada" });
  assert.equal(result.structured_valid, true);
  assert.equal(result.structured_enforcement, "native");
});

test("all provider failures return the Python-compatible failure shape", async () => {
  const service = new GenerationService({
    primary: new ProviderConfig({ provider: "openai", api_key: "key", model: "test" }),
    fetch_impl: async () => fakeResponse({ error: "offline" }, 503),
  });
  const result = await service.generate_answer("q", [], {
    response_format: { type: "object" },
  });
  assert.equal(result.provider_used, null);
  assert.equal(result.chunks_sent, 0);
  assert.deepEqual(result.sources, []);
  assert.equal(result.structured, null);
  assert.equal(result.structured_valid, false);
  assert.match(result.answer, /all configured providers failed/i);
});

test("streaming parses OpenAI-compatible SSE chunks", async () => {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode('data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n'));
      controller.enqueue(encoder.encode('data: {"choices":[{"delta":{"content":"world"}}]}\n\n'));
      controller.enqueue(encoder.encode("data: [DONE]\n\n"));
      controller.close();
    },
  });
  const service = new GenerationService({
    primary: new ProviderConfig({ provider: "openai", api_key: "key", model: "test" }),
    fetch_impl: async () => new Response(stream, { status: 200 }),
  });
  const parts = [];
  for await (const part of service.generate_answer_stream("q", [])) parts.push(part);
  assert.equal(parts.join(""), "Hello world");
});




async function collectStream(provider, chunks) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  const service = new GenerationService({
    primary: new ProviderConfig({
      provider,
      api_key: "test-key",
      model: "test-model",
    }),
    fetch_impl: async () => new Response(stream, { status: 200 }),
  });
  const parts = [];
  for await (const part of service.generate_answer_stream("q", [])) parts.push(part);
  return parts.join("");
}

test("stream parser handles final OpenAI event without trailing newline", async () => {
  const output = await collectStream("openai", [
    'data: {"choices":[{"delta":{"content":"final"}}]}',
  ]);
  assert.equal(output, "final");
});

test("stream parser handles final Gemini event without trailing newline", async () => {
  const output = await collectStream("gemini", [
    'data: {"candidates":[{"content":{"parts":[{"text":"Gemini final"}]}}]}',
  ]);
  assert.equal(output, "Gemini final");
});

test("stream parser handles Anthropic text delta events", async () => {
  const output = await collectStream("anthropic", [
    'event: content_block_delta\n',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Anthropic "}}\n',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"stream"}}',
  ]);
  assert.equal(output, "Anthropic stream");
});

test("stream parser handles events split across network chunks", async () => {
  const output = await collectStream("openai", [
    'data: {"choices":[{"delta":',
    '{"content":"split"}}]}\n\n',
  ]);
  assert.equal(output, "split");
});

test("empty OpenAI-compatible answer does not trigger provider fallback", async () => {
  const fallback = new ProviderConfig({
    provider: "mistral", api_key: "key", model: "fallback",
  });
  const service = new GenerationService({
    primary: new ProviderConfig({
      provider: "openai", api_key: "key", model: "primary",
    }),
    fallbacks: [fallback],
    fetch_impl: async () => fakeResponse({
      choices: [{ message: { content: "" } }],
      usage: { prompt_tokens: 3, completion_tokens: 0, total_tokens: 3 },
    }),
  });

  const result = await service.generate_answer("q", []);
  assert.equal(result.answer, "No answer generated.");
  assert.equal(result.provider_used, "openai");
  assert.equal(result.usage.total_tokens, 3);
});

test("empty Gemini answer uses the Python-compatible result", async () => {
  const service = new GenerationService({
    primary: new ProviderConfig({
      provider: "gemini", api_key: "key", model: "test-model",
    }),
    fetch_impl: async () => fakeResponse({
      candidates: [{ content: { parts: [] } }],
      usageMetadata: {
        promptTokenCount: 4,
        candidatesTokenCount: 0,
        totalTokenCount: 4,
      },
    }),
  });

  const result = await service.generate_answer("q", []);
  assert.equal(result.answer, "No answer generated.");
  assert.equal(result.provider_used, "gemini");
  assert.equal(result.usage.total_tokens, 4);
});

test("empty Anthropic answer uses the Python-compatible result", async () => {
  const service = new GenerationService({
    primary: new ProviderConfig({
      provider: "anthropic", api_key: "key", model: "test-model",
    }),
    fetch_impl: async () => fakeResponse({
      content: [],
      usage: { input_tokens: 4, output_tokens: 0 },
    }),
  });

  const result = await service.generate_answer("q", []);
  assert.equal(result.answer, "No answer generated.");
  assert.equal(result.provider_used, "anthropic");
  assert.equal(result.usage.total_tokens, 4);
});

test("Anthropic structured tool input is validated as an object", async () => {
  const service = new GenerationService({
    primary: new ProviderConfig({
      provider: "anthropic", api_key: "key", model: "test-model",
    }),
    fetch_impl: async () => fakeResponse({
      content: [{
        type: "tool_use",
        name: "structured_response",
        input: { name: "Ada" },
      }],
      usage: { input_tokens: 4, output_tokens: 3 },
    }),
  });

  const result = await service.generate_answer("name?", [], {
    response_format: {
      type: "object",
      properties: { name: { type: "string" } },
      required: ["name"],
    },
  });

  assert.equal(result.answer, '{"name":"Ada"}');
  assert.deepEqual(result.structured, { name: "Ada" });
  assert.equal(result.structured_valid, true);
  assert.equal(result.structured_enforcement, "native");
  assert.equal(result.structured_validation_method, "basic_type_check_only");
});

test("describe_image requires a Gemini provider in the chain", async () => {
  const service = new GenerationService({
    primary: new ProviderConfig({ provider: "openai", api_key: "key", model: "test" }),
    fetch_impl: async () => fakeResponse({}),
  });
  await assert.rejects(() => service.describe_image(new Uint8Array([1, 2, 3])), /requires a Gemini provider/);
});


test("generation result includes cost for a model with known seed pricing", async () => {
  const service = new GenerationService({
    primary: new ProviderConfig({
      provider: "openai", api_key: "key", model: "gpt-5.6-luna",
    }),
    fetch_impl: async () => fakeResponse(openAIResponse("priced answer", {
      prompt_tokens: 1000, completion_tokens: 500, total_tokens: 1500,
    })),
  });

  const result = await service.generate_answer("q", []);
  assert.equal(result.cost_usd, 0.004);
});

test("generation result cost is null for unknown pricing and missing usage", async () => {
  const unknownModel = new GenerationService({
    primary: new ProviderConfig({
      provider: "openai", api_key: "key", model: "unlisted-model",
    }),
    fetch_impl: async () => fakeResponse(openAIResponse("answer")),
  });
  assert.equal((await unknownModel.generate_answer("q", [])).cost_usd, null);

  const noUsage = new GenerationService({
    primary: new ProviderConfig({
      provider: "openai", api_key: "key", model: "gpt-5.6-luna",
    }),
    fetch_impl: async () => fakeResponse(openAIResponse("answer", null)),
  });
  assert.equal((await noUsage.generate_answer("q", [])).cost_usd, null);
});

test("generation service supports custom pricing", async () => {
  const service = new GenerationService({
    primary: new ProviderConfig({
      provider: "openai", api_key: "key", model: "custom-model",
    }),
    pricing_table: {
      openai: { "custom-model": { input: 2, output: 4 } },
    },
    fetch_impl: async () => fakeResponse(openAIResponse("answer", {
      prompt_tokens: 1000, completion_tokens: 500, total_tokens: 1500,
    })),
  });

  const result = await service.generate_answer("q", []);
  assert.equal(result.cost_usd, 0.004);
});

test("configured CostTracker accumulates successful generation costs", async () => {
  const tracker = new CostTracker({
    pricing_table: {
      openai: { "custom-model": { input: 2, output: 4 } },
    },
  });
  const service = new GenerationService({
    primary: new ProviderConfig({
      provider: "openai", api_key: "key", model: "custom-model",
    }),
    cost_tracker: tracker,
    fetch_impl: async () => fakeResponse(openAIResponse("answer", {
      prompt_tokens: 1000, completion_tokens: 500, total_tokens: 1500,
    })),
  });

  const first = await service.generate_answer("q", []);
  const second = await service.generate_answer("q", []);
  assert.equal(first.cost_usd, 0.004);
  assert.equal(second.cost_usd, 0.004);
  assert.equal(tracker.cumulative_cost_usd, 0.008);
});

test("failed generation returns null cost and does not update CostTracker", async () => {
  const tracker = new CostTracker();
  const service = new GenerationService({
    primary: new ProviderConfig({
      provider: "openai", api_key: "key", model: "gpt-5.6-luna",
    }),
    cost_tracker: tracker,
    fetch_impl: async () => fakeResponse({ error: "offline" }, 503),
  });

  const result = await service.generate_answer("q", []);
  assert.equal(result.cost_usd, null);
  assert.equal(tracker.cumulative_cost_usd, 0);
});
