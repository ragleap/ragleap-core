"""Review findings: attempts to break observation.py, tools.py and mcp_server.py.

Every test here is a FINDING that states the risk in one sentence. Each one failed on the code before the hardening
change and passes now; they stay as regression tests.
"""
import http.client

import pytest

from ragleap_physical import PhysicalError
from ragleap_physical.errors import ValidationError
from ragleap_physical.observation import MAX_ENTITIES, MAX_SENSORS, Observation, actor, finite, ident

from helpers import make, propose
from test_mcp_server import Running, post, rpc, setup
from test_tools import args

SOURCE = {"adapter": "a", "adapter_version": "1", "engine_version": "1", "model_id": "m", "model_sha256": "x"}


def raw(**kw):
    d = {"source": SOURCE, "sim_time_s": 0.0, "step": 0, "seed": 0, "entities": [], "sensors": []}
    d.update(kw)
    return d


def entity(i, pos=0.0):
    return {"id": i, "kind": "joint", "pos": pos, "vel": 0.0, "pos_unit": "m", "vel_unit": "m/s"}


def from_raw(**kw):
    return Observation.from_raw(raw(**kw), org_id="org_demo", observation_id="obs_000001")


# ---- observation.py
def test_finding_1a_an_identifier_with_a_trailing_newline_is_refused():
    """Risk: ident() and actor() accept 'abc\\n' (the $ in the pattern matches before a final newline), so an id
    that looks safe can carry a hidden line break into logs and comparisons."""
    for fn, value in ((ident, "joint_1\n"), (actor, "person:alice\n")):
        with pytest.raises(ValidationError):
            fn(value)


def test_finding_1b_an_approver_name_with_a_trailing_newline_is_refused():
    """Risk: a person id such as 'person:alice\\n' is accepted by approve() and written into the audit log as
    typed, which can split or forge a line in a line-based log."""
    b, _, _ = make()
    p = propose(b)
    with pytest.raises(PhysicalError):
        b.approve(p, "person:alice\n")
    assert all(not str(r.get("approver", "")).endswith("\n") for r in b.audit)


def test_finding_2a_a_huge_integer_is_a_validation_error():
    """Risk: finite(10**400) raises OverflowError, not ValidationError, so code that catches only the project's
    errors lets an unexpected exception through."""
    with pytest.raises(ValidationError):
        finite(10 ** 400)


def test_finding_2b_a_huge_integer_from_a_device_drops_one_item_not_the_whole_observation():
    """Risk: one sensor value above the float range makes Observation.from_raw raise OverflowError, so a single
    untrusted device value takes down the whole read instead of being dropped and counted like other bad items."""
    obs = from_raw(sensors=[{"id": "s1", "kind": "k", "value": 10 ** 400, "unit": "1"},
                            {"id": "s2", "kind": "k", "value": 1.0, "unit": "1"}])
    assert obs.dropped == 1 and [s.id for s in obs.sensors] == ["s2"]


def test_finding_2c_a_huge_integer_in_a_proposal_is_reported_as_an_invalid_value():
    """Risk: the model-facing tool answers 'internal_error' for value=10**400, which hides a plain input mistake
    behind a message that tells the model and the operator nothing."""
    t, _, _ = setup()
    r = t.call("physical_propose_action", args(value=10 ** 400))
    assert r["ok"] is False and r["error"]["code"] == "invalid_value"


def test_finding_3_bad_items_count_against_the_size_cap():
    """Risk: the size cap counts accepted items only, so a device can make from_raw read an unbounded number of
    invalid items (here 100000) and still report truncated=False."""
    for key, limit in (("entities", MAX_ENTITIES), ("sensors", MAX_SENSORS)):
        seen = {"n": 0}

        class Endless(list):
            def __iter__(self):
                for _ in range(100_000):
                    seen["n"] += 1
                    yield {"id": "bad id", "kind": "k", "pos": 0, "vel": 0, "pos_unit": "m", "vel_unit": "m/s",
                           "value": 0, "unit": "1"}

        obs = from_raw(**{key: Endless([1])})
        assert seen["n"] <= 8 * limit and obs.truncated is True, key


def test_finding_4_two_entities_with_the_same_id_are_not_both_kept():
    """Risk: a device can report the same id twice with different positions; the model is shown both, but
    Observation.entity(id) silently returns only the first, so the two views of the state disagree."""
    obs = from_raw(entities=[entity("j", 0.1), entity("j", 9.0)])
    assert len([e for e in obs.entities if e.id == "j"]) <= 1


# ---- tools.py
def test_finding_5_a_reason_with_terminal_or_bidi_control_characters_is_refused():
    """Risk: the free-text reason is shown to the person who approves; an ESC sequence or a bidi override in it can
    redraw or reorder what that person reads (plain text and newlines can stay allowed)."""
    t, _, _ = setup()
    for bad in ("ok\x1b[2J", "ok\u202e", "ok\x00", "ok\u2066"):
        r = t.call("physical_propose_action", args(reason=bad))
        assert r["ok"] is False, repr(bad)


# ---- mcp_server.py
def test_finding_6_deeply_nested_json_gets_a_clean_error_answer():
    """Risk: a body under the 64 KB cap with 30000 nested brackets makes json.loads raise RecursionError, which the
    handler does not catch: the client gets no answer (connection dropped) and a traceback goes to stderr."""
    t, _, _ = setup()
    with Running(t) as s:
        port = s.server.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/mcp", body=b"[" * 30000 + b"]" * 30000, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        assert r.status in (400, 413)
        assert post(s.url, rpc("ping"))[0] == 200  # and the server still answers afterwards
