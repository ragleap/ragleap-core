"""Adapters for the providers' current tool formats, and strict_schema()."""

import copy
import json
from typing import Literal, Optional

import pytest

from ragleap_tools import Toolkit, ToolDefinitionError, default_toolkit, strict_schema, tool
from ragleap_tools.strict import PROVIDERS


# ---------------------------------------------------------------------------
# Independent checkers for what the providers' documents say strict mode needs.
# They look only at an output schema, never at the code under test.
# ---------------------------------------------------------------------------

_OPENAI_KEYS = {"type", "description", "enum", "const", "properties", "required", "additionalProperties", "items", "anyOf"}
_ANTHROPIC_KEYS = _OPENAI_KEYS | {"default", "pattern", "format"}


def _walk(schema, rules, provider, path="root"):
    assert isinstance(schema, dict), path
    assert set(schema) <= rules, (path, sorted(set(schema) - rules))
    if schema.get("type") == "object" or "properties" in schema:
        assert schema["additionalProperties"] is False, path
        props = schema["properties"]
        if provider == "openai":
            assert schema["required"] == list(props), path
        else:
            assert set(schema.get("required", [])) <= set(props), path
        for name, sub in props.items():
            _walk(sub, rules, provider, path + "." + name)
    if "items" in schema:
        _walk(schema["items"], rules, provider, path + "[]")
    for i, branch in enumerate(schema.get("anyOf", [])):
        _walk(branch, rules, provider, "%s|%d" % (path, i))
    if "type" not in schema:
        assert any(k in schema for k in ("anyOf", "const", "enum")), path


def check_strict(schema, provider):
    assert schema["type"] == "object"
    assert "anyOf" not in schema  # a root object cannot be anyOf
    _walk(schema, _OPENAI_KEYS if provider == "openai" else _ANTHROPIC_KEYS, provider)


@tool
def lookup(order_id: str, status: Literal["open", "closed"] = "open", limit: int = 10, tags: Optional[list] = None) -> dict:
    """Look up an order.

    Args:
        order_id: The order id.
        status: Which orders to search.
        limit: How many to return.
        tags: Only orders with these tags.
    """
    return {"order_id": order_id, "status": status, "limit": limit, "tags": tags}


NESTED = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Who", "title": "Name"},
        "address": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "zip": {"type": "string"}},
            "required": ["city"],
        },
        "items": {"type": "array", "items": {"type": "object", "properties": {"sku": {"type": "string"}, "qty": {"type": "integer", "default": 1}}, "required": ["sku"]}},
        "note": {"type": "string", "default": "none", "examples": ["x"]},
    },
    "required": ["name"],
}


# ---------------------------------------------------------------------------
# The existing three methods keep their output
# ---------------------------------------------------------------------------

