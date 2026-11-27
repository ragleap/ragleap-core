"""
ragleap_tools - standalone, dependency-light tool implementations for
LLM tool-calling. Exposes OpenAI/Gemini-style function-calling schemas.

Deliberately does NOT own a tool-calling execution loop - that's
ragleap-agents' job (per the project roadmap's own split). This package
is just the tools: name, description, JSON Schema parameters, and a
safe handler function you invoke yourself once your LLM picks a tool.

v0.1.0 - 7 tools:
  calculator, file ops (read/write/list), get_current_datetime,
  add_to_date, date_difference, convert_length, convert_weight,
  convert_temperature, parse_json, parse_csv, regex_extract,
  word_count, text_case_transform, ingest_document (needs the
  optional ragleap-rag dependency: pip install ragleap-tools[ingest])

v0.1.1 - search_documents: wraps ragleap-rag's retrieve() for hybrid
vector+keyword search over already-ingested documents. Same optional
ragleap-rag dependency as ingest_document. Accepts an optional
filename= to scope search to one document (requires that document
was ingested via this package's ingest_document tool, which as of
v0.1.1 now stores filename as metadata - previously it stored none,
silently making per-document filtering impossible).

v0.2.0 - search_web: pluggable, bring-your-own-key web search
(SearchProvider ABC; Tavily and Serper reference providers), standard
library only - no new dependency. See docs/design/web-search-tool.md.

v0.3.0 - search_github_repositories: GitHub repository search via the
REST API; the token is optional. Standard library only. See
docs/design/web-search-tool.md.

v0.4.0 - describe_image: pluggable, bring-your-own-key image
description (VisionProvider ABC; Gemini and Anthropic reference
providers). Reads images only from the sandbox directory; URLs are
deliberately not accepted. Standard library only. See
docs/design/vision-tool.md.

v0.4.1 - hardening of the network tools: every HTTP provider now has a
response-size cap and a hard total deadline (a server that drips bytes
can no longer hold a tool-calling loop), and Tool.call accepts a tool
argument literally named "self". New optional fields max_response_bytes
and total_timeout on the providers and GitHubSearchConfig. Standard
library only. See docs/design/http-limits.md.

v0.5.0 - easy to extend, safe to dispatch: @tool turns a type-annotated
function into a Tool (schema from the annotations and docstring), Toolkit
collects tools and dispatches a model's call safely (JSON arguments parsed
and size-limited, arguments validated against the schema before the handler
runs, exceptions contained), ToolError for expected failures,
Tool.metadata, Tool.to_anthropic_schema(), default_toolkit(). Standard
library only. See docs/design/tool-authoring.md.

Deliberately out of scope for now (each needs its own security-focused
design pass, not a rushed inclusion here): code execution,
HTTP fetch, and database/business-system connectors (SQL,
CRM, payment processors, etc.) - the last of those specifically because
some of the connectors this ecosystem already has elsewhere (e.g. a
live payment processor) would be a materially different risk if
exposed to LLM tool-calling without deliberate guardrails.
"""

from ragleap_tools._schema import ToolDefinitionError, validate_arguments
from ragleap_tools.strict import strict_schema
from ragleap_tools.base import Tool, ToolError, ToolResult
from ragleap_tools.decorator import tool
from ragleap_tools.toolkit import Toolkit
from ragleap_tools.governed import GovernedToolkit
from ragleap_tools.calculator import CALCULATOR_TOOL
from ragleap_tools.datetime_tools import ADD_TO_DATE_TOOL, CURRENT_DATETIME_TOOL, DATE_DIFFERENCE_TOOL
from ragleap_tools.file_ops import FileOpsConfig, make_file_tools
from ragleap_tools.ingest import IngestConfig, make_ingest_tool
from ragleap_tools.json_csv import PARSE_CSV_TOOL, PARSE_JSON_TOOL
from ragleap_tools.search import SearchConfig, make_search_tool
from ragleap_tools.text_utils import REGEX_EXTRACT_TOOL, TEXT_CASE_TRANSFORM_TOOL, WORD_COUNT_TOOL
from ragleap_tools.unit_conversion import CONVERT_LENGTH_TOOL, CONVERT_TEMPERATURE_TOOL, CONVERT_WEIGHT_TOOL
from ragleap_tools.web_search import (
    SearchProvider,
    SerperSearchProvider,
    TavilySearchProvider,
    WebSearchConfig,
    make_web_search_tool,
)
from ragleap_tools.github_search import GitHubSearchConfig, make_github_search_tool
from ragleap_tools.vision import (
    AnthropicVisionProvider,
    GeminiVisionProvider,
    VisionConfig,
    VisionProvider,
    make_vision_tool,
)

__version__ = "0.7.0"

# Tools with no config/state needed - usable directly.
STATELESS_TOOLS = [
    CALCULATOR_TOOL,
    CURRENT_DATETIME_TOOL,
    ADD_TO_DATE_TOOL,
    DATE_DIFFERENCE_TOOL,
    CONVERT_LENGTH_TOOL,
    CONVERT_WEIGHT_TOOL,
    CONVERT_TEMPERATURE_TOOL,
    PARSE_JSON_TOOL,
    PARSE_CSV_TOOL,
    REGEX_EXTRACT_TOOL,
    WORD_COUNT_TOOL,
    TEXT_CASE_TRANSFORM_TOOL,
]



def default_toolkit() -> Toolkit:
    """A new Toolkit holding the 12 STATELESS_TOOLS - a start-here toolbox
    that needs no configuration."""
    return Toolkit(STATELESS_TOOLS)


__all__ = [
    "Tool",
    "ToolResult",
    "ToolError",
    "ToolDefinitionError",
    "strict_schema",
    "tool",
    "Toolkit",
    "GovernedToolkit",
    "default_toolkit",
    "validate_arguments",
    "STATELESS_TOOLS",
    "CALCULATOR_TOOL",
    "CURRENT_DATETIME_TOOL",
    "ADD_TO_DATE_TOOL",
    "DATE_DIFFERENCE_TOOL",
    "CONVERT_LENGTH_TOOL",
    "CONVERT_WEIGHT_TOOL",
    "CONVERT_TEMPERATURE_TOOL",
    "PARSE_JSON_TOOL",
    "PARSE_CSV_TOOL",
    "REGEX_EXTRACT_TOOL",
    "WORD_COUNT_TOOL",
    "TEXT_CASE_TRANSFORM_TOOL",
    "FileOpsConfig",
    "make_file_tools",
    "IngestConfig",
    "make_ingest_tool",
    "SearchConfig",
    "make_search_tool",
    "SearchProvider",
    "TavilySearchProvider",
    "SerperSearchProvider",
    "WebSearchConfig",
    "make_web_search_tool",
    "GitHubSearchConfig",
    "make_github_search_tool",
    "VisionProvider",
    "GeminiVisionProvider",
    "AnthropicVisionProvider",
    "VisionConfig",
    "make_vision_tool",
    "__version__",
]
