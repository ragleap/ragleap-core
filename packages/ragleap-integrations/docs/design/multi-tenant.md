# Multi-tenant use of the MCP client (0.3.0)

## Problem

Until 0.2.0 one `McpConfig` held one set of servers and tokens for everybody.
A product that serves several customers needs each customer's calls to carry
that customer's credentials, see only that customer's tools, follow that
customer's approval rule and quota, and leave an audit trail that says whose
call it was, without a way for one customer (or a model acting for one) to
reach another's.

## Design

`McpHub` holds one owner-level `McpConfig` and returns a `Connection` per
`Scope` (`hub.connect(scope)`). A `Connection` is the same type `connect()`
returns, so tools and the `ragleap-tools` `Toolkit` work unchanged.

1. **The scope is closed over, never an argument.** Each scope gets its own
   `McpClient` (its own protocol-era cache, tool snapshot and lock). Tool
   handlers are bound to that client, so a model cannot name another tenant,
   and `ragleap-tools` needs no change.
2. **Tokens are resolved per request.** `token_provider(scope, server)`
   returns a `str`, a wrapper with `.reveal()` (such as
   `ragleap_security.SecretStr`) or `None`. Nothing is cached by this package.
   `None` means no token: the static `McpServerConfig.token` is not used then,
   so one tenant can never fall back to a shared credential. A provider that
   raises, or returns an empty value, a non-string or a string with control
   characters, gives the constant text "MCP token is unavailable"; the exception
   text and the token are never kept (only the exception class name is logged).
3. **A tenant can only narrow.** `allow_for(scope)` is intersected with the
   owner's allowlist; entries outside it are ignored. An empty result is a
   configuration error. `policy_for(scope)` picks a per-tenant `McpPolicy`;
   `pre_approved` entries the tenant does not have are dropped.
4. **`approve_scoped(scope, server, tool, arguments)`** is a new field of
   `McpPolicy`. It wins over `approve`, and without a scope it denies (fail
   closed). The existing `approve` signature is unchanged.
5. **`RateLimit(calls, per_seconds)` is per tenant.** All scopes of a tenant
   share one sliding window. It is checked after the allowlist and argument
   checks (invalid calls cost nothing) and before approval and any network call.
6. **Events carry the scope.** Scoped clients add `tenant` and `scope` (the
   scope key) to every event; arguments and results are still never in an event.
   An `audit` object (anything with `append(event, data, scope)`, such as
   `ragleap_security.AuditLog`) receives every event with the scope. A failing
   hook or audit never breaks a call.
7. **A scope is required and exact.** `hub.connect(None)`, a string, a dict or a
   subclass raises `ScopeError`, as `ragleap-security` does: a forged object
   cannot claim to be someone else.
8. **Threads.** The hub keeps a bounded, locked cache (`max_scopes`, least
   recently used dropped) and builds each scope once; the requests of one scope
   are serialized by a lock held per request. Scopes do not share a lock.

`ragleap-security` is an optional extra (`pip install
"ragleap-integrations[security]"`). When installed its `Scope` is the one in
use; otherwise a small built-in `Scope` with the same fields, validation and
key format.

## Rejected alternatives

- **A scope argument on `Toolkit.call`.** It would need a `ragleap-tools`
  change and puts the tenant on a path a model's arguments travel.
- **Our own `Scope` type next to `ragleap-security`'s.** Two tenant models in one
  repository.
- **A hard dependency on `ragleap-security`.** Every user would get an alpha
  package (its own README says it is not independently audited), and this
  package keeps its "needs only `ragleap-tools`" promise.
- **One shared client with a per-call token.** The protocol-era cache and the
  tool snapshot would be shared state between tenants.
- **Caching tokens in the hub.** Expiry and revocation belong to the provider.

## Security notes

- Isolation is only as strong as what the owner passes in. This package does
  not decide who the caller is; the caller must pass the right `Scope`, and the
  provider must return the right token for it.
- The per-scope lock is held across the network request, up to the total
  timeout, so a slow server slows that scope's other calls.
- `approve_scoped` may block (for example on a person); the deadlines do not
  cover it. It runs outside the lock.

## Limitations

- A rate window is kept for every tenant seen until `forget(tenant=...)`; the
  scope cache is bounded, the windows are not.
- `allow_for` and `policy_for` are called once per scope, when it is first
  connected; later changes need `forget(...)`.
- One discovery request per scope, so N scopes make N `tools/list` requests.
- The rate limit is per process; it does not coordinate several processes.

## Verification status

- 331 tests (the 275 earlier ones unchanged plus 56 new); 327 passed and 4
  skipped without `ragleap-security`. 40 of 40 deliberate breaks of the new
  code were caught (one equivalent mutant, removing the empty-allowlist check,
  is not counted: `McpConfig` raises the same error type).
- Live-checked on 2026-10-26 (repository source of the 0.3.0 branch, Python
  3.10.12) against a third party's public mock MCP auth server with
  `scripts/tenant_live.py`; see the CHANGELOG for the results and their limit
  (the server cannot tell tenants apart, so separation is shown from the
  requests that were sent).
- Not live-verified: `approve_scoped`, `allow_for` and `policy_for` against a
  real server, a real `ragleap_security.AuditLog` fed by a live run, concurrent
  tenants against a real server, and a server that really separates tenants'
  data.
