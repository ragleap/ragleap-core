"""The operator console: a named person approves the exact proposal. Standard library only, loopback only."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from ragleap_physical import Capability, ConfigError, PhysicalTools
from ragleap_physical.console import MAX_BODY, make_console

from helpers import make, propose

ALICE, BOB = "a" * 24, "b" * 24
TOKENS = {ALICE: "person:alice", BOB: "person:bob"}


class Running:
    def __init__(self, bridge, tokens=TOKENS, **kw):
        self.server = make_console(bridge, tokens, **kw)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


def call(base, path, body=None, token=ALICE, method=None, headers=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    h = {"Content-Type": "application/json"}
    if token is not None:
        h["Authorization"] = "Bearer " + token
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=data, headers=h, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, (json.loads(body) if body else None)


def setup():
    b, adapter, clock = make()
    return b, adapter, clock


def digest_of(base, pid):
    return call(base, f"/v1/proposals/{pid}")[1]["digest"]


# ---- configuration
def test_configuration_is_strict():
    b, _, _ = setup()
    bad = [dict(tokens={}), dict(tokens={"short": "person:alice"}), dict(tokens={ALICE: "alice"}),
           dict(tokens={ALICE: "employee:ops"}), dict(tokens={ALICE: "person:alice\n"}),
           dict(tokens={ALICE: "person:alice", BOB: "person:alice"}), dict(tokens=None)]
    for kw in bad:
        with pytest.raises(ConfigError):
            make_console(b, **kw)
    for host in ("0.0.0.0", "192.168.1.5", "example.org", ""):
        with pytest.raises(ConfigError):
            make_console(b, TOKENS, host=host)


# ---- the happy path: a person approves the exact proposal, then it is applied and verified
def test_person_approves_then_applies_and_the_outcome_is_verified():
    b, adapter, _ = setup()
    p = propose(b)
    with Running(b) as s:
        st, body = call(s.base, "/v1/pending")
        assert st == 200 and [x["proposal_id"] for x in body["pending"]] == [p.proposal_id]
        assert adapter.observe_raw()["entities"][0]["pos"] == 0.0  # nothing moved yet
        st, body = call(s.base, "/v1/approve", {"proposal_id": p.proposal_id, "digest": p.digest()})
        assert st == 200 and body["approver"] == "person:alice" and body["digest"] == p.digest()
        st, body = call(s.base, "/v1/apply", {"proposal_id": p.proposal_id})
        assert st == 200 and body["outcome_ok"] is True and body["proposal_digest"] == p.digest()
        assert call(s.base, "/v1/pending")[1]["pending"] == []
    assert any(r.get("approver") == "person:alice" for r in b.audit)


def test_the_approver_comes_from_the_token_never_from_the_request():
    b, _, _ = setup()
    p = propose(b)
    with Running(b) as s:
        st, _ = call(s.base, "/v1/approve", {"proposal_id": p.proposal_id, "digest": p.digest(), "approver": "person:mallory"})
        assert st == 400
        st, body = call(s.base, "/v1/approve", {"proposal_id": p.proposal_id, "digest": p.digest()}, token=BOB)
        assert st == 200 and body["approver"] == "person:bob"


# ---- nothing happens without a valid token, digest and state
def test_no_token_or_a_wrong_token_is_refused_everywhere():
    b, adapter, _ = setup()
    p = propose(b)
    with Running(b) as s:
        for path, body in (("/v1/pending", None), (f"/v1/proposals/{p.proposal_id}", None),
                           ("/v1/approve", {"proposal_id": p.proposal_id, "digest": p.digest()}),
                           ("/v1/apply", {"proposal_id": p.proposal_id}), ("/v1/stop", {}), ("/v1/clear-stop", {})):
            for tok in (None, "x" * 24, ALICE + "x", ALICE[:-1], "", "Bearer", "zz" + ALICE, " " + ALICE):
                assert call(s.base, path, body, token=tok)[0] == 401, (path, tok)
            for header in (ALICE, "Basic " + ALICE, "bearer " + ALICE, "Bearer  " + ALICE, "Token " + ALICE):
                assert call(s.base, path, body, token=None, headers={"Authorization": header})[0] == 401, (path, header)
    assert adapter.observe_raw()["entities"][0]["pos"] == 0.0


def test_a_wrong_digest_is_refused_and_nothing_is_applied():
    b, adapter, _ = setup()
    p = propose(b)
    with Running(b) as s:
        for bad in ("0" * 64, p.digest()[:-1] + ("0" if p.digest()[-1] != "0" else "1"), "x", p.digest().upper(), 5, None):
            st, body = call(s.base, "/v1/approve", {"proposal_id": p.proposal_id, "digest": bad})
            assert st in (400, 409), bad
        st, body = call(s.base, "/v1/apply", {"proposal_id": p.proposal_id})
        assert st == 409  # never approved
    assert adapter.observe_raw()["entities"][0]["pos"] == 0.0


def test_approval_is_single_use_and_apply_is_single_use():
    b, _, _ = setup()
    p = propose(b)
    with Running(b) as s:
        ok = {"proposal_id": p.proposal_id, "digest": p.digest()}
        assert call(s.base, "/v1/approve", ok)[0] == 200
        assert call(s.base, "/v1/approve", ok)[0] == 409
        assert call(s.base, "/v1/apply", {"proposal_id": p.proposal_id})[0] == 200
        assert call(s.base, "/v1/apply", {"proposal_id": p.proposal_id})[0] == 409


def test_an_expired_proposal_cannot_be_approved():
    b, _, clock = setup()
    p = propose(b)
    clock.t += 10_000
    with Running(b) as s:
        assert call(s.base, "/v1/pending")[1]["pending"] == []
        assert call(s.base, "/v1/approve", {"proposal_id": p.proposal_id, "digest": p.digest()})[0] == 409


def test_unknown_and_malformed_proposal_ids_are_refused():
    b, _, _ = setup()
    with Running(b) as s:
        for pid in ("prop_999999", "../etc", "prop_1", "", None, 5, ["prop_000001"], "prop_000001\n"):
            assert call(s.base, "/v1/approve", {"proposal_id": pid, "digest": "0" * 64})[0] in (400, 409), pid
        assert call(s.base, "/v1/proposals/prop_999999")[0] == 409
        assert call(s.base, "/v1/proposals/../x")[0] in (404, 400)


# ---- stop
def test_stop_blocks_apply_and_needs_no_approval():
    b, adapter, _ = setup()
    p = propose(b)
    with Running(b) as s:
        assert call(s.base, "/v1/approve", {"proposal_id": p.proposal_id, "digest": p.digest()})[0] == 200
        assert call(s.base, "/v1/stop", {})[1] == {"stopped": True}
        assert call(s.base, "/v1/apply", {"proposal_id": p.proposal_id})[0] == 423
        assert adapter.observe_raw()["entities"][0]["pos"] == 0.0
        assert call(s.base, "/v1/clear-stop", {})[1] == {"stopped": False}


def test_stop_takes_no_fields():
    b, _, _ = setup()
    with Running(b) as s:
        assert call(s.base, "/v1/stop", {"x": 1})[0] == 400


# ---- the HTTP layer
def test_host_origin_size_type_and_method_checks():
    b, _, _ = setup()
    with Running(b) as s:
        assert call(s.base, "/v1/pending", headers={"Host": "evil.example"})[0] == 403
        assert call(s.base, "/v1/pending", headers={"Origin": "http://evil.example"})[0] == 403
        assert call(s.base, "/v1/stop", raw=b"x" * (MAX_BODY + 1))[0] == 413
        assert call(s.base, "/v1/stop", {}, headers={"Content-Type": "text/plain"})[0] in (200, 415)  # empty-object body is {}
        assert call(s.base, "/v1/approve", raw=b'{"proposal_id": 1}', headers={"Content-Type": "text/plain"})[0] == 415
        assert call(s.base, "/v1/pending", method="DELETE", raw=b"")[0] == 405
        assert call(s.base, "/v1/nothing")[0] == 404
        assert call(s.base, "/v1/approve", raw=b"[]")[0] == 400
        assert call(s.base, "/v1/approve", raw=b"not json")[0] == 400
        assert call(s.base, "/v1/approve", raw=b"[" * 2000 + b"]" * 2000)[0] == 400
        assert call(s.base, "/v1/pending")[0] == 200  # still alive


def test_allowed_origin_is_accepted():
    b, _, _ = setup()
    with Running(b, allowed_origins=["http://localhost:3000"]) as s:
        assert call(s.base, "/v1/pending", headers={"Origin": "http://localhost:3000"})[0] == 200


def test_errors_do_not_leak_tokens_or_internals():
    b, _, _ = setup()
    with Running(b) as s:
        st, body = call(s.base, "/v1/approve", {"proposal_id": "prop_000001", "digest": "0" * 64})
        text = json.dumps(body)
        assert ALICE not in text and BOB not in text and "Traceback" not in text and "File " not in text


# ---- the console is not a tool
def test_the_four_model_tools_are_unchanged_and_cannot_reach_the_console():
    b, _, _ = setup()
    names = sorted(t["name"] for t in PhysicalTools(b).list_tools())
    assert names == ["physical_get_envelope", "physical_get_observation", "physical_list_sources", "physical_propose_action"]


def test_an_unexpected_internal_error_does_not_leak_its_details():
    b, _, _ = setup()

    def boom():
        raise RuntimeError("secret-detail /root/path")

    b.pending_proposals = boom
    with Running(b) as s:
        st, body = call(s.base, "/v1/pending")
        assert st == 500 and body["error"]["code"] == "internal"
        assert "secret" not in json.dumps(body) and "/root" not in json.dumps(body)
