# Agent loop - design

## Problem

`ragleap-tools` ships `Tool` objects and deliberately no execution loop (its
`__init__` says so). RagLeap Core has an act-observe loop (`core/agent_loop.py`,
commit `59fd555`) but it is welded to the app: Postgres tables, owner approval
through chat channels, a fixed set of nine action names, env-var configuration.
This package is a standalone loop over `ragleap_tools.Tool` objects that keeps
Core's safety ideas and drops the app coupling. It imports nothing from `core/`.

## Design

- The model is a callable `llm(prompt: str) -> str`. The loop asks for ONE JSON
  object per step: `{"tool": "<name or done>", "arguments": {...}}`. Works with any
  text model; native function-calling would be a later adapter.
- `Agent(llm, tools, policy, store, summarize)`; `run(task)` and `resume(run_id, decisions)`.
- A run is a JSON-safe dict (`run_id, task, status, steps, tainted, pending, summary`)
  saved after every step through a `StateStore`. Pausing for approval is just
  "save and return"; `resume()` loads it. No threads, no database.
- Each tool has a `ToolPolicy(taints, outbound, requires_approval)` kept in the
  agent's `Policy`, not on `Tool`, so `ragleap-tools` needs no change.

## What it mirrors from Core

One action per step, seeing each result; step cap with hard maximum 8; duplicate
action stop; results treated as untrusted data (capped, fenced, labelled, lookalike
closing tags neutralised); taint rule (after external content, outbound actions need
approval; can only tighten); a tools-disabled summary pass; never raises into the caller.

## Where it differs (deliberately)

| Topic | Core | This package |
|---|---|---|
| Tool set | 9 fixed actions with `target`/`content` strings | any `Tool`; arguments validated against its JSON Schema subset |
| Taint / outbound sets | hard-coded names | declared per tool; **undeclared = tainting and outbound** |
| What taints | a successful fetch/MCP result that does not look like a refusal | **any** execution of a tool declared as tainting, success or not (a failure message can carry remote text) |
| Fence neutralising | case-sensitive replace | case-insensitive |
| At the step limit | status `done` with a note | status `stopped`, `stop_reason="step_limit"` |
| Approval | owner reply through a channel; off/semi/full modes; sensitive-role forcing | `requires_approval` and the taint rule only; who approves, and how, is the caller's business |
| Resume | background thread on approval | synchronous `resume()`; refuses replays and mismatched ids |
| Budget | token budget | wall-clock deadline between calls |
| Storage | `agent_runs` in Postgres | pluggable `StateStore`; in-memory included |

## Nested approvals (0.6.0)

Design: issue #963. A child agent started by `make_delegate_tool` runs inside one tool call of its parent. In 0.5.x a
child that reached an action needing approval was stopped (`approval_not_available`). With `nested_approval=True` the
pause comes up to the top-level run as one pending approval (the child's real tool and arguments, plus the path), and
one `resume()` decides it. Decisions on the open questions in #963: a rejection ends the whole tree, and the parent's
delegate step records what the child runs had already executed (the outside review by renezander030); the release is
0.6.0, because a new public parameter is new API; `nested_approval` stays separate from `allow_outbound`.
Two properties carry the weight: resume reads the whole path before it changes anything (every child run must exist,
belong to exactly the caller's scope, be awaiting approval and hold the same pending action), and the deepest run is
claimed first, so only one resume can pass the atomic gate. See the CHANGELOG for what it does not defend.

## Limits and open risks

- Prompt injection is not prevented, only constrained by the declared policies.
  A wrong `TRUSTED` declaration defeats the gate.
- Tool descriptions are shown to the model (capped at 300 characters). Descriptions
  from an untrusted source can inject.
- `resume()` is load, check, save: not atomic across processes. The store must
  provide an atomic claim if several workers can resume the same run.
- An invalid proposal stops the run (as in Core) instead of letting the model repair
  it. Whether one repair attempt would help is unmeasured.
- Verified with a scripted model only. Nothing has been run against a real provider.
