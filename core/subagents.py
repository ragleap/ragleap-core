"""Sub-agents: a run can hand one self-contained task to a colleague role, which works on it as a child run and returns a
short result. Off unless SUBAGENTS_ENABLED=true (and the agent loop is on).

Rules, all enforced here and in core/agent_loop.py:
- depth 1: a child cannot hand off further; at most SUBAGENT_MAX_CHILDREN children per run (default 3);
- at most SUBAGENT_MAX_PARALLEL children work at the same moment on this server (default 2);
- the whole tree shares a token cap (SUBAGENT_TREE_TOKENS) and each child has its own (SUBAGENT_CHILD_TOKENS); tokens are
  estimated from the text sent and received, like the usage ledger does when a provider reports none;
- a child works for at most SUBAGENT_SECONDS in its first stretch (a resumed child has no such limit);
- every child action passes the same approval gate as any action, with the child's own role; a child starts as tainted when
  its parent is, and the parent becomes tainted when a child read outside content;
- what a child returns is untrusted text for its parent (the loop's observation block neutralises it);
- a child that needs an approval puts its parent on hold; the parent goes on when the child ends.
"""
import hashlib
import logging
import os
import threading
import time
from typing import Dict, List, Optional

from core import budget

logger = logging.getLogger(__name__)

MAX_DEPTH = 1
TASK_CAP = 600
CAPACITY_WAIT_SECONDS = 30
_sem = None
_sem_lock = threading.Lock()


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        n = int(os.environ.get(name, default))
    except ValueError:
        n = default
    return max(lo, min(n, hi))


def enabled() -> bool:
    return os.environ.get("SUBAGENTS_ENABLED", "").strip().lower() == "true"


def max_children() -> int:
    return _int_env("SUBAGENT_MAX_CHILDREN", 3, 1, 5)


def max_parallel() -> int:
    return _int_env("SUBAGENT_MAX_PARALLEL", 2, 1, 4)


def tree_tokens() -> int:
    return _int_env("SUBAGENT_TREE_TOKENS", 30000, 1000, 200000)


def child_tokens() -> int:
    return _int_env("SUBAGENT_CHILD_TOKENS", 8000, 500, 100000)


def child_seconds() -> int:
    return _int_env("SUBAGENT_SECONDS", 120, 10, 600)


def _semaphore() -> threading.BoundedSemaphore:
    global _sem
    with _sem_lock:
        if _sem is None:
            _sem = threading.BoundedSemaphore(max_parallel())
        return _sem


def _count_children(run_id: str) -> int:
    from core.employees._db import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM agent_runs WHERE parent_id = %s", (str(run_id),))
        n = cur.fetchone()[0]
        cur.close()
        return int(n)
    finally:
        conn.close()


def reservation_limit(parent: Dict) -> int:
    """How many tokens the children of this parent may hold in total (reserved while running, actual once ended): the tree cap
    minus what the parent itself used, and never more than the parent role's own remaining budget. The role ledger is a cost
    counter and fails OPEN (None = no cap); the tree cap always applies."""
    room = tree_tokens() - int((parent or {}).get("tokens_used") or 0)
    try:
        role = (parent or {}).get("role")
        left = budget.remaining(None if role in ("auto", "team") else role)
    except Exception:
        left = None
    return room if left is None else min(room, left)


def reserve_child(parent_id: str, cap: int, limit: int) -> Optional[Dict]:
    """Reserve `cap` tokens and one child slot on the PARENT's row in ONE conditional UPDATE (compare-and-swap in the database).
    Two spawns that look at the same remaining budget cannot both win: the row lock makes the second UPDATE re-check the
    condition against the first one's result. None = refused (slots used up, not enough budget left, or the database could not
    be reached: a spawn limit is a safety counter, so it fails CLOSED). A crash after this call leaves a reservation that only
    lowers what the parent may still spawn and goes away with the parent run."""
    from core.employees._db import get_connection
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE agent_runs SET reserved_tokens = reserved_tokens + %s, spawned = spawned + 1 "
                        "WHERE id = %s AND spawned < %s AND reserved_tokens + %s <= %s "
                        "RETURNING reserved_tokens, spawned", (int(cap), str(parent_id), max_children(), int(cap), int(limit)))
            row = cur.fetchone()
            conn.commit()
            cur.close()
        finally:
            conn.close()
        return {"reserved": int(row[0]), "spawned": int(row[1])} if row else None
    except Exception as e:
        logger.warning("Sub-agent reservation failed (refusing the spawn): %s", e)
        return None


