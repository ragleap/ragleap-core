import test from "node:test";
import assert from "node:assert/strict";
import {
  EmbeddingConfig,
  EmbeddingService,
  OPENAI_COMPATIBLE_BASE_URLS,
  CUSTOM_SHAPE_DEFAULT_BASE_URLS,
} from "../dist/embedding.js";

function config(provider, overrides = {}) {
  const base = {
    provider,
    api_key: provider === "ollama" ? undefined : "example-test-key",
    model: "test-model",
    dimensions: 4,
    ...overrides,
  };
  if (provider === "custom" && !base.base_url) {
    base.base_url = "https://example.test/v1";
  }
  return new EmbeddingConfig(base);
}

function mockFetch(handler) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url, options) => handler(String(url), options);
  return () => {
    globalThis.fetch = original;
  };
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  };
}

test("provider defaults and provider name normalization", () => {
  assert.equal(config("OPENAI").provider, "openai");
  assert.equal(config("mistral").base_url, OPENAI_COMPATIBLE_BASE_URLS.mistral);
  assert.equal(config("cohere").base_url, CUSTOM_SHAPE_DEFAULT_BASE_URLS.cohere);
  assert.equal(config("voyage").base_url, CUSTOM_SHAPE_DEFAULT_BASE_URLS.voyage);
  assert.equal(config("ollama").api_key, null);
});

test("requires a model, dimensions and API key where applicable", () => {
  assert.throws(
    () => new EmbeddingConfig({ provider: "mistral", api_key: "key" }),
    /No embedding model specified/,
  );
  assert.throws(
    () => new EmbeddingConfig({
      provider: "cohere", api_key: "key", model: "embed-model",
    }),
    /No dimensions specified/,
  );
  assert.throws(
    () => new EmbeddingConfig({ provider: "voyage", model: "embed-model", dimensions: 4 }),
    /No API key/,
  );
  assert.throws(
    () => new EmbeddingConfig({ provider: "invalid" }),
    /Unknown embedding provider/,
  );
});

test("rejects inherited object property names as providers", () => {
  for (const provider of ["constructor", "toString", "__proto__"]) {
    assert.throws(
      () => new EmbeddingConfig({
        provider,
        api_key: "example-test-key",
        model: "test-model",
        dimensions: 4,
      }),
      /Unknown embedding provider/,
    );
  }
});

test("custom provider requires an endpoint", () => {
  assert.throws(
    () => new EmbeddingConfig({
      provider: "custom", api_key: "key", model: "m", dimensions: 4,
    }),
    /No base_url for provider 'custom'/,
  );
});

test("OpenAI-compatible single embedding sends dimensions only to OpenAI", async () => {
  for (const provider of ["openai", "mistral", "together", "ollama", "custom"]) {
    let captured;
    const restore = mockFetch(async (url, options) => {
      captured = { url, body: JSON.parse(options.body), headers: options.headers };
      return jsonResponse({ data: [{ embedding: [0.1, 0.2, 0.3, 0.4] }] });
    });

    try {
      const result = await new EmbeddingService(config(provider)).embed_text("hello");
      assert.deepEqual(result, [0.1, 0.2, 0.3, 0.4]);
      assert.equal(captured.url, `${config(provider).base_url}/embeddings`);
      assert.equal(captured.body.model, "test-model");
      assert.equal(captured.body.input, "hello");
      assert.equal("dimensions" in captured.body, provider === "openai");
    } finally {
      restore();
    }
  }
});

test("OpenAI-compatible batch preserves returned vector order", async () => {
  const restore = mockFetch(async (_url, options) => {
    assert.deepEqual(JSON.parse(options.body).input, ["first", "second"]);
    return jsonResponse({
      data: [
        { embedding: [1, 2] },
        { embedding: [3, 4] },
      ],
    });
  });

  try {
    const result = await new EmbeddingService(config("mistral"))
      .embed_batch(["first", "second"]);
    assert.deepEqual(result, [[1, 2], [3, 4]]);
  } finally {
    restore();
  }
});

test("Cohere honors custom base_url and uses search_document", async () => {
  let captured;
  const restore = mockFetch(async (url, options) => {
    captured = { url, body: JSON.parse(options.body) };
    return jsonResponse({ embeddings: [[0.1, 0.2]] });
  });

  try {
    const service = new EmbeddingService(config("cohere", {
      base_url: "https://gateway.example.test/v1/",
    }));
    assert.deepEqual(await service.embed_text("hello"), [0.1, 0.2]);
    assert.equal(captured.url, "https://gateway.example.test/v1/embed");
    assert.equal(captured.body.input_type, "search_document");
  } finally {
    restore();
  }
});

test("Voyage honors custom base_url", async () => {
  let capturedUrl;
  const restore = mockFetch(async (url) => {
    capturedUrl = url;
    return jsonResponse({ data: [{ embedding: [0.5, 0.6] }] });
  });

  try {
    const service = new EmbeddingService(config("voyage", {
      base_url: "https://gateway.example.test/v1/",
    }));
    assert.deepEqual(await service.embed_text("hello"), [0.5, 0.6]);
    assert.equal(capturedUrl, "https://gateway.example.test/v1/embeddings");
  } finally {
    restore();
  }
});

test("Gemini single and batch requests parse vectors", async () => {
  const restore = mockFetch(async (url, options) => {
    const body = JSON.parse(options.body);
    assert.match(url, /key=example-test-key/);

    if (url.includes(":batchEmbedContents")) {
      assert.equal(body.requests.length, 2);
      assert.equal(body.requests[0].outputDimensionality, 4);
      assert.equal(body.requests[1].outputDimensionality, 4);
      return jsonResponse({
        embeddings: [{ values: [1, 2] }, { values: [3, 4] }],
      });
    }
    assert.equal(body.outputDimensionality, 4);
    return jsonResponse({ embedding: { values: [1, 2] } });
  });

  try {
    const service = new EmbeddingService(config("gemini"));
    assert.deepEqual(await service.embed_text("hello"), [1, 2]);
    assert.deepEqual(await service.embed_batch(["one", "two"]), [[1, 2], [3, 4]]);
  } finally {
    restore();
  }
});

test("empty input and failures match Python return conventions", async () => {
  const originalWarn = console.warn;
  const originalError = console.error;
  console.warn = () => {};
  console.error = () => {};
  const restore = mockFetch(async () => jsonResponse({ error: "failed" }, 500));

  try {
    const service = new EmbeddingService(config("openai"));
    assert.equal(await service.embed_text("  "), null);
    assert.deepEqual(await service.embed_batch([]), []);
    assert.equal(await service.embed_text("hello"), null);
    assert.deepEqual(await service.embed_batch(["a", "b"]), [null, null]);
  } finally {
    restore();
    console.warn = originalWarn;
    console.error = originalError;
  }
});

test("malformed provider vector is treated as an embedding failure", async () => {
  const originalError = console.error;
  console.error = () => {};
  const restore = mockFetch(async () =>
    jsonResponse({ data: [{ embedding: ["not-a-number"] }] }),
  );

  try {
    assert.equal(
      await new EmbeddingService(config("openai")).embed_text("hello"),
      null,
    );
  } finally {
    restore();
    console.error = originalError;
  }
});
