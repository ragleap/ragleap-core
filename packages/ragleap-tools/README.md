# ragleap-tools

Standalone, dependency-light tool implementations for LLM tool-calling.
Exposes OpenAI-, Gemini- and Anthropic-style function-calling schemas.

```bash
pip install ragleap-tools
```

## What this is (and isn't)

`ragleap-tools` provides `Tool` objects - a name, a description, a JSON
Schema for parameters, and a safe handler function. It does **not** own
a tool-calling execution loop (deciding when to call a tool, running
it, feeding the result back to the model) - that's `ragleap-agents`'
job, per the project roadmap's own split. Wire these tools into your
own tool-calling code, or into `ragleap-agents` once that ships.

## Quickstart

```python
from ragleap_tools import STATELESS_TOOLS, CALCULATOR_TOOL

# Give these to your LLM provider's tools= parameter:
openai_tools = [t.to_openai_schema() for t in STATELESS_TOOLS]
gemini_tools = [t.to_gemini_schema() for t in STATELESS_TOOLS]

# When the model calls one, invoke the handler yourself:
result = CALCULATOR_TOOL.call(expression="2 + 2 * sqrt(16)")
print(result.success, result.result)  # True 10.0
```

## Write your own tool

A tool is a type-annotated function. The schema comes from the annotations and
the docstring; the model's arguments are checked before your function runs.

```python
from typing import Literal
from ragleap_tools import tool, ToolError

@tool
def lookup_order(order_id: str, status: Literal["open", "closed"] = "open") -> dict:
    """Look up an order by id.

    Args:
        order_id: The order id, for example "A-1001".
        status: Which orders to search.
    """
    if order_id not in ORDERS:
        raise ToolError("order not found")   # safe to show the model
    return ORDERS[order_id]

lookup_order.to_openai_schema()      # also to_gemini_schema() / to_anthropic_schema()
```

Supported annotations: `str`, `int`, `float`, `bool`, `Optional[X]` (give it a
default), `Literal[...]`, `list[X]`, `dict`, `Any`. Return a `ToolResult` to
control the result, or any value to have it wrapped. `ToolError` messages reach
the model; any other exception reaches it only as "name failed (Type)" (the
detail goes to the `ragleap_tools` logger). Async functions are not supported
yet. See `docs/design/tool-authoring.md`.

## Dispatch safely with a Toolkit

`Toolkit` collects tools and dispatches one model call without ever raising.
Your loop is still yours.

```python
from ragleap_tools import Toolkit, default_toolkit

kit = default_toolkit().add(lookup_order)            # the 12 stateless tools + yours
provider_tools = kit.to_openai_schema()               # for the provider's tools= parameter

# name and arguments exactly as the provider returned them (a JSON string or a dict):
result = kit.call(name_from_model, arguments_from_model)
print(result.success, result.result or result.error)

read_only = kit.select(where=lambda t: t.metadata.get("read_only"))
```

`call` fails cleanly (a failed `ToolResult`, no exception) for an unknown tool,
invalid or oversize JSON (100,000 characters by default), arguments that do not
match the tool's schema, and any exception from the handler. `Tool.metadata` is
a free-form dict for your own policy code; it is never sent to a provider.

`call` is `prepare` followed by `run`. Use them separately when you must
decide something between validating the arguments and running the handler
(a policy, an approval, a rate limit): `prepare(name, arguments)` returns
`(tool, arguments)` or the same failed `ToolResult` that `call` would return,
without running the handler, and `run(tool, arguments)` runs the handler with
the same containment as `call`.

## Run tools for several tenants (GovernedToolkit)

`GovernedToolkit` wraps a `Toolkit` for a host that serves several tenants.
Every call carries a `Scope` (a tenant, and optionally a subject, agent and
session) that your own code has already established, and your rules decide what
that scope may do:

```python
from ragleap_tools import GovernedToolkit
from ragleap_tools.governed import RateLimit, Scope

gk = GovernedToolkit(
    kit,
    allow_for=lambda scope: ["calculator"] if scope.tenant == "free" else kit.names(),
    require_approval=["write_file"],        # approve() must say exactly True
    approve=lambda scope, tool, arguments: ask_a_human(scope, tool, arguments),
    rate_limit=RateLimit(calls=60, per_seconds=60),   # per tenant, per process
    on_event=audit_sink,                    # one small dict per call attempt
)
result = gk.call(Scope(tenant="acme", subject="alice"), name, arguments)
```

The checks run in this order and the first failure stops the call: the scope
must be exactly a `Scope`; the tool must exist and be named by
`allow_for(scope)` (deny by default, and an unknown tool and a tool that is not
allowed get the same answer, "tool not available"); the tenant's rate limit
must have room; the arguments are validated; `approve` must return exactly
`True` for a tool named in `require_approval`; then the handler runs. `call`
never raises. The events never contain arguments or results, and `allow_for` or
`approve` raising means refused.

What it does not do:

