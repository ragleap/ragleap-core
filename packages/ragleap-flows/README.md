# ragleap-flows

Multi-tenant flows as plain JSON data: **tool steps**, **approval checkpoints** and **branches**. Every run belongs
to a `ragleap_security.Scope`; a paused run survives a restart and is resumed later, only by the tenant that owns it.

Alpha (0.1.0). MIT. Python 3.10+. Not independently audited.

    pip install ragleap-flows

This is not n8n and not a workflow engine with connectors. A flow file holds no code: no `eval`, no scripts, no
string interpolation. It is a small, auditable core for "do these tool calls, ask a human here, branch there".

## A flow

    {
      "version": 1,
      "steps": [
        {"id": "look", "type": "tool", "tool": "lookup_order", "arguments": {"order": "$input.order"}, "next": "check"},
        {"id": "check", "type": "branch",
         "cases": [{"when": {"path": "$look.amount", "op": "gt", "value": 100}, "goto": "ask"}], "else": "pay"},
        {"id": "ask", "type": "approval", "prompt": "Approve this refund?", "show": {"amount": "$look.amount"}, "next": "pay"},
        {"id": "pay", "type": "tool", "tool": "refund", "outbound": true, "arguments": {"order": "$input.order"}}
      ],
      "output": {"refunded": "$pay.ok"}
    }

Step types: `tool` (`tool`, `arguments`, `next`, `outbound`, `external`), `approval` (`prompt`, `show`, `next`),
`branch` (`cases` of `when` and `goto`, then `else`). A missing `next` ends the run.

Values move between steps with paths: `$input.x` and `$step_id.field.0`. A string that is exactly a path is replaced by
the value it points at (dict keys by name, list items by position). There is no interpolation and no attribute access;
`"$$x"` is the literal text `$x`. Operators: `eq ne lt le gt ge in exists truthy`, combined with `all` and `any`. A
number never equals a `True`, and a missing value satisfies no comparison (not even `ne`).

## Run it

    import ragleap_flows as rf
    from ragleap_security import Scope

    flow = rf.from_json(open("refund.json").read())          # checked once, strictly
    runner = rf.FlowRunner(flow, toolkit, store=my_store)    # toolkit: a ragleap_tools Toolkit
    tenant = Scope("acme")                                   # the host authenticates; the library never does

    result = runner.run(tenant, {"order": "A-17"})
    if result.status == "awaiting_approval":
        result = runner.resume(tenant, result.run_id, {result.pending["step"]: True})

`status` is one of `done`, `awaiting_approval`, `rejected`, `failed`, `stopped`. Use `dispatch=` instead of `tools=`
to route tool calls yourself, for example through a governed, per-tenant dispatcher: `dispatch(scope, name, arguments)`
must return a `ToolResult`. Any `ragleap_agents` state store works (`InMemoryStateStore`, `SQLiteStateStore`).

### Governed dispatch (ragleap-tools 0.6.0 or newer)

`GovernedToolkit.call` has the shape `dispatch` expects, so the owner's rules apply to every tool step of a flow:
a per-tenant allowlist (deny by default), an approval hook for chosen tools, and a rate limit per tenant. The
flow's own `approval` step and the owner's approval hook are separate: the flow asks its checkpoint first, and a
tool in `require_approval` is still refused when no hook says yes.

    from ragleap_tools import GovernedToolkit
    from ragleap_tools.governed import RateLimit

    governed = GovernedToolkit(
        toolkit,
        allow_for=lambda scope: ["lookup", "send"] if scope.tenant == "acme" else ["lookup"],
        require_approval=["send"],
        approve=lambda scope, tool, arguments: arguments.get("to", "").endswith("@acme.test"),
        rate_limit=RateLimit(calls=30, per_seconds=60),
    )
    runner = rf.FlowRunner(flow, dispatch=governed.call)

A refused step ends the run as `failed` with the stop reason `tool_failed:<step id>`, and the tool does not run.
Not covered: the owner's hook decides who may approve; the flow does not check who the approver is.

## Multi-tenant rules

1. Every call takes exactly a `Scope`. There is no unscoped run; single-tenant use is one fixed scope.
2. A run records its scope key. `resume` continues it only for exactly that scope (every part). A wrong scope, an
   unknown run id and a damaged stored state all give the same `ResumeError("unknown run")`, and ownership is checked
   before any decision is read, so nobody learns that another tenant's run exists.
3. Run ids are 128-bit random tokens.
4. `store_for(tenant)` gives each tenant its own store. One shared store is safe because of the checks above, but
   anyone with access to the store file sees every tenant.
5. Audit events (an `AuditLog`) hold the scope, ids, step ids, tool names and sizes, never input, arguments or
   results. If a record cannot be written, the error is raised and nothing continues.
6. A `rate_limiter` (anything with `check(scope)`) is checked before a run starts or resumes.
7. Taint, as in ragleap-agents: after a step marked `"external": true`, or for a run started with `tainted=True`, every
   later `"outbound": true` tool step pauses for approval first (`pending["forced"]` is true), each time.

## Hard limits

100 steps per flow, 256 KiB flow file, 64 KiB input, 64 KiB per step output, 1 MiB saved state, 300 s deadline per
`run` or `resume` call (checked between steps), no cycles, every step reachable, a path may only read a step that
always runs before it. Each limit is refused or stopped with a named reason, never silently cut.

## What this does not defend

- It does not authenticate anybody. Pass only a `Scope` your application has established.
- A tool that reads another tenant's data by itself is not stopped. Tools must be written tenant-safe, or called through
  a per-tenant dispatcher.
- Approval is only as good as who is allowed to call `resume`. The library does not check who the approver is.
- Taint only works if your flow marks `external` and `outbound` steps honestly. An unmarked step is trusted.
- The deadline is checked between steps; a single slow tool call is not interrupted.
- A run that crashes while `running` stays `running`; 0.1.0 does not recover it automatically.
- One shared store file shows every tenant to whoever can read it; the state is not encrypted.
- Not tested on Windows or macOS, network file systems, or several processes sharing one store beyond the atomic claim.
- No independent security review.

## Tests

153 tests; `scripts/mutation_check.py` breaks 68 protections one at a time and requires a test to fail. All 68 are caught.
One more candidate was dropped as an equivalent mutant (the `$$` check in `parse_path` is also enforced by the segment
rule).
