# Governed, auditable, multi-tenant dispatch (proposal for ragleap-tools 0.6.0)

Status: proposal, written 2026-11-15. Nothing in this note is implemented, and
the open decisions at the end have not been taken by the maintainer.

## Problem

`Toolkit.call(name, arguments)` (0.5.x) is safe against bad input: it parses
and size-limits the JSON, validates the arguments against the tool's schema
before the handler runs, and contains exceptions. It knows nothing about the
caller:

- no caller identity;
- no per-caller list of tools: every tool in a toolkit can be called by anyone
  who can reach `call`;
- no approval gate for risky tools;
- no rate limit;
- no record of what was called, for whom, and with what outcome.

`Tool.metadata` is documented as advice for the caller's own policy code:
nothing in this package acts on it. A host that serves several tenants has to
build all of the above around the toolkit itself. `ragleap-integrations`
already did that for MCP tools (`McpPolicy`, `on_event`, `Scope`, `RateLimit`,
`McpHub`), so the controls exist for remote tools but not for the local ones.

Goal: an opt-in layer that gives local tools the same owner-side controls with
the same vocabulary, and leaves `Toolkit` behaving exactly as it does today.

The words are used narrowly:

- governed: owner-supplied rules (which tools, which need approval, how fast)
  are enforced before a handler runs;
- auditable: every call attempt produces one event that never contains
  arguments or results;
- multi-tenant: every call carries a scope (a tenant, and optionally a
  subject, agent and session), and the rules and limits are decided per scope.

Not claimed: authentication (this layer cannot check who the caller is; the
host authenticates and passes a scope), isolation stronger than the scope the
host passes, or any enterprise-grade property.

## Design (proposal)

One new class, `GovernedToolkit`, in a new module `ragleap_tools.governed`. It
wraps a `Toolkit` (composition, not inheritance) and never changes it:

    gk = GovernedToolkit(
        toolkit,
        allow_for=lambda scope: ["calculator", "get_current_datetime"],
        require_approval=["write_file"],
        approve=lambda scope, tool, arguments: ask_a_human(scope, tool),
        rate_limit=RateLimit(calls=60, per_seconds=60),
        on_event=audit_sink,
    )
    result = gk.call(scope, name_from_model, arguments_from_model)

`call` never raises, like `Toolkit.call`, and returns a `ToolResult`. The
checks run in this order and the first failure stops the call:

1. `scope` must be exactly a `Scope`. A subclass, a string or a dict is
   refused, so a caller cannot pass a forged object.
2. The tool must exist AND be named by `allow_for(scope)`. Deny by default. An
   unknown tool and a tool that is not allowed give the same constant refusal
   ("tool not available"), so probing cannot list another tenant's tools.
3. The tenant's rate limit must have room. This runs before any parsing, so
   invalid calls cannot be used to spend CPU for free.
4. The arguments are parsed and validated by the toolkit's own code.
5. If the tool is named in `require_approval`, `approve(scope, tool,
   arguments)` must return exactly `True`. An exception or any other value
   denies. With no `approve` supplied, such a call is refused.
6. The handler runs under the toolkit's existing exception containment.

The order is cheap checks first, bounded work next, and validation before
approval, so a human is never asked to approve arguments that would be
rejected anyway.

Steps 4 and 6 must be callable separately, so `Toolkit` gets two small public
methods. The change is additive and `Toolkit.call` is rewritten in terms of
them without changing its behavior: `prepare(name, arguments)` returns `(tool,
arguments)` or a failed `ToolResult`, and `run(tool, arguments)` runs the
handler with the containment.

Events. `on_event(event)` receives one dict per call attempt, shaped like the
MCP events of `ragleap-integrations` so that one sink can take both:

    {"event": "call", "scope": scope.key, "tool": "<name>" or None,
     "ok": True or False, "error": "<constant text>" or None,
     "approval": "not_required" | "approved" | "denied" | "unchecked",
     "duration_ms": 12}

`tool` is `None` when the tool was unknown or not allowed, because that name
came from a model and is not logged. `error` is always a constant reason,
never a handler message or exception text. Arguments and results never appear.
A hook that raises is logged and ignored: telemetry cannot break a call.

Scope. `ragleap_security.Scope` when the optional extra is installed,
otherwise a built-in fallback with the same fields (tenant required; subject,
agent and session optional), the same character rules and the same `key`
format as the fallback in `ragleap-integrations`.

Rate limit. `RateLimit(calls, per_seconds)`: at most `calls` calls per
`per_seconds` seconds for each tenant, with the same field names as the
`RateLimit` of `ragleap-integrations`. Windows are kept per process;
`forget(tenant=...)` drops one tenant's window. State is guarded by a lock, so
one `GovernedToolkit` can be shared between threads. The handlers' own thread
safety stays the host's responsibility.

