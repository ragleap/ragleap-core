"""P1-A: an approval is bound to the hash of the exact action. Change one field after it was proposed and it does not run."""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import autonomy
from core.employees._db import get_connection

BASE = dict(action_type="followup", channel="telegram", target="999", content="hello there", subject="subj", role="support")
CHANGES = {"action_type": "send_slack", "channel": "slack", "target": "998", "content": "hello therE", "subject": "subj2", "role": "sales"}


@pytest.fixture
def world(monkeypatch):
    sent, events, prompts = [], [], []
    monkeypatch.setenv("ADDON_ENCRYPTION_KEY", "approval-test-key")
    monkeypatch.delenv("SPAWN_SIGNING_KEY", raising=False)
    monkeypatch.delenv("SPAWN_SIGNING_KEY_PREVIOUS", raising=False)
    monkeypatch.setattr(autonomy, "_send_via_channel", lambda ch, t, c: sent.append((ch, t, c)) or "ok")
    monkeypatch.setattr(autonomy, "log_autonomous_action", lambda *a, **k: None)
    monkeypatch.setattr(autonomy.employee_learning, "learn_from_owner_approval", lambda *a, **k: None)
    monkeypatch.setattr(autonomy, "_notify_agent_loop", lambda *a, **k: None)
    monkeypatch.setattr(autonomy, "_audit_event", lambda event, **f: events.append((event, f)))
    monkeypatch.setattr(autonomy, "request_approval", lambda *a, **k: prompts.append((a, k)) or True)
    autonomy.set_autonomy(mode="semi", actions=[], channels=[], approval_channel="telegram", approval_target="")
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM autonomy_pending")
    conn.commit()
    cur.close()
    conn.close()
    return sent, events, prompts


def _propose(**over):
    f = dict(BASE, **over)
    res = autonomy.execute_or_request(action_type=f["action_type"], channel=f["channel"], target=f["target"], content=f["content"],
                                      subject=f["subject"], role=f["role"])
    assert res["status"] == "pending_approval", res
    return res["action_id"]


def _row(aid):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT action_type, channel, target, content, subject, role, action_hash, action_sig FROM autonomy_pending WHERE action_id = %s", (aid,))
    r = cur.fetchone()
    cur.close()
    conn.close()
    return r


def _set(aid, **cols):
    conn = get_connection()
    cur = conn.cursor()
    for k, v in cols.items():
        cur.execute(f"UPDATE autonomy_pending SET {k} = %s WHERE action_id = %s", (v, aid))
    conn.commit()
    cur.close()
    conn.close()


def test_a_proposed_action_stores_its_hash_and_signature_and_shows_a_ref(world):
    _, _, prompts = world
    aid = _propose()
    r = _row(aid)
    assert r[6] == autonomy.action_hash(aid, *[BASE[k] for k in ("action_type", "channel", "target", "content", "subject", "role")])
    assert r[7] and r[7].count(":") == 1
    assert prompts[-1][1]["ref"] == r[6]
    assert any(p["ref"] == r[6][:12] for p in autonomy.list_pending(50) if p["action_id"] == aid)


def test_the_prompt_text_has_the_ref_line(monkeypatch):
    monkeypatch.setattr(autonomy, "get_autonomy_settings", lambda: {"approval_channel": "telegram", "approval_target": "1"})
    seen = []
    monkeypatch.setattr(autonomy, "_send_via_channel", lambda ch, t, m: seen.append(m) or "sent")
    assert autonomy.request_approval("followup", "telegram", "9", "body", "ABCD1234", role="support", ref="a" * 64) is True
    assert "Ref: " + "a" * 12 + "\n" in seen[0] and "Target: 9\nRef:" in seen[0]
    seen.clear()
    autonomy.request_approval("followup", "telegram", "9", "body", "ABCD1234")
    assert "Ref:" not in seen[0]


@pytest.mark.parametrize("keyed", [True, False])
@pytest.mark.parametrize("field", list(CHANGES))
def test_changing_one_field_after_the_proposal_is_refused(world, monkeypatch, field, keyed):
    sent, events, _ = world
    if not keyed:
        monkeypatch.delenv("ADDON_ENCRYPTION_KEY")
    aid = _propose()
    _set(aid, **{field: CHANGES[field]})
    reply = autonomy.process_approval_response(f"YES {aid}")
    assert sent == [] and "NOT run" in reply, reply
    assert any(e == "approval_mismatch" and f["reason"] == "hash_mismatch" for e, f in events)
    assert "not found" in autonomy.process_approval_response(f"YES {aid}")           # the row was consumed: it cannot be retried


