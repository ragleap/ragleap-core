"""A validly signed Discord request is refused when its timestamp is old, so it cannot be replayed (AMQIC review)."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from channels.discord import router as dc

BODY = b'{"channel_id": "55", "content": "hello"}'


@pytest.fixture
def signer(monkeypatch):
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    monkeypatch.setattr(dc, "DISCORD_PUBLIC_KEY", pub)
    monkeypatch.delenv("DISCORD_MAX_SKEW_SECONDS", raising=False)
    return lambda ts: key.sign(str(ts).encode() + BODY).hex()


def test_a_fresh_signed_request_is_accepted(signer):
    ts = str(int(time.time()))
    assert dc.verify_discord_signature(signer(ts), ts, BODY) is True


def test_a_replayed_old_request_is_refused_even_with_a_valid_signature(signer):
    ts = str(int(time.time()) - 3600)
    assert dc.verify_discord_signature(signer(ts), ts, BODY) is False


def test_a_far_future_timestamp_is_refused(signer):
    ts = str(int(time.time()) + 3600)
    assert dc.verify_discord_signature(signer(ts), ts, BODY) is False


def test_a_timestamp_that_is_not_a_number_is_refused(signer):
    assert dc.verify_discord_signature(signer("soon"), "soon", BODY) is False


def test_a_bad_signature_is_still_refused(signer):
    ts = str(int(time.time()))
    assert dc.verify_discord_signature("00" * 64, ts, BODY) is False


def test_the_limit_can_be_changed_and_turned_off(signer, monkeypatch):
    old = str(int(time.time()) - 600)
    monkeypatch.setenv("DISCORD_MAX_SKEW_SECONDS", "900")
    assert dc.verify_discord_signature(signer(old), old, BODY) is True
    monkeypatch.setenv("DISCORD_MAX_SKEW_SECONDS", "0")
    older = str(int(time.time()) - 99999)
    assert dc.verify_discord_signature(signer(older), older, BODY) is True
    monkeypatch.setenv("DISCORD_MAX_SKEW_SECONDS", "x")
    assert dc.verify_discord_signature(signer(old), old, BODY) is False
