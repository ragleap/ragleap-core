# Sub-agents

A run of the agent loop can hand one self-contained task to a colleague role. The colleague works on it as a child run and returns a short result. This page describes what exists on main. It is off by default and is not a security or compliance claim.

## Switches and limits

Set in the app environment (all optional, shown with their defaults):

- `SUBAGENTS_ENABLED=false`: nothing happens unless this is `true` and the agent loop is on.
- `SUBAGENT_MAX_CHILDREN=3`: children per run (1 to 5). A child cannot hand work off further (depth 1).
- `SUBAGENT_MAX_PARALLEL=2`: children working at the same moment on this server (1 to 4). A child that cannot get a place within 30 seconds is not started.
- `SUBAGENT_TREE_TOKENS=30000` and `SUBAGENT_CHILD_TOKENS=8000`: token caps for the whole tree and for one child. The numbers are estimates from the text sent and received. When a cap is reached the run stops with status `budget`.
- `SUBAGENT_SECONDS=120`: how long a child may work in its first stretch (10 to 600). A child that was waiting for an approval has no such limit when it continues.

## Safety rules

- Every child action goes through the same approval gate as any other action, with the child's own role (sensitive roles stay on approval).
- A child starts as tainted when its parent is. When a child read outside content, its parent becomes tainted too, so outbound actions of the parent then need approval.
- What a child returns is untrusted text for the parent, like any tool result.
- A child that needs an approval puts its parent on hold. The parent goes on when the child ends (approved and finished, or rejected). Cancelling the parent cancels the child and rejects its pending action.

## Who may start a child, and what it inherits

The gate decides every start (`authorize_spawn` in `core/autonomy.py`) and writes an audit event for each decision, allowed or denied, with the reason: `sub_agents_off`, `depth_limit`, `child_limit` or `check_failed` (a check that cannot be made also denies). A child inherits its parent's limits:

- If the parent, or any run above it, is a sensitive role (legal, medical and similar), **every action of the child needs approval**, whatever role the child has. The parent chain is read each time, and a parent that cannot be read counts as sensitive.
- A child's tools are the tools its parent has, never more.
- A child starts with its parent's taint, and its own taint comes back to the parent.
- A child cannot start a child.
- Every child has a signed **start record** in its own table, `agent_run_starts`: `spawn_id`, the run, its parent, role and depth, whether the child is forced to ask, the **key id** and a signature. The gate issues it when it allows the start. The table is **append-only**: database triggers refuse UPDATE, DELETE and TRUNCATE, there is no foreign key, and retention never touches it. The record is read from the table every time a child runs or resumes and must match the live row's parent, role and depth; a child without a valid record is refused. A child that was forced to ask stays forced even if the rows above it are pruned. The same `spawn_id` is in the audit events. Without a signing key no sub-agent starts.
- The signing key is `SPAWN_SIGNING_KEY`, or `ADDON_ENCRYPTION_KEY` when that is not set. A **key id** (a short hash, never the key) is stored in every record.
- The limit of this: the application's database user owns the table, so a database administrator could drop the triggers. They stop runtime code, bugs and injected SQL from changing a record; the signature stops anyone without the key from forging one.
- A child may draw at most the **smaller of** its own role's remaining token budget and its parent's (what the other children already spent is subtracted). The whole run tree shares one **step limit**, so delegating does not multiply the steps. Token budgets fail open when the ledger cannot be read, because they are cost control; the step limit fails closed.

## Rotating the start-record key

1. Set `SPAWN_SIGNING_KEY_PREVIOUS` to the old key and `SPAWN_SIGNING_KEY` to the new one (several old keys may be listed, separated by commas), then restart. New records use the new key; old records still verify.
2. Run `docker exec ragleap-core-app-1 python -m core.spawn_keys`. It never prints a key. It lists each key id, how many records use it and how many of those runs are not finished yet.
3. When the old key id shows `"safe_to_retire": true`, remove it from `SPAWN_SIGNING_KEY_PREVIOUS`. A record whose key is no longer configured is refused, so a run that still needs it would stop; the report also counts those runs.

## Watching and stopping

