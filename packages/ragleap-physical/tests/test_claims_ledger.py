"""The claims ledger (docs/claims.json) stays true: every claim is still in the file it names, every test it names still exists,
verified claims have evidence, and the README and docs contain none of the phrases we promised not to use."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = json.loads((ROOT / "docs" / "claims.json").read_text(encoding="utf-8"))
STATUSES = {"verified", "limitation", "unverified"}


def squash(text):
    return re.sub(r"\s+", " ", text)


def test_the_ledger_is_well_formed():
    ids = [c["id"] for c in LEDGER["claims"]]
    assert len(ids) == len(set(ids)), "duplicate claim ids"
    for c in LEDGER["claims"]:
        assert c["status"] in STATUSES, c["id"]
        assert c["claim"].strip() and c["where"].strip(), c["id"]
        if c["status"] == "verified":
            assert c["evidence"], "%s is 'verified' but has no evidence" % c["id"]


def test_every_claim_is_still_stated_where_the_ledger_says():
    for c in LEDGER["claims"]:
        if c.get("external"):
            continue
        text = squash((ROOT / c["where"]).read_text(encoding="utf-8"))
        assert squash(c["claim"]) in text, "%s: '%s' is no longer in %s" % (c["id"], c["claim"], c["where"])


def test_every_named_test_and_file_exists():
    for c in LEDGER["claims"]:
        for e in c["evidence"]:
            if e["type"] == "test":
                path, name = e["ref"].split("::")
                source = (ROOT / path).read_text(encoding="utf-8")
                assert re.search(r"^def %s\(" % re.escape(name), source, re.M), "%s: %s does not exist" % (c["id"], e["ref"])
            elif e["type"] == "file-contains":
                assert e["text"] in (ROOT / e["file"]).read_text(encoding="utf-8"), "%s: %s lacks '%s'" % (c["id"], e["file"], e["text"])
            else:
                assert e["type"] == "run" and c.get("external"), "%s: only an external claim may rest on a recorded run" % c["id"]


def test_the_phrases_we_promised_not_to_use_are_absent():
    files = [ROOT / "README.md"] + sorted((ROOT / "docs").rglob("*.md"))
    for f in files:
        text = squash(f.read_text(encoding="utf-8")).lower()
        for phrase in LEDGER["banned_phrases"]:
            assert phrase.lower() not in text, "%s contains the banned phrase '%s'" % (f.relative_to(ROOT), phrase)
