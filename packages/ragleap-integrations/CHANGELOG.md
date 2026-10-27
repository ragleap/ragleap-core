# Changelog

All notable changes to `ragleap-integrations` are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.3.0] - 2026-10-27

### Added

- `McpHub` (`ragleap_integrations.tenancy`): multi-tenant use. One owner-level
  `McpConfig`, one `Connection` per `Scope` (`hub.connect(scope)`), cached
  (`max_scopes`, least recently used dropped; `scopes`, `forget(scope)` and
  `forget(tenant=...)`). Each scope has its own client, protocol-era cache,
  tool snapshot and lock; the scope is closed over by the tool handlers and is
  never a tool argument.
- `token_provider(scope, server)`: called for every request, never cached; a
  `str`, a wrapper with `.reveal()` (such as `ragleap_security.SecretStr`) or
  `None` (no token; the static token is not used then). A failing or unusable
  provider gives the constant text "MCP token is unavailable".
- `allow_for(scope)` (can only narrow the owner's allowlist), `policy_for(scope)`
  (a per-tenant `McpPolicy`), `McpPolicy.approve_scoped(scope, server, tool,
  arguments)` (wins over `approve`; without a scope it denies), `RateLimit(calls,
  per_seconds)` (per tenant, checked before approval and the network; denial text
  "MCP call limit reached") and `audit` (anything with `append(event, data,
  scope)`, such as `ragleap_security.AuditLog`).
- Events from scoped clients carry `tenant` and `scope` (the scope key);
  arguments and results are still never in an event.
- `Scope`, `ScopeError`, `require_scope`, `HAS_SECURITY`. When `ragleap-security`
  is installed (`pip install "ragleap-integrations[security]"`) its `Scope` is
  used and only that exact class is accepted; otherwise a built-in `Scope` with
  the same fields, validation and key format.
- `McpClient` keyword-only `token_for`, `scope`, `guard` and `lock`;
  `connect_config(..., client=...)`.
- `scripts/tenant_live.py`, the two-tenant live check (repository only, not in
  the wheel). Design doc `docs/design/multi-tenant.md`.

### Changed

- Code that does not use `McpHub` behaves as in 0.2.0: the 275 earlier tests
  pass unchanged and a static `McpServerConfig.token` still works.
- The `test` extra now includes `ragleap-security`.

### Verified (0.3.0)

- 331 tests (the 275 earlier ones plus 56 new: scopes, token isolation,
  allowlists, policies, limits, events, audit, threads) on Python 3.10 and 3.12;
  327 passed and 4 skipped without `ragleap-security`. 40 of 40 deliberate
  breaks of the new code caught (one equivalent mutant is not counted:
  removing the empty-allowlist check still raises the same error type from
  `McpConfig`).
- Live-checked on 2026-10-26 with the repository source of the 0.3.0 branch on Python
  3.10.12 against a third party's public mock MCP auth server
  (mcpplaygroundonline.com; its own description is not independently
  verified), using `scripts/tenant_live.py`, read-only: two tokens were minted
  and differ; two tenants each discovered and called `get_user` with their own
  token, and all four recorded requests carried only that tenant's token
  (compared by hash; the tokens were not printed); a made-up token was refused
  with HTTP 401 while the real tenants kept working; after one tenant's token
  was changed that tenant got HTTP 401 and the other was unaffected (so the token
  is resolved per request); `RateLimit(2, 60)` allowed two calls and denied the
  third locally, and exactly two `tools/call` requests reached the server; nine
  events all carried a tenant and none a token. **The limit of this check:** the
  mock server cannot tell tenants apart (both tokens map to the same demo user),
  so separation is shown from the requests that were sent, not from the server
  separating data.

### Not verified (0.3.0)

- `approve_scoped`, `allow_for` and `policy_for` against a real server; a real
  `ragleap_security.AuditLog` fed by a live run; concurrent tenants against a
  real server; a server that really separates tenants' data.
- The published wheel (the live check above ran on the branch source; it is
  repeated with the wheel after release, as for 0.2.0).

### Known limitations

- This package does not decide who the caller is: isolation is only as strong as
  the `Scope` the caller passes and the token the provider returns for it.
- A rate window is kept for every tenant seen until `forget(tenant=...)`; the
  scope cache is bounded, the windows are not. The limit is per process.
- `allow_for` and `policy_for` are called once per scope, when it is connected.
- One `tools/list` request per scope. A scope's requests are serialized by a
  lock held across the network request (up to the total timeout).
- `approve_scoped` may block; the deadlines do not cover it.

### Verified (after 0.2.0)

- Live-checked on 2026-10-25 with the published 0.2.0 wheel on Python 3.10.12
  against Google Cloud's Pub/Sub MCP endpoint (`https://pubsub.googleapis.com/mcp`,
  modern path, `tools/list` only, no credentials, no `tools/call`), using
  `scripts/connect_live.py` (repository only, not in the wheel). All 15 tools
  carry the four boolean hints (plus a `title`, which is dropped).
  `connect()` with the default limits registered 11 tools and skipped the four
  whose input schemas exceed `max_schema_chars` (`create_topic`,
  `update_topic`, `create_subscription`, `update_subscription`), with the
  stated reason. With `trust_annotations=False` all 11 are flagged
  `requires_approval`. With `trust_annotations=True` exactly six are not (the
  `get_` and `list_` tools for topics, subscriptions and snapshots, which
  declare `readOnlyHint: true` and `destructiveHint: false`); `publish`,
  `create_snapshot` and every `delete_` tool stay flagged. In both runs the
  flags and the stored hints equal an independent calculation from the raw
  hints, and each `connect()` produced one `discover` event (11 ready, 4
  skipped, 95 to 111 ms) and no `call` event.

