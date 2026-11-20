"""Attacks a hostile tenant would try against the shared building blocks: scope keys, rate limits, the
audit log and secret wrappers. Each test keeps a positive control so an empty result cannot pass by accident."""
import itertools
import json

import pytest

from ragleap_security import AuditLog, RateLimiter, Scope, ScopeError, SecretStr, SQLiteRateLimiter

PARTS = [None, "a", "b", "a-b", "a.b", "a:b", "a@b", "a_b", "A", "0", "tenant", "subject", "a" * 128]


def test_no_two_scopes_share_a_key_and_every_key_parses_back():
    scopes = []
    for t, s, g, n in itertools.product(["a", "tenant", "subject"], PARTS, [None, "a", "agent", "b"], [None, "a", "session"]):
        if t is None:
            continue
        scopes.append(Scope(t, s, g, n))
    keys = {}
    for sc in scopes:
        assert keys.setdefault(sc.key, sc) == sc, sc.key        # same key only for the same scope
        assert Scope.parse(sc.key) == sc
    assert len(keys) == len(set(scopes)) > 400                  # positive control: many distinct scopes


@pytest.mark.parametrize("bad", ["acme\n", "acme\r", "acme\x00", "acme\u2028", "٣", "ａｃｍｅ", "acme/subject=x", "a=b",
                                 "../acme", "acme ", " acme", "acme\t", "a" * 129, ".acme", "_acme"])
def test_hostile_tenant_names_are_refused(bad):
    with pytest.raises(ScopeError):
        Scope(bad)
    with pytest.raises(ScopeError):
        Scope("acme", subject=bad)
    Scope("acme")                                                # positive control


@pytest.mark.parametrize("key", ["tenant=acme/tenant=globex", "subject=x/tenant=acme", "tenant=acme//subject=x",
                                 "tenant=acme/subject=", "tenant=acme\n", "tenant=acme/subject=x/subject=y",
                                 "tenant=acme/session=s/agent=a", "TENANT=acme", "tenant=acme/extra=1"])
def test_hostile_scope_keys_are_refused(key):
    with pytest.raises(ScopeError):
        Scope.parse(key)
    assert Scope.parse("tenant=acme/subject=x") == Scope("acme", "x")      # positive control


def test_flooding_with_new_keys_cannot_reset_a_victim_and_only_blocks_new_keys_until_refill(tmp_path):
    """Documented trade-off: the limiter fails closed. While a flood keeps every key slot busy, a brand-new key is
    refused (reason too_many_keys); keys already known keep their own bucket; once buckets refill, new keys get in."""
    class Clock:
        t = 1000.0

        def __call__(self):
            return self.t

    for make in (lambda c: RateLimiter(1.0, 2, max_keys=100, clock=c),
                 lambda c: SQLiteRateLimiter(tmp_path / f"rl{id(c)}.db", 1.0, 2, max_keys=100, clock=c)):
        clock = Clock()
        lim = make(clock)
        victim, known = Scope("victim"), Scope("known")
        assert lim.check(victim).allowed and lim.check(victim).allowed and lim.check(known).allowed
        assert not lim.check(victim).allowed                          # throttled
        for i in range(500):
            lim.check(Scope(f"flood{i}"))
        assert not lim.check(victim).allowed                          # flooding did not reset the victim
        assert lim.check(known).allowed                               # a known key keeps its own bucket
        newcomer = lim.check(Scope("newcomer"))
        assert not newcomer.allowed and newcomer.reason == "too_many_keys"
        clock.t += 10                                                 # buckets refill, idle full keys are forgotten
        assert lim.check(Scope("newcomer")).allowed


def test_audit_data_cannot_forge_extra_records(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    evil = 'x"}\n{"seq":99,"event":"forged"}\u2028\u2029\r\n'
    log.append("step.done", {"note": evil, evil: 1}, Scope("acme"))
    log.append("step.done", {"n": 2}, Scope("globex"))
    raw = open(log.path, "rb").read()
    lines = raw.split(b"\n")[:-1]
    assert len(lines) == 2 and all(json.loads(l)["event"] == "step.done" for l in lines)
    assert b"forged" not in b"".join(json.loads(l)["event"].encode() for l in lines)
    assert log.verify().ok and log.verify().records == 2
    tampered = lines[0].replace(b"tenant=acme", b"tenant=globex")     # move a record to another tenant
    open(log.path, "wb").write(b"\n".join([tampered, lines[1]]) + b"\n")
    assert not log.verify().ok


def test_secret_values_do_not_leak_through_containers_or_exceptions():
    s = SecretStr("sk-very-secret-value-123")
    shown = [repr({"k": s}), repr([s]), repr((s,)), str({"k": s}), f"{[s]}"]
    try:
        json.dumps({"k": s})
    except TypeError as e:
        shown.append(str(e))
    for text in shown:
        assert "sk-very-secret-value-123" not in text
    assert s.reveal() == "sk-very-secret-value-123"             # positive control
