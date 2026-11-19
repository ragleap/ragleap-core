"""A reasoning model that returns no visible text must not count as an answer (found by the real-model sub-agent test)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core.employees import actions
from core.generation import NO_ANSWER


class Svc:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def _fallback_chain(self):
        return [{"provider": name} for name in self.replies]

    def _call_provider(self, cfg, prompt, temperature, max_tokens):
        self.calls.append((cfg["provider"], max_tokens))
        return self.replies[cfg["provider"]](max_tokens), {}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setenv("ACTION_RETRY_DELAY", "0")
    monkeypatch.setenv("ACTION_PROVIDER_TIMEOUT", "0")


def test_the_placeholder_is_treated_as_empty_and_the_next_provider_answers():
    svc = Svc({"groq": lambda n: NO_ANSWER, "custom": lambda n: "a real summary"})
    assert actions.call_with_fallback(svc, "p", 400) == "a real summary"
    assert svc.calls == [("groq", 400), ("groq", 800), ("custom", 400)]


def test_a_bigger_budget_that_gives_text_is_used_before_changing_provider():
    svc = Svc({"groq": lambda n: NO_ANSWER if n < 800 else "answer with more room", "custom": lambda n: "unused"})
    assert actions.call_with_fallback(svc, "p", 400) == "answer with more room"
    assert svc.calls == [("groq", 400), ("groq", 800)]


def test_a_normal_answer_is_returned_at_once():
    svc = Svc({"groq": lambda n: "fine"})
    assert actions.call_with_fallback(svc, "p", 400) == "fine"
    assert svc.calls == [("groq", 400)]


def test_the_placeholder_text_is_the_one_the_provider_paths_use():
    import inspect
    from core import generation
    assert NO_ANSWER == "No answer generated."
    assert inspect.getsource(generation).count('"No answer generated."') == 1   # only the constant definition