def refund_child(parent_id: str, cap: int) -> None:
    """Give back a reservation whose child row was never written."""
    from core.employees._db import get_connection
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE agent_runs SET reserved_tokens = GREATEST(0, reserved_tokens - %s), spawned = GREATEST(0, spawned - 1) "
                        "WHERE id = %s", (int(cap), str(parent_id)))
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Sub-agent refund failed (the reservation stays; that only lowers the budget): %s", e)


def release_child(child_id: str) -> bool:
    """A child ended: turn its reservation into what it really used. Safe to call twice (the child's own reservation is zeroed
    under a row lock, so only the first call changes the parent). True when a reservation was settled."""
    from core.employees._db import get_connection
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT parent_id, reserved_tokens, tokens_used FROM agent_runs WHERE id = %s AND reserved_tokens > 0 FOR UPDATE",
                        (str(child_id),))
            row = cur.fetchone()
            if not row:
                conn.commit()
                cur.close()
                return False
            parent_id, cap, used = row[0], int(row[1]), int(row[2] or 0)
            cur.execute("UPDATE agent_runs SET reserved_tokens = 0 WHERE id = %s", (str(child_id),))
            cur.execute("UPDATE agent_runs SET reserved_tokens = GREATEST(0, reserved_tokens - %s + %s) WHERE id = %s",
                        (cap, used, str(parent_id)))
            conn.commit()
            cur.close()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Sub-agent reservation release failed (the reservation stays; that only lowers the budget): %s", e)
        return False


def _tree_used(root_id: str) -> int:
    from core.employees._db import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT coalesce(sum(tokens_used), 0) FROM agent_runs WHERE id = %s OR parent_id = %s",
                    (str(root_id), str(root_id)))
        n = cur.fetchone()[0]
        cur.close()
        return int(n)
    finally:
        conn.close()


def _active_roles() -> List[str]:
    from core.employees import roles
    return [r["role"] for r in roles.list_roles(active_only=True)]


def with_delegate(tools: Dict[str, str], run: Dict) -> Dict[str, str]:
    """The tools of a run, plus `delegate` when this run may hand work to a colleague."""
    try:
        if not enabled() or int(run.get("depth") or 0) >= MAX_DEPTH:
            return tools
        if run.get("id") and _count_children(run["id"]) >= max_children():
            return tools
        names = _active_roles()
        if not names:
            return tools
        out = dict(tools)
        out["delegate"] = (
            "Hand ONE self-contained task to a colleague role, who works on it and returns a short result. "
            "target: exactly one of these role names: " + ", ".join(names) + ". "
            f"content: the task in plain words (at most {TASK_CAP} characters) with everything the colleague needs, "
            "because the colleague sees only this text. A colleague cannot hand work off further. "
            f"At most {max_children()} per request."
        )
        return out
    except Exception as e:
        logger.warning("Sub-agent tool check failed (non-fatal): %s", e)
        return tools


