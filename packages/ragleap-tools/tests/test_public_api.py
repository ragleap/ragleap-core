"""Tests for the package's public __init__.py surface - the actual
API a real caller interacts with, not just individual modules."""
import ragleap_tools


def test_version_is_exposed():
    assert ragleap_tools.__version__ == "0.6.0"


def test_stateless_tools_all_have_valid_openai_schemas():
    assert len(ragleap_tools.STATELESS_TOOLS) == 12
    for tool in ragleap_tools.STATELESS_TOOLS:
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == tool.name
        assert "properties" in schema["function"]["parameters"]


def test_stateless_tools_all_have_valid_gemini_schemas():
    for tool in ragleap_tools.STATELESS_TOOLS:
        schema = tool.to_gemini_schema()
        assert schema["name"] == tool.name
        assert "parameters" in schema


def test_stateless_tool_names_are_unique():
    names = [t.name for t in ragleap_tools.STATELESS_TOOLS]
    assert len(names) == len(set(names))


def test_web_search_names_are_exported():
    for name in (
        "SearchProvider",
        "TavilySearchProvider",
        "SerperSearchProvider",
        "WebSearchConfig",
        "make_web_search_tool",
    ):
        assert hasattr(ragleap_tools, name), name
        assert name in ragleap_tools.__all__, name


def test_github_search_names_are_exported():
    for name in ("GitHubSearchConfig", "make_github_search_tool"):
        assert hasattr(ragleap_tools, name), name
        assert name in ragleap_tools.__all__, name
