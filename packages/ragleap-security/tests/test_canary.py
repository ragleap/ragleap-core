"""ragleap_security.canary (design: issue #1569). Canaries detect and trace; they never prevent copying.

Every behaviour is tested against both stores (memory and SQLite). The real key derivation, MAC, base32 coding,
text normalisation and scope logic run in every test."""
import json
import os
import re
import stat
import threading

import pytest

from ragleap_security import AuditLog, Key, KeyRing, RateLimiter, Scope
from ragleap_security.canary import (_decode, _encode, CEILING_MAX_CANDIDATES, CEILING_MAX_PER_SCOPE, CEILING_MAX_TEXT_BYTES, PREFIX,
                                     Canary, CanaryError, CanaryHit, CanaryIssuer, CanaryRecord, CanaryStore,
                                     Hits, MemoryCanaryStore, SQLiteCanaryStore)

A, B = Scope("acme"), Scope("globex")
ALICE, BOB = Scope("acme", subject="alice"), Scope("acme", subject="bob")
TOKEN_RE = re.compile(r"rlc1_[a-z2-7]{26}")


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    return MemoryCanaryStore() if request.param == "memory" else SQLiteCanaryStore(tmp_path / "canary.db")


@pytest.fixture
def key():
    return Key.generate("k1")


@pytest.fixture
def issuer(key, store):
    return CanaryIssuer(key, store)


def reason(call, *args, **kwargs):
    with pytest.raises(CanaryError) as e:
        call(*args, **kwargs)
    return e.value.reason


# ---------------------------------------------------------------- tokens
def test_a_token_has_the_documented_shape_and_is_random(issuer):
    a, b = issuer.issue(A, "one"), issuer.issue(A, "one")
    for c in (a, b):
        assert c.token.startswith(PREFIX) and len(c.token) == 31 and TOKEN_RE.fullmatch(c.token)
        assert re.fullmatch(r"[0-9a-f]{20}", c.id)
        assert c.scope_key == "tenant=acme" and c.label == "one"
    assert a.token != b.token and a.id != b.id


def test_repr_of_a_canary_does_not_show_the_token(issuer):
    c = issuer.issue(A, "one")
    assert c.token not in repr(c) and c.token not in str(c)


def test_the_token_is_not_stored(issuer, store, tmp_path):
    c = issuer.issue(A, "one")
    rec = store.get(c.id)
    assert isinstance(rec, CanaryRecord) and c.token not in repr(rec) and c.token[5:] not in repr(rec)
    if isinstance(store, SQLiteCanaryStore):
        raw = open(store.path, "rb").read()
        assert c.token.encode() not in raw and c.token[5:].encode() not in raw


def test_labels_are_validated(issuer):
    for bad in ("", "x" * 65, " a", "a b", "a/b", "a'b", "é", None, 5, "a\nb"):
        assert reason(issuer.issue, A, bad) == "invalid_label"
    issuer.issue(A, "a" + "b" * 63)


def test_only_a_real_scope_is_accepted(issuer):
    class Fake(Scope):
        pass
    for bad in ("acme", None, {"tenant": "acme"}, Fake("acme")):
        assert reason(issuer.issue, bad, "x") == "invalid_argument"
        assert reason(issuer.scan, "text", bad) == "invalid_argument"
        assert reason(issuer.revoke, bad, "0" * 20) == "invalid_argument"


def test_constructor_arguments_are_checked(key, store):
    class K(Key):
        pass
    for bad in ("key", None, b"x"):
        assert reason(CanaryIssuer, bad, store) == "invalid_argument"
    assert reason(CanaryIssuer, key, object()) == "invalid_argument"
    assert reason(CanaryIssuer, key, store, audit="path") == "invalid_argument"
    assert reason(CanaryIssuer, key, store, rate_limiter=object()) == "invalid_argument"
    for name, ceiling in (("max_per_scope", CEILING_MAX_PER_SCOPE), ("max_text_bytes", CEILING_MAX_TEXT_BYTES),
                          ("max_candidates", CEILING_MAX_CANDIDATES)):
        CanaryIssuer(key, store, **{name: ceiling})
        for bad in (0, -1, ceiling + 1, True, 1.5, "5"):
            assert reason(CanaryIssuer, key, store, **{name: bad}) == "invalid_argument"