## Rejected alternatives

1. Put scope, policy and events into `Toolkit.call`. That changes the
   signature and behavior of the simple dispatcher for every user, and a
   mistake there reaches everyone. An opt-in wrapper leaves the default path
   alone.
2. Subclass `Toolkit` and override `call`. A subclass can be bypassed by
   calling the base method, and it invites overriding half of it. Composition
   leaves one path.
3. Trust `Tool.metadata` (for example `read_only`) to skip approval. Nothing
   acts on metadata today, so making it load-bearing would change its meaning
   silently, and a tool author can mislabel. It could be added later as an
   explicit owner opt-in.
4. A separate package (`ragleap-governance`). The decision point is the
   dispatcher, and a new package adds a version-matching burden for little
   gain. Revisit if other packages want to share it.
5. Authentication here (callers' tokens, OAuth, API keys). This layer holds no
   credentials of callers and verifies nobody; it requires a scope from the
   host. Authentication needs its own design pass.
6. Put it in `ragleap-integrations`. That package depends on `ragleap-tools`,
   not the other way round, and the dispatcher lives here. Integrations can
   adopt it later.

## Security notes

- Identity is only as strong as the scope the host passes. Nothing in this
  layer verifies it.
- A toolkit shared by several tenants shares the tools' own state: the file
  tools are bound to one `FileOpsConfig(root_dir=...)`, the web search and
  vision providers each hold one `api_key`, and `ingest_document` and
  `search_documents` use one `RagLeap` instance. The optional `filename`
  filter of the search tool is a convenience, not a tenant boundary.
  `GovernedToolkit` can refuse a call but cannot make a shared handler tenant-
  safe. The recommended pattern is one toolkit per tenant, built from that
  tenant's own configuration. A hub that builds and caches them (like
  `McpHub`) is a possible next slice and is not part of this one.
- `approve` may block on a human and has no deadline in this layer. It is
  called only with arguments that already passed validation.
- Events carry the scope key (tenant, subject and so on), which is personal
  data in many settings. The host chooses where events go. Arguments and
  results are never included.
- A rate window is kept for every tenant seen until `forget`. A host with
  unbounded tenant names should call it, or bound the names it passes.
- Out of scope: prompt injection through tool results, filtering of results,
  and CPU or memory limits for handlers.

## Limitations (planned)

- The rate limit is per process: several processes each allow the full amount.
- No persistence of events and no tamper evidence for them. That belongs to
  the sink (for example an audit log in `ragleap-security`, whose fit with
  this event shape has not been checked).
- No cost or budget limit and no rules based on time or data: `allow_for` and
  `approve` are the owner's code.
- Needs the small additive `Toolkit.prepare` and `Toolkit.run` change.

## Open decisions for the maintainer

Each has a recommendation and can be reversed before any code is written.

1. Placement. A new opt-in module in `ragleap-tools`, released as 0.6.0
   (recommended); not inside `Toolkit`, not a new package.
2. Identity. Require exactly a `Scope` (recommended), from `ragleap-security`
   when installed and from the built-in fallback otherwise. The cost is a
   second copy of the fallback until one package imports the other.
3. Allowlist default. Deny unless `allow_for(scope)` names the tool
   (recommended).
4. Approval. The owner names the tools that need it, and no `approve` callable
   means refused (recommended). This is stricter than `ragleap-integrations`,
   where the flag is advice unless `approve` is supplied; this layer is the
   governed one.
5. Events. The MCP event shape plus `scope` (recommended).
6. Toolkit change. Additive `prepare` and `run` (recommended), rather than
   reaching into private methods.
7. Rate limit. Per tenant, per process, with the same fields as `RateLimit` in
   `ragleap-integrations` (recommended).
8. Slicing. 0.6.0 `GovernedToolkit`; 0.7.0 a per-tenant toolkit hub; after
   that a UTCP manual exporter for `ragleap-tools`. Each with its own design
   pass.

## Verification status

- Read in code (public repository, commit 90293d08, 2026-11-15): `Tool`,
  `ToolResult` and `Toolkit` (`call`, `select`, the private `_parse`) in
  `ragleap-tools`; the config classes of the file, web search, vision, ingest
  and search tools; that `ragleap-tools` has no identity, policy, event or
  rate-limit code; and in `ragleap-integrations` the `Scope`, `RateLimit` and
  `McpHub` code and the event shape in `mcp-governance.md`.
- Observed: nothing. No prototype exists and no test was run for this
  proposal.
- Not verified: that the proposed event shape works with
  `ragleap_security.AuditLog` or a tracing library; that
  `ragleap_security.Scope` behaves like the fallback (only the fallback's own
  comments were read); whether any caller depends on `Toolkit.call` being the
  only dispatch path. No comparison with another framework was made and none
  is implied.
