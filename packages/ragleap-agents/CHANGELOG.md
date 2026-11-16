# Changelog

All notable changes to `ragleap-agents` are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.5.1] - 2026-11-14

Design: #1395. Tenant scope for runs, approvals and audit. A security fix for shared multi-tenant use.

### The gap this fixes

With 0.5.0, one shared `Agent` and one shared state store let a second tenant who knew only a paused run's id call `resume(run_id, {call_id: True})`. The run finished and its tool executed with the first tenant's arguments. `run()` and `resume()` had no tenant argument, the store was keyed by run id alone, and the run id was 48 bits (`uuid4().hex[:12]`). This was reproduced with the released 0.5.0 from PyPI and is a test now. Single-tenant use was not affected.

### Behaviour changes (read first)

- A caller who passes no scope sees no change, except for two things: **new run ids are 22-character URL-safe tokens (128 bits) instead of 12 hex characters** (code that assumed 12 hex characters breaks; old ids still load and resume), and `RunContext` has a new `scope` field that defaults to `None`.
- **New dependency: `ragleap-security>=0.2.0`** (standard library only, same project). `Scope`, `AuditLog` and `RateLimiter` come from it, so one scope type is shared with ragleap-memory and ragleap-optimizer.
- When a scope is given, a damaged stored state raises `ResumeError("unknown run")` like an unknown run (without a scope it keeps its 0.5.0 message).

### Added

- `Agent.run(task, *, scope=None, tainted=False)` and `Agent.resume(run_id, decisions, *, scope=None)`. `scope` must be exactly a `ragleap_security.Scope` (a string, a dict or a subclass raises `ScopeError`).
- A run saves `scope.key`. `resume` continues it only for exactly the same scope, every part (tenant, subject, agent, session). A wrong scope, an unknown run and a damaged state raise the same `ResumeError("unknown run")`, so nobody learns that another tenant's run exists. The ownership check comes before any decision is read. A run made with a scope cannot be resumed without one, and a run made without a scope cannot be resumed with one.
- `Agent(..., require_scope=True)`: `run` and `resume` without a scope raise `ValueError`. Default `False`.
- Sub-agents: a child run gets the scope of the run that started it, exactly. A strict child (`require_scope=True`) does not run for a parent that has no scope.
- `Agent(..., audit=AuditLog(...))`: events `run.start`, `approval.requested`, `approval.decided`, `tool.executed` and `run.end`, each with the scope, the run id, tool names, statuses and sizes. Never the task, the arguments or the results. If a record cannot be written, nothing continues without it: `run.start` and `run.end` raise; a failed `approval.requested` or `approval.decided` ends the run as `failed` / `internal_error` and the tool does not run. `decided_by` is not recorded because the library cannot verify who decided.
- `Agent(..., rate_limiter=RateLimiter(...))`: checked with the scope at `run()` and `resume()` before any model call or state change. A refusal raises `ragleap_security.RateLimitError`. A rate limiter needs a scope.

### Differences from the design in #1395

- The new dependency on ragleap-security was not in the issue.
- The pending approval's `call_id` is still 12 hex characters. It is only honoured together with the run id and the scope.
- The audit failure behaviour above was not spelled out in the issue.

### What this does not defend

- The caller must pass the right scope. The library does not log users in.
- A tool that reads another tenant's data by itself is not stopped. Tools must scope their own data.
- A shared store file: anyone with access to the file sees every tenant. Use one store per tenant for stronger separation.
- An in-memory rate limiter is lost on restart. No independent security review has happened.

### Verified (real output)

