# ragleap-rag (Java)

Java port of the RAG pipeline of [ragleap-rag](https://pypi.org/project/ragleap-rag/), published on Maven Central as
`io.github.antonyrag:ragleap-rag`. Needs Java 21. It is versioned independently from the Python package.

````xml
<dependency>
    <groupId>io.github.antonyrag</groupId>
    <artifactId>ragleap-rag</artifactId>
    <version>0.13.0</version>
</dependency>
````

(The newest version is on [Maven Central](https://central.sonatype.com/artifact/io.github.antonyrag/ragleap-rag).)

## Quick start

````java
import com.ragleap.rag.AskOptions;
import com.ragleap.rag.AskResult;
import com.ragleap.rag.RagLeap;
import com.ragleap.rag.RagLeapConfig;
import com.ragleap.rag.embedding.EmbeddingConfig;
import com.ragleap.rag.generation.ProviderConfig;

public class Quickstart {
    public static void main(String[] args) throws Exception {
        String key = System.getenv("OPENAI_API_KEY");
        // any OpenAI-compatible chat model and embedding model; the dimension must match the embedding model
        ProviderConfig llm = new ProviderConfig("openai", key, "gpt-4o-mini", null);
        EmbeddingConfig embedder = new EmbeddingConfig("openai", key, "text-embedding-3-small", 1536, null);
        RagLeapConfig config = RagLeapConfig
                .builder("postgresql://user:password@localhost:5432/ragleap", llm, embedder)
                .build();

        try (RagLeap rag = new RagLeap(config)) {
            rag.initSchema();   // creates the tables and the vector index
            rag.ingestText("notes.txt", "RagLeap answers questions from your own documents.");

            AskResult answer = rag.ask("What does RagLeap do?");
            System.out.println(answer.answer());

            AskOptions options = AskOptions.builder().topK(5).hybrid(true).sessionId("demo").build();
            rag.askStream("Summarize the notes.", options, piece -> System.out.print(piece));
        }
    }
}
```

The snippet was compiled against this module's sources but not run (running needs a PostgreSQL with pgvector and a
provider key). The database url has the same form as `RAGLEAP_JAVA_TEST_DATABASE_URL` in the CI job.

## What is included

- **Pipeline:** connection pooling (HikariCP), chunking, embeddings, retrieval, generation (streaming, structured
  output with validation, provider fallback, vision captioning), conversation memory, query rewriting (HyDE and
  multi-query), response cache, cost tracking with a monthly budget and a cheaper fallback provider, guardrails,
  observability hooks and evaluation scoring.
- **Providers:** chat providers with a built-in base URL are openai, mistral, groq, together, openrouter, ollama,
  deepseek, xai, cohere and perplexity (demonroute from the release after 0.13.0); gemini and anthropic have their own
  clients; `custom` takes a base URL. Seven embedding providers: gemini, openai, mistral, together, ollama, cohere and
  voyage.
- **Vector backends:** pgvector (default; dense, sparse and hybrid search with RRF), FAISS, Qdrant, Weaviate, Milvus
  and Pinecone. The sibling module `java/ragleap-vectorstores` (published separately as `io.github.antonyrag:ragleap-vectorstores`) adds OpenSearch, Upstash and Redis
  backends for the same `VectorBackend` interface.
- **Ingestion:** a document parser for 27 of the Python package's 28 formats (no Parquet; legacy `.xls` needs the
  optional Apache POI dependency), OCR through the Tesseract binary, audio extraction from video through the ffmpeg
  binary, audio transcription (OpenAI Whisper, Deepgram or your own function), archive limits, and batch ingestion of
  mixed items.

## The `RagLeap` entry point

| Area | Methods |
|---|---|
| Setup | `new RagLeap(config)`, `initSchema()`, `close()`, `cacheStats()` |
| Ingest | `ingest`, `ingestText`, `ingestImage`, `ingestAudio`, `ingestVideo`, `ingestBatch`, `updateDocument` |
| Ask | `ask`, `askStream` (callback), `retrieve`, `evaluate` |
| Sessions | `getHistory`, `clearSession` |
| Documents | `listDocuments`, `deleteDocument` |

Configuration goes through `RagLeapConfig.builder(databaseUrl, primaryProvider, embeddingConfig)` (vector backend,
fallbacks, temperature, token and context limits, chunk size and overlap, cache, guardrails, hooks, pricing table,
budget) and per-call settings through `AskOptions.builder()` (top-k, temperature, system prompt, max tokens, hybrid
search, session id, metadata filter, response format, query rewrite, multi-query count).

## Not live-verified

- **Milvus and Pinecone** are tested against HTTP stubs only (no infrastructure to run them).
- **Audio transcription** (Whisper, Deepgram) is tested with stubs only.
- The test suite runs against stubs and a local Ollama; hosted providers with real API keys are not part of the
  verification.

## Not ported, and differences from Python

- Not ported: Parquet, URL ingestion, reranking, the Redis query cache and the async variants (use your own executor).
- Query rewriting is an enum (`QueryRewrite`), streaming uses a callback, and the cache and the cost tracker are
  guarded by locks.
- `ingestBatch` keeps the metadata of `file` items; the Python package did not until ragleap-rag 0.14.0.
- Java versions are independent of the Python versions.

## How it is verified

- **Parity with the real Python code:** fixtures are generated by running the Python package at a pinned commit and the
  Java output is compared with them (parsers, OCR and video extraction, and the whole `RagLeap` facade: 33 scenarios
  compared as one ordered log of every call against fake collaborators). The facade was pinned at Python commit
  `d210dab` (ragleap-rag 0.13.0). Python 0.14.0 changed only the FAISS and Pinecone score scale, the batch metadata
  and the `demonroute` entry; the Java backends already used the 0 to 1 scale and the batch metadata behavior, and
  `demonroute` is on `main`.
- **Cross-backend benchmark** (`benchmark/RESULTS.md`): FAISS, pgvector, Qdrant and Weaviate return identical
  retrieval on the same corpus; it found five bugs that were fixed.
- **Tests:** 502 tests, 0 failures and 8 skipped in the release build of 0.13.0, which excludes five live-Ollama tests
  that are flaky on a slow CPU. The parity and live tests skip unless their environment variables are set, so the CI
  job (`ragleap-rag-java-tests`, with a PostgreSQL service) runs only the rest.

```bash
# unit tests (parity and live tests skip without their environment variables)
cd java/ragleap-rag && mvn -B test

# facade parity: fixtures come from the real Python package at the pinned commit
python3 parity/gen_facade_fixtures.py
RAGLEAP_FACADE_PARITY_DIR=/tmp/fixtures-facade \
RAGLEAP_JAVA_TEST_DATABASE_URL=postgresql://user:password@localhost:5432/ragleap_java_test \
mvn -B test -Dtest='RagLeapTest,RagLeapParityTest,RagLeapWiringTest'
```

## Requirements

Java 21; PostgreSQL with the pgvector extension for the default backend; the Tesseract and ffmpeg binaries only if you
use OCR or video ingestion; Apache POI only for legacy `.xls`. License: MIT.
