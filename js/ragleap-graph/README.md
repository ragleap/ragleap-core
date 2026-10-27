# @ragleap/graph (work in progress, not published)

TypeScript port of the Python package ragleap-graph, built the same way as the Java port: outputs are compared with the real Python code at a pinned commit.

Ported so far (first slice): composite MERGE key, entity name normalization, regex entity extraction for queries.

Not ported yet: the Neo4j HTTP runner, GraphIndex, retrieval, LLM extraction, migrations, backfills, the Postgres audit writer.

Tests: `RAGLEAP_GRAPH_PARITY_DIR=<fixtures> node --test test/parity.test.ts` (Node 24). Without the variable the parity tests skip and only the small unit tests run. Fixtures come from java/ragleap-graph/parity/gen_fixtures_*.py.

Known difference: Python raises on lone surrogates when hashing, JavaScript replaces them.
