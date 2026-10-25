# MCP tool governance, audit events and connect() (ragleap-integrations v0.2.0)

## Problem

`ragleap-integrations` 0.1.x already limits what a model can do with a remote
MCP server: the owner names the exact tools, the transport is bounded, and
errors are constant text. Three things were missing for real deployments.

1. **No way to say "this tool needs a human".** Servers describe tools with
   annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
   `openWorldHint`), but the client ignored them and passed nothing on, so an
   agent loop with an approval gate had no signal to act on.
2. **No audit trail.** There was no way to see which tool was called on which
   server, how long it took and whether it succeeded, without wrapping every
   handler by hand.
3. **Too much setup.** Connecting took a config object, a server object, an
   allowlist and a separate call to build tools.

## Design

Standard library only. New dependency floor: `ragleap-tools>=0.5.1` (for
`Tool.metadata` and `Toolkit`). Nothing in the 0.1.1 behavior changes unless the
owner opts in.

### Annotations are untrusted hints

The MCP specification (2026-07-28, server/tools) says a client must treat tool
annotations as untrusted unless they come from a trusted server. So:

- Discovery keeps only the four boolean hints, and only as booleans. A title,
  free text, an unknown key or a non-boolean value is dropped.
- Every tool is flagged `requires_approval` in `Tool.metadata["mcp"]` unless
  the owner relaxes it: by listing `"server.tool"` in `McpPolicy.pre_approved`,
  or by setting `McpServerConfig.trust_annotations` for that server (then a tool
  declaring `readOnlyHint: true` is not flagged).
- A server claiming a tool is both read-only and destructive is treated as
  contradictory and stays flagged. `destructiveHint` never relaxes anything.
- `Tool.metadata["mcp"]` holds `server`, `tool`, `annotations` (the sanitized
  hints) and `requires_approval`.

### The flag is advice; `approve` enforces it

By default nothing is blocked: the flag is information for a caller such as an
agent loop with its own approval gate. If the owner supplies
`McpPolicy.approve(server, tool, arguments)`, a call to a flagged tool runs only
if that callable returns exactly `True`. An exception, `None`, `1`, `"yes"` or
any other value denies the call, the server is not contacted, and the result is
the constant text "MCP call was not approved". The callable receives a copy of
the arguments, and it runs after the arguments are validated. It may block (for
example on a person); the network deadlines do not cover it. A direct
`client.call_tool(...)` with no flag is treated as flagged.

### Audit events

`McpConfig.on_event` receives one small dict per discovery and per call:

- `{"event": "discover", "ok", "tools", "skipped", "duration_ms"}` (and `"error"`
  on failure);
- `{"event": "call", "server", "tool", "ok", "error", "approval", "duration_ms"}`
  where `approval` is `not_required`, `not_enforced`, `approved`, `denied` or
  `unchecked` (the call failed before the check).

Arguments and results never appear in events. When a tool itself reports an
error the event says "tool reported an error", never the tool's text. A hook
that raises is logged and ignored: telemetry cannot break a call. The shape is
easy to forward to a logger or a tracing library; this package adds no
dependency for that.

### connect()

`connect(url, allow=[...], name="mcp", token=None, trust_annotations=False,
approve=None, pre_approved=(), on_event=None, **limits)` builds the config,
discovers once, and returns a `Connection` with `.tools`, `.toolkit` (a
`ragleap_tools.Toolkit`, so a model call is validated against the server's schema
and dispatched without raising), `.skipped` and `.report()`. `allow` is an exact
list (no wildcard). `connect_config(config, specs=None)` does the same for any
`McpConfig`, including several servers.

## Rejected alternatives

- **Trusting `readOnlyHint` by default.** A hostile or sloppy server could mark a
  destructive tool read-only. Trust is the owner's explicit choice, per server.
- **Enforcing approval by default.** It would change behavior for every 0.1.x
  user and duplicates the approval gate that `ragleap-agents` already has.
- **Wildcard allowlists** (`allow="*"`). They break the owner-names-every-tool
  rule that the whole trust model rests on.
- **Changing the skip-reason texts to name settings.** The texts are asserted by
  existing tests and users; `Connection.report()` adds the hint instead.
- **An OpenTelemetry dependency.** One callback is enough to forward events.

## Security notes

- Hints and every other server-supplied string stay untrusted. The only server
  data that reaches `Tool.metadata` is four booleans.
- A denied call never reaches the network. The event and the result carry
  constant text, never an exception message.
- The approve and event callables are the owner's code and run in the caller's
  thread.

## Limitations

- No OAuth, token provider, retry, circuit breaker or caching yet; they are the
  next releases. `token` is still a static string.
- `approve` is synchronous. Whether an agent loop honors `requires_approval` is
  that loop's behavior.
- Events cover calls and discovery made through this client only.

## Verification status

- Verified: 67 new tests (275 in total) pass on Python 3.10 and 3.12, against
  fake servers and the existing local TLS transport tests; 53 deliberate breaks of
  the new code, all caught by the new tests.
- Verified live (2026-10-25, published 0.2.0 wheel, Pub/Sub MCP endpoint,
  `tools/list` only, no credentials): `connect()` registers 11 tools and skips
  the four with oversized schemas; all 15 tools carry the four boolean hints;
  with `trust_annotations=False` all 11 are flagged, with `True` exactly the
  six `get_` and `list_` tools (read-only, not destructive) are not, equal to
  an independent calculation from the raw hints; one `discover` event per
  `connect()` and no `call` event (`scripts/connect_live.py`).
- Not verified: the `approve` hook, its denial text and the per-call events
  against a real server (they need a real `tools/call`, and that server
  requires credentials).
