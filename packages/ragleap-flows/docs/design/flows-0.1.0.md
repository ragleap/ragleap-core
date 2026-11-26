# ragleap-flows 0.1.0: design notes

Design proposal: issue #794 (API surface) and its multi-tenant design comment. These notes record where the code
differs from that comment, and why, so the issue is not silently rewritten.

## Differences from the #794 comment

- `run(scope, input)` and `resume(scope, run_id, decisions)` take the scope as the FIRST, required argument. There is
  no unscoped run and no `require_scope` switch.
- Tool steps run through `tools=` (a `ragleap_tools.Toolkit`) or `dispatch=` (any `dispatch(scope, name, arguments)`
  returning a ToolResult). `GovernedToolkit.call` has exactly that shape, but it was unreleased in ragleap-tools 0.5.1 (update 2026-11-25: it is released in ragleap-tools 0.6.0),
  so flows 0.1.0 does not depend on it.
- Taint is runtime, not static: a tool step may be marked `external` (its result is untrusted) or `outbound`. An
  outbound step reached while tainted pauses for approval (forced), each time.
- A path may only read a step that always runs before it. This is stricter than "an earlier step" and is checked when
  the flow loads.
- Flow output: an optional `output` template. A path in it to a step that did not run gives `output=None` and stop
  reason `output_unavailable` (the run still counts as `done`; its side effects already happened).
- Saved state reuses the ragleap-agents `StateStore` protocol and statuses, so `SQLiteStateStore` and its atomic
  `save_if_status` claim are shared, not copied.

## Not in 0.1.0

`AgentStep` (0.2.0), parallel branches, `WebhookStep` (belongs in ragleap-integrations), automatic recovery of a run
that crashed while `running`, a per-tenant run count (the StateStore has no list or count), post-quantum or other
signing of flow files.
