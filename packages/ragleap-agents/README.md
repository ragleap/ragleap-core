# ragleap-agents

A small agent loop over [`ragleap-tools`](https://pypi.org/project/ragleap-tools/)
`Tool` objects. The model proposes **one action at a time**, sees the result,
and proposes the next, up to a hard step cap. You bring the model as a plain
callable, or use the included adapter for OpenAI-compatible chat APIs; there is no database.

```bash
pip install ragleap-agents
# or: uv add ragleap-agents
```

## Quickstart

```python
from ragleap_tools import CALCULATOR_TOOL
from ragleap_agents import Agent, Policy, TRUSTED

def my_llm(prompt: str) -> str:
    ...  # call any model, return its text

agent = Agent(
    llm=my_llm,
    tools=[CALCULATOR_TOOL],
    policy=Policy(max_steps=4, tools={"calculator": TRUSTED}),
)
result = agent.run("What is 6 * 7?")
print(result.status, result.answer)   # done, a short summary of what was found
for step in result.steps:             # the transcript: tool, arguments, status, observation
    print(step["tool"], step["arguments"], step["status"])
```

## Using a real model

`openai_compatible` returns the `llm(prompt) -> str` callable for any provider that speaks
the OpenAI chat-completions API. Standard library only. You pass the key in; it is never
read from the environment and never printed.

```python
from ragleap_agents import Agent, Policy, TRUSTED, openai_compatible

tools = [CALCULATOR_TOOL]
llm = openai_compatible(
    "https://api.groq.com/openai/v1", api_key, "openai/gpt-oss-20b",
    mode="native", tools=tools,   # or mode="json": the model replies with the JSON action as text
)
agent = Agent(llm, tools, Policy(tools={"calculator": TRUSTED}))
```

- https is required (`allow_insecure_http=True` is for local testing), redirects are refused,
  responses are size-capped, and 408/429/5xx and network errors are retried with backoff
  (at most 5 retries, `Retry-After` honoured up to 30 s).
- Errors are `ProviderError` and carry only a status code or an exception type, never a
  response body, URL or key. `error_detail=True` adds the first 300 characters of the error
  body (key scrubbed) to diagnose a provider; it is off by default.
- The base URL is yours to set. There is no address check, so do not build it from untrusted input.
- In `native` mode the first tool call becomes the JSON action, and a reply with no tool call
  becomes an explicit `done`. `Agent` still validates, taints and gates every proposal.
- The instruction in the prompt follows the mode. `Agent` reads `prompt_style` (`"json"` or
  `"native"`) from the model callable and the adapter advertises its mode, so nothing needs to be
  passed; `Agent(prompt_style=...)` overrides it. Native mode never asks for JSON.
- `Policy(repair_attempts=1)` asks the model once more after an unusable reply, with a short
  note that never repeats the bad reply. It is off by default, and a repaired proposal goes
  through the same checks.

**What was observed with a real model** (Groq, `openai/gpt-oss-20b`; one run of 20 labeled tasks
per configuration with `scripts/live_runner.py`; one model on one provider, not a benchmark):

- With 0.2.0's wording, JSON mode: the provider rejected the request with HTTP 400 `tool_use_failed`
  on 3 of 20 tasks (the same three in both sweeps) when the model answered in its own
  function-call format.
- With 0.3.0's wording, two of those three tasks completed 4 of 4 times; the third (`i3`, which reads
  a note containing an injected instruction) still failed 4 of 4. Tasks done in the sweep: JSON 19 of
  20, native 19 of 20. Prefer `mode="native"` with models that have one.
- Native mode: with 0.2.0's wording, 2 of 46 saved native runs (three models) failed with HTTP 400
  because the model called a tool named `json` that was not offered. With 0.3.0's wording, 0 of 26
  runs did, which is too few to show an effect.