def test_existing_methods_are_unchanged_for_every_builtin_tool():
    kit = default_toolkit()
    for t in kit.get_all() if hasattr(kit, "get_all") else [kit.get(n) for n in kit.names()]:
        assert t.to_openai_schema() == {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        assert t.to_gemini_schema() == {"name": t.name, "description": t.description, "parameters": t.parameters}
        assert t.to_anthropic_schema() == {"name": t.name, "description": t.description, "input_schema": t.parameters}
        assert list(t.to_openai_schema()["function"]) == ["name", "description", "parameters"]
        assert list(t.to_anthropic_schema()) == ["name", "description", "input_schema"]
    assert kit.to_openai_schema() == [kit.get(n).to_openai_schema() for n in kit.names()]
    assert kit.to_gemini_schema() == [kit.get(n).to_gemini_schema() for n in kit.names()]
    assert kit.to_anthropic_schema() == [kit.get(n).to_anthropic_schema() for n in kit.names()]


def test_default_output_does_not_contain_new_keys():
    assert "strict" not in lookup.to_openai_schema()["function"]
    assert "strict" not in lookup.to_anthropic_schema()
    assert "input_examples" not in lookup.to_anthropic_schema()


# ---------------------------------------------------------------------------
# New shapes
# ---------------------------------------------------------------------------

def test_openai_responses_shape_is_flat_and_strict_is_always_explicit():
    out = lookup.to_openai_responses_schema()
    assert list(out) == ["type", "name", "description", "parameters", "strict"]
    assert out["type"] == "function" and out["name"] == "lookup"
    assert out["strict"] is False
    assert out["parameters"] == lookup.parameters
    assert "function" not in out


def test_openai_responses_strict_true_uses_the_openai_transform():
    out = lookup.to_openai_responses_schema(strict=True)
    assert out["strict"] is True
    assert out["parameters"] == strict_schema(lookup.parameters, provider="openai")
    check_strict(out["parameters"], "openai")


def test_chat_completions_strict_sits_inside_the_function_object():
    out = lookup.to_openai_schema(strict=True)
    assert out["function"]["strict"] is True
    assert out["function"]["parameters"] == strict_schema(lookup.parameters, provider="openai")
    assert "strict" not in out


def test_gemini_tool_shape_has_type_function_and_gemini_schema_is_unchanged():
    out = lookup.to_gemini_tool_schema()
    assert out == {"type": "function", "name": "lookup", "description": lookup.description, "parameters": lookup.parameters}
    assert "type" not in lookup.to_gemini_schema()


def test_anthropic_strict_uses_the_anthropic_transform():
    out = lookup.to_anthropic_schema(strict=True)
    assert out["strict"] is True
    assert out["input_schema"] == strict_schema(lookup.parameters, provider="anthropic")
    check_strict(out["input_schema"], "anthropic")


def test_anthropic_input_examples_are_validated_and_copied():
    examples = [{"order_id": "A-1"}, {"order_id": "A-2", "status": "closed", "limit": 5}]
    out = lookup.to_anthropic_schema(input_examples=examples)
    assert out["input_examples"] == examples
    examples[0]["order_id"] = "changed"
    assert out["input_examples"][0]["order_id"] == "A-1"      # a copy, not an alias
    assert lookup.to_anthropic_schema(input_examples=[])["input_examples"] == []


@pytest.mark.parametrize("bad", [[{"status": "open"}], [{"order_id": 5}], [{"order_id": "A", "extra": 1}], ["not a dict"], [None]])
def test_invalid_input_examples_raise(bad):
    with pytest.raises(ToolDefinitionError, match=r"input_examples\[0\]"):
        lookup.to_anthropic_schema(input_examples=bad)


def test_input_examples_must_be_a_list_of_dicts():
    for bad in ({"order_id": "A"}, "text", 5):
        with pytest.raises(ToolDefinitionError):
            lookup.to_anthropic_schema(input_examples=bad)


@pytest.mark.parametrize("method", ["to_openai_schema", "to_openai_responses_schema", "to_anthropic_schema"])
@pytest.mark.parametrize("bad", ["yes", 1, None, "false"])
def test_strict_must_be_a_real_bool(method, bad):
    with pytest.raises(TypeError):
        getattr(lookup, method)(strict=bad)
    with pytest.raises(TypeError):
        getattr(Toolkit([lookup]), method)(strict=bad)


def test_strict_is_keyword_only():
    with pytest.raises(TypeError):
        lookup.to_openai_schema(True)
    with pytest.raises(TypeError):
        lookup.to_anthropic_schema(True)


def test_toolkit_lists_match_the_tools():
    kit = Toolkit([lookup])
    assert kit.to_openai_responses_schema(strict=True) == [lookup.to_openai_responses_schema(strict=True)]
    assert kit.to_openai_schema(strict=True) == [lookup.to_openai_schema(strict=True)]
    assert kit.to_gemini_tool_schema() == [lookup.to_gemini_tool_schema()]
    assert kit.to_anthropic_schema(strict=True) == [lookup.to_anthropic_schema(strict=True)]


def test_toolkit_error_names_the_tool():
    from ragleap_tools import Tool, ToolResult

    bad = Tool("odd", "d", {"type": "object", "properties": {"n": {"type": "integer", "minimum": 0}}}, lambda **k: ToolResult(True, 1))
    with pytest.raises(ToolDefinitionError, match=r"tool 'odd'.*minimum"):
        Toolkit([bad]).to_openai_responses_schema(strict=True)
    assert Toolkit([bad]).to_openai_responses_schema()[0]["strict"] is False   # non-strict still works


# ---------------------------------------------------------------------------
# strict_schema: OpenAI
# ---------------------------------------------------------------------------

def test_openai_makes_every_property_required_and_optionals_nullable():
    out = strict_schema(lookup.parameters, provider="openai")
    assert out["required"] == ["order_id", "status", "limit", "tags"]
    assert out["additionalProperties"] is False
    assert out["properties"]["order_id"] == {"type": "string", "description": "The order id."}
    status = out["properties"]["status"]
    assert status["description"] == "Which orders to search."          # hoisted to the outer level
    assert status["anyOf"] == [{"type": "string", "enum": ["open", "closed"]}, {"type": "null"}]
    assert "default" not in json.dumps(out)
    check_strict(out, "openai")


def test_openai_nested_objects_and_array_items_are_made_strict():
    out = strict_schema(NESTED, provider="openai")
    check_strict(out, "openai")
    address = out["properties"]["address"]["anyOf"][0]
    assert address["required"] == ["city", "zip"]
    assert address["properties"]["zip"]["anyOf"][1] == {"type": "null"}
    item = out["properties"]["items"]["anyOf"][0]["items"]
    assert item["required"] == ["sku", "qty"] and item["additionalProperties"] is False
    assert "title" not in json.dumps(out) and "examples" not in json.dumps(out)


def test_openai_does_not_wrap_a_value_that_already_allows_null():
    schema = {"type": "object", "properties": {"a": {"type": ["string", "null"]}, "b": {"anyOf": [{"type": "integer"}, {"type": "null"}]}, "c": {"type": "null"}}}
    out = strict_schema(schema, provider="openai")
    assert out["properties"]["a"] == {"type": ["string", "null"]}
    assert out["properties"]["b"] == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert out["properties"]["c"] == {"type": "null"}


def test_openai_is_idempotent():
    once = strict_schema(NESTED, provider="openai")
    assert strict_schema(once, provider="openai") == once


@pytest.mark.parametrize("keyword,value", [("pattern", "^a$"), ("format", "date")])
def test_openai_refuses_pattern_and_format(keyword, value):
    schema = {"type": "object", "properties": {"d": {"type": "string", keyword: value}}, "required": ["d"]}
    with pytest.raises(ToolDefinitionError, match=keyword):
        strict_schema(schema, provider="openai")


# ---------------------------------------------------------------------------
# strict_schema: Anthropic
# ---------------------------------------------------------------------------

def test_anthropic_keeps_optionals_optional_and_keeps_defaults():
    out = strict_schema(lookup.parameters, provider="anthropic")
    assert out["required"] == ["order_id"]
    assert out["additionalProperties"] is False
    assert out["properties"]["status"]["default"] == "open"
    assert out["properties"]["limit"]["default"] == 10
    assert "anyOf" not in json.dumps(out)
    check_strict(out, "anthropic")


def test_anthropic_nested_and_pattern_format_are_kept():
    schema = copy.deepcopy(NESTED)
    schema["properties"]["when"] = {"type": "string", "format": "date", "pattern": "^2"}
    out = strict_schema(schema, provider="anthropic")
    check_strict(out, "anthropic")
    assert out["properties"]["when"] == {"type": "string", "format": "date", "pattern": "^2"}
    assert out["properties"]["address"]["required"] == ["city"]
    assert out["properties"]["items"]["items"]["properties"]["qty"]["default"] == 1
    assert "title" not in json.dumps(out) and "examples" not in json.dumps(out)


def test_anthropic_is_idempotent():
    once = strict_schema(NESTED, provider="anthropic")
    assert strict_schema(once, provider="anthropic") == once


# ---------------------------------------------------------------------------
# strict_schema: refusals and safety, both providers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("keyword,value", [
    ("minimum", 0), ("maximum", 9), ("exclusiveMinimum", 0), ("exclusiveMaximum", 9), ("multipleOf", 2),
    ("minLength", 1), ("maxLength", 5), ("minItems", 1), ("maxItems", 3), ("uniqueItems", True),
    ("oneOf", [{"type": "string"}]), ("allOf", [{"type": "string"}]), ("not", {"type": "string"}), ("$ref", "#/x"),
    ("patternProperties", {}), ("unknownKeyword", 1),
])
def test_unsupported_keywords_are_refused_with_the_keyword_in_the_message(provider, keyword, value):
    schema = {"type": "object", "properties": {"x": {"type": "string", keyword: value}}, "required": ["x"]}
    with pytest.raises(ToolDefinitionError) as excinfo:
        strict_schema(schema, provider=provider)
    assert keyword in str(excinfo.value) and "parameters.x" in str(excinfo.value)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_other_refusals(provider):
    def obj(**props):
        return {"type": "object", "properties": props}
    for bad in (
        obj(a={}),                                                      # no type
        obj(a={"type": "string", "enum": [{"x": 1}]}),                  # enum of objects
        obj(a={"type": "string", "enum": []}),
        obj(a={"anyOf": []}),
        obj(a={"type": "array", "items": [{"type": "string"}]}),        # tuple form
        {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True},
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["missing"]},
        {"type": "string"},                                              # root must be an object
        {"properties": {}},
    ):
        with pytest.raises(ToolDefinitionError):
            strict_schema(bad, provider=provider)
    for bad_params in ("not a schema", None, 5, []):
        with pytest.raises(ToolDefinitionError):
            strict_schema(bad_params, provider=provider)