# ---------------------------------------------------------------- finding tokens
def test_an_own_token_is_found_with_its_id_label_and_count(issuer):
    c = issuer.issue(A, "system-prompt")
    hits = issuer.scan(f"here: {c.token} and again {c.token}.", A, source="model-output")
    assert isinstance(hits, Hits) and hits.truncated is False
    assert hits == [CanaryHit("own", 2, c.id, "system-prompt", "model-output")]


def test_text_without_a_token_gives_no_hits(issuer):
    issuer.issue(A, "x")
    assert issuer.scan("nothing to see, rlc1_ or rlc1_short", A) == []
    assert issuer.scan("", A) == []


def test_two_canaries_give_two_hits(issuer):
    c1, c2 = issuer.issue(A, "one"), issuer.issue(A, "two")
    hits = issuer.scan(f"{c2.token} {c1.token} {c1.token}", A)
    assert sorted((h.label, h.count) for h in hits) == [("one", 2), ("two", 1)]


def test_a_token_inside_a_word_or_at_the_edges_is_found(issuer):
    c = issuer.issue(A, "x")
    for text in (c.token, "abc" + c.token + "def", c.token + "\n", "\t" + c.token, "<" + c.token + ">"):
        assert [h.canary_id for h in issuer.scan(text, A)] == [c.id]


def test_changed_case_fullwidth_and_hidden_characters_do_not_hide_a_token(issuer):
    c = issuer.issue(A, "x")
    fullwidth = "".join(chr(ord(ch) + 0xFEE0) if "!" <= ch <= "~" else ch for ch in c.token)
    variants = [
        c.token.upper(), c.token.title(), fullwidth,
        c.token[:8] + "\u200b" + c.token[8:],          # zero-width space
        c.token[:8] + "\u200d" + c.token[8:],          # zero-width joiner
        c.token[:8] + "\u202e" + c.token[8:],          # bidi override
        c.token[:8] + "\u00ad" + c.token[8:],          # soft hyphen
        c.token[:8] + "\ufeff" + c.token[8:],          # byte order mark
    ]
    for v in variants:
        assert [h.canary_id for h in issuer.scan(v, A)] == [c.id], repr(v)


def test_visible_changes_do_hide_a_token(issuer):
    """Not a defence: documented. Splitting with a visible character, other encodings and paraphrase are not found."""
    c = issuer.issue(A, "x")
    import base64
    for v in (c.token[:8] + " " + c.token[8:], c.token[:8] + "-" + c.token[8:], c.token[::-1],
              base64.b64encode(c.token.encode()).decode(), c.token.encode().hex()):
        assert issuer.scan(v, A) == []


# ---------------------------------------------------------------- forged and foreign tokens
def test_lookalikes_are_ignored(issuer, key):
    issuer.issue(A, "x")
    base = "rlc1_" + "a" * 26
    flipped = issuer.issue(A, "y").token
    flipped = flipped[:-1] + ("a" if flipped[-1] != "a" else "b")
    other = CanaryIssuer(Key.generate("k2"), MemoryCanaryStore()).issue(A, "z").token
    assert issuer.scan(f"{base} {flipped} {other}", A) == []


def test_a_valid_mac_with_an_id_that_is_not_in_the_store_is_ignored(issuer, store):
    c = issuer.issue(A, "x")
    assert store.remove(c.id, "tenant=acme")
    assert issuer.scan(c.token, A) == []


def test_a_non_canonical_spelling_is_refused(issuer):
    c = issuer.issue(A, "x")
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    last = alphabet.index(c.token[-1])
    twin = c.token[:-1] + alphabet[last ^ 1]          # differs only in the unused bits of the last character
    assert twin != c.token and issuer.scan(twin, A) == []


def test_another_tenants_token_is_a_foreign_hit_that_reveals_nothing(issuer):
    secret = issuer.issue(B, "globex-secret")
    hits = issuer.scan(f"leak: {secret.token}", A, source="log")
    assert hits == [CanaryHit("foreign", 1, None, None, "log")]
    shown = repr(hits) + str(hits[0])
    assert "globex" not in shown and secret.id not in shown and secret.token not in shown