class _ToolView(dict):
    """The only fields resolved_tools may read of a run. At spawn it is called with a child that has no row yet, when the child is
    driven with the full row: reading any other field (for instance run["forced"]) would make the two digests differ and refuse
    every in-flight child, so it raises here instead (tests/test_start_record.py)."""
    ALLOWED = ("id", "parent_id", "role", "depth")

    def _check(self, k):
        if k not in self.ALLOWED:
            raise KeyError(f"resolved_tools may only read {self.ALLOWED}, not {k!r}")

    def __getitem__(self, k):
        self._check(k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        self._check(k)
        return dict.get(self, k, default)


def resolved_tools(run: Dict) -> Dict[str, str]:
    """THE tool list of a run. The gate hashes it into a child's signed start record and the loop uses the same function to
    drive the child, so what was signed is what runs. It sees only a four-field view of the run (see _ToolView)."""
    from core.employees import actions
    view = _ToolView({k: (run or {}).get(k) for k in _ToolView.ALLOWED})
    return limit_to_parent(with_delegate(actions.available_tools(), view), view)


def parent_tool_names(parent: Dict) -> set:
    """The tool names a parent run may use. Today every run has the same list; this is the one place to narrow it per run."""
    from core.employees import actions
    return set(actions.available_tools())


def limit_to_parent(tools: Dict[str, str], run: Dict) -> Dict[str, str]:
    """A child's tools are its parent's tools, never more. A top-level run is unchanged. Fails closed (no tools)."""
    parent_id = (run or {}).get("parent_id")
    if not parent_id:
        return tools
    try:
        from core import agent_loop
        parent = agent_loop.get_run(parent_id)
        if parent is None:
            return {}
        allowed = parent_tool_names(parent)
        return {k: v for k, v in tools.items() if k in allowed}
    except Exception as e:
        logger.warning("Sub-agent tool limit failed, giving the child no tools: %s", e)
        return {}


def _children_tokens(parent_id: str, self_id) -> int:
    """Tokens the OTHER children of this parent already spent (they are booked to their own roles, not to the parent's)."""
    from core.employees._db import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT coalesce(sum(tokens_used), 0) FROM agent_runs WHERE parent_id::text = %s AND id::text <> %s",
                    (str(parent_id), str(self_id or "")))
        n = cur.fetchone()[0]
        cur.close()
        return int(n)
    finally:
        conn.close()


def budget_stop(run: Dict) -> bool:
    """True when a child must stop because its PARENT's role budget is spent, so a child draws the smaller of its own
    remaining budget and its parent's. The child's OWN role budget and the global caps need no check here: agent_loop._llm
    checks them before every model call, children included (a test pins that). What the other children of this parent
    already spent is subtracted, because it is booked to their own roles. Token budgets are cost control and fail OPEN,
    like core/budget.py: a ledger that cannot be read stops nobody."""
    pid = (run or {}).get("parent_id")
    if not pid:
        return False
    try:
        from core import agent_loop
        parent = agent_loop.get_run(pid)
        if parent is None:
            return False
        left = budget.remaining(None if parent.get("role") in ("auto", "team") else parent.get("role"))
        if left is None:
            return False
        return left - (_children_tokens(pid, run.get("id")) + int(run.get("tokens_used") or 0)) <= 0
    except Exception as e:
        logger.warning("Sub-agent budget stop check failed (not stopping): %s", e)
        return False


def _other_steps(root_id: str, self_id) -> int:
    """Steps taken by the other runs of this tree (the root and its children)."""
    from core.employees._db import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT coalesce(sum(jsonb_array_length(steps)), 0) FROM agent_runs "
                    "WHERE (id::text = %s OR parent_id::text = %s) AND id::text <> %s", (str(root_id), str(root_id), str(self_id or "")))
        n = cur.fetchone()[0]
        cur.close()
        return int(n)
    finally:
        conn.close()


def steps_exhausted(run: Dict, limit: int) -> bool:
    """True when this run, or its whole run tree, has used the step limit: delegating does not multiply the steps.
    A step limit is an action cap, so it fails CLOSED: a tree whose count cannot be read stops."""
    own = len(run.get("steps") or [])
    if own >= limit:
        return True
    parent = run.get("parent_id")
    if not parent and not any(s.get("tool") == "delegate" for s in run.get("steps", [])):
        return False
    try:
        root = parent or run.get("id")
        return bool(root) and own + _other_steps(root, run.get("id")) >= limit
    except Exception as e:
        logger.warning("Sub-agent step check failed (stopping): %s", e)
        return True


