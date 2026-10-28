# @ragleap/rag

TypeScript RAG utilities for RAGLeap, designed for Node.js 22 and later.

This package provides typed building blocks for document ingestion, text
chunking, embeddings, answer generation, vector search, PostgreSQL/pgvector,
conversation memory, structured output validation, caching, sanitization,
and observability hooks.

## Requirements

- Node.js 22 or later
- PostgreSQL with pgvector for the PostgreSQL-backed features
- API credentials for whichever embedding or generation providers you use

## Installation

```bash
npm install @ragleap/rag
```

## Build and test from source

```bash
npm install
npm run build
npm test
```

## Basic usage

Import the utilities you need from the package:

```ts
import {
  TextChunker,
  sanitize_text,
  detect_injection_risk,
} from "@ragleap/rag";

const text = sanitize_text("Example document text");
const chunker = new TextChunker();
const chunks = chunker.chunk_text(text);

console.log(chunks);
console.log(detect_injection_risk(text));
```

Provider configuration, database setup, and ingestion options depend on the
features you use. See the exported TypeScript declarations and source code for
the current API.

## Scope

This is the TypeScript package in the RAGLeap Core repository. It is distinct
from the Python `ragleap-rag` package; installing or building this package does
not modify the Python package.

## Project

- Repository: https://github.com/ragleap/ragleap-core
- Website: https://ragleap.com
- License: MIT

## Optional reranking

`RerankerService` can reorder retrieved chunks by how relevant they are to a
query. The Transformers.js runtime is optional; install it only if you use the
reranker. The rest of the package does not require this additional dependency.

Install the package and the optional runtime in your application:

```bash
npm install @ragleap/rag @huggingface/transformers
```

Example:

```ts
import { RerankerService } from "@ragleap/rag";

const reranker = new RerankerService();

const candidates = [
  { text: "Paris is the capital of France." },
  { text: "Bananas contain potassium." },
  { text: "France is a country in Europe." },
];

const ranked = await reranker.rerank(
  "What is the capital of France?",
  candidates,
  2,
);

console.log(ranked);
```

The default model is `Xenova/ms-marco-MiniLM-L-6-v2`. Model loading is lazy;
the first reranking call may download model files and therefore needs network
access and disk space. Reranking scores are raw model scores (logits), not
probabilities or percentages. Results are ordered from highest to lowest
score, and `top_k` limits the returned results.

## Status and limits

This is an early TypeScript port. It has not been checked for parity with the full Python `ragleap-rag` package: missing features and behavioral differences have not been audited, and no comparison against recordings of the real Python code has been done yet. Do not assume it behaves like the Python or Java versions.

The published 0.2.0 was released by hand without provenance. The source lives in `js/ragleap-rag` of https://github.com/ragleap/ragleap-core; later releases are meant to be published from GitHub Actions.