def test_several_foreign_tokens_are_one_hit_with_a_total(issuer):
    t1, t2 = issuer.issue(B, "a"), issuer.issue(Scope("initech"), "b")
    hits = issuer.scan(f"{t1.token} {t2.token} {t1.token}", A)
    assert hits == [CanaryHit("foreign", 3, None, None, "unspecified")]


def test_subjects_inside_one_tenant_are_separate_scopes(issuer):
    alice = issuer.issue(ALICE, "alice-doc")
    assert [h.kind for h in issuer.scan(alice.token, BOB)] == ["foreign"]       # bob's data holds alice's canary
    assert [h.kind for h in issuer.scan(alice.token, ALICE)] == ["own"]
    assert [h.kind for h in issuer.scan(alice.token, A)] == ["own"]             # the tenant-wide scope covers alice
    wide = issuer.issue(A, "tenant-doc")
    assert [h.kind for h in issuer.scan(wide.token, ALICE)] == ["foreign"]      # a narrower scope does not cover its tenant


def test_a_token_cannot_be_moved_to_another_scope_by_the_one_who_holds_it(issuer):
    c = issuer.issue(A, "x")
    # whoever copies the token into globex's data only makes a foreign hit for globex
    assert [h.kind for h in issuer.scan(c.token, B)] == ["foreign"]
    assert [h.kind for h in issuer.scan(c.token, A)] == ["own"]


# ---------------------------------------------------------------- revoke
def test_revoke_makes_a_token_inert(issuer):
    c = issuer.issue(A, "x")
    issuer.revoke(A, c.id)
    assert issuer.scan(c.token, A) == [] and issuer.scan(c.token, B) == []


def test_revoke_gives_the_same_error_for_a_wrong_scope_and_an_unknown_id(issuer):
    c = issuer.issue(A, "x")
    errors = []
    for scope, cid in ((B, c.id), (ALICE, c.id), (A, "0" * 20), (A, c.id[:-1] + ("0" if c.id[-1] != "0" else "1")),
                       (A, "nothex"), (A, 5), (A, None)):
        with pytest.raises(CanaryError) as e:
            issuer.revoke(scope, cid)
        errors.append((e.value.reason, str(e.value), e.value.args))
    assert set(errors) == {("no_such_canary", "no_such_canary", ("no_such_canary",))}
    assert [h.kind for h in issuer.scan(c.token, A)] == ["own"]          # nothing was removed
    issuer.revoke(A, c.id)
    assert reason(issuer.revoke, A, c.id) == "no_such_canary"            # revoking twice


def test_a_tenant_wide_scope_cannot_revoke_a_narrower_scopes_canary(issuer):
    c = issuer.issue(ALICE, "x")
    assert reason(issuer.revoke, A, c.id) == "no_such_canary"
    issuer.revoke(ALICE, c.id)


# ---------------------------------------------------------------- limits
def test_the_cap_is_per_scope(key, store):
    iss = CanaryIssuer(key, store, max_per_scope=3)
    for _ in range(3):
        iss.issue(A, "x")
    assert reason(iss.issue, A, "x") == "limit"
    assert store.count("tenant=acme") == 3
    for _ in range(3):
        iss.issue(B, "x")                 # a noisy tenant does not use up another tenant's room
    iss.issue(ALICE, "x")                 # the tenant-wide cap does not include a narrower scope
    canaries = [iss.issue(Scope("t3"), "x")]
    iss.revoke(Scope("t3"), canaries[0].id)
    iss.issue(Scope("t3"), "x")           # revoking frees room


def test_text_size_boundary_is_in_bytes_and_never_truncates(key, store):
    iss = CanaryIssuer(key, store, max_text_bytes=100)
    c = iss.issue(A, "x")
    ok = c.token + "a" * (100 - len(c.token))
    assert len(ok.encode()) == 100 and [h.kind for h in iss.scan(ok, A)] == ["own"]
    assert reason(iss.scan, ok + "a", A) == "text_too_large"
    assert reason(iss.scan, "é" * 51, A) == "text_too_large"            # 102 bytes, 51 characters
    late = "a" * 100 + c.token
    assert reason(iss.scan, late, A) == "text_too_large"               # a token after the cap is refused, not missed


