"""
An OpenAI-compatible model can answer with no text (content is None, for example when it spends its budget on
hidden reasoning). That used to crash with AttributeError; it now returns the same fallback as the Gemini and
Anthropic paths, so the caller can retry or say so.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.generation import GenerationService


def _response(content):
    choice = SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    return SimpleNamespace(choices=[choice], usage=usage)


def _call(content):
    client = MagicMock()
    client.chat.completions.create.return_value = _response(content)
    with patch("openai.OpenAI", return_value=client):
        return GenerationService._call_openai_compatible(None, "prompt", 0.0, 10, "key", "model", "http://example.invalid/v1")


def test_a_reply_with_no_content_does_not_crash():
    text, usage = _call(None)
    assert text == "No answer generated."
    assert usage["total_tokens"] == 2


def test_a_normal_reply_is_unchanged():
    assert _call("  hello  ")[0] == "hello"