The Runs tab of the dashboard shows sub-agent runs under their parent and has a Cancel run button (it asks for a second click). `GET /agent-runs/{id}/tree` shows a run with its children. `POST /agent-runs/{id}/cancel` stops a run and everything below it. AUDIT lines `subagent_started`, `subagent_finished` and `agent_run_cancel` are written.

## Limits to know

- One delegation at a time per run: a parent waits for its child. The limit of children working at the same moment matters when several requests delegate together.
- Each child costs extra model calls. The caps above are the control; start with the defaults.
- A child that was running when the server restarted stays "running" until you cancel it (or the cancel call finds it idle for 15 minutes).
- Not included yet: a research skill built on this.

## The start record binds what the child can do, what it was told and its taint

A child's signed start record (`spawn4`) includes three things besides its ids:

- **What it could do.** A digest (with a version tag) of its tool list: for every tool the name, the `module:qualname` of the function that does the work, read from the live function, and the declared effect class. The table of handlers (`TOOL_HANDLERS` in `core/autonomy.py`) is checked against the dispatcher by a test, so the two cannot drift apart. Tool descriptions are not part of it on purpose: they embed your own configuration (webhook names, allowlists). A child whose tool has no known handler is not started.
- **What it was told.** A digest of its role and instruction text exactly as stored in its run row. Editing either after the child was signed makes it refuse to run.
- **The taint it started with.** A child may become more tainted while it runs, never less than it started. A child whose taint was cleared is refused.

The same function (`resolved_tools`) builds the tool list when the child is started and when it is driven, and it can only read four fields of the run (`id`, `parent_id`, `role`, `depth`), so the two digests cannot differ for the same child. An empty tool list is not hidden behind a missing record: a child with no tools is not started (`no_tools`), and every refusal of a record writes an audit event with its real reason (`no_record`, `field_mismatch`, `empty_tool_list`, `old_record_format`, `tools_changed`, `task_changed`, `taint_lowered`, `unknown_key`, `bad_signature`, `check_failed`) and puts it in the summary of the refused run. A setting change while a child waits (a tool removed or added, for instance) refuses that child; that is the safe direction. Records from before this version (`spawn3`) are refused.

## A child's budget is reserved before it exists

Before a child run is created, the parent's row takes the child's token cap and one child slot with a single conditional database statement (an atomic compare-and-swap, not a read followed by a write). Two spawns that look at the same remaining budget at the same moment cannot both take it: the second one is refused. The room for children is the tree cap (`SUBAGENT_TREE_TOKENS`) minus what the parent itself used, and never more than the parent role's remaining budget. The limit is a hard bound: the total reserved never goes above it.

When a child ends, its reservation turns into what it really used (a child that used more than its cap is counted at its real use). A child that waits for an approval keeps its reservation. If the child row cannot be written the reservation is given back; if the app stops in between, the reservation stays and only lowers what that parent may still spawn. The check fails closed: if the database cannot be reached, no child starts. The role ledger is a cost counter and still fails open.

The reservation is worst case: a parent that already used much of the tree cap may be refused a child even though the child would have used less. Children of one parent still run one after another today, so this guards the Phase 3 parallel spawning that comes next.

## Only the gate starts a child

The agent loop never starts a child itself. It calls `execute_delegation` in `core/autonomy.py` (the same module as the gate for every other action). That function checks the plan, asks `authorize_spawn` (owner switch, depth, number of children, signing key, forced approval for sensitive roles) and only on a yes hands over to `subagents.run_delegation`, which reserves the budget, writes the signed start record and drives the child. `run_delegation` refuses anything that does not carry the gate's decision. The boundary scan treats `run_delegation` like the other executors: it may only appear inside `execute_delegation`, and a test pins that the loop calls nothing else.

There is no separate owner prompt for the spawn itself. A spawn has no effect on the outside world, and every action a child takes goes through the gate on its own (autonomy mode, taint, sensitive roles, forced approval), so the owner still approves each effect. In the capability declaration `delegate` keeps `per_mode`, which for this one operation means: the gate decides the spawn, the effects are decided one by one.