def test_text_that_expands_a_lot_when_normalised_is_refused(key, store):
    iss = CanaryIssuer(key, store, max_text_bytes=1000)
    assert reason(iss.scan, "\ufdfa" * 300, A) == "text_too_large"     # 900 bytes, 18 characters each after NFKC
    iss.scan("\ufdfa" * 10, A)


def test_junk_lookalikes_cannot_push_a_real_token_out(key, store):
    iss = CanaryIssuer(key, store, max_candidates=4)
    c = iss.issue(A, "x")
    junk = " ".join("rlc1_" + format(i, "026d").translate(str.maketrans("0189", "abcd")) for i in range(500))
    hits = iss.scan(junk + " " + c.token, A)
    assert [h.canary_id for h in hits] == [c.id] and hits.truncated is False


def test_more_valid_tokens_than_the_cap_are_cut_and_flagged(key, store):
    iss = CanaryIssuer(key, store, max_candidates=3)
    cs = [iss.issue(A, f"c{i}") for i in range(5)]
    hits = iss.scan(" ".join(c.token for c in cs), A)
    assert len(hits) == 3 and hits.truncated is True
    assert [h.canary_id for h in hits] == [c.id for c in cs[:3]]


def test_scan_arguments_are_checked(issuer):
    assert reason(issuer.scan, b"bytes", A) == "invalid_argument"
    assert reason(issuer.scan, None, A) == "invalid_argument"
    assert reason(issuer.scan, "x", A, source="bad source") == "invalid_label"
    assert reason(issuer.scan, "x", A, source="") == "invalid_label"


# ---------------------------------------------------------------- rate limit
def test_the_scan_rate_limit_is_per_scope(key, store):
    now = [0.0]
    limiter = RateLimiter(rate=0.001, burst=2, clock=lambda: now[0])
    iss = CanaryIssuer(key, store, rate_limiter=limiter)
    iss.scan("x", A)
    iss.scan("x", A)
    assert reason(iss.scan, "x", A) == "rate_limited"
    iss.scan("x", B)                       # another scope has its own bucket
    assert reason(iss.scan, "x" * 5, A) == "rate_limited"


# ---------------------------------------------------------------- audit
def read_log(path):
    return [json.loads(line) for line in open(path, encoding="utf-8")]


