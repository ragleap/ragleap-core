"""ragleap_flows.runner - run a validated Flow for one tenant, with durable approval checkpoints.

Multi-tenant from the first line (the same rules as ragleap-agents 0.5.1):
- every call takes a ragleap_security Scope; there is no unscoped run. Single-tenant use is one fixed scope.
- a run records its scope key; resume() continues it only for exactly that scope (every part). A wrong
  scope, an unknown run id and a damaged stored state all raise the same ResumeError("unknown run"), and
  ownership is checked before anything else is read.
- run ids are 128-bit random tokens, so they cannot be guessed or counted.
- audit events hold the scope, ids, step ids, tool names and sizes, never input, arguments or results.
  If the audit record cannot be written the error is raised and nothing continues.
- store_for(tenant) lets the host keep one store per tenant (stronger separation than one shared store).

What this does NOT do: it does not authenticate anybody (pass only a Scope you have established), it cannot
make a tool that reads another tenant's data by itself safe, and a run that crashes while "running" stays
"running" (it is not resumed automatically in 0.1.0).
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ragleap_agents import InMemoryStateStore, ResumeError, StateStore
from ragleap_security import AuditLog, RateLimitError, Scope
from ragleap_security import require_scope as _require_scope

from ragleap_flows.flow import (DEADLINE_SECONDS, MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, MAX_STATE_BYTES, Flow, FlowError,
                                Missing, eval_cond, resolve_template)

logger = logging.getLogger(__name__)
_ERROR_CAP = 200


@dataclass
class FlowResult:
    run_id: str
    status: str  # done | awaiting_approval | rejected | failed | stopped
    stop_reason: str
    output: Any
    steps: List[Dict[str, Any]]  # [{"step", "type", "status"}] in the order they ran
    pending: Optional[Dict[str, Any]]  # {"step", "prompt", "show", "forced"} when awaiting approval
    tainted: bool = False


class _TooLarge(Exception):
    pass


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii"))


class FlowRunner:
    """Runs a Flow. flow: a ragleap_flows.Flow. tools: a ragleap_tools Toolkit (tool steps call
    tools.call(name, arguments)). dispatch: instead of tools, a callable dispatch(scope, name, arguments)
    that returns a ToolResult (a GovernedToolkit's .call has exactly this shape). store: any ragleap_agents
    StateStore (default in memory). store_for: store_for(tenant) returns the store for that tenant.
    audit: a ragleap_security AuditLog. rate_limiter: anything with check(scope) -> Decision."""

    def __init__(
        self,
        flow: Flow,
        tools: Any = None,
        *,
        dispatch: Optional[Callable[[Any, str, Dict[str, Any]], Any]] = None,
        store: Optional[StateStore] = None,
        store_for: Optional[Callable[[str], StateStore]] = None,
        audit: Optional[AuditLog] = None,
        rate_limiter: Optional[Any] = None,
        deadline_seconds: Optional[float] = DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(flow, Flow):
            raise FlowError("flow must be a ragleap_flows.Flow (use from_json or from_dict)")
        if tools is not None and dispatch is not None:
            raise FlowError("give tools or dispatch, not both")
        if flow.has_tool_steps and tools is None and dispatch is None:
            raise FlowError("this flow has tool steps: give tools or dispatch")
        if tools is not None:
            if not callable(getattr(tools, "call", None)) or not hasattr(tools, "__contains__"):
                raise FlowError("tools must be a ragleap_tools Toolkit")
            for sid, s in flow.steps.items():
                if s["type"] == "tool" and s["tool"] not in tools:
                    raise FlowError("step %r names a tool that is not in the toolkit" % sid)
        if dispatch is not None and not callable(dispatch):
            raise FlowError("dispatch must be callable")
        if store is not None and store_for is not None:
            raise FlowError("give store or store_for, not both")
        if store_for is not None and not callable(store_for):
            raise FlowError("store_for must be callable")
        if audit is not None and not isinstance(audit, AuditLog):
            raise FlowError("audit must be a ragleap_security AuditLog")
        if rate_limiter is not None and not callable(getattr(rate_limiter, "check", None)):
            raise FlowError("rate_limiter must have a check(scope) method")
        if deadline_seconds is not None and (isinstance(deadline_seconds, bool)
                                             or not isinstance(deadline_seconds, (int, float)) or deadline_seconds <= 0):
            raise FlowError("deadline_seconds must be a positive number or None")
        if not callable(clock):
            raise FlowError("clock must be callable")
        self.flow = flow
        self._tools = tools
        self._dispatch = dispatch
        self._store: StateStore = store if store is not None else InMemoryStateStore()
        self._store_for = store_for
        self.audit = audit
        self.rate_limiter = rate_limiter
        self.deadline_seconds = deadline_seconds
        self._clock = clock

    # ---- public ----

    def run(self, scope: Scope, input: Any = None, *, tainted: bool = False) -> FlowResult:
        """Start a run for `scope` (exactly a ragleap_security Scope). input is plain JSON (default {}), at most
        64 KiB. tainted=True says the input comes from an untrusted source: every outbound tool step then needs
        an approval first. A rate limiter, if set, is checked before anything else and raises RateLimitError."""
        scope = _require_scope(scope)
        if not isinstance(tainted, bool):
            raise ValueError("tainted must be True or False")
        data = {} if input is None else input
        try:
            if _json_size(data) > MAX_INPUT_BYTES:
                raise ValueError("input is too large (max %d bytes)" % MAX_INPUT_BYTES)
        except (TypeError, ValueError, RecursionError) as e:
            raise ValueError(str(e) if "too large" in str(e) else "input must be plain JSON") from None
        self._limit(scope)
        store = self._store_of(scope)
        state: Dict[str, Any] = {
            "run_id": secrets.token_urlsafe(16),
            "scope": scope.key,
            "flow": self.flow.digest,
            "status": "running",
            "stop_reason": "",
            "input": data,
            "outputs": {},
            "steps": [],
            "approved": [],
            "current": self.flow.start,
            "pending": None,
            "tainted": tainted,
        }
        self._audit(state, "flow.start", tainted=tainted, input_bytes=_json_size(data))
        try:
            return self._drive(state, store, scope, self._deadline())
        except _TooLarge:
            return self._finish(state, store, "failed", "state_too_large")
        except Exception as e:  # noqa: BLE001 - last resort; step errors are handled where they happen
            logger.error("flow run failed unexpectedly: %s", type(e).__name__)
            return self._finish(state, store, "failed", "internal_error")

    def resume(self, scope: Scope, run_id: str, decisions: Dict[str, bool]) -> FlowResult:
        """Continue a run that is awaiting approval. decisions maps the pending step id to True or False.
        A wrong scope, an unknown run and a damaged stored state all raise ResumeError("unknown run")."""
        scope = _require_scope(scope)
        self._limit(scope)
        store = self._store_of(scope)
        try:
            state = store.load(run_id)
        except (ValueError, TypeError):
            raise ResumeError("unknown run") from None
        if not isinstance(state, dict) or not self._owns(state, scope):
            raise ResumeError("unknown run")
        if state.get("flow") != self.flow.digest:
            raise ResumeError("this run belongs to a different version of the flow")
        pending = state.get("pending")
        if state.get("status") != "awaiting_approval" or not pending:
            raise ResumeError("run is not awaiting approval")
        step_id = pending.get("step") if isinstance(pending, dict) else None
        if step_id not in self.flow.steps:
            raise ResumeError("unknown run")
        if not isinstance(decisions, dict) or step_id not in decisions:
            raise ResumeError("no decision for the pending step")
        if set(decisions) != {step_id}:
            raise ResumeError("decision for a step that is not pending")
        approved = decisions[step_id]
        if not isinstance(approved, bool):
            raise ResumeError("a decision must be True or False")
        state["status"] = "running"  # claim the run so a second resume() is refused
        state["pending"] = None
        claim = getattr(store, "save_if_status", None)
        if claim is not None:
            if not claim(state["run_id"], state, "awaiting_approval"):
                raise ResumeError("run is not awaiting approval")
        else:
            store.save(state["run_id"], state)
        try:
            self._audit(state, "approval.decided", step=step_id, approved=approved)
            if not approved:
                state["steps"].append({"step": step_id, "type": self.flow.steps[step_id]["type"], "status": "rejected"})
                return self._finish(state, store, "rejected", "rejected")
            state["approved"].append(step_id)
            return self._drive(state, store, scope, self._deadline())
        except _TooLarge:
            return self._finish(state, store, "failed", "state_too_large")
        except Exception as e:  # noqa: BLE001
            logger.error("flow resume failed unexpectedly: %s", type(e).__name__)
            return self._finish(state, store, "failed", "internal_error")

    # ---- loop ----

    def _deadline(self) -> Optional[float]:
        return None if self.deadline_seconds is None else self._clock() + self.deadline_seconds

    def _drive(self, state: Dict[str, Any], store: Any, scope: Scope, deadline: Optional[float]) -> FlowResult:
        flow = self.flow
        while state["current"] is not None:
            if deadline is not None and self._clock() > deadline:
                return self._finish(state, store, "stopped", "deadline")
            if len(state["steps"]) > len(flow.steps):  # cannot happen in an acyclic flow: defence in depth
                return self._finish(state, store, "failed", "step_cap")
            sid = state["current"]
            step = flow.steps[sid]
            values = {"input": state["input"], **state["outputs"]}
            typ = step["type"]
            if typ == "branch":
                nxt = step.get("else")
                for case in step["cases"]:
                    if eval_cond(case["when"], values):
                        nxt = case["goto"]
                        break
                state["steps"].append({"step": sid, "type": typ, "status": "ok"})
                state["current"] = nxt
            elif typ == "approval":
                if sid not in state["approved"]:
                    try:
                        show = resolve_template(step.get("show", {}), values)
                    except Missing:
                        return self._finish(state, store, "failed", "missing_value")
                    return self._pause(state, store, sid, step["prompt"], show, forced=False)
                state["steps"].append({"step": sid, "type": typ, "status": "approved"})
                state["current"] = step.get("next")
            else:  # tool
                if step.get("outbound") and state["tainted"] and sid not in state["approved"]:
                    return self._pause(state, store, sid, "outbound tool step after untrusted input", None, forced=True)
                try:
                    args = resolve_template(step.get("arguments", {}), values)
                except Missing:
                    state["steps"].append({"step": sid, "type": typ, "status": "failed"})
                    return self._finish(state, store, "failed", "missing_value")
                ok, value, err = self._call(scope, step["tool"], args)
                if ok:
                    try:
                        size = _json_size(value)
                    except (TypeError, ValueError, RecursionError):
                        ok, err = False, "output_not_json"
                    else:
                        if size > MAX_OUTPUT_BYTES:
                            ok, err = False, "output_too_large"
                if not ok:
                    state["steps"].append({"step": sid, "type": typ, "status": "failed", "error": (err or "")[:_ERROR_CAP]})
                    self._audit(state, "step.executed", step=sid, type=typ, tool=step["tool"], ok=False)
                    return self._finish(state, store, "failed", "tool_failed:" + sid)
                state["outputs"][sid] = value
                if step.get("external"):
                    state["tainted"] = True
                state["steps"].append({"step": sid, "type": typ, "status": "ok"})
                self._audit(state, "step.executed", step=sid, type=typ, tool=step["tool"], ok=True, output_bytes=size)
                state["current"] = step.get("next")
            self._save(state, store)
        return self._finish(state, store, "done", "completed")

    def _call(self, scope: Scope, name: str, args: Dict[str, Any]) -> "tuple[bool, Any, Optional[str]]":
        try:
            if self._dispatch is not None:
                res = self._dispatch(scope, name, args)
            else:
                res = self._tools.call(name, args)
        except Exception as e:  # noqa: BLE001 - only the type reaches the result
            logger.warning("tool step raised %s", type(e).__name__)
            return False, None, "tool raised " + type(e).__name__
        if not hasattr(res, "success") or not hasattr(res, "result"):
            return False, None, "tool returned an invalid result"
        if res.success is not True:
            return False, None, str(getattr(res, "error", "") or "tool failed")
        return True, res.result, None

    # ---- state ----

    def _pause(self, state: Dict[str, Any], store: Any, sid: str, prompt: str, show: Any, forced: bool) -> FlowResult:
        state["status"] = "awaiting_approval"
        state["pending"] = {"step": sid, "prompt": prompt, "show": show, "forced": forced}
        self._audit(state, "approval.requested", step=sid, forced=forced)
        self._save(state, store)
        return self._result(state)

    def _save(self, state: Dict[str, Any], store: Any) -> None:
        if _json_size(state) > MAX_STATE_BYTES:
            raise _TooLarge()
        store.save(state["run_id"], state)

    def _finish(self, state: Dict[str, Any], store: Any, status: str, reason: str) -> FlowResult:
        state["status"] = status
        state["stop_reason"] = reason
        state["pending"] = None
        state["current"] = None if status != "stopped" else state["current"]
        if status == "done" and self.flow.output is not None:
            try:
                state["final"] = resolve_template(self.flow.output, {"input": state["input"], **state["outputs"]})
                if _json_size(state["final"]) > MAX_OUTPUT_BYTES:
                    state["final"], state["stop_reason"] = None, "output_unavailable"
            except Missing:
                state["final"], state["stop_reason"] = None, "output_unavailable"
        self._audit(state, "flow.end", status=status, reason=reason.split(":")[0], steps=len(state["steps"]))
        try:
            self._save(state, store)
        except _TooLarge:
            state["outputs"], state["input"], state["final"] = {}, {}, None
            store.save(state["run_id"], state)
        return self._result(state)

    @staticmethod
    def _result(state: Dict[str, Any]) -> FlowResult:
        return FlowResult(run_id=state["run_id"], status=state["status"], stop_reason=state["stop_reason"],
                          output=state.get("final"), steps=[dict(s) for s in state["steps"]],
                          pending=state["pending"], tainted=state["tainted"])

    def _store_of(self, scope: Scope) -> Any:
        if self._store_for is None:
            return self._store
        store = self._store_for(scope.tenant)
        if not callable(getattr(store, "load", None)) or not callable(getattr(store, "save", None)):
            raise FlowError("store_for must return a state store")
        return store

    def _limit(self, scope: Scope) -> None:
        if self.rate_limiter is None:
            return
        decision = self.rate_limiter.check(scope)
        if not decision.allowed:
            raise RateLimitError("rate limit reached", decision.retry_after, decision.reason or "limited")

    @staticmethod
    def _owns(state: Dict[str, Any], scope: Scope) -> bool:
        stored = state.get("scope")
        return isinstance(stored, str) and hmac.compare_digest(stored.encode("utf-8"), scope.key.encode("utf-8"))

    def _audit(self, state: Dict[str, Any], event: str, **data: Any) -> None:
        if self.audit is None:
            return
        self.audit.append(event, {"run_id": state["run_id"], **data}, Scope.parse(state["scope"]))