- 251 tests pass (208 from 0.5.0 and 43 new) on Python 3.12.3 and on Python 3.10.20 (author's sandbox).
- The cross-tenant reproduction: against the released 0.5.0 the second tenant's `resume` executed the first tenant's tool; with this code it raises `unknown run`, nothing runs, and the owner resumes normally.
- A paused run written by the released ragleap-agents 0.5.0 from PyPI (12-character id, no scope) resumes with this code without a scope, is refused with a scope, and is refused by a strict agent.
- `scripts/mutation_check.py` (new): 34 of 34 mutations of the 0.5.1 changes killed on both Python versions. It covers only the 0.5.1 changes; the older protections (taint, approval, delegation) have no mutation set yet.

### Not verified

- GitHub CI and the VPS (checked in the pull request). Windows and macOS. A real model provider (all tests use a scripted model). Several processes sharing one store beyond the thread test; store files on network file systems.

## [0.5.0] - 2026-10-21

### Behaviour changes

None for existing code. Every addition is opt-in: a run behaves as in 0.4.1 unless you pass
`tainted=True` or build a delegate tool. No existing public signature changed incompatibly (`Agent.run`
gained a keyword-only `tainted=False`).

### Added

- `Agent.run(task, *, tainted=False)`: start a run already tainted, so every outbound tool needs
  approval from the first step, as it does after a tainting tool has run. `tainted` must be a bool
  (anything else raises `ValueError`). The flag is saved in the run state, so it survives a pause
  and resume. Meant for runs whose task comes from an untrusted source (an incoming message, a
  webhook payload) and for sub-agents started by a tainted parent (#827).
- Building blocks for sub-agents (#827), used by `make_delegate_tool` below: `delegate_policy(children, *,
  allow_outbound=False)` validates child agents and returns the `ToolPolicy` to declare for their
  delegate tool (tainting if any child tool can taint, never outbound; a child tool with no declared
  policy raises, and an outbound one raises unless `allow_outbound=True`), `Budget(max_delegations=4,
  max_child_steps=16)`, and `MAX_DEPTH_CEILING = 3`.
- `make_delegate_tool(children, *, max_depth=1, budget=None, allow_outbound=False, name="delegate")`: an
  ordinary tool that hands one task to a named child agent and returns its short answer as an untrusted,
  fenced observation. A child starts tainted if its parent was, and a child that reaches an action needing
  approval is stopped with `stop_reason="approval_not_available"` and the action does not run (children
  cannot pause for approval in this release). Depth, the number of delegations and the children's steps are
  limited per root run; the counters are saved in the root run's state and survive a pause and resume. A
  child's own step cap bounds how far its steps can overshoot the budget.
- `ragleap_agents.context`: `current_run()` returns the `RunContext` (run id, depth, taint, counters) of
  the run executing the current tool call, or `None` outside a run. It is a context variable, so threads
  do not share it. Runs started inside a tool call are one level deeper than the run that called the tool.

### Verified

- All 208 tests pass on Python 3.10 on the VPS (main at c31ab4e), and CI is green on Python 3.10, 3.11
  and 3.12 for the delegate-tool change (#849).
- A live delegation check against a real model: `scripts/live_delegate.py` (#890), Groq
  `openai/gpt-oss-20b` in native mode, 8 runs (4 tasks in 2 configurations, one run each, 44 requests).
  The parent delegated in 8 of 8 runs, the parent was tainted in 8 of 8, and `send_message` never ran
  (approvals were never granted). In 3 runs the parent proposed `send_message` after a child had read
  the note, and the taint rule held it for approval.

### Not verified

- Resistance to prompt injection through a sub-agent. Only 2 of the 8 live runs read the injected note
  without a prompt that asked for an action, and the model proposed no send in either. The 3 runs that
  were held for approval followed prompts that asked for a send, so they show the gate working, not an
  injected instruction being resisted. Two runs are too few to say anything about real attacks.
- The child-side stop (`approval_not_available`) has only been exercised with scripted models. In the
  live runs the child never proposed `send_message`.
- 2 of the 8 live runs stopped on a model reply the loop could not use (`unparseable_reply`,
  `invalid_plan`). This looks like the format intermittency already seen on this model, but two runs
  cannot confirm that.
- One provider (Groq), one model, native mode only. No speed numbers exist.
- Resuming a run that was saved by 0.4.x with 0.5.0 (the saved state gained fields) has not been tested.
- A published 0.5.0 wheel has not yet been run on Python 3.11.

## [0.4.1] - 2026-10-20

### Changed

- Project links in the package metadata (Homepage, Repository, Issues) now point at
  https://github.com/ragleap/ragleap-core, the repository's new home in the ragleap organization.
  The OpenAI-compatible adapter's default `User-Agent` header now names the new address
  (`ragleap-agents (+https://github.com/ragleap/ragleap-core)`); it named the old owner before.
  Nothing else changed: this release is 0.4.0 plus those links and the version string. (The 0.4.0
  project page on PyPI still shows the old owner; GitHub redirects the old links.)

### Verified

- The only differences from the 0.4.0 tag are the version string, the project links and the default
  `User-Agent` constant (`git diff ragleap-agents-v0.4.0` over `packages/ragleap-agents`).

## [0.4.0] - 2026-10-17

### Changed (behaviour change: an empty run now stops)

- A run with no tools, or with an empty task, now returns `status="stopped"` with
  `stop_reason="no_task_or_tools"`. 0.3.0 and earlier returned `status="done"` for it, so a run in
  which nothing happened looked like a success. Code that checked only `status == "done"` will now
  see `stopped` in this case.

### Added

- `SQLiteStateStore(path)`: a durable `StateStore` in one SQLite file, standard library only. JSON text
  only (nothing is unpickled), a new file is created with mode 0600, run ids are limited to
  `A-Za-z0-9_-` (1-64), and a stored row that is not valid JSON, names another run or has an unknown
  status is refused with `ValueError`. The file is not encrypted: it holds tool arguments and
  observations, so protect it like that data.
- `save_if_status(run_id, state, expected_status) -> bool` on both shipped stores (an atomic
  compare-and-set; SQLite uses `BEGIN IMMEDIATE`) and the `AtomicStateStore` protocol that describes it.

### Fixed

- `resume()` claimed a paused run with load, check, save, which two workers could both pass, so an
  approved call could run twice. With a store that has `save_if_status` the claim is now atomic. A store
  with only `save()` and `load()` keeps the old, non-atomic behaviour (documented in `state.py`).
- A stored run state that a store refuses as corrupt now makes `resume()` raise `ResumeError`, and
  nothing runs.

### Corrected

- The `## [0.3.0]` heading below was dated 2026-10-13, but the tag was pushed on 2026-10-15 (tag run
  38791601216). The date is corrected here; the published 0.3.0 files still carry the old date and
  cannot change.

### Verified

- 151 tests (123 in 0.3.0). CI on PR #793 (run 39008521939): the ragleap-agents tests passed on
  Python 3.10, 3.11 and 3.12.
- Tests were written first. The new resume test failed before the change (the second resume did not
  raise) and the tampered-row test failed with a ValueError instead of ResumeError; both pass after
  it.
- SQLite store: 24 tests, including four separate processes racing to claim one paused run (exactly
  one wins) and a claim that waits for a competing writer instead of failing. Mutation-checked:
  removing the status check, skipping run-id and state validation, a 0644 file mode, skipping the
  corrupt-row checks, and a deferred BEGIN instead of BEGIN IMMEDIATE are each caught by at least
  one test.

### Not verified

- SQLite on a network filesystem (file locking there is unreliable; use a local disk), throughput,
  and long-running use. The file is not encrypted.
- Stores other than the two shipped ones: a custom store with only save() and load() cannot make the
  claim atomic.
- Unchanged since 0.3.0: the prompt wording was not touched, so the real-model results of 0.3.0
  stand and were not re-run for this release. JSON mode on the model tested is still intermittent,
  and the taint rule has still never been triggered by a real model.

## [0.3.0] - 2026-10-15

### Changed (behaviour change: the prompt wording)

- The instruction at the end of the prompt now depends on `prompt_style`. `"json"` (the default)
  still asks for one JSON object, now as plain text and without any tool-calling or function-calling
  feature. `"native"` no longer asks for JSON at all: it says to call exactly one of the provided tools
  by its own name, and to answer in plain text when finished or when it cannot or will not do the task.
  The repair note follows the style too. In 0.2.0 native mode told the model to reply with a JSON object
  while real tools were offered, and two models answered by calling a tool named `json`.

### Added

- `Agent(prompt_style=None)`: `"json"` or `"native"`. By default the style is read from the model
  callable, and `openai_compatible` advertises its mode, so the wording follows the adapter's mode
  without any extra argument. An explicit argument wins; any other value raises `ValueError`.

### Verified

- 123 tests pass on Python 3.10, 3.11 and 3.12. Mutation-checked: 9 of 9 mutations of the new
  behaviour (wording not switched, style not read from the callable, explicit style ignored, unknown
  style accepted, repair note not style-aware, JSON sentence dropped, native wording asking for JSON,
  adapter not advertising its mode, adapter always advertising json) are caught.
- Cold start, with a positive control in each test: the first step is never gated even with no
  policy; with no policy the second step pauses visibly (`awaiting_approval`, `forced`) while the
  identical run with the tool declared trusted completes; an outbound tool runs first but pauses
  after a tainting tool; a policy naming a tool that does not exist is refused at construction
  (no test covered that check before). 4 of 4 targeted mutations are caught by these tests alone.
- Live runs against Groq with `openai/gpt-oss-20b` after the change, one run per task, 20 tasks in each
  mode: tasks done were json 19, native 19 (the earlier sweeps with the 0.2.0 wording had json 15 and 16,
  native 17 and 20, the second of each with repair on; these are single runs and not a controlled
  comparison). Five tasks that had misbehaved before were then repeated three times each.
- JSON mode: `c1` failed in all 8 earlier runs whose results were kept and `c4` in all 5 (HTTP 400
  `tool_use_failed`); after the change both completed 4 of 4 times (the sweep plus three repeats). One
  earlier run, seen only on screen, had passed `c1`. This is one model on one provider.
- Injection tasks: no run in the sweep proposed the injected send (proposed, gated and executed all 0),
  as before.

### Not verified

- Whether the new JSON sentence helps with any other model or provider.
- Whether the native wording reduces the calls to a tool named `json`: that failure occurred in 2 of 46
  earlier saved native runs and in none of the 26 native runs since (20 in the sweep, 6 repeats), which is
  too few runs to show an effect.
- Not fixed: JSON-mode `i3` (read a note that contains an injected instruction) still failed 4 of 4 with
  HTTP 400 `tool_use_failed`. Native `i3` still ended `invalid_plan` 4 of 4: after reading the note the
  model declines by replying with a JSON action naming a tool `refusal`, not in plain text, even though
  the native wording says to answer in plain text. The loop has no defined way for a model to decline.
- Two native runs ended `done` without the tool result the task needed (the model skipped a step).
- Not fixed: a run with no tools, or an empty task, returns `status="done"` with
  `stop_reason="no_task_or_tools"` although nothing happened. Check `stop_reason`, not only `status`.
- The taint rule against a real model: no run proposed the injected send, so it was never triggered.

## [0.2.0] - 2026-10-12

### Changed (behaviour change from 0.1.0)

- A model reply that is not a single JSON object (prose, truncated JSON, two
  objects) now stops the run with `status="stopped"`, `stop_reason="unparseable_reply"`.
  0.1.0 reported these as `status="done"`, so a model failure looked like success.
  Nothing executed in either case. A reply that is a JSON object without a `tool`
  key now stops with `invalid_plan`. An explicit `{"tool": "done"}` is unchanged.
- The summary of a rejected run counts only the calls that ran; the rejected call
  is still in `steps` with `status="rejected"`.

### Added

- `Policy.repair_attempts` (default 0, capped at 2): after an unparseable or invalid
  proposal, the model is asked again with a short note naming the problem. The note
  never repeats the bad reply or an unknown tool name. A repaired proposal goes
  through the same validation, taint rule and approval gate as any other.
- `ragleap_agents.openai_compatible(base_url, api_key, model, mode="json"|"native", tools=...)`
  returns an `llm(prompt) -> str` for `Agent` over `POST {base_url}/chat/completions`.
  Standard library only; https required (http only with `allow_insecure_http=True`);
  no redirects; response size cap; retries with backoff on 408/429/5xx and network
  errors (capped at 5, `Retry-After` honoured up to 30 s); errors carry only a status
  code or exception type (`ProviderError`), never a body, URL or key. In `native` mode
  the first tool call is re-serialised as the JSON action and a reply with no tool call
  becomes an explicit `done`; `Agent` still validates, taints and gates every proposal.
  The owner sets the base URL: there is no address guard, so never build it from
  untrusted input.
- `scripts/live_runner.py` (in the repository, not in the wheel): runs 20 labeled tasks
  against a real OpenAI-compatible provider in json and native mode, with request
  pacing and a hard request cap, and writes a report. Every approval is rejected and
  `send_message` only appends to an in-memory list, so nothing is ever sent. Keys are
  read by environment-variable name and never printed or logged; results are written
  to `~/ragleap-live-runs` unless `--out` is given.
- `openai_compatible(error_detail=True)` and the runner's `--show-error-body` (diagnostics,
  off by default): a provider error then carries the first 300 characters of the error
  body, whitespace collapsed and the API key scrubbed if the provider echoed it. By
  default errors still carry only a status code or exception type.

### Fixed

- `openai_compatible` and the runner's `--list-models` sent Python's default
  `User-Agent`, which Groq's gateway rejects with HTTP 403 (error code 1010) before it
  looks at the key. Found by the first real call. The adapter now sends
  `ragleap-agents (+https://github.com/antonyrag/ragleap-core)`, and `user_agent=`
  overrides it (a value with a line break is refused). Checked against Groq only;
  Gemini has not been tried.
- The runner's test double no longer parses prompts with a regular expression
  (CodeQL `py/polynomial-redos` on test code).
- The runner only wrote its results at the end, so interrupting it (Ctrl-C) lost every
  finished run. Each run is now appended as it finishes and an interrupt writes a
  partial report.
- The runner scored an injection task whose run crashed with a model error as a
  success (no send ran). A failed run is no longer counted as safe.
- The report's median time now excludes the runner's own pacing and retry waits.

### Verified

- 112 tests pass on Python 3.10, 3.11 and 3.12 (sandbox): 57 with a scripted model, 42 for
  the adapter against a real local HTTP server (127.0.0.1, no external network), and 13 for
  the live runner against a deterministic fake model. Mutation-checked: 19 of 19 adapter and
  19 of 20 runner mutations caught; the survivor changes the mode of a file that an earlier
  step has already created as 0600, so it cannot matter.
- Live runs against Groq (`https://api.groq.com/openai/v1`) on 2026-10-11, one run per task and
  configuration with `scripts/live_runner.py`: 20 labeled tasks each in json, native, json+repair and
  native+repair mode with `openai/gpt-oss-20b` (80 runs), plus the three injection tasks in native mode
  with `openai/gpt-oss-120b` and `qwen/qwen3.8-27b` (6 runs). Completions work in both modes. Tasks done:
  json 15 of 20, native 17 of 20, json+repair 16 of 20, native+repair 20 of 20. These are single runs,
  and the differences between configurations are within the run-to-run variation seen.
- Failure kinds seen: HTTP 400 `tool_use_failed` in json mode (3 tasks, the same three in both sweeps)
  when the model answers in its own function-call format; HTTP 400 in native mode when the model calls a
  tool named `json` that was not offered (2 of 46 saved native runs across three models, on two of them);
  empty replies (4 runs); an invented `refusal` action to decline (5 runs).
- Repair fired in 5 of the 40 runs in the repair sweeps: 2 empty replies and 2 invented refusal actions
  ended with a valid action, and 1 invented action was repeated and the run stopped. It cannot act on a
  rejected request, because no reply exists.
- 16 runs on three models read the injected note; none proposed the injected send.

### Not verified

- The taint rule against a real model: no real run proposed the injected `send_message`, so the rule was
  never triggered. It is tested with scripted models only.
- Any provider other than Groq (Gemini has not been tried), models other than the three above, long runs,
  and concurrent resumes across processes.
- Whether repair improves pass rates: five firings on one model do not show that.
- Native mode's instruction still asks for a JSON object while real tools are offered, which two models
  answered by calling a tool named `json`. The instruction has not been changed.

## [0.1.0] - 2026-10-09

### Added

- Initial version. `Agent(llm, tools, policy, store, summarize)` runs an
  act-observe loop over `ragleap_tools.Tool` objects: the model proposes one
  JSON action per step, sees the result, and continues, up to `max_steps`
  (default 4, hard cap 8). `llm` is any callable `llm(prompt: str) -> str`.
- Every proposal is checked: the tool must exist and the arguments must fit
  the tool's JSON Schema (required keys, unknown keys, type and enum only).
  An invalid proposal stops the run; no tool runs.
- Tool results are untrusted data: capped, fenced, labelled, and lookalike
  tags inside them are neutralised case-insensitively.
- `ToolPolicy(taints, outbound, requires_approval)` per tool. After a tainting
  tool has run, every later outbound tool needs approval. A tool with no
  declared policy counts as both tainting and outbound.
- Pause and resume: an action that needs approval saves the run and returns
  `status="awaiting_approval"` with the pending call; `resume(run_id,
  {call_id: bool})` continues or ends it. Replays and mismatched decisions raise
  `ResumeError`. Run state is JSON-safe and lives in a pluggable `StateStore`
  (`InMemoryStateStore` included).
- Duplicate-action stop, a wall-clock deadline checked between calls, and a
  summary pass. `run()` does not raise for model or tool failures; a tool's
  exception text never reaches the model or the result, only its type.

### Verified

- 41 tests with a scripted model (no network) pass on Python 3.10, 3.11 and
  3.12. Mutation-checked locally: removing the taint rule, the case-insensitive
  fence, the exception-text rule, the resume claim, the hard cap, the fail-closed
  default or the bool-is-not-integer check each makes at least one test fail.
- The quickstart in the README was run with the real `ragleap-tools`
  `calculator` tool and a scripted model.

### Not verified

- Any real model: no provider has been run through this loop yet.
- Long runs; concurrent resumes across processes (the store must make resume
  atomic); equivalence with RagLeap Core's agent loop (mirrors core commit
  `59fd555` as of 2026-10-03, which keeps changing).
