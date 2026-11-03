# UTCP: governed UTCP manuals as ragleap-tools Tools, and Code Mode (design note)

Status: first slice implemented in ragleap-integrations 0.4.0 (connect_utcp); see
"Implemented in 0.4.0" below. Written 2026-10-31 (task J-7).
Every statement about UTCP or Code Mode below comes from the sources listed
under Verification status; none of it was tested by us.

## Problem

UTCP (the Universal Tool Calling Protocol) lets a provider publish a JSON
"manual" that lists its tools and, for each tool, a call template that says how
to call it directly over its own protocol (no wrapper server in between). Teams
that already publish such manuals will want their agents to use those tools
with the same rules we apply to MCP tools: the owner decides which tools exist,
each tenant's calls carry that tenant's credentials, calls can be approved,
limited and audited.

Code Mode is a related idea: instead of one JSON tool call at a time, the model
writes a block of code that calls many tools and returns only a summary. It can
cut round trips, but it runs model-written code, and our deferred-capabilities
list keeps code execution out of the libraries until it has its own security
design.

## Design (proposal)

### Placement

- A. UTCP client: a new optional module in `ragleap-integrations`. That package
  already connects to external tool sources, returns `ragleap-tools` `Tool`
  objects and has the bounded transport, the exact allowlist, `McpPolicy`,
  `RateLimit`, `Scope` and the audit events.
- B. UTCP export: later, optionally, in `ragleap-tools`: turn a `Toolkit` into a
  UTCP manual, next to the OpenAI, Gemini and Anthropic schema exporters. No
  dependencies. Only useful to owners who have an HTTPS endpoint to point at.
- C. Code Mode: neither package. A separate package after its own design note
  (see below).

### First slice of A