### Not verified (after 0.2.0)

- The `approve` hook, its denial text and the per-call audit events against a
  real server: they need a real `tools/call`, and this server requires
  credentials. `connect(token=...)` against a real authorization server.

## [0.2.0] - 2026-10-24

### Added

- `connect(url, allow=[...])`: one call from a URL to ready tools. Returns a
  `Connection` with `.tools`, `.toolkit` (a `ragleap_tools.Toolkit`, so a model
  call is validated against the server's schema and dispatched without
  raising), `.skipped` and `.report()` (what is ready, what was skipped and
  why, with the setting that controls it). `allow` is an exact list, no
  wildcard. `connect_config(config, specs=None)` does the same for any
  `McpConfig`, including several servers.
- Governance. Discovery keeps only the four boolean MCP tool hints
  (untrusted per the specification) as `McpToolSpec.annotations`. Every tool
  carries `Tool.metadata["mcp"]` with `server`, `tool`, `annotations` and
  `requires_approval`. A tool is flagged unless the owner lists it in
  `McpPolicy.pre_approved`, or sets `McpServerConfig.trust_annotations` and the
  tool declares `readOnlyHint: true` without `destructiveHint: true`.
- `McpPolicy.approve(server, tool, arguments)`: when set, a call to a flagged
  tool runs only if it returns exactly `True`; otherwise the server is not
  contacted and the result is "MCP call was not approved".
- `McpConfig.on_event`: one small event per discovery and per call (server,
  tool, ok, error, approval, duration). Arguments and results are never in
  an event, and a failing hook cannot break a call.
- See `docs/design/mcp-governance.md`.

### Changed

- Requires `ragleap-tools>=0.5.1` (for `Tool.metadata` and `Toolkit`). With no
  policy and no hook, calls behave exactly as in 0.1.1; the only visible
  difference is the new `Tool.metadata`.
- The project links in the package metadata point at the ragleap
  organization repository.

### Verified

- 67 new tests (275 in total) on Python 3.10 and 3.12. Mutation-tested: 53
  deliberate breaks of the new code, all caught.

### Not verified

- `connect()`, the annotation handling and the events against a real MCP
  server (the new tests use fake servers).

### Verified (after 0.1.1)

- Live check on 2026-10-16 of the published 0.1.1 wheel (clean virtual
  environment, Python 3.10.12) against pubsub.googleapis.com/mcp, read-only:
  tools/list only, no credentials, no tools/call, no limit overrides. The
  359,293-byte list of the 15-tool server is accepted with the 1 MiB default
  (0.1.0 refused it at 256 KiB) and the client recorded the modern era.
- Live check on 2026-10-17 against a third party's public test servers
  (mcpplaygroundonline.com mock servers; their own description of each
  server is not independently verified), repository source at commit 912d100
  (0.1.1), Python 3.10.12, `scripts/live_check_mock.py`: a modern-path
  tools/call succeeded (pinned to 2026-07-28 and unpinned); bearer
  authentication worked (no token: HTTP 401 and refused; fresh token: call
  succeeded); against the same server pinned to the legacy revision the
  client fell back to the legacy handshake and a tool call succeeded.

### Observed

- Four of that server's 15 tools have an inputSchema above max_schema_chars
  (10,000): update_subscription 33,502 characters, create_subscription
  32,989, update_topic 23,537 and create_topic 22,835; the other eleven are
  2,631 or smaller. With the first three tools allowlisted, discovery
  registered one and skipped create_topic and update_topic, as designed. As
  an experiment only, max_schema_chars=40000 (an override; the default is
  unchanged) registered all 15 tools.
- A raw legacy initialize sent to the modern-only mock server (not through
  this client) got HTTP 400 and -32022 with the supported versions in
  error.data.supported, which is where the client looks for them and where
  the specification's versioning page puts them.
- A tool-level error (isError) came back as success=False with the server's
  own message in ToolResult.error, capped at max_result_chars like a result;
  nothing raised. None of the four tools on the complex-schema mock carries
  x-mcp-header.

### Not verified

