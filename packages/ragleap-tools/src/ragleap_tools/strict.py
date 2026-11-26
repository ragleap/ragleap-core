"""strict_schema(): the parameters schema a provider's strict mode accepts.

OpenAI and Anthropic can constrain a model's tool arguments to a JSON Schema
(strict mode), but both accept only a subset of JSON Schema, and the two
subsets differ. strict_schema() turns a tool's own parameters into the form a
provider's strict mode takes and refuses, with an error that names the
keyword, anything it cannot express. It never changes the tool: the Toolkit
keeps validating every call against the ORIGINAL parameters, so provider-side
strictness is defence in depth, not a replacement for validation.

What each provider's documents say (read 2026-11-23 and 2026-11-24; see
docs/design/provider-formats.md for the sources and what was not verified):

openai (strict function calling)
    supported types are string, number, boolean, integer, object, array, enum
    and anyOf; every property must be listed in ``required`` (an optional
    value is emulated with a union with null); ``additionalProperties`` must
    be false. This module also refuses ``pattern`` and ``format``, which are
    not in the subset listed in the sources read.

anthropic (strict tool use)
    optional properties stay optional; ``additionalProperties`` must be false
    for objects; ``default``, ``pattern`` and ``format`` are supported;
    numeric constraints (minimum, maximum, multipleOf) and string-length
    constraints (minLength, maxLength) are not.

Both: ``title``, ``examples`` and ``$comment`` are annotations that do not
change which values are valid, so they are dropped. oneOf, allOf, $ref and
any other keyword are refused.

Not verified: that either provider accepts the output of this function in a
live request.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping

from ragleap_tools._schema import ToolDefinitionError

__all__ = ["strict_schema", "PROVIDERS"]

PROVIDERS = ("openai", "anthropic")

_STRUCTURAL = ("type", "description", "enum", "const", "properties", "required", "additionalProperties", "items", "anyOf")
_ANNOTATIONS = ("title", "examples", "$comment")
_ANTHROPIC_ONLY = ("pattern", "format")
_UNSUPPORTED = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "minItems", "maxItems", "uniqueItems",
    "oneOf", "allOf", "not", "$ref", "$defs", "definitions", "patternProperties",
)
_SCALARS = (str, int, float, bool, type(None))


def strict_schema(parameters: Mapping[str, Any], *, provider: str) -> Dict[str, Any]:
    """Returns a transformed deep copy of ``parameters`` for ``provider``
    ("openai" or "anthropic"). ``parameters`` is never modified. Raises
    ToolDefinitionError when the schema uses something the provider's strict
    mode cannot take. Nothing is dropped or rewritten silently apart from the
    annotations and ``default`` rules above."""
    if provider not in PROVIDERS:
        raise ToolDefinitionError("strict_schema: provider must be one of %s, got %r" % (", ".join(PROVIDERS), provider))
    if not isinstance(parameters, Mapping):
        raise ToolDefinitionError("strict_schema: parameters must be a JSON Schema object")
    if parameters.get("type") != "object":
        raise ToolDefinitionError("strict_schema: the root schema must have type 'object'")
    return _convert(parameters, provider, "parameters")


def _allows_null(schema: Mapping[str, Any]) -> bool:
    declared = schema.get("type")
    if declared == "null" or (isinstance(declared, list) and "null" in declared):
        return True
    branches = schema.get("anyOf")
    return isinstance(branches, list) and any(isinstance(b, Mapping) and b.get("type") == "null" for b in branches)


def _nullable(schema: Dict[str, Any]) -> Dict[str, Any]:
    """An optional value for OpenAI: the same schema or null. The description
    moves to the outer level so the model reads it once."""
    if _allows_null(schema):
        return schema
    inner = dict(schema)
    outer: Dict[str, Any] = {}
    if "description" in inner:
        outer["description"] = inner.pop("description")
    outer["anyOf"] = [inner, {"type": "null"}]
    return outer


def _convert(node: Any, provider: str, path: str) -> Dict[str, Any]:
    if not isinstance(node, Mapping):
        raise ToolDefinitionError("strict_schema: %s must be a JSON Schema object" % path)
    out: Dict[str, Any] = {}
    original_required: List[str] = []
    for key, value in node.items():
        if key in _ANNOTATIONS:
            continue
        if key == "default":
            if provider == "anthropic":
                out[key] = copy.deepcopy(value)
            continue
        if key in _ANTHROPIC_ONLY:
            if provider != "anthropic":
                raise ToolDefinitionError("strict_schema: %s: keyword %r is not in the subset OpenAI strict mode documents" % (path, key))
            out[key] = copy.deepcopy(value)
            continue
        if key in _UNSUPPORTED:
            raise ToolDefinitionError("strict_schema: %s: keyword %r is not supported by %s strict mode" % (path, key, provider))
        if key not in _STRUCTURAL:
            raise ToolDefinitionError("strict_schema: %s: keyword %r is outside the supported subset" % (path, key))
        if key == "properties":
            if not isinstance(value, Mapping):
                raise ToolDefinitionError("strict_schema: %s: 'properties' must be an object" % path)
            out[key] = {name: _convert(sub, provider, "%s.%s" % (path, name)) for name, sub in value.items()}
        elif key == "items":
            out[key] = _convert(value, provider, path + "[]")
        elif key == "anyOf":
            if not isinstance(value, list) or not value:
                raise ToolDefinitionError("strict_schema: %s: 'anyOf' must be a non-empty list" % path)
            out[key] = [_convert(b, provider, "%s|%d" % (path, i)) for i, b in enumerate(value)]
        elif key == "additionalProperties":
            if value is not False:
                raise ToolDefinitionError("strict_schema: %s: additionalProperties must be false in strict mode" % path)
        elif key == "required":
            if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
                raise ToolDefinitionError("strict_schema: %s: 'required' must be a list of names" % path)
            original_required = list(value)
        elif key == "enum":
            if not isinstance(value, list) or not value or not all(isinstance(v, _SCALARS) for v in value):
                raise ToolDefinitionError("strict_schema: %s: 'enum' must be a non-empty list of strings, numbers, booleans or null" % path)
            out[key] = copy.deepcopy(value)
        else:
            out[key] = copy.deepcopy(value)
    is_object = out.get("type") == "object" or "properties" in out
    if is_object:
        props = out.setdefault("properties", {})
        unknown = [r for r in original_required if r not in props]
        if unknown:
            raise ToolDefinitionError("strict_schema: %s: 'required' names %s which are not properties" % (path, ", ".join(sorted(unknown))))
        out["additionalProperties"] = False
        if provider == "openai":
            for name in list(props):
                if name not in original_required:
                    props[name] = _nullable(props[name])
            out["required"] = list(props)
        else:
            out["required"] = [r for r in original_required]
    elif original_required:
        raise ToolDefinitionError("strict_schema: %s: 'required' is only meaningful on an object" % path)
    elif not any(k in out for k in ("type", "anyOf", "const", "enum")):
        raise ToolDefinitionError("strict_schema: %s: needs a type (a value with no type cannot be expressed in strict mode)" % path)
    return out
