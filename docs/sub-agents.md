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

## Watching and stopping

The Runs tab of the dashboard shows sub-agent runs under their parent and has a Cancel run button (it asks for a second click). `GET /agent-runs/{id}/tree` shows a run with its children. `POST /agent-runs/{id}/cancel` stops a run and everything below it. AUDIT lines `subagent_started`, `subagent_finished` and `agent_run_cancel` are written.

## Limits to know

- One delegation at a time per run: a parent waits for its child. The limit of children working at the same moment matters when several requests delegate together.
- Each child costs extra model calls. The caps above are the control; start with the defaults.
- A child that was running when the server restarted stays "running" until you cancel it (or the cancel call finds it idle for 15 minutes).
- Not included yet: a research skill built on this.