def over_budget(run: Dict) -> bool:
    """True when this run (a child, or a parent that has delegated) must stop because a token cap was reached."""
    try:
        parent = run.get("parent_id")
        delegated = any(s.get("tool") == "delegate" for s in run.get("steps", []))
        if not parent and not delegated:
            return False
        if parent and int(run.get("tokens_used") or 0) >= child_tokens():
            return True
        root = parent or run.get("id")
        return bool(root) and _tree_used(root) >= tree_tokens()
    except Exception as e:
        logger.warning("Sub-agent budget check failed (not stopping): %s", e)
        return False


def _emit(event: str, **fields) -> None:
    try:
        from core import audit
        audit.emit(event, **fields)
    except Exception:
        pass


def _apply_child(parent: Dict, step: Dict, child: Optional[Dict]) -> None:
    from core import agent_loop
    st = (child or {}).get("status", "failed")
    step["status"] = "executed" if st == "done" else "failed"
    text = ((child or {}).get("summary") or f"The sub-agent stopped ({st}).")
    step["observation"] = f"Sub-agent {step.get('target')} finished ({st}): {text}"[:agent_loop.OBS_CAP]
    if child and child.get("tainted"):
        parent["tainted"] = True


def run_delegation(parent: Dict, service, role: Optional[str], target: str, task: str, auth: Dict) -> Optional[Dict]:
    """Start and drive one child for a parent run. NOT to be called directly: core.autonomy.execute_delegation (the gate) is the
    only caller (a tripwire test and the boundary scan enforce it) and passes the decision `auth` it got from authorize_spawn.
    Returns None when the parent should go on with its next step, or an outcome dict when the parent must stop (finished, or
    waiting because the child needs an approval)."""
    from core import agent_loop, autonomy
    if not isinstance(auth, dict) or auth.get("allowed") is not True or not auth.get("spawn_id"):
        return agent_loop._finish(parent, service, role, "done", "The proposed sub-agent task was not allowed.")      # defence in depth
    sig = ["delegate", target, hashlib.sha256(task.encode()).hexdigest()[:16]]
    if any(s.get("sig") == sig for s in parent["steps"]):
        return agent_loop._finish(parent, service, role, "done", "Stopped: the same action was proposed twice.")
    if over_budget(parent):
        return agent_loop._finish(parent, None, role, "budget", "Stopped: the sub-agent token cap was reached.")
    if parent.get("id") is None:
        agent_loop._save(parent)
    import uuid
    child_id = str(uuid.uuid4())
    child_depth = int(parent.get("depth") or 0) + 1
    child_tools = resolved_tools({"id": child_id, "parent_id": parent["id"], "role": target, "depth": child_depth})
    if not child_tools:                                  # an explicit check with its own reason, not a missing record
        _emit("subagent_denied", parent_run=parent["id"], role=target, reason="no_tools")
        return agent_loop._finish(parent, service, role, "done", "Stopped: this sub-agent would have no tools, so it was not started.")
    unknown = autonomy.unresolved_tools(child_tools)
    if unknown:
        _emit("subagent_denied", parent_run=parent["id"], role=target, reason="unknown_tool_handler", tools=",".join(unknown))
        return agent_loop._finish(parent, service, role, "done", "Stopped: a tool of this sub-agent has no known handler, so it was not started.")
    child_query = task[:agent_loop.TEXT_CAP]
    child_tainted = bool(parent["tainted"])
    record = autonomy.spawn_record(auth.get("spawn_id"), child_id, parent["id"], target, child_depth, auth["force_approval"],
                                   autonomy.tools_digest(child_tools), autonomy.task_digest(target, child_query), child_tainted)
    if record is None:
        _emit("subagent_denied", parent_run=parent["id"], role=target, reason="no_record")
        return agent_loop._finish(parent, service, role, "done", "Stopped: no start record could be made for this sub-agent.")
    step = {"tool": "delegate", "target": target, "content": task[:600], "sig": sig, "status": "running",
            "forced_approval": False, "observation": "", "child_run_id": None}
    sem = _semaphore()
    if not sem.acquire(timeout=CAPACITY_WAIT_SECONDS):
        step["status"] = "failed"
        step["observation"] = "Sub-agent capacity is busy right now; try again later."
        parent["steps"].append(step)
        agent_loop._save(parent)
        return None
    try:
        cap = child_tokens()
        if reserve_child(parent["id"], cap, reservation_limit(parent)) is None:       # P1-B: one atomic UPDATE, before anything is created
            if _count_children(parent["id"]) >= max_children():
                return agent_loop._finish(parent, service, role, "done", f"Stopped: the limit of {max_children()} sub-agents was reached.")
            return agent_loop._finish(parent, None, role, "budget", "Stopped: the sub-agent token budget is fully reserved.")
        child = {"id": None, "role": target, "query": child_query, "context_answer": "", "status": "running",
                 "steps": [], "tainted": child_tainted, "pending_action_id": None, "summary": "",
                 "parent_id": parent["id"], "depth": int(parent.get("depth") or 0) + 1, "tokens_used": 0,
                 "deadline": time.time() + child_seconds()}
        try:
            agent_loop._save_child(child, record, reserved=cap)
        except Exception:
            refund_child(parent["id"], cap)
            raise
        step["child_run_id"] = child["id"]
        parent["steps"].append(step)
        agent_loop._save(parent)
        _emit("subagent_started", parent_run=parent["id"], child_run=child["id"], role=target, depth=child["depth"], spawn_id=record["spawn_id"])
        previous = budget.get_role()
        try:
            budget.set_role(None if target in ("auto", "team") else target)
            agent_loop._drive(child, service, target)
        finally:
            budget.set_role(previous)
    finally:
        sem.release()
    fresh = agent_loop.get_run(child["id"])
    if not fresh or fresh["status"] != "waiting_approval":
        release_child(child["id"])           # idempotent: normally the child's own end already settled it
    if fresh and fresh["status"] == "waiting_approval":
        step["status"] = "waiting_child"
        parent["status"] = "waiting_approval"
        parent["pending_action_id"] = None
        agent_loop._save(parent)
        out = agent_loop._outcome(parent)
        out["action_id"] = fresh.get("pending_action_id")
        return out
    _apply_child(parent, step, fresh)
    agent_loop._save(parent)
    _emit("subagent_finished", parent_run=parent["id"], child_run=child["id"], status=(fresh or {}).get("status"))
    return None