def test_unknown_provider_is_refused():
    for bad in ("gemini", "OpenAI", "", None):
        with pytest.raises(ToolDefinitionError):
            strict_schema(lookup.parameters, provider=bad)


def test_provider_is_keyword_only():
    with pytest.raises(TypeError):
        strict_schema(lookup.parameters, "openai")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_input_is_never_modified_and_the_output_is_a_deep_copy(provider):
    before = copy.deepcopy(NESTED)
    out = strict_schema(NESTED, provider=provider)
    assert NESTED == before
    out["properties"]["name"]["description"] = "changed"
    out["properties"]["address"] = None
    assert NESTED == before


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_tool_without_parameters_is_strict_too(provider):
    out = strict_schema({"type": "object", "properties": {}}, provider=provider)
    assert out["additionalProperties"] is False and out["properties"] == {}
    assert out.get("required", []) == []
    out2 = strict_schema({"type": "object"}, provider=provider)
    assert out2["properties"] == {} and out2["additionalProperties"] is False


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_builtin_tool_can_be_made_strict(provider):
    kit = default_toolkit()
    assert len(kit.names()) == 12
    for name in kit.names():
        t = kit.get(name)
        out = strict_schema(t.parameters, provider=provider)
        check_strict(out, provider)
        assert t.parameters == default_toolkit().get(name).parameters   # untouched