- x-mcp-header mirroring (none of the four tools checked carries the
  annotation), pagination (no server checked paginated), and the client's
  own handling of a live -32022 reply (it never received one; only the shape
  was observed with a raw request).
- That schemas of this size are usable by a model.

## [0.1.1] - 2026-10-15

### Changed

- The default `max_response_bytes` is now 1 MiB (it was 256 KiB). A real 15-tool
  server (Google Cloud Pub/Sub's MCP endpoint) answered `tools/list` with 359,293
  bytes, which the old default refused, so discovery failed out of the box.
- The "response too large" error now names the setting ("response too large;
  raise max_response_bytes"). The text stays constant: no server text in errors.

### Verified

- 208 tests (the existing 201 unchanged plus 7 new: the default value, a reply
  the size of the real Pub/Sub list, exactly 1 MiB passes and 1 MiB plus one
  byte is refused, an explicit smaller cap still applies, and the message in
  discovery and tool-call errors), passing on Python 3.10.12 and 3.12.3.
  Mutation-tested: 8 deliberate breaks of this change, all caught.

### Verified (after 0.1.0)

- Live check of the MODERN 2026-07-28 path on 2026-10-12, using the published
  0.1.0 wheel in a clean virtual environment (Python 3.10.12) against two
  public Google Cloud MCP endpoints (developerknowledge.googleapis.com/mcp and
  pubsub.googleapis.com/mcp). Read-only: tools/list only, no credentials, no
  tools/call. The server accepted the modern request (MCP-Protocol-Version,
  Mcp-Method and per-request _meta), the client recorded the modern era, the
  reply was plain application/json (non-stream), and the client discovered 3
  tools with their input schemas from one server.

### Observed

- A 15-tool server sent a 359,293-byte tools/list reply, above the default
  max_response_bytes (256 KiB): discovery failed with "response too large";
  with the limit raised to 8 MiB it succeeded on the modern path, and two tools
  were skipped with a stated reason (inputSchema larger than max_schema_chars,
  10,000 characters), as designed.
- Both servers answered an unsupported protocol version with HTTP 400 and
  JSON-RPC -32600 "Unsupported protocol version" and no supported-version list,
  and neither implements server/discover (-32601). This client never calls
  server/discover. A 400 whose JSON-RPC code is outside the recognized modern
  set is still read as a legacy server (behaviour unchanged).

### Not verified

- Modern-path tools/call, x-mcp-header mirroring, bearer-token authentication,
  pagination (neither server paginated), and where a -32022 error carries its
  supported-version list (not observed; the live servers returned -32600).

## [0.1.0] - 2026-10-06

Correction: the copy of this file inside the published 0.1.0 wheel and sdist says 2026-10-05. The files reached PyPI on 2026-10-06 (UTC), which is the real release date. PyPI files cannot be edited, so this entry is the correction. The live check described below really was on 2026-10-05.

### Added

- An MCP client over Streamable HTTP that returns `ragleap_tools.Tool`
  objects (`McpConfig`, `McpServerConfig`, `McpToolSpec`, `McpClient`,
  `make_mcp_tools`). Owner-configured servers and an exact `server.tool`
  allowlist; nothing is read from the environment; tools are discovered once
  (a snapshot) or supplied by the owner as specs, in which case nothing is
  sent over the network at setup.
- Modern (2026-07-28) requests with `Mcp-Method`, `Mcp-Name` and
  `Mcp-Param-*` headers (Base64 sentinel encoding where required) and
  `params._meta`; detection of legacy servers per the specification's
  backward-compatibility rules, with a legacy `initialize` fallback for
  2025-06-18 only.
- A bounded HTTPS transport using only the standard library: public-address
  check with the connection pinned to the resolved IP, TLS verified against the
  hostname, no redirects, a response-size cap, constant error messages, and a
  wall-clock deadline enforced by a watchdog (see
  `docs/design/mcp-client.md` for the slow-drip experiment behind it).

### Fixed

- Found by the live check, before release: a legacy server that rejects the
  modern request with its own JSON-RPC error (DeepWiki answered HTTP 400 and
  `-32600`) was reported as an error instead of triggering the legacy
  fallback. Only `-32020`, `-32021` and `-32022` are treated as recognized
  modern errors now, and a failed fallback still reports the first reply's
  code.

### Verified

- 201 tests, including the real-TLS transport tests described in the README.
- Live-checked on 2026-10-05 against DeepWiki's public MCP server (legacy path;
  see the README's Verification status for exactly what that covers).

### Not verified

- The modern 2026-07-28 path against a real server, `x-mcp-header`
  mirroring, the location of a `-32022` error's supported-version list,
  bearer-token authentication, plain JSON responses and pagination.

### Known limitations

- On a legacy server every call performs a full `initialize` handshake and
  never closes the session. `HeaderMismatch` is not retried. Only text
  content is returned. Server-supplied text (descriptions, schemas, results)
  is untrusted and not screened for prompt injection.