- Models sometimes returned an empty reply, or invented an action name (`refusal`) to decline. In
  native `i3` the model declined the injected instruction 4 of 4 times, but as a JSON action naming
  `refusal`, which the loop reports as `invalid_plan`: it has no defined way to decline. With repair
  on (0.2.0's wording), 4 of the 5 runs where it fired ended with a valid action. Repair cannot help
  when the provider rejects the request, because no reply exists.
- No run on three models ever proposed the injected `send_message`, so the taint rule has **not**
  been triggered by a real model. It is tested with scripted models.

## Approval gates: pause and resume

```python
from ragleap_agents import ToolPolicy

policy = Policy(tools={"write_file": ToolPolicy(taints=False, outbound=True, requires_approval=True)})
agent = Agent(my_llm, tools, policy, store=my_store)

result = agent.run("save the report")
if result.status == "awaiting_approval":
    call = result.pending                 # {"call_id", "tool", "arguments", "forced"}
    # ... show it to a human, then, in this process or another one:
    result = agent.resume(result.run_id, {call["call_id"]: True})   # or False to reject
```

A paused run is a plain JSON dict in a `StateStore` (`save`/`load`). The
in-memory store and a SQLite store (`SQLiteStateStore(path)`: one local file, standard library only,
not encrypted) are included; write your own for Redis or a database. If several workers can resume the
same run, the store needs an atomic `save_if_status(run_id, state, expected_status)`; both shipped
stores have it, and a store with only `save`/`load` cannot make the claim atomic.
`resume()` raises `ResumeError` if the run is unknown (or belongs to another scope), not awaiting approval,
or the decision does not match the pending call, so an approval cannot be replayed.

## Tenant scope (0.5.1)

If one process serves several tenants, give every run a scope. A run then belongs to exactly that scope, and nobody else can approve, reject or read it.

    from ragleap_security import Scope

    agent = Agent(llm, tools, policy, store, require_scope=True)      # strict: a scope is mandatory
    acme = Scope("acme", subject="alice")
    result = agent.run("pay the invoice", scope=acme)
    agent.resume(result.run_id, {call["call_id"]: True}, scope=acme)  # any other scope raises ResumeError("unknown run")

A wrong scope, an unknown run and a damaged saved state all raise the same `ResumeError("unknown run")`, so nobody learns that another tenant's run exists. Every part of the scope must match. Sub-agents get their parent's scope. You can add `audit=AuditLog(path)` (ids, tool names and sizes, never text) and `rate_limiter=RateLimiter(...)` (checked per scope before any model call). Without a scope everything works as in 0.5.0, except that new run ids are 128-bit tokens.

**What this does not do.** It does not log users in: you pass the right scope. A tool that reads another tenant's data by itself is not stopped. Anyone with access to a shared store file sees every tenant; use one store per tenant for stronger separation. See the CHANGELOG for the 0.5.0 gap this fixes.

## The safety model

- **Every proposal is checked.** The tool must exist and the arguments must fit
  its JSON Schema (required keys, unknown keys, type and enum only; no `$ref` or `oneOf`).
  Anything else stops the run with `stop_reason="invalid_plan"`; no tool runs.
- **Tool results are untrusted data.** They are capped (1,500 characters each,
  4,000 in the prompt), fenced in `<observation>` tags, and lookalike tags inside
  them are neutralised, case-insensitively.
- **Taint rule.** Declare each tool with a `ToolPolicy`: `taints` (its result is
  external content) and `outbound` (it acts on the outside world). Once a tainting
  tool has run, every later outbound tool needs approval, whatever else is configured.
  The policy can only tighten.
- **Fail closed.** A tool with no declared policy counts as both tainting and outbound.
  Use `TRUSTED` only for tools that neither return external content nor act outside.
- **Limits.** `max_steps` (default 4, hard cap 8), a duplicate-action stop, and a
  deadline checked between calls (a running tool is not interrupted).
- **Failures stay contained.** `run()` does not raise for model or tool failures.
  A tool's exception text is never shown to the model or returned; only its type is.

**What this does not do:** it does not stop prompt injection. It limits what an
injected instruction can do, and only as far as your `ToolPolicy` declarations are
accurate. Tool descriptions are shown to the model too (capped at 300 characters),
so tools from an untrusted source (for example a remote MCP server) can inject
through them. Approval prompts are only as good as the human reading them.

## Status

v0.5.1, alpha. 251 tests on Python 3.10, 3.11 and 3.12 (see `tests/`), most with a scripted
model or a local fake server; each safety property above has a test that fails if the property
is broken.

**Behaviour changes:** since 0.2.0 the instruction wording depends on the mode (`prompt_style`);
since 0.1.0 a model reply that is not a single JSON object stops the run
(`stop_reason="unparseable_reply"`) instead of reporting `done`. See the CHANGELOG.

**Not verified:** the taint rule against a real model (none ever tried the injected action), any
provider other than Groq (Gemini has not been tried), long runs, concurrent resumes across
processes (your store must make resume atomic), and equivalence with RagLeap Core's own agent
loop, which this mirrors as of core commit `59fd555` (see `docs/design/agent-loop.md`).

## License

MIT
