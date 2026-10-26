# ragleap-vectorstores (Java)

Java port of [ragleap-vectorstores](https://pypi.org/project/ragleap-vectorstores/): extra vector store backends for
[ragleap-rag](../ragleap-rag). Every backend implements ragleap-rag's `VectorBackend` interface and talks to its store
over REST with the JDK `HttpClient` and Jackson, with no vendor SDK.

Published on Maven Central as `io.github.antonyrag:ragleap-vectorstores` (Java 21; [ragleap-rag](../ragleap-rag) comes in as a dependency). OpenSearch, Upstash and Redis backends exist so far; Chroma and LanceDB are not ported.

```xml
<dependency>
    <groupId>io.github.antonyrag</groupId>
    <artifactId>ragleap-vectorstores</artifactId>
    <version>0.1.0</version>
</dependency>
```

## OpenSearch

```java
OpenSearchBackend backend = new OpenSearchBackend("http://localhost:9200");
RagLeapConfig config = RagLeapConfig.builder(databaseUrl, provider, embeddingConfig).vectorBackend(backend).build();
```

(Snippet not compiled; check names against the ragleap-rag javadoc.)

- Index with an HNSW k-NN field (engine lucene, cosine); the document registry is a second index named
  `<index>_documents`.
- Scores are OpenSearch's own, already in [0, 1]; no sparse search (`supportsSparse()` is false).
- A `metadataFilter` can only use indexed fields (`document_id`, `document_name`, `chunk_index`, `token_count`). A key from the chunk's own metadata matches nothing, because the metadata is stored as an unindexed string; an unknown field matches nothing either. The Python backend behaves the same (confirmed against a live OpenSearch 3.9.0 and copied on purpose).
- Constructor options: index name, documents index name, `useSsl`, basic authentication, your own `HttpClient`.

### Differences from Python

- TLS certificates are verified by default (Python's `verify_certs` defaults to false). Pass your own `HttpClient`
  for custom trust.
- Only basic authentication and `use_ssl` are supported, not opensearch-py's `client_kwargs`.
- Like Python, the URL scheme is ignored: only `useSsl` chooses http or https.
- Failures throw `SQLException` (Python: `TransportError`); a dimension mismatch throws `IllegalStateException`
  (Python: `RuntimeError`).

### How it was verified

Recorded-request parity: the real Python `OpenSearchBackend` with the real opensearch-py 3.2.0 client runs against a
fake HTTP server that records every request; the Java backend runs the same 29 scripted scenarios against a JDK fake
server and must send identical requests (method, path, query, body, auth) and return identical results. Ten
deliberate breaks of the Java code were each caught by the tests.

**Live check:** the gated `OpenSearchLiveTest` (env `RAGLEAP_VS_LIVE_OPENSEARCH_URL`, skipped in CI) was run once against a throwaway OpenSearch 3.9.0 container (index create and reuse, dimension mismatch, insert, search order and scores, `document_id` and `chunk_index` filters, listing, delete); no other OpenSearch version and no security plugin, TLS or authentication was tried. Python was pinned at commit `de9d55b`.

```bash
# regenerate fixtures (needs the pinned Python package and a venv with opensearch-py 3.2.0, upstash-vector 0.8.0, redis-py 8.0.1 and numpy)
VS_PIN=de9d55b python parity/gen_fixtures_opensearch.py
VS_PIN=de9d55b python parity/gen_fixtures_upstash.py
VS_PIN=de9d55b python parity/gen_fixtures_redis.py
RAGLEAP_VS_PARITY_DIR=/root/fixtures-vs mvn -B test
```

Without `RAGLEAP_VS_PARITY_DIR` the parity tests are skipped (this is what CI runs).

## Upstash

```java
UpstashBackend backend = new UpstashBackend("https://YOUR-INDEX.upstash.io", token);
// optional: new UpstashBackend(url, token, "my-namespace", "/path/to/registry.sqlite3")
```

(Snippet not compiled.) The index must already exist as a pure dense index (create it in the Upstash console);
`initSchema` only checks it. Chunk text and metadata live in Upstash; the document registry (filename and upload
time) is a local SQLite file, by default `~/.ragleap_vectorstores/upstash_documents.sqlite3`. Use a distinct
namespace for each separate deployment that shares one index. Scores are Upstash's own, already in [0, 1]; no sparse
search.

### Differences from Python

- No retry on connection failures (the Python SDK retries 3 times, 1 s apart); 30 s request timeout (SDK: 600 s).
- The url is used exactly as given, like the SDK (no scheme is added; a trailing slash gives `//info`).
- A namespace is sent percent-encoded; the SDK leaves a few characters raw that `java.net.URI` cannot send
  (invalid percent escapes, square brackets, `|`, `^`). Those two scenarios compare the decoded path.
- A filter value that is a list or map throws; a missing or non-numeric `chunk_index` comes back as 0.
- `listDocuments` follows the `range` cursor until the scan ends or the cursor repeats, like the fixed Python (before
  that fix Python counted at most 1000 chunks per document); a `nextCursor` that is neither a string nor null throws.
- Failures throw `SQLException`; a wrong dimension or a hybrid index throws `IllegalStateException`.

Copied from Python on purpose (possible Python issues, not fixed here): `deleteDocument` deletes by the prefix `<id>:`, so a document id that
contains a colon can collide with another document's chunks.

### How it was verified

Recorded-request parity against the real Python `UpstashBackend`, the real upstash-vector 0.8.0 SDK and a real SQLite
registry: 42 scripted scenarios (88 requests). 40 are compared request by request and result by result (85 requests); one (url with a trailing slash) is checked by a raw-socket test that reads the literal request line;
one is skipped because Python's own HTTP client rejects it (a tab in the namespace). Fifteen deliberate breaks of the
Java code were each caught by the tests. **Not live-verified:** no real Upstash index was used. Four more deliberate breaks cover the cursor paging and two more the metadata merge order, each caught by the tests.

## Redis

```java
RedisBackend backend = new RedisBackend("redis://localhost:6379/0");
// optional: new RedisBackend(url, "my_index", "my:prefix:", "/path/to/registry.sqlite3")
```

(Snippet not compiled.) Needs Redis Stack, or Redis with the RediSearch module: `initSchema` checks `MODULE LIST` and
creates an HNSW cosine index over hashes. Chunks are hashes (text, metadata, float32 vector, little-endian); the
document registry (filename and upload time) is a local SQLite file, by default
`~/.ragleap_vectorstores/redis_documents.sqlite3`. The default key prefix derives from the index name, so two indexes
never share keys. Scores are `1 - distance / 2` (clamped), on the same 0 to 1 scale as the other backends; no sparse search. Commands go over a small built-in RESP2 client
(no vendor SDK, no connection pool, one socket); urls are `redis://` or `rediss://` with optional user, password and
database.

### Differences from Python

- No client library: the built-in client does not pool connections; authentication, database selection and TLS are
  not covered by the Python parity tests (a small unit test checks the `AUTH` and `SELECT` commands).
- A filter value that is a list or map throws; failures throw `SQLException`; a missing search module throws
  `IllegalStateException` (Python: `RuntimeError`).

Copied from Python on purpose (possible Python issues, not fixed here): only `document_id` can be filtered (other
keys are accepted and ignored); an existing index is reused without checking its dimension; any failure of `FT.INFO`,
even a connection failure, makes `initSchema` try to create the index; `deleteDocument` removes at most 10000 chunks
of a document.

### How it was verified

Recorded-command parity against the real Python `RedisBackend`, the real redis-py 8.0.1 client (forced to RESP2) and
a real SQLite registry: 25 scripted scenarios (75 commands, arguments compared byte for byte, including the float32
vector bytes). Eighteen deliberate breaks of the Java code were each caught by the tests. **Live check:** the gated `RedisLiveTest` (env `RAGLEAP_VS_LIVE_REDIS_URL`, skipped in CI) was run once on 2026-10-21 against a throwaway Redis Stack 7.4.7 (index create and reuse, insert, search, `document_id` filter, list, delete and the score scale all worked); no other Redis version was tried. The Python docstring says it was verified against redis-py 7.3.0; parity here ran against 8.0.1.

## Not ported yet

Chroma and LanceDB.