# ---------------------------------------------------------------------------
# What the strict schema changes for the model, and what it does not change for us
# ---------------------------------------------------------------------------

def test_null_for_an_optional_reaches_the_handler_as_omitted_through_toolkit_call():
    kit = Toolkit([lookup])
    strict = kit.to_openai_responses_schema(strict=True)[0]["parameters"]
    assert "limit" in strict["required"]                                  # the model is told to send every property...
    result = kit.call("lookup", json.dumps({"order_id": "A-1", "status": None, "limit": None, "tags": None}))
    assert result.success and result.result == {"order_id": "A-1", "status": "open", "limit": 10, "tags": None}   # ...null means omitted


def test_validation_still_uses_the_original_schema():
    kit = Toolkit([lookup])
    kit.to_openai_responses_schema(strict=True)
    bad = kit.call("lookup", {"order_id": 5})
    assert not bad.success
    unknown = kit.call("lookup", {"order_id": "A", "nope": 1})
    assert not unknown.success
    assert lookup.parameters["required"] == ["order_id"]


def test_builtin_calls_still_work_after_building_strict_schemas():
    kit = default_toolkit()
    for provider_method in ("to_openai_responses_schema", "to_anthropic_schema"):
        getattr(kit, provider_method)(strict=True)
    assert kit.call("calculator", {"expression": "2 + 3 * 4"}).success


# ---------------------------------------------------------------------------
# Malformed input, nested unions, objects without a type, copies of enums
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider", PROVIDERS)
def test_malformed_required_and_properties_are_refused(provider):
    for bad in (
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": "a"},
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": [1]},
        {"type": "object", "properties": ["a"]},
        {"type": "object", "properties": "a"},
        {"type": "object", "properties": {"a": {"type": "string", "required": ["x"]}}},   # required on a non-object
    ):
        with pytest.raises(ToolDefinitionError):
            strict_schema(bad, provider=provider)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_objects_inside_a_union_are_made_strict(provider):
    schema = {"type": "object", "properties": {"x": {"anyOf": [{"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}, {"type": "null"}]}}, "required": ["x"]}
    out = strict_schema(schema, provider=provider)
    check_strict(out, provider)
    branch = out["properties"]["x"]["anyOf"][0]
    assert branch["additionalProperties"] is False and branch["required"] == ["a"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_nested_object_without_a_type_keyword_is_still_an_object(provider):
    schema = {"type": "object", "properties": {"o": {"properties": {"a": {"type": "string"}}, "required": ["a"]}}, "required": ["o"]}
    out = strict_schema(schema, provider=provider)
    assert out["properties"]["o"]["additionalProperties"] is False
    assert out["properties"]["o"]["required"] == ["a"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_enum_lists_are_copied_not_shared(provider):
    schema = {"type": "object", "properties": {"s": {"type": "string", "enum": ["a", "b"]}}, "required": ["s"]}
    out = strict_schema(schema, provider=provider)
    out["properties"]["s"]["enum"].append("z")
    assert schema["properties"]["s"]["enum"] == ["a", "b"]
