"""One slow provider must not stall a planning call: after ACTION_PROVIDER_TIMEOUT the next provider goes (found by the real-model sub-agent test)."""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core.employees import actions


class Svc:
    def __init__(self, behaviours):
        self.behaviours, self.calls = behaviours, []

    def _fallback_chain(self):
        return [{"provider": name} for name in self.behaviours]

    def _call_provider(self, cfg, prompt, temperature, max_tokens):
        self.calls.append(cfg["provider"])
        return self.behaviours[cfg["provider"]]()


def slow(seconds=1.5):
    return lambda: (threading.Event().wait(seconds), ("late answer", {}))[1]


def fast(text):
    return lambda: (text, {})


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setenv("ACTION_RETRY_DELAY", "0")
    monkeypatch.setenv("ACTION_PROVIDER_TIMEOUT", "0.2")


def test_a_slow_provider_is_skipped_after_the_limit_and_not_retried():
    svc = Svc({"gemini": slow(), "groq": fast("from groq")})
    t = time.time()
    assert actions.call_with_fallback(svc, "p", 50) == "from groq"
    assert time.time() - t < 1.0
    assert svc.calls == ["gemini", "groq"]


def test_when_every_provider_is_too_slow_the_call_fails_quickly():
    svc = Svc({"gemini": slow(), "groq": slow()})
    t = time.time()
    with pytest.raises(actions.ProviderTimeout):
        actions.call_with_fallback(svc, "p", 50)
    assert time.time() - t < 1.0


def test_a_fast_provider_is_unaffected():
    svc = Svc({"gemini": fast("quick")})
    assert actions.call_with_fallback(svc, "p", 50) == "quick"
    assert svc.calls == ["gemini"]


def test_the_limit_can_be_turned_off_and_a_bad_value_uses_the_default(monkeypatch):
    monkeypatch.setenv("ACTION_PROVIDER_TIMEOUT", "0")
    assert actions._provider_timeout() == 0
    assert actions.call_with_fallback(Svc({"gemini": slow(0.4)}), "p", 50) == "late answer"
    monkeypatch.setenv("ACTION_PROVIDER_TIMEOUT", "nonsense")
    assert actions._provider_timeout() == 45.0


def test_an_ordinary_error_still_retries_once_and_then_moves_on(monkeypatch):
    def boom():
        raise RuntimeError("503")
    svc = Svc({"gemini": boom, "groq": fast("ok")})
    assert actions.call_with_fallback(svc, "p", 50) == "ok"
    assert svc.calls == ["gemini", "gemini", "groq"]
