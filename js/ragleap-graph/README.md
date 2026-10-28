# @ragleap/graph (work in progress, not published)

TypeScript port of the Python package ragleap-graph, built the same way as the Java port: outputs are compared with the real Python code at a pinned commit.

Ported so far: composite MERGE key, entity name normalization, regex entity extraction for queries, and the Neo4j HTTP Query API runner (GraphConfig, QueryApiRunner, GraphException, GraphTransientException), and the GraphIndex shell (async GraphIndex.connect, the six composite_key constraints, health check, close, query entity extraction).

Not ported yet: the GraphIndex upsert and query methods, the extraction config, the audit hook, retrieval, LLM extraction, migrations, backfills, the Postgres audit writer.

Tests: `RAGLEAP_GRAPH_PARITY_DIR=<fixtures> node --test test/parity.test.ts` (Node 24). Without the variable the parity tests skip and only the small unit tests run. Fixtures come from java/ragleap-graph/parity/gen_fixtures_*.py.

Known difference: Python raises on lone surrogates when hashing, JavaScript replaces them.

Runner notes: `fetch` has no separate connect timeout, so one request timeout (default 60 s) covers the whole call. JavaScript cannot tell 1 from 1.0: wrap values that must be FLOAT in `neo4jFloat(x)`; plain integral numbers are sent as INTEGER. Integers beyond 2^53 are returned as `BigInt`, and must be passed as `BigInt`. `undefined` parameters are sent as null. Python talks to Neo4j over Bolt, so the runner has no recorded-request parity with Python: it is checked against a fake HTTP server (the Java runner test scenarios) and against a live Neo4j 5.26.29 Community server (gated by NEO4J_HTTP_URL).

GraphIndex notes: create an index with `await GraphIndex.connect(config)` (a JavaScript constructor cannot wait for the network); it never throws when Neo4j is unreachable, `isAvailable()` is then false. Warnings go to `console.warn` unless you pass `{ logger }`. `close()` and `Symbol.asyncDispose` close the runner. The construction behaviour (statements run, availability, closing, health check) is compared with the real Python GraphIndex recorded against a fake driver (parity/gen_fixtures_index.py); the Python branch for a missing neo4j package and a failing session open have no equivalent here.