- Owner configuration, in code: the manual URL, an exact list of tool names, a
  list of approved origins (default: the manual's own origin only), and a token
  or a `token_provider(scope, server)` as in `McpHub`.
- Only the `http` call template is accepted, and only for tools whose own
  template is `http`. Every other template type (cli, file, text, websocket,
  grpc, graphql, sse, direct-call, mcp inside a manual) is skipped with a stated
  reason. MCP servers are reached through the existing `McpClient`, not through
  a manual.
- Origin pinning: the final URL of a tool call must be https and its origin
  must be one of the approved origins. Checked at discovery and again at every
  call. A tool that points elsewhere is skipped with a stated reason. UTCP's
  own protocol allow-list limits the transport type, not the host; this is
  stricter on purpose.
- Snapshot once: the manual is read at setup and never re-read during a call,
  so a manual that changes later has no effect until the owner reconnects.
- The model supplies only the JSON arguments that match the tool's input
  schema. It never supplies a URL, header, token or tool outside the allowlist.
- Governance as for MCP: every tool is flagged `requires_approval` unless the
  owner pre-approves it (a UTCP manual carries no read-only hints we trust), an
  optional approve hook, an `on_event` audit hook that never carries arguments
  or results, constant error text with no server text in it, response size cap
  and a wall-clock deadline, all through the existing transport.
- Multi-tenant: the same pattern as `McpHub`: one connection per `Scope`,
  tokens resolved per request and never cached, `allow_for` can only narrow,
  per-tenant policy and `RateLimit`, events carry the tenant. Open decision: one
  generalized hub for MCP and UTCP (stage B) or a separate hub class.
- Auth in the first slice: a bearer token in a header only. UTCP defines other
  auth types; they are not supported until each has been read and tested.

### Code Mode (C)

Not in `ragleap-tools` or `ragleap-integrations`. Before any build it needs its
own design note and a security review. Requirements I would set:

- every tool call made by the model's code passes through the same governed
  `Toolkit` (allowlist, approval, rate limit, audit with the scope), so code
  cannot do anything the individual tool call could not;
- isolation strong enough for untrusted code, proven by escape tests and
  mutation checks before any safety statement is made;
- no network, no filesystem, no environment access of its own; time and memory
  limits enforced from outside the process.

Options to evaluate, in this order: (1) a declarative tool-chain plan, which is
data and not code (a sequence of calls, references to earlier results, a few
safe transformations), validated against schemas; smaller attack surface, but
how much of the batching benefit it keeps is not measured; (2) our own
restricted runner in a separate process with operating-system limits; (3) the
UTCP `code-mode` package as an optional dependency.

## Implemented in 0.4.0

The first slice of A is in `ragleap_integrations.utcp` (`connect_utcp`). Where
it differs from the proposal above, or settles a point the proposal left open:

- The `http` call template fields were read in the project's documentation:
  `http_method`, `url`, `content_type`, `auth`, `auth_tools`, `headers`,
  `body_field` and `header_fields`; path parameters are `{name}` in the URL, the
  `body_field` argument is the body and the remaining arguments are query
  parameters. A template may carry only those fields plus `name`; any other
  field (for example `query_params`) makes the tool skipped, because ignoring
  it would change what a call means. `auth`, `auth_tools` and `header_fields`
  are accepted only when empty; with a value, the tool is skipped.
- No `${NAME}` or `$NAME` placeholder is substituted anywhere and nothing is
  read from the environment; a tool with a placeholder is skipped. This closes
  the one risk the documentation leaves open (a manual naming a local
  variable so that its value is sent to the provider).
- OpenAPI documents are refused (the Python reference client converts them
  automatically; this package does not).
- The transport gained a `method` parameter (`https_request`); `https_post`
  is unchanged for callers.
- Declared auth is not used. The owner's token is sent as a bearer token to
  approved origins. Tools that declare their own auth are skipped until each
  auth type has been read and tested.
- Still open: a multi-tenant `UtcpHub` (or one hub for MCP and UTCP), and
  everything in item C.

## Rejected alternatives

- Depend on the `utcp` Python packages for the client. It would add
  dependencies to a package whose rule is a single dependency, and it would
  bring transports we deliberately defer.
- Support all transports UTCP lists. Each needs its own security pass. The
  first slice is `http` only.
- Re-read the manual at call time. That is exactly the weakness described for
  CVE-2025-14542 (below).
- Trust the URLs a manual declares. A manual served from one host could point
  calls at another; origin pinning removes that.
- Adopt the Python `code-mode` engine for multi-tenant use. Its own README says
  it is for cooperative model-generated code and not for production
  multi-tenant environments or untrusted code.

## Security notes

- CVE-2025-14542, as described by one vulnerability database (a secondary
  source; the advisory itself was not read): a remote manual that began benign
  was changed by its provider to add tools with dangerous protocols such as
  `cli`, and the client registered and ran them. The fix was an
  `allowed_communication_protocols` field enforced at registration and at every
  call, defaulting to the manual's own protocol. Our design avoids this class
  by accepting `http` only, by the one-time snapshot and by the owner's exact
  allowlist.
- Server-supplied text (descriptions, schemas, results) is untrusted and is not
  screened for prompt injection; the structural limits are the allowlist, the
  snapshot, length caps and optional owner-supplied specs, as for MCP.
- Tokens are sent only to approved origins and never cross tenants.
- The existing network rules apply: https only, no credentials in the URL, no
  IP-literal hosts, every resolved address public, TLS 1.2 or newer verified
  against the hostname, the connection pinned to the resolved address, no
  redirects.

## Limitations

- The proposal above was written before any code; the first slice is now built and tested (see
  "Implemented in 0.4.0") but not live-verified against a real UTCP server.
- The first slice would cover `http` manuals only and a bearer token only.
- Origin pinning will reject manuals whose tools legitimately live on several
  hosts unless the owner lists them.
- "Enterprise grade" is a goal. It is not claimed for anything in this note.

## Verification status

- 0.4.0: 545 tests including the new client, no live check against a real UTCP server.
  The HTTP call template fields were additionally read in the project's HTTP protocol
  documentation and the Python client's API reference on 2026-10-31.
- Read on 2026-10-31: the UTCP implementation guide (utcp.io), the UTCP
  specification repository's documentation PR, the README and pull requests of
  the Python, TypeScript and Ruby UTCP clients (as shown by search results and
  fetched pages), the README of the `code-mode` repository and the PyPI page of
  `code-mode` 1.1.0 (Beta, MPL-2.0, released 2026-05-03, uploaded without
  provenance), and a vulnerability-database page for CVE-2025-14542.
- Not verified: the normative text of the UTCP specification; UTCP's auth
  types; whether the reference clients restrict hosts; differences between
  manual versions 1.0 and 1.1; the exact field mapping of the `http` call
  template (path, query, body, headers); the behavior of any real UTCP server.
- The benchmark figures published by the Code Mode project and a third-party
  study were not reproduced and are not repeated here.

## Open decisions

1. Placement of A in `ragleap-integrations` (recommended) and of B in
   `ragleap-tools` (later).
2. One generalized hub for MCP and UTCP, or a separate class.
3. For C: evaluate the declarative plan first, or a sandboxed runner.
4. Who reviews security-relevant parts besides the author.
5. TypeScript and Java ports follow the Python version, not the other way round.