def test_an_untouched_action_runs_once_with_exactly_the_proposed_fields(world):
    sent, events, _ = world
    aid = _propose()
    assert "approved and executed" in autonomy.process_approval_response(f"YES {aid}")
    assert sent == [("telegram", "999", "hello there")]
    assert not any(e == "approval_mismatch" for e, _ in events)
    autonomy.process_approval_response(f"YES {aid}")
    assert len(sent) == 1


def test_execute_approved_recomputes_from_the_fields_it_is_given(world):
    sent, events, _ = world
    aid = "AAAA1111"
    h, sig = autonomy.approval_binding(aid, *[BASE[k] for k in ("action_type", "channel", "target", "content", "subject", "role")])
    pd = autonomy.policy_digest()
    assert autonomy._execute_approved(aid, dict(BASE), h, sig, pd) == (True, "ok") and len(sent) == 1
    for field, value in CHANGES.items():
        assert autonomy._execute_approved(aid, dict(BASE, **{field: value}), h, sig, pd) == (False, "hash_mismatch"), field
    assert autonomy._execute_approved("BBBB2222", dict(BASE), h, sig, pd) == (False, "hash_mismatch")      # a hash cannot move to another id
    assert len(sent) == 1


def test_verify_approval_names_the_reason_for_each_refusal(world):
    f = [BASE[k] for k in ("action_type", "channel", "target", "content", "subject", "role")]
    h, sig = autonomy.approval_binding("AAAA1111", *f)
    pd = autonomy.policy_digest()
    kid = sig.split(":")[0]
    assert autonomy.verify_approval("AAAA1111", *f, h, sig, pd) == (True, "ok")
    assert autonomy.verify_approval("AAAA1111", *f, h, "ffffffffffff:" + "0" * 64, pd) == (False, "unknown_key")
    assert autonomy.verify_approval("AAAA1111", *f, h, kid + ":" + "0" * 64, pd) == (False, "bad_signature")
    assert autonomy.verify_approval("AAAA1111", *f, h, None, pd) == (False, "signature_missing")
    assert autonomy.verify_approval("AAAA1111", *f, "0" * 64, sig, pd) == (False, "hash_mismatch")
    assert autonomy.verify_approval("AAAA1111", *f, None, None, None) == (False, "unbound_refused")
    assert autonomy.verify_approval("AAAA1111", *f, h, sig, None) == (False, "unbound_policy_refused")
    assert autonomy.verify_approval("AAAA1111", *f, h, sig, pd, current_policy="0" * 64) == (False, "policy_changed")
    assert autonomy.verify_approval("AAAA1111", *f, h, sig, "0" * 64, current_policy="0" * 64) == (False, "bad_signature")      # the policy is signed too


def test_a_rewritten_hash_needs_the_key(world, monkeypatch):
    sent, events, _ = world
    aid = _propose()
    new = dict(BASE, content="send the money elsewhere")
    forged = autonomy.action_hash(aid, *[new[k] for k in ("action_type", "channel", "target", "content", "subject", "role")])
    _set(aid, content=new["content"], action_hash=forged)                    # signature is still the old one
    assert "NOT run" in autonomy.process_approval_response(f"YES {aid}") and sent == []
    assert any(f.get("reason") == "bad_signature" for e, f in events)


def test_removing_the_signature_when_a_key_is_set_is_refused(world):
    sent, events, _ = world
    aid = _propose()
    _set(aid, action_sig=None)
    assert "NOT run" in autonomy.process_approval_response(f"YES {aid}") and sent == []
    assert any(f.get("reason") == "signature_missing" for e, f in events)


def test_an_unknown_key_id_or_a_wrong_mac_is_refused(world):
    sent, events, _ = world
    for sig in ("ffffffffffff:" + "0" * 64, autonomy._signing_keys()[0] + ":" + "0" * 64, "garbage"):
        aid = _propose()
        _set(aid, action_sig=sig)
        assert "NOT run" in autonomy.process_approval_response(f"YES {aid}")
    assert sent == []


