import pytest

from ragleap_optimizer import count_request, estimate_tokens
from ragleap_optimizer.model import content_text


@pytest.mark.parametrize("text,expected", [("a", 1), ("abc", 1), ("abcd", 2), ("abcdefghi", 3), ("\u65e5\u672c\u8a9e", 3), ("", 1)])
def test_estimate_values(text, expected):
    assert estimate_tokens(text) == expected          # one token per 3 UTF-8 bytes, at least 1


def test_content_text_for_strings_lists_and_other_values():
    assert content_text("plain") == "plain"
    assert content_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert content_text([{"type": "image", "url": "u"}]) == '{"type": "image", "url": "u"}'
    assert content_text(None) == "null"
    assert content_text(5) == "5"


def test_count_request_counts_roles_messages_tools_and_system():
    base = {"messages": [{"role": "user", "content": "abcdef"}]}
    n = count_request(base)
    assert n == estimate_tokens("abcdef") + estimate_tokens("user")
    with_tools = dict(base, tools=[{"name": "f"}])
    assert count_request(with_tools) == n + estimate_tokens('[{"name": "f"}]')
    with_system = dict(base, system="s" * 30)
    assert count_request(with_system) == n + estimate_tokens("s" * 30)
    assert count_request({"messages": []}) == 0
    assert count_request({}) == 0