- It does not authenticate anybody. It trusts the `Scope` you pass, so its
  isolation is only as strong as your own check of who the caller is.
- A toolkit shared by several tenants shares its tools' state (one file root,
  one API key per provider, one `RagLeap` instance). Build one toolkit per
  tenant from that tenant's own configuration.
- The rate limit is kept per process. Windows are kept for every tenant seen
  until `forget(tenant)` is called.

`Scope` is `ragleap_security.Scope` when `pip install ragleap-tools[security]`
is installed, and an equivalent built-in class otherwise. The design, with the
rejected alternatives and the limits, is in `docs/design/governed-toolkit.md`.

## The 12 stateless tools

No configuration needed - import and use directly.

- `CALCULATOR_TOOL` - safe arithmetic (AST-based whitelist, never `eval()`)
- `CURRENT_DATETIME_TOOL`, `ADD_TO_DATE_TOOL`, `DATE_DIFFERENCE_TOOL` - date/time math
- `CONVERT_LENGTH_TOOL`, `CONVERT_WEIGHT_TOOL`, `CONVERT_TEMPERATURE_TOOL` - unit conversion
- `PARSE_JSON_TOOL`, `PARSE_CSV_TOOL` - structured data parsing
- `REGEX_EXTRACT_TOOL`, `WORD_COUNT_TOOL`, `TEXT_CASE_TRANSFORM_TOOL` - text utilities

## File operations (sandboxed, needs configuration)

```python
from ragleap_tools import FileOpsConfig, make_file_tools

config = FileOpsConfig(root_dir="/path/to/a/safe/directory")
read_tool, write_tool, list_tool = make_file_tools(config)
```

Every operation is confined to `root_dir` - both `../` path traversal
and symlink-based escapes are rejected (verified via real security
tests, not just documented), not just naive string-prefix checking.
There is no unsandboxed mode.

## Document ingestion and search (optional, needs ragleap-rag)

```bash
pip install ragleap-tools[ingest]
```

```python
from ragleap import RagLeap, ProviderConfig, EmbeddingConfig
from ragleap_tools import IngestConfig, make_ingest_tool, SearchConfig, make_search_tool

rag = RagLeap(database_url="...", primary=ProviderConfig(...), embedder=EmbeddingConfig(...))
ingest_tool = make_ingest_tool(IngestConfig(rag=rag))
search_tool = make_search_tool(SearchConfig(rag=rag))
```

`ingest_document` wraps `ragleap-rag`'s already-tested `ingest_text()` -
no new ingestion logic, just a tool schema on top of the real
28-format-capable pipeline. As of v0.1.1, it stores the filename as
metadata (`{"filename": filename}`), enabling the per-document search
below - v0.1.0 did not pass any metadata, which silently made
per-document filtering impossible.

`search_documents` wraps `ragleap-rag`'s already-tested `retrieve()`
for hybrid vector+keyword search over previously ingested documents.
Chunk dicts are returned unmodified - this tool doesn't assume
`ragleap-rag`'s exact field set. Pass an optional `filename=` to scope
the search to a single document previously ingested via
`ingest_document`:

```python
result = search_tool.call(query="what was the Q3 revenue?", filename="q3-report.pdf")
```

`ragleap-rag` owns the actual ingestion and retrieval logic; these are
thin adapters, same pattern `ragleap-graph` uses for its own optional
`ragleap-rag` dependency.

## Web search (BYOK, no extra dependencies)

```python
from ragleap_tools import WebSearchConfig, TavilySearchProvider, make_web_search_tool

provider = TavilySearchProvider(api_key="...")  # or SerperSearchProvider(api_key="...")
search_tool = make_web_search_tool(WebSearchConfig(provider=provider))

result = search_tool.call(query="latest pgvector release", num_results=5)
# result.result == {"results": [{"title": ..., "url": ..., "snippet": ...}, ...], "count": 5}
```

Bring your own key: you construct the provider you want with your own
`api_key`. There is no default provider and no environment-variable
fallback. Uses only the standard library (`urllib.request`), so
`ragleap-tools` still has zero required dependencies. `SearchProvider`
is a small abstract class - implement `search(query, num_results)` to
plug in any other search engine.

`num_results` is chosen by the model and costs your API quota, so it is
clamped to 1-20.

Honest limitations: request shapes for both reference providers were
checked against their current public docs, but **neither has been
called against a live account** - treat them as best-effort until
confirmed by someone with a real key. Results are text from arbitrary
third-party pages, so treat them as untrusted input: this tool does not
screen them for prompt injection. There is no caching, rate limiting
or deduplication.

## GitHub repository search (BYOK-optional, no extra dependencies)

```python
from ragleap_tools import GitHubSearchConfig, make_github_search_tool

tool = make_github_search_tool(GitHubSearchConfig())  # token optional
# or: GitHubSearchConfig(token="...") for a higher rate limit

result = tool.call(query="language:python topic:llm", num_results=5, sort="stars")
# result.result == {"results": [{"full_name": ..., "url": ..., "description": ..., "stars": ..., "language": ...}, ...], "count": 5}
```