def on_child_ended(child: Dict) -> None:
    """A child run ended. If its parent was put on hold for it (the child needed an approval), the parent goes on."""
    try:
        from core import agent_loop
        if child.get("id"):
            release_child(child["id"])       # settle the child's budget reservation (P1-B), whatever happens to the parent
        parent = agent_loop.get_run(child["parent_id"]) if child.get("parent_id") else None
        if not parent or parent["status"] != "waiting_approval":
            return
        step = next((s for s in reversed(parent["steps"])
                     if s.get("tool") == "delegate" and s.get("child_run_id") == child["id"]), None)
        if step is None or step.get("status") != "waiting_child":
            return
        _apply_child(parent, step, agent_loop.get_run(child["id"]))
        parent["status"] = "running"
        parent["pending_action_id"] = None
        agent_loop._save(parent)
        _emit("subagent_finished", parent_run=parent["id"], child_run=child["id"], status=child.get("status"))
        role = parent.get("role")
        if not agent_loop.enabled():
            agent_loop._finish(parent, None, role, "done", "The agent loop was switched off, so the run stopped here.")
            return

        def go():
            try:
                budget.set_role(None if role in ("auto", "team") else role)
                agent_loop._drive(parent, agent_loop._service(), role)
            except Exception as e:
                logger.warning("Parent run could not be resumed (non-fatal): %s", e)
                try:
                    agent_loop._finish(parent, None, role, "failed", "Stopped: the run could not be resumed.")
                except Exception:
                    pass
        agent_loop._spawn(go)
    except Exception as e:
        logger.warning("Sub-agent follow-up failed (non-fatal): %s", e)