def test_audit_events_hold_ids_and_labels_but_never_text_or_tokens(key, store, tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    iss = CanaryIssuer(key, store, audit=log)
    own, foreign = iss.issue(A, "prompt"), iss.issue(B, "globex-only")
    iss.scan(f"SECRET TEXT {own.token} {foreign.token} {foreign.token}", A, source="model-output")
    iss.revoke(A, own.id)
    raw = open(tmp_path / "audit.jsonl", encoding="utf-8").read()
    assert "SECRET TEXT" not in raw and PREFIX not in raw and own.token[5:] not in raw
    events = read_log(tmp_path / "audit.jsonl")
    assert [e["event"] for e in events] == ["canary.issued", "canary.issued", "canary.hit", "canary.foreign", "canary.revoked"]
    hit, foreign_ev = events[2], events[3]
    assert hit["scope"] == "tenant=acme" and hit["data"] == {"canary": own.id, "label": "prompt", "source": "model-output", "count": 1}
    assert foreign_ev["scope"] == "tenant=acme"
    assert foreign_ev["data"] == {"owners": ["tenant=globex"], "canaries": [foreign.id], "source": "model-output", "count": 2}
    assert log.verify().ok


def test_a_failed_audit_write_fails_issue_and_leaves_no_canary(key, store, tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    iss = CanaryIssuer(key, store, audit=log)

    def boom(*a, **k):
        raise OSError("disk full")
    log.append = boom
    assert reason(iss.issue, A, "x") == "audit_failed"
    assert store.count("tenant=acme") == 0


def test_a_failed_audit_write_makes_scan_return_nothing(key, store, tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    iss = CanaryIssuer(key, store, audit=log)
    c = iss.issue(A, "x")
    calls = []
    real = log.append

    def boom(*a, **k):
        calls.append(1)
        raise OSError("disk full")
    log.append = boom
    assert reason(iss.scan, c.token, A) == "audit_failed" and calls
    log.append = real
    assert [h.kind for h in iss.scan(c.token, A)] == ["own"]


def test_scan_without_a_finding_writes_no_audit_record(key, store, tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    iss = CanaryIssuer(key, store, audit=log)
    iss.issue(A, "x")
    iss.scan("nothing", A)
    assert [e["event"] for e in read_log(tmp_path / "audit.jsonl")] == ["canary.issued"]


# ---------------------------------------------------------------- keys
def test_old_canaries_verify_while_their_key_is_in_the_ring(store):
    k1, k2 = Key.generate("k1"), Key.generate("k2")
    old = CanaryIssuer(KeyRing([k1]), store).issue(A, "old")
    rotated = CanaryIssuer(KeyRing([k2, k1], active="k2"), store)
    new = rotated.issue(A, "new")
    assert sorted(h.label for h in rotated.scan(f"{old.token} {new.token}", A)) == ["new", "old"]
    k2_only = CanaryIssuer(KeyRing([k2]), store)
    assert [h.label for h in k2_only.scan(f"{old.token} {new.token}", A)] == ["new"]      # the old key was dropped


def test_a_different_key_does_not_validate_a_token(store):
    c = CanaryIssuer(Key.generate("k1"), store).issue(A, "x")
    assert CanaryIssuer(Key.generate("k1"), store).scan(c.token, A) == []                  # same id, different bytes


# ---------------------------------------------------------------- stores
def test_the_sqlite_store_survives_a_restart_and_is_private(key, tmp_path):
    path = tmp_path / "c.db"
    c = CanaryIssuer(key, SQLiteCanaryStore(path)).issue(A, "x")
    again = CanaryIssuer(key, SQLiteCanaryStore(path))
    assert [h.canary_id for h in again.scan(c.token, A)] == [c.id]
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_hostile_values_cannot_reach_the_sql(store):
    rec = CanaryRecord("0" * 20, "tenant=acme", "x", "k1", "2026-11-26T00:00:00+00:00")
    assert store.add(rec, 10)
    for nasty in ("' OR '1'='1", "0" * 20 + "'; DROP TABLE canaries; --", "%", "_" * 20):
        assert store.get(nasty) is None
        assert store.remove(nasty, "tenant=acme") is False
        assert store.remove("0" * 20, nasty) is False
    assert store.get("0" * 20) == rec and store.count("tenant=acme") == 1
    assert store.count("tenant=ac%") == 0 and store.count("tenant=acme' OR '1'='1") == 0


def test_a_duplicate_id_is_refused_by_the_store(store):
    rec = CanaryRecord("1" * 20, "tenant=acme", "x", "k1", "t")
    assert store.add(rec, 10) and not store.add(rec, 10)
    assert store.count("tenant=acme") == 1


def test_the_cap_holds_under_a_forced_race(key, store):
    iss = CanaryIssuer(key, store, max_per_scope=5)
    barrier = threading.Barrier(20)
    results = []

    def worker():
        barrier.wait()
        try:
            iss.issue(A, "x")
            results.append("ok")
        except CanaryError as e:
            results.append(e.reason)
    threads = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("ok") == 5 and results.count("limit") == 15 and store.count("tenant=acme") == 5


def test_a_store_that_fails_fails_closed(key):
    class Broken(CanaryStore):
        def add(self, record, limit):
            raise RuntimeError("db down")

        def get(self, canary_id):
            raise RuntimeError("db down")

        def remove(self, canary_id, scope_key):
            raise RuntimeError("db down")

        def count(self, scope_key):
            raise RuntimeError("db down")
    good = MemoryCanaryStore()
    c = CanaryIssuer(key, good).issue(A, "x")
    iss = CanaryIssuer(key, Broken())
    assert reason(iss.issue, A, "x") == "store_error"
    assert reason(iss.scan, c.token, A) == "store_error"
    assert reason(iss.revoke, A, "0" * 20) == "store_error"


def test_a_store_cannot_be_something_that_merely_looks_like_one(key):
    class Duck:
        def add(self, *a): return True
        def get(self, *a): return None
        def remove(self, *a): return True
        def count(self, *a): return 0
    assert reason(CanaryIssuer, key, Duck()) == "invalid_argument"


def test_error_messages_never_contain_text_tokens_or_scopes(issuer):
    c = issuer.issue(A, "x")
    for call in (lambda: issuer.scan("x" * (2 << 20) + c.token, A), lambda: issuer.revoke(B, c.id),
                 lambda: issuer.issue(A, "bad label with " + c.token)):
        with pytest.raises(CanaryError) as e:
            call()
        shown = str(e.value) + repr(e.value)
        assert c.token not in shown and "acme" not in shown and "globex" not in shown and c.id not in shown


# ---------------------------------------------------------------- added after the first mutation run
def test_documented_defaults_and_ceilings(key, store):
    import ragleap_security.canary as c
    assert (c.DEFAULT_MAX_TEXT_BYTES, c.CEILING_MAX_TEXT_BYTES) == (1 << 20, 16 << 20)
    assert (c.DEFAULT_MAX_CANDIDATES, c.CEILING_MAX_CANDIDATES) == (64, 256)
    assert (c.DEFAULT_MAX_PER_SCOPE, c.CEILING_MAX_PER_SCOPE) == (1_000, 100_000)
    assert c.MAX_OWNERS_IN_AUDIT == 16
    iss = CanaryIssuer(key, store)
    assert (iss.max_text_bytes, iss.max_candidates, iss.max_per_scope) == (1 << 20, 64, 1_000)


def test_a_record_with_a_broken_scope_key_is_ignored_not_fatal(key, store):
    iss = CanaryIssuer(key, store)
    raw_id = os.urandom(10)
    token = _encode(raw_id + iss._mac(key, raw_id))
    assert store.add(CanaryRecord(raw_id.hex(), "this is not a scope key", "x", "k1", "t"), 10)
    assert iss.scan(token, A) == []


def test_the_audit_lists_each_owner_once_and_caps_the_lists(key, store, tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    iss = CanaryIssuer(key, store, audit=log)
    two = [iss.issue(B, "a"), iss.issue(B, "b")]
    iss.scan(" ".join(c.token for c in two), A)
    ev = read_log(tmp_path / "audit.jsonl")[-1]
    assert ev["event"] == "canary.foreign" and ev["data"]["owners"] == ["tenant=globex"]
    assert ev["data"]["canaries"] == [c.id for c in two] and ev["data"]["count"] == 2
    many = [iss.issue(Scope(f"t{i:02d}"), "x") for i in range(20)]
    hits = iss.scan(" ".join(c.token for c in many), A)
    assert hits == [CanaryHit("foreign", 20, None, None, "unspecified")]
    ev = read_log(tmp_path / "audit.jsonl")[-1]
    assert len(ev["data"]["owners"]) == 16 and len(ev["data"]["canaries"]) == 16 and ev["data"]["count"] == 20


def test_revoke_does_not_touch_the_store_for_an_id_that_cannot_exist(key):
    class Spy(MemoryCanaryStore):
        calls = 0

        def remove(self, canary_id, scope_key):
            Spy.calls += 1
            return super().remove(canary_id, scope_key)
    iss = CanaryIssuer(key, Spy())
    for bad in ("nothex", "ABCDEF0123456789ABCD", "0" * 19, "0" * 21, 5, None):
        assert reason(iss.revoke, A, bad) == "no_such_canary"
    assert Spy.calls == 0
    assert reason(iss.revoke, A, "0" * 20) == "no_such_canary" and Spy.calls == 1


def test_a_scan_that_finds_nothing_does_not_call_the_store_for_lookalikes(key):
    class Spy(MemoryCanaryStore):
        gets = 0

        def get(self, canary_id):
            Spy.gets += 1
            return super().get(canary_id)
    iss = CanaryIssuer(key, Spy())
    iss.scan("rlc1_" + "b" * 26 + " " + "rlc1_" + "c" * 26, A)
    assert Spy.gets == 0                       # forged tokens are rejected by the MAC before any lookup


def test_the_mac_belongs_to_its_id(issuer):
    """A real id with the MAC of another canary is a forgery, even though both parts are real."""
    c1, c2 = issuer.issue(A, "one"), issuer.issue(A, "two")
    r1, r2 = _decode(c1.token), _decode(c2.token)
    assert r1 is not None and r2 is not None and r1[10:] != r2[10:]
    mixed = _encode(r1[:10] + r2[10:])
    assert issuer.scan(mixed, A) == []
    assert [h.label for h in issuer.scan(c1.token, A)] == ["one"] and [h.label for h in issuer.scan(c2.token, A)] == ["two"]
