# ragleap-integrations

An MCP (Model Context Protocol) client that turns allowlisted tools on remote
MCP servers into `ragleap_tools.Tool` objects, so MCP tools and native tools
share one shape.

```bash
pip install ragleap-integrations
```

## What this is (and isn't)

v0.3.0 is an MCP client over **Streamable HTTP only**. The *owner* configures
servers and an exact `server.tool` allowlist; the model only supplies a tool's
JSON arguments, never a URL, a token or a tool outside the allowlist. It does
not own a tool-calling loop (that is `ragleap-agents`' job).

Not supported in this version: stdio (it launches a subprocess), the
deprecated HTTP+SSE transport, sampling/elicitation/roots (a server asking
for client input gets a clear "unsupported" result), resources, prompts,
subscriptions and OAuth. Connectors, code execution and HTTP fetch are
deliberately not here: each needs its own security design pass.

## Quickstart

```python
from ragleap_integrations import McpConfig, McpServerConfig, make_mcp_tools

config = McpConfig(
    servers=[McpServerConfig("deepwiki", "https://mcp.deepwiki.com/mcp")],  # token="..." if a server needs one
    allowed_tools=["deepwiki.read_wiki_structure"],
)
tools = make_mcp_tools(config)  # discovers the allowlisted tools once (tools/list)

openai_tools = [t.to_openai_schema() for t in tools]  # or t.to_gemini_schema()
result = tools[0].call(repoName="sqlite/sqlite")
print(result.success, result.result)  # True {"text": "..."}
```

Nothing is read from the environment: your application builds the config.
Tools are exposed as `<server>__<tool>` (characters outside letters, digits,
`_` and `-` become `_`, at most 64 characters).

To keep server-controlled text out of the model's context entirely, give the
descriptions and schemas yourself; nothing is sent over the network at setup:

```python
from ragleap_integrations import McpToolSpec

spec = McpToolSpec(
    server="deepwiki", name="read_wiki_structure",
    description="List the documentation topics of a GitHub repository.",
    parameters={"type": "object", "properties": {"repoName": {"type": "string"}}, "required": ["repoName"]},
)
tools = make_mcp_tools(config, specs=[spec])
```

## Easy mode: connect()

```python
from ragleap_integrations import connect

conn = connect("https://mcp.example.com/mcp", allow=["list_orders", "get_order"], token=MY_TOKEN)
print(conn.report())   # which tools are ready, which were skipped and why (with the setting to change)

provider_tools = conn.toolkit.to_openai_schema()    # also to_gemini_schema() / to_anthropic_schema()
result = conn.toolkit.call(name_from_model, arguments_from_model)   # never raises
```

`allow` is an exact list of tool names (no wildcard). `name` (default `"mcp"`)
prefixes the exposed names (`mcp__get_order`). The toolkit checks a model's
arguments against the server's schema before anything is sent. For several
servers, build an `McpConfig` and call `connect_config(config)`.

## Governance: approval flags and audit events

A server's tool annotations (`readOnlyHint`, `destructiveHint`, ...) are
untrusted hints, so they can only make policy stricter. Every tool carries
`tool.metadata["mcp"]` with `requires_approval`, true unless you list the tool
in `pre_approved`, or trust a server's `readOnlyHint` for that server.

```python
def approve(server, tool, arguments):
    return ask_a_person(f"Run {server}.{tool} with {arguments}?")   # must return exactly True

conn = connect(URL, allow=["list_orders", "delete_order"], approve=approve,
               pre_approved=["list_orders"], on_event=print)
```

Without `approve` the flag is advice for your own approval gate (for example in
`ragleap-agents`). With it, a flagged call runs only if `approve` returns
`True`; an exception or any other value denies the call, the server is not
contacted, and the result is "MCP call was not approved". `on_event` receives
one small dict per discovery and per call (server, tool, ok, error, approval,
duration); arguments and results never appear in it. See
`docs/design/mcp-governance.md`.

## Multi-tenant use: McpHub

One owner-level `McpConfig`, one `Connection` per tenant. The tenant is a
`Scope`: with `pip install "ragleap-integrations[security]"` it is
`ragleap-security`'s `Scope`; without it a small built-in `Scope` with the same
fields and key format. The model never sees or sets the scope.

```python
from ragleap_integrations import McpConfig, McpHub, McpServerConfig, RateLimit, Scope

hub = McpHub(
    McpConfig(servers=[McpServerConfig("crm", "https://mcp.example.com/mcp")],
              allowed_tools=["crm.get_order", "crm.update_order"]),
    token_provider=lambda scope, server: vault.token_for(scope.tenant, server),
    allow_for=lambda scope: ["crm.get_order"] if scope.tenant == "trial" else None,
    limit=RateLimit(60, 60),   # 60 calls a minute per tenant
    audit=audit_log,           # anything with append(event, data, scope)
)
conn = hub.connect(Scope(tenant="acme", subject="u1"))
conn.toolkit.call(name_from_model, arguments_from_model)  # same Connection as connect()
```

- Each scope has its own client, tool snapshot and token; nothing mutable is
  shared between scopes.
- `token_provider(scope, server)` is called for every request and nothing is
  cached by this package. `None` sends no token (the static token is not used
  then); a failing provider gives the constant text "MCP token is unavailable".
- `allow_for(scope)` can only narrow the owner's allowlist. `policy_for(scope)`
  picks a per-tenant `McpPolicy`; its `approve_scoped(scope, server, tool,
  arguments)` wins over `approve`, and without a scope it denies.
- `RateLimit` is per tenant (all of a tenant's scopes share it) and is checked
  before approval and before any network call.
- Events carry `tenant` and `scope`; arguments and results are never in them.
  `audit` receives every event with the scope.
- `hub.connect()` needs an exact `Scope` (a string, a dict, `None` or a
  subclass raises `ScopeError`). This package does not decide who the caller is.

Design and limits: `docs/design/multi-tenant.md`.

## Trust model

- Everything a server says is untrusted text: tool descriptions, input
  schemas and results flow into a model's context. This package does **not**
  screen them for prompt injection. The structural limits are the allowlist,
  a snapshot of the tool list taken once at setup (a server that changes a
  description later has no effect), length caps, and optional owner-supplied
  specs.
- Server-supplied error text is never put in a result; failures carry a
  constant message plus, at most, an HTTP status or a JSON-RPC error code.
- Network access is bounded in code: https only, no credentials in the URL,
  no IP-literal hosts, DNS resolved once with every address required to be
  public (so DNS rebinding cannot swap in an internal address), the
  connection pinned to that IP with TLS (minimum 1.2) verified against the
  hostname, no redirects, no compressed responses, a response-size cap, and a
  wall-clock deadline that also covers the TLS handshake and the headers.
- A bearer token, if configured, is sent only to its own server URL and is
  never logged.

## Protocol

The current revision (2026-07-28) is tried first: every request is its own
POST carrying `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` headers and
the version, client info and capabilities in `params._meta`; parameters marked
`x-mcp-header` in a tool's schema are mirrored into `Mcp-Param-*` headers.
A 4xx without a recognized modern error (`-32020`, `-32021`, `-32022`)
identifies a legacy server, and the client then uses the legacy `initialize`
handshake with protocol version 2025-06-18 only (an explicit allowlist, not
"whatever the server answers"). The era found is remembered per server.

## Limits and defaults

| Setting | Default |
|---|---|
| Whole-call deadline (`total_timeout`) | 30 s |
| Per-operation socket timeout (`op_timeout`) | 10 s |
| Response size (`max_response_bytes`) | 1 MiB (256 KiB in 0.1.0) |
| Result text (`max_result_chars`) | 4000 characters |
| Tool description (`max_description_chars`) | 500 characters |
| Input schema (`max_schema_chars`) | 10000 characters (larger: tool skipped) |

## Known limitations

- This package does not decide who the caller is: tenant isolation is only as
  strong as the `Scope` you pass and the token your `token_provider` returns.
- Rate windows are kept per tenant until `hub.forget(tenant=...)`, the limit is
  per process, and `allow_for` and `policy_for` are read once per scope.
- On a legacy server every tool call performs a full `initialize` handshake
  (three POSTs) and never closes the session.
- A `HeaderMismatch` error is reported, not retried after re-reading the
  tool list.
- Only text content is returned; other content types are counted and omitted.
- `ragleap_tools.Tool.call` in ragleap-tools 0.4.0 cannot take an argument
  literally named `self`.
- Not safe to share one client between threads while calls are running.
- Server-supplied text is untrusted and not screened for prompt injection:
  tool descriptions and schemas, tool results, and the text of a tool-level
  error (it becomes `ToolResult.error`, capped at `max_result_chars` like a
  result).
- A server whose tool list is larger than the response cap (1 MiB by default;
  256 KiB in 0.1.0, which a real 15-tool server's 359 KB list exceeded) makes
  discovery fail with "response too large; raise max_response_bytes" until
  the limit is raised.

## Verification status

- 331 tests (the 275 earlier ones unchanged plus 56 new in 0.3.0: scope and
  token isolation, allowlists, policies, limits, events, audit, threads; 327
  passed and 4 skipped without `ragleap-security`); 40 of 40 deliberate breaks
  of the new code caught.
- **Live-checked on 2026-10-26** with the repository source of the 0.3.0 branch on
  Python 3.10.12 against a third party's public mock MCP auth server, using
  `scripts/tenant_live.py` (repository only, not in the wheel): two real tokens
  were minted; each tenant's requests carried only its own token (compared by
  hash); a made-up token got HTTP 401 and the real tenants kept working; after
  one tenant's token was changed only that tenant was refused (the token is
  resolved per request); `RateLimit(2, 60)` denied the third call locally and
  only two `tools/call` requests reached the server; no event carried a token.
  The mock server cannot tell tenants apart, so separation is shown from the
  requests that were sent.
- 275 tests (67 of them new in 0.2.0: governance, events and `connect()`;
  53 of 53 deliberate breaks of the new code caught), including a real local
  TLS server with a throwaway certificate
  for the transport (hostname verification, IP pinning, size cap, truncated
  bodies, and the deadline against slow-drip bodies, slow-drip headers and a
  stalled handshake).
- **Live-checked on 2026-10-05** against DeepWiki's public MCP server
  (`https://mcp.deepwiki.com/mcp`, no authentication): the server answered
  the modern request with HTTP 400 and a JSON-RPC `-32600` error, the client
  fell back to the legacy handshake, negotiated 2025-06-18, listed tools and
  read a repository's documentation structure. This verifies the legacy path,
  the fallback decision, event-stream responses and the pinned TLS transport
  against a real server.
- **Live-checked on 2026-10-12** against two public Google Cloud MCP
  endpoints (`https://developerknowledge.googleapis.com/mcp` and
  `https://pubsub.googleapis.com/mcp`) on the modern 2026-07-28 path, using
  the published 0.1.0 wheel in a clean virtual environment (Python 3.10.12).
  Read-only: `tools/list` only, no credentials, no `tools/call`. The server
  accepted the modern request, replied with plain `application/json`
  (non-stream), and the client discovered the tools and read their input
  schemas (3 tools from one server). A second server's tool list was 359,293
  bytes, above the default `max_response_bytes` (256 KiB): discovery failed
  with "response too large" until the limit was raised, and two of its tools
  were skipped because their input schemas exceed `max_schema_chars`. Both
  servers answered an unsupported protocol version with HTTP 400 and
  `-32600` and no supported-version list; neither implements `server/discover`
  (this client never calls it).
- **Live-checked on 2026-10-16** against `https://pubsub.googleapis.com/mcp`
  using the published 0.1.1 wheel in a clean virtual environment (Python
  3.10.12), with no limit overrides. Read-only: `tools/list` only, no
  credentials, no `tools/call`. The 359,293-byte list of the 15-tool server,
  which 0.1.0 refused at its 256 KiB default, is now accepted with the
  default `max_response_bytes` (1 MiB), and the client recorded the modern
  era. Four of the 15 tools have an input schema above the default
  `max_schema_chars` (10,000): `update_subscription` (33,502 characters),
  `create_subscription` (32,989), `update_topic` (23,537) and `create_topic`
  (22,835); the other eleven are 2,631 or smaller. With the first three tools
  allowlisted, discovery registered one and skipped `create_topic` and
  `update_topic`, as designed. As an experiment only, `max_schema_chars=40000`
  (an override; the default is unchanged) registered all 15 tools. Not
  verified: that schemas of this size are usable by a model.
- **Live-checked on 2026-10-17** against a third party's public test servers
  (`https://mcpplaygroundonline.com/mock-mcp-servers`; their own description
  of what each server does is not independently verified), using the
  repository source at commit 912d100 (0.1.1) on Python 3.10.12 and
  `scripts/live_check_mock.py` (repository only, not in the wheel): (a) a
  modern-path `tools/call` succeeded against the stateless server, both
  pinned with `?rev=2026-07-28` and unpinned, and the tool itself reported
  the modern era and revision 2026-07-28; (b) bearer-token authentication: a
  call without a token was refused (HTTP 401) and the same call with a
  freshly minted token succeeded; (c) against the same server pinned to the
  legacy revision (`?rev=2025-11-25`) the client fell back to the legacy
  handshake and a tool call succeeded, the tool reporting the legacy era
  (the reply that triggered the fallback was not recorded); (d) a tool that
  reports an error returned `success=False` with the server's message in
  `error` and nothing raised.
- **Live-checked on 2026-10-25** with the published 0.2.0 wheel on Python
  3.10.12 against Google Cloud's Pub/Sub MCP endpoint
  (`https://pubsub.googleapis.com/mcp`, modern path, `tools/list` only, no
  credentials, no `tools/call`), using `scripts/connect_live.py` (repository
  only, not in the wheel): `connect()` with the default limits registered 11
  tools and skipped the four whose input schemas exceed `max_schema_chars`.
  All 15 tools carry the four boolean hints. With `trust_annotations=False`
  all 11 are flagged `requires_approval`; with `trust_annotations=True`
  exactly six are not (the `get_` and `list_` tools for topics,
  subscriptions and snapshots, which declare `readOnlyHint: true` and
  `destructiveHint: false`). In both runs the flags equal an independent
  calculation from the raw hints, and each `connect()` produced one
  `discover` event and no `call` event.
- **Not live-verified:** `x-mcp-header` mirroring (none of the four tools on
  the complex-schema server carries the annotation), pagination (none of the
  servers checked paginated) and this client's own handling of a `-32022`
  reply (it never received one live; a raw legacy `initialize` sent to the
  modern-only server got HTTP 400 and `-32022` with the supported versions
  in `error.data.supported`, which is where the client looks). They follow
  the public specification and are covered by tests against fakes only;
  treat them as best-effort until confirmed live.
- **Not live-verified (new in 0.2.0):** the `approve` hook, its denial text
  and the per-call audit events against a real server (they need a real
  `tools/call`, and the Pub/Sub server requires credentials), and
  `connect(token=...)` against a real authorization server.
- **Not live-verified (new in 0.3.0):** `approve_scoped`, `allow_for` and
  `policy_for` against a real server, a real `ragleap-security` `AuditLog` fed
  by a live run, concurrent tenants against a real server, a server that really
  separates tenants' data, and the published wheel (the live check ran on the
  branch source and is repeated with the wheel after release).

## License

MIT
