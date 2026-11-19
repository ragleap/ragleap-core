# ragleap-graph — Build Handoff Document

**Purpose:** continuity doc for a new chat session to resume building
`ragleap-graph`. Scoping is done; implementation hasn't started.

---

## Current state

- **Package skeleton created** at `~/ragleap-core/packages/ragleap-graph/`
  on the VPS (`srv1477778`)
- `pyproject.toml` written and matches `ragleap-rag`'s exact conventions
  (hatchling build backend, same author/license/classifiers pattern).
  Verified complete via direct `cat` inspection (not `tomllib` — that
  module wasn't available in the system Python being used; real
  validation will happen when `hatchling` actually builds the package)
- Directory structure: `src/ragleap_graph/` and `tests/` both exist,
  currently empty
- **Nothing has been coded yet** — no `GraphConfig`, no `GraphIndex`,
  no tests

## Where the real production code lives (read this first, don't guess)

`cdprod` → `/var/www/ragleap/backends/claude_backend` (the private
production Django backend — NOT the `ragleap-core` public monorepo,
don't confuse the two, this has bitten this session before).

Core files, already reviewed this session:
- `retrieval/graph_service.py` — 563 lines, the real `GraphService` class
- `retrieval/neo4j_startup.py` — 293 lines, connection/startup logic

28 files across 8 Django apps reference Neo4j/graph in some way, but
match-density analysis (`grep -c`) confirmed these two files are where
the actual logic lives — everything else is either a consumer calling
into `GraphService` or a passing reference (settings flag, migration
field). Full file list and match counts are in this session's history
if needed again, but the two files above are the ones that matter.

## The 5 real methods to port (verified via direct code review)

| Production method (Django-coupled) | Target public API | What it does |
|---|---|---|
| `upsert_document_graph(document_id, workspace_id, document_title, chunks, language=None, max_entities=80, max_pairs=150)` | `graph.upsert_document(...)` | Regex-based entity extraction per chunk, builds co-occurrence edges between entities in the same chunk, `MERGE`-based idempotent Neo4j writes |
| `find_documents_by_entities(entity_names, workspace_id, limit=25)` | `graph.find_documents_by_entities(...)` | Given entity names, returns documents connected to them, ranked by matched-entity count + summed edge weight |
| `search_related_entities(entity_names, workspace_id, max_depth=2, limit=10)` | `graph.search_related_entities(...)` | Graph traversal — finds entities related via N-hop paths |
| `find_document_entities(document_id, workspace_id, entity_types=None)` | `graph.document_entities(...)` | Per-document entity lookup, optional type filter |
| `health_check()` | `graph.health_check()` | Already generic, minimal changes needed |

Full method bodies (Cypher queries, exact logic) were reviewed line by
line this session — if this handoff doc is being read fresh without
that history, re-fetch them from the production file directly rather
than guessing at the Cypher:

```bash
cdprod
sed -n '1,60p' ./retrieval/graph_service.py      # class init, entity normalization
sed -n '163,326p' ./retrieval/graph_service.py   # upsert_document_graph, full body
sed -n '326,548p' ./retrieval/graph_service.py   # find_documents_by_entities, search_related_entities, find_document_entities, health_check
```

## Two required adaptations — decided, not open questions

### 1. `workspace_id` → optional `namespace=`

**Decision made and confirmed this session**: production code requires
`workspace_id` on every method (Django multi-tenant SaaS assumption).
For the open-source package, this becomes an **optional** `namespace=`
parameter, defaulting to `None`:

```python
graph.upsert_document(document_id="...", title="...", chunks=[...])                    # single global graph
graph.upsert_document(document_id="...", title="...", chunks=[...], namespace="acme")  # multi-tenant isolated
```

This mirrors `ragleap-rag`'s own `metadata_filter=` pattern — optional,
generic, doesn't force one usage model onto every user. This is the
established, deliberate convention for this ecosystem; don't relitigate
it without a real reason.

### 2. Django `settings` coupling → explicit `GraphConfig`

Production code does `from django.conf import settings` and
`getattr(settings, 'NEO4J_URI', ...)`. The package needs a config
dataclass instead, matching `ragleap-rag`'s `EmbeddingConfig`/
`ProviderConfig` pattern:

```python
from ragleap_graph import GraphIndex, GraphConfig

graph = GraphIndex(config=GraphConfig(
    uri="bolt://localhost:7687",
    user="neo4j",
    password="...",
))
```

## One real security fix required before shipping

`search_related_entities()` builds its Cypher query using
**`.format(max_depth=max_depth)`** to inject the traversal depth
directly into the query string (`[*1..{max_depth}]`), rather than using
parameterized query args like every other method in the file. This is
likely a genuine Neo4j limitation (Cypher can't parameterize
variable-length path bounds normally) — but as a **public library API**
accepting `max_depth` from any caller, this needs explicit validation
(`isinstance(max_depth, int)` and a sane upper bound, e.g. reject
values > 10) before string formatting, or it's a real Cypher injection
vector. Don't carry this over unguarded.

## Package conventions to follow (from `ragleap-rag`, don't reinvent)

- `pyproject.toml`: hatchling build backend, same classifiers/license
  pattern, `[project.optional-dependencies]` with a `test` extra
- Version bumps go in both `pyproject.toml` and
  `src/ragleap_graph/__init__.py`'s `__version__` — grep-check both
- CHANGELOG.md discipline: never rewrite past entries, add corrections
  as new entries
- For any third-party SDK usage (the `neo4j` Python driver), verify
  exact method signatures via `inspect.signature()` against the real
  installed package before writing code — don't trust memory or docs.
  This caught a real pre-ship bug in `PineconeBackend` before; apply the
  same discipline here.
- Full test suite must pass before any commit is considered done — this
  project's standard, demonstrated repeatedly (238/238 for `ragleap-rag`)
- Live-verification advantage specific to this package: **you have real,
  working Neo4j access already** (it's live in production) — meaning
  `ragleap-graph` can be fully live-verified from day one, unlike several
  `ragleap-rag` vector backends that shipped honestly labeled
  "code-complete but unverified" due to no account access. Don't skip
  the live verification just because it's more available this time —
  make sure it actually happens.

## Immediate next steps for the new session

1. Write `src/ragleap_graph/__init__.py` with `GraphConfig` and
   `GraphIndex` classes, porting the 5 methods above with the two
   adaptations (namespace, config) and the `max_depth` fix applied
2. Write a real `README.md` for the package (required — `pyproject.toml`
   already references `readme = "README.md"` and will fail to build
   without it)
3. Write tests — mirror `ragleap-rag`'s test structure (constructor
   validation, pure logic tests where possible without a live Neo4j,
   then real integration tests against your actual working Neo4j
   instance)
4. Verify build: `python3 -m build` inside
   `~/ragleap-core/packages/ragleap-graph/`
5. Only after tests pass and a real Neo4j round-trip is verified: decide
   whether to publish to PyPI or keep it in-repo longer

## Session artifacts also produced today (separate from this task)

- `ragleap-rag` v0.11.2 published to PyPI (README diagram fixes,
  benchmark DB isolation bug fixed)
- Full 9-page GitHub wiki written and pushed
  (`github.com/ragleap/ragleap-core/wiki`)
- Marketing/launch copy prepared in a separate handoff doc (Product
  Hunt, LinkedIn, Dev.to) — for `ragleap-rag` specifically, not
  `ragleap.com` the commercial platform (these are different products,
  don't mix their messaging)