def test_without_any_key_only_the_hash_is_kept_and_it_still_catches_a_changed_field(world, monkeypatch):
    monkeypatch.delenv("ADDON_ENCRYPTION_KEY")
    sent, _, _ = world
    aid = _propose()
    r = _row(aid)
    assert r[6] and r[7] is None
    assert "approved and executed" in autonomy.process_approval_response(f"YES {aid}") and len(sent) == 1


def _insert_raw(aid, hashed=True, policy=True, sig=None):
    f = [BASE[k] for k in ("action_type", "channel", "target", "content", "subject", "role")]
    h, s_ = autonomy.approval_binding(aid, *f)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO autonomy_pending (action_id, action_type, channel, target, content, subject, role, action_hash, action_sig, policy_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (aid, *f, h if hashed else None, s_ if sig is None else sig, autonomy.policy_digest() if policy else None))
    conn.commit()
    cur.close()
    conn.close()


def test_a_row_without_a_hash_is_refused_never_run(world):
    sent, events, _ = world
    aid = uuid.uuid4().hex[:8].upper()
    _insert_raw(aid, hashed=False)                       # a row from before P1-A, or one written by hand
    reply = autonomy.process_approval_response(f"YES {aid}")
    assert sent == [] and "NOT run" in reply and "created before approvals were bound" in reply, reply
    assert any(e == "approval_mismatch" and f["reason"] == "unbound_refused" for e, f in events)


def test_a_row_with_a_hash_but_no_policy_digest_is_refused(world):
    sent, events, _ = world
    aid = uuid.uuid4().hex[:8].upper()
    _insert_raw(aid, policy=False)                       # proposed by v0.13.10 or v0.13.11, still pending at the upgrade
    reply = autonomy.process_approval_response(f"YES {aid}")
    assert sent == [] and "NOT run" in reply and "bound to your autonomy settings" in reply, reply
    assert any(f.get("reason") == "unbound_policy_refused" for e, f in events)


SETTINGS = dict(mode="semi", actions=[], channels=[], approval_channel="telegram", approval_target="")
SETTING_CHANGES = {"mode": "full", "actions": ["send_message"], "channels": ["telegram"], "approval_channel": "slack", "approval_target": "9"}


@pytest.mark.parametrize("keyed", [True, False])
@pytest.mark.parametrize("field", list(SETTING_CHANGES))
def test_changing_the_autonomy_settings_after_the_proposal_refuses_the_approval(world, monkeypatch, field, keyed):
    sent, events, _ = world
    if not keyed:
        monkeypatch.delenv("ADDON_ENCRYPTION_KEY")
    aid = _propose()
    autonomy.set_autonomy(**dict(SETTINGS, **{field: SETTING_CHANGES[field]}))
    reply = autonomy.process_approval_response(f"YES {aid}")
    assert sent == [] and "NOT run" in reply and "autonomy settings changed" in reply, reply
    assert any(e == "approval_mismatch" and f["reason"] == "policy_changed" for e, f in events)
    assert "not found" in autonomy.process_approval_response(f"YES {aid}")


def test_settings_changed_and_changed_back_still_run_and_list_order_does_not_matter(world):
    sent, _, _ = world
    autonomy.set_autonomy(**dict(SETTINGS, actions=["followup", "zz"], channels=["telegram", "zz"]))
    aid = _propose()
    autonomy.set_autonomy(**dict(SETTINGS, mode="off"))
    autonomy.set_autonomy(**dict(SETTINGS, actions=["zz", "followup"], channels=["zz", "telegram"]))
    assert "approved and executed" in autonomy.process_approval_response(f"YES {aid}") and len(sent) == 1


def test_the_policy_digest_covers_every_setting_and_ignores_list_order():
    base = dict(mode="semi", actions=["a", "b"], channels=["x", "y"], approval_channel="telegram", approval_target="1")
    d = autonomy.policy_digest(base)
    assert len(d) == 64 and d == autonomy.policy_digest(dict(base, actions=["b", "a"], channels=["y", "x"]))
    for k, v in (("mode", "off"), ("actions", ["a"]), ("channels", ["x"]), ("approval_channel", "slack"), ("approval_target", "2")):
        assert autonomy.policy_digest(dict(base, **{k: v})) != d, k


def test_a_proposal_stores_the_digest_of_the_settings_it_was_given_under(world):
    aid = _propose()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT policy_hash FROM autonomy_pending WHERE action_id = %s", (aid,))
    assert cur.fetchone()[0] == autonomy.policy_digest()
    cur.close()
    conn.close()


