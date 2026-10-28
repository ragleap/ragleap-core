import test from "node:test";
import assert from "node:assert/strict";

import {
  backend_supports_sparse,
  search_hybrid_fallback,
} from "../dist/vectorstores.js";

test("sparse search defaults to unsupported", () => {
  assert.equal(backend_supports_sparse({}), false);
});

test("sparse capability is respected when explicitly enabled", () => {
  assert.equal(
    backend_supports_sparse({ supports_sparse: () => true }),
    true,
  );
});

test("hybrid fallback delegates to dense search", async () => {
  const expected = [
    {
      chunk_id: "chunk-1",
      text: "Example content",
      similarity_score: 0.9,
      document_id: "doc-1",
      document_name: "example.txt",
      chunk_index: 0,
    },
  ];

  let receivedArgs;
  const backend = {
    search_dense: async (args) => {
      receivedArgs = args;
      return expected;
    },
  };

  const result = await search_hybrid_fallback(backend, {
    query_text: "example",
    embedding: [0.1, 0.2],
    top_k: 3,
    document_id: undefined,
    metadata_filter: { tenant: "acme" },
  });

  assert.deepEqual(result, expected);
  assert.deepEqual(receivedArgs, {
    embedding: [0.1, 0.2],
    top_k: 3,
    document_id: undefined,
    metadata_filter: { tenant: "acme" },
  });
});