Works without a token, at GitHub's lower unauthenticated limit for the
search resource (a live `/rate_limit` check on 2026-10-01 showed 10 for
search, versus 60 for GitHub's core API; check `/rate_limit` for your
own) - pass `token=` for a higher limit. No
environment-variable fallback: if you want a token used, you pass it.
Supports GitHub's real search qualifiers in the query string
(`language:`, `stars:`, `topic:`, etc.), same as GitHub's own search UI.
Scoped to repository search only, not code or issue search. Standard
library only (`urllib.request`) - no new dependency.

## Image description (BYOK, no extra dependencies)

```python
from ragleap_tools import (
    FileOpsConfig, GeminiVisionProvider, VisionConfig, make_vision_tool,
)

provider = GeminiVisionProvider(api_key="...", model="...")  # you choose the model
tool = make_vision_tool(VisionConfig(
    provider=provider,
    sandbox=FileOpsConfig(root_dir="/path/to/images"),
))
result = tool.call(path="chart.png", prompt="What does this chart show?")
# result.result == {"description": "..."}
```

`api_key` and `model` are both required - no environment-variable
fallback, no default provider, no default model. `AnthropicVisionProvider`
works the same way (and also takes `max_tokens`, default 1024, which caps
the description's length and cost). The tool only reads images inside the
sandbox directory, using the same path-escape protection as the file tools
(`../` traversal, absolute paths and symlink escapes are rejected). The
image type is detected from the file's bytes, not its extension. Defaults:
5,000,000 bytes per image (`max_image_bytes`) and 2,000 characters for the
model-supplied prompt (`max_prompt_chars`). Gemini accepts JPEG, PNG and
WebP here; Anthropic also accepts GIF. Standard library only
(`urllib.request`) - no new dependency.

URLs are deliberately not accepted: letting a model name a URL to fetch
carries the same SSRF risk as the HTTP-fetch tool listed below as out of
scope. The returned description is text derived from an image that may be
attacker-controlled - treat it as untrusted input (it is not screened for
prompt injection).

`GeminiVisionProvider` was live-checked once on 2026-10-03 (one call on a
small PNG, model `gemini-3.6-flash`): the request shape and response parsing
worked. JPEG and WebP input and Gemini's error responses were not
live-checked. `AnthropicVisionProvider` has not been called against a live
account - its request shape was checked against current public
documentation only, so treat it as best-effort until confirmed live.

## Network limits (all HTTP providers)

Every provider that makes an HTTP request (Tavily, Serper, GitHub search,
Gemini and Anthropic vision) enforces a response-size cap and a hard total
deadline. Both are configurable on the provider (or on `GitHubSearchConfig`):

```python
from ragleap_tools import TavilySearchProvider

provider = TavilySearchProvider(
    api_key="...",
    max_response_bytes=2_000_000,  # default 1,048,576
    total_timeout=30,              # seconds; default 20 (90 for vision)
)
```

An oversize response or an exceeded deadline returns a failed `ToolResult`
instead of hanging your tool-calling loop. The per-operation socket timeout
is unchanged (15 seconds; 60 for vision). Python cannot kill a thread, so
after the deadline an abandoned request is stopped on a best-effort basis
(its socket is shut down; failing that it ends within one per-operation
timeout), and DNS resolution is not covered by the per-operation timeout.
See `docs/design/http-limits.md`.

## Deliberately out of scope

Each of these needs its own security-focused design pass, not a
rushed inclusion here:

- **Code execution** - a real sandboxing/resource-limit design
  decision, not something to bolt on alongside a calculator.
- **HTTP fetch** - letting an LLM request arbitrary URLs carries real
  SSRF risk, same care level as code execution.
- **Database/business-system connectors** (SQL, CRM, payment
  processors, etc.) - some of what this ecosystem already has
  elsewhere (e.g. a live payment processor) would be a materially
  different risk if exposed to LLM tool-calling without deliberate
  guardrails (dry-run modes, confirmation steps, scoped permissions).

## Status

v0.6.0. 525 tests pass and 1 is skipped (526 pass with the optional
`security` extra installed), including real security verification
for the two risk-sensitive tools (calculator's code-injection
rejection, file ops' path-traversal and symlink-escape rejection) -
not just documented as safe, actually tested against real attack
vectors. `GovernedToolkit` has no network component, so there is nothing
to live-check for it, and it does not authenticate anybody.

Not live-verified: that OpenAI, Gemini or Anthropic accept the schemas `@tool`
generates (they use the same shape as the built-in tools plus `default` and
integer `enum`); the web search providers (Tavily, Serper), the
Anthropic vision provider, and authenticated GitHub requests - request
shapes were checked against current public documentation only. The Gemini
vision provider was live-checked once (see above). The v0.4.1 bounded
transport was live-checked for Gemini vision (2026-10-09) and for
unauthenticated GitHub search.

## License

MIT