def test_a_rewritten_policy_digest_needs_the_key(world):
    sent, events, _ = world
    aid = _propose()
    autonomy.set_autonomy(**dict(SETTINGS, mode="full"))
    _set(aid, policy_hash=autonomy.policy_digest())        # someone rewrites the stored digest to the new settings
    assert "NOT run" in autonomy.process_approval_response(f"YES {aid}") and sent == []
    assert any(f.get("reason") == "bad_signature" for e, f in events)


def test_without_any_key_a_rewritten_policy_digest_is_not_caught(world, monkeypatch):
    """Documented limit: with no signing key only the digests are stored, so a database writer can rewrite both."""
    monkeypatch.delenv("ADDON_ENCRYPTION_KEY")
    sent, _, _ = world
    aid = _propose()
    autonomy.set_autonomy(**dict(SETTINGS, mode="full"))
    _set(aid, policy_hash=autonomy.policy_digest())
    assert "approved and executed" in autonomy.process_approval_response(f"YES {aid}") and len(sent) == 1


def test_a_policy_check_that_cannot_run_refuses(world, monkeypatch):
    sent, events, _ = world
    aid = _propose()
    monkeypatch.setattr(autonomy, "policy_digest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("settings unreadable")))
    assert "NOT run" in autonomy.process_approval_response(f"YES {aid}") and sent == []
    assert any(f.get("reason") == "check_failed" for e, f in events)


class _Ids:
    def __init__(self, *ids):
        self.ids = list(ids)

    def uuid4(self):
        v = self.ids.pop(0) if len(self.ids) > 1 else self.ids[0]
        return type("U", (), {"__str__": lambda s: v})()


def test_an_id_clash_draws_a_new_id_and_never_overwrites_the_other_action(world, monkeypatch):
    sent, _, prompts = world
    first = _propose(content="first action")
    monkeypatch.setattr(autonomy, "uuid", _Ids(first.lower() + "-x", "ZZZZ9999-x"))
    second = _propose(content="second action")
    assert second == "ZZZZ9999" and first != second
    assert _row(first)[3] == "first action" and _row(second)[3] == "second action"
    assert [p[0][4] for p in prompts] == [first, second]


def test_when_no_id_can_be_stored_nothing_is_sent_and_the_gate_says_error(world, monkeypatch):
    sent, events, prompts = world
    first = _propose(content="first action")
    monkeypatch.setattr(autonomy, "uuid", _Ids(first.lower() + "-x"))
    n = len(prompts)
    res = autonomy.execute_or_request(action_type="followup", channel="telegram", target="1", content="never stored")
    assert res["status"] == "error" and len(prompts) == n and sent == []
    assert any(e == "action_not_stored" for e, _ in events)


def test_a_failed_insert_sends_no_prompt(world, monkeypatch):
    sent, events, prompts = world
    monkeypatch.setattr(autonomy, "approval_binding", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    res = autonomy.execute_or_request(action_type="followup", channel="telegram", target="1", content="x")
    assert res["status"] == "error" and prompts == []


def test_the_api_reference_must_match_and_a_wrong_one_consumes_nothing(world):
    sent, _, _ = world
    aid = _propose()
    ref = _row(aid)[6]
    wrong = "0" * 12 if not ref.startswith("0" * 12) else "1" * 12
    assert "not run" in autonomy.resolve_pending(aid, True, action_hash=wrong)
    assert "not run" in autonomy.resolve_pending(aid, True, action_hash="xyz")           # not hex
    assert "not run" in autonomy.resolve_pending(aid, True, action_hash=ref[:11])        # too short
    assert sent == [] and _row(aid) is not None
    assert "approved and executed" in autonomy.resolve_pending(aid, True, action_hash=ref[:12].upper())
    assert len(sent) == 1


def test_the_api_without_a_reference_still_works(world):
    sent, _, _ = world
    aid = _propose()
    assert "approved and executed" in autonomy.resolve_pending(aid, True)
    assert len(sent) == 1


def test_the_approve_route_takes_the_reference(world):
    import inspect
    from core import api
    assert "action_hash" in inspect.signature(api.approve_pending_action).parameters
    src = inspect.getsource(api.approve_pending_action)
    assert "action_hash=action_hash" in src


def test_rejecting_needs_no_reference_and_runs_nothing(world):
    sent, _, _ = world
    aid = _propose()
    assert "rejected" in autonomy.process_approval_response(f"NO {aid}") and sent == []
