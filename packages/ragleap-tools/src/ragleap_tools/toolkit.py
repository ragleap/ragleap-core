"""
ragleap_tools.toolkit

Toolkit - a named collection of Tool objects with safe dispatch, so a
tool-calling loop is a few lines and a bad model call cannot crash it:

    kit = Toolkit(STATELESS_TOOLS).add(my_tool)
    provider_tools = kit.to_openai_schema()          # or to_gemini_schema() / to_anthropic_schema()
    result = kit.call(name_from_model, arguments_from_model)   # never raises

call() accepts the arguments as the JSON string the provider returned or as an
already-parsed dict. It returns a failed ToolResult for an unknown tool,
oversize or invalid JSON, arguments that do not match the tool's schema (before
the handler runs), and any exception from the handler (only the exception type
reaches the model; the detail goes to the "ragleap_tools" logger).

call() is prepare() followed by run(); use them separately to decide something
between validating the arguments and running the handler.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple, Union

from ragleap_tools._schema import clip, drop_null_optionals, safe_text, validate_arguments
from ragleap_tools.base import Tool, ToolError, ToolResult

_LOG = logging.getLogger("ragleap_tools")
DEFAULT_MAX_ARGUMENT_CHARS = 100_000


def _fail(message: str) -> ToolResult:
    return ToolResult(success=False, error=message)


class Toolkit:
    def __init__(self, tools: Iterable[Tool] = (), *, max_arguments_chars: int = DEFAULT_MAX_ARGUMENT_CHARS) -> None:
        if not isinstance(max_arguments_chars, int) or isinstance(max_arguments_chars, bool) or max_arguments_chars < 1:
            raise ValueError("max_arguments_chars must be a positive integer")
        self.max_arguments_chars = max_arguments_chars
        self._tools: Dict[str, Tool] = {}
        for t in tools:
            self.add(t)

    def add(self, tool: Tool) -> "Toolkit":
        """Adds a tool and returns the toolkit (so calls chain). A duplicate
        name raises ValueError: two tools with one name would be ambiguous to
        the model."""
        if not isinstance(tool, Tool):
            raise TypeError("a Toolkit holds Tool objects")
        if not isinstance(tool.name, str) or not tool.name:
            raise ValueError("a tool needs a non-empty name")
        if tool.name in self._tools:
            raise ValueError("duplicate tool name '%s'" % clip(tool.name))
        self._tools[tool.name] = tool
        return self

    def names(self) -> List[str]:
        return list(self._tools)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name) if isinstance(name, str) else None

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(list(self._tools.values()))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __repr__(self) -> str:
        return "Toolkit(%s)" % ", ".join(self._tools)

    def select(
        self,
        include: Optional[Iterable[str]] = None,
        exclude: Iterable[str] = (),
        where: Optional[Callable[[Tool], bool]] = None,
    ) -> "Toolkit":
        """A new Toolkit with a subset of the tools. include/exclude take exact
        names and an unknown name raises ValueError (a typo must not silently
        drop a tool); where is a predicate over Tool (for example
        lambda t: t.metadata.get("read_only"))."""
        include_list = None if include is None else list(include)
        exclude_list = list(exclude)
        for n in (include_list or []) + exclude_list:
            if not isinstance(n, str) or n not in self._tools:
                raise ValueError("unknown tool name '%s'" % clip(n))
        picked = []
        for name, t in self._tools.items():
            if include_list is not None and name not in include_list:
                continue
            if name in exclude_list:
                continue
            if where is not None and not where(t):
                continue
            picked.append(t)
        return Toolkit(picked, max_arguments_chars=self.max_arguments_chars)

    def to_openai_schema(self) -> List[Dict[str, Any]]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def to_gemini_schema(self) -> List[Dict[str, Any]]:
        return [t.to_gemini_schema() for t in self._tools.values()]

    def to_anthropic_schema(self) -> List[Dict[str, Any]]:
        return [t.to_anthropic_schema() for t in self._tools.values()]

    def prepare(self, name: Any, arguments: Any = None) -> Union[Tuple[Tool, Dict[str, Any]], ToolResult]:
        """Everything call() does before the handler runs.

        Finds the tool, parses and size-checks the arguments, drops nulls for
        optional parameters and validates against the tool's schema. Returns
        ``(tool, arguments)`` when the call may proceed, or the failed
        ToolResult that call() would return. The handler is not run. Pass the
        pair to run(). Use prepare() and run() instead of call() when you must
        decide something in between (a policy, an approval, a rate limit).
        """
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _fail("unknown tool '%s'" % clip(name))
        parsed = self._parse(arguments)
        if isinstance(parsed, ToolResult):
            return parsed
        args = drop_null_optionals(tool.parameters, parsed)
        problem = validate_arguments(tool.parameters, args)
        if problem:
            return _fail("invalid arguments: " + problem)
        return tool, args

    def run(self, tool: Tool, arguments: Dict[str, Any]) -> ToolResult:
        """Run the handler of one of this toolkit's tools; never raises.

        Meant for the pair that prepare() returned: it does not validate the
        arguments against the schema, that is prepare()'s job. It refuses a
        tool that is not in this toolkit and arguments that are not a dict of
        string keys, without running anything. Handler failures are contained
        exactly as in call().
        """
        if not any(t is tool for t in self._tools.values()):
            return _fail("unknown tool '%s'" % clip(getattr(tool, "name", None)))
        if not isinstance(arguments, dict):
            return _fail("arguments must be a JSON object")
        if any(not isinstance(k, str) for k in arguments):
            return _fail("argument names must be strings")
        try:
            result = tool.call(**arguments)
        except ToolError as exc:
            return _fail(safe_text(exc) or "%s failed" % tool.name)
        except Exception as exc:  # noqa: BLE001 - one bad tool must not crash the loop
            _LOG.warning("tool %r raised %s", tool.name, type(exc).__name__, exc_info=True)
            return _fail("%s failed (%s)" % (tool.name, type(exc).__name__))
        if not isinstance(result, ToolResult):
            return _fail("%s returned an invalid result" % tool.name)
        return result

    def call(self, name: Any, arguments: Any = None) -> ToolResult:
        prepared = self.prepare(name, arguments)
        if isinstance(prepared, ToolResult):
            return prepared
        tool, args = prepared
        return self.run(tool, args)

    def _parse(self, arguments: Any) -> Any:
        if arguments is None:
            return {}
        if isinstance(arguments, str):
            if len(arguments) > self.max_arguments_chars:
                return _fail("arguments are too large (max %d characters)" % self.max_arguments_chars)
            if not arguments.strip():
                return {}
            try:
                arguments = json.loads(arguments)
            except (ValueError, RecursionError):
                return _fail("arguments are not valid JSON")
        if not isinstance(arguments, dict):
            return _fail("arguments must be a JSON object")
        if any(not isinstance(k, str) for k in arguments):
            return _fail("argument names must be strings")
        return dict(arguments)
