# Changelog

All notable changes to `ragleap-integrations` are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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

### Not verified

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
