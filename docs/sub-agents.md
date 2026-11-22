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
- Every child row carries a signed **start record**: `spawn_id`, `spawn_forced` and a signature made with a key derived from `ADDON_ENCRYPTION_KEY`. The gate issues it when it allows the start, it is written once and never updated, and a child without a valid record is refused every time it runs or resumes. Without that key no sub-agent starts. A child that was forced to ask stays forced even if the rows above it are pruned. The same `spawn_id` is in the audit events.
- A child may draw at most the **smaller of** its own role's remaining token budget and its parent's (what the other children already spent is subtracted). The whole run tree shares one **step limit**, so delegating does not multiply the steps. Token budgets fail open when the ledger cannot be read, because they are cost control; the step limit fails closed.

## Watching and stopping

The Runs tab of the dashboard shows sub-agent runs under their parent and has a Cancel run button (it asks for a second click). `GET /agent-runs/{id}/tree` shows a run with its children. `POST /agent-runs/{id}/cancel` stops a run and everything below it. AUDIT lines `subagent_started`, `subagent_finished` and `agent_run_cancel` are written.

## Limits to know

- One delegation at a time per run: a parent waits for its child. The limit of children working at the same moment matters when several requests delegate together.
- Each child costs extra model calls. The caps above are the control; start with the defaults.
- A child that was running when the server restarted stays "running" until you cancel it (or the cancel call finds it idle for 15 minutes).
- Not included yet: a research skill built on this.
