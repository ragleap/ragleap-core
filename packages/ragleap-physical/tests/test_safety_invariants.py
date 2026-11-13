"""docs/safety-invariants.md stays true, and the apply-time re-check (S5) has a test of its own."""
import re
from pathlib import Path

import pytest

from ragleap_physical.errors import EnvelopeViolation

from helpers import make, propose

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "safety-invariants.md").read_text(encoding="utf-8")


def test_every_invariant_s1_to_s13_has_a_section():
    assert [int(n) for n in re.findall(r"^### S(\d+) ", DOC, re.M)] == list(range(1, 14))


def test_every_test_the_document_names_still_exists():
    refs = re.findall(r"`(tests/test_[a-z0-9_]+\.py)::([A-Za-z0-9_]+)`", DOC)
    assert len(refs) > 30, "the document names far fewer tests than expected"
    for path, name in refs:
        source = (ROOT / path).read_text(encoding="utf-8")
        assert re.search(r"^def %s\(" % re.escape(name), source, re.M), "%s::%s does not exist" % (path, name)


def test_apply_checks_the_state_again_and_refuses_when_the_episode_limit_was_reached():
    """S5. The proposal is valid when it is made and approved. The simulation then runs past the episode limit (60 s). Apply must look at the
    fresh state and refuse with the 'episode' code; the later 'outcome_time' check would also refuse, so the code is what shows the re-check ran."""
    bridge, adapter, _clock = make()
    proposal = propose(bridge)
    bridge.approve(proposal, "person:alice")
    adapter.advance(61.0, 100000)
    with pytest.raises(EnvelopeViolation) as exc:
        bridge.apply(proposal)
    assert exc.value.code == "episode"
    assert adapter.applied == []
