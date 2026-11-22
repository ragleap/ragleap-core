"""Strict typing for every field of capability schema v1 (Phase 0, P0-G).

A value of the wrong type must never pass a type, enum, constant or bound check: `1` is not `true`, `"true"` is not
`true`, `True` is not an integer, `1.5` is not an integer, and so on. The walk below covers every field of a declaration
that uses every field of the schema, so a field added to the schema without a place here fails the completeness test.
"""
import copy
import json
import pathlib

import pytest

from core.connector_host.schema import SCHEMA_V1, check_declaration

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUBLISHED = json.loads((ROOT / "docs" / "capability-schema-v1.schema.json").read_text(encoding="utf-8"))

FULL = {
    "schema": "ragleap.capability/1", "id": "example.full", "version": "1.2.3", "kind": "action",
    "operations": [{"name": "send_it", "effect": "external_send", "approval": "always", "taint": "both", "args": "send_args",
                    "grant": "explicit",
                    "inherits": {"max_depth": 1, "max_children": 3, "child_tokens": 8000, "tree_tokens": 30000,
                                 "tools": "parent_subset", "taint": "both_ways"}}],
    "scope": {"level": "user", "user_filter_field": "owner_id"},
    "network": {"hosts": ["api.example.com"], "https_only": True, "egress": "fixed_hosts",
                "tcp": [{"host": "db.example.com", "port": 5432}]},
    "secrets": ["EXAMPLE_TOKEN"],
    "budgets": {"max_calls_per_run": 5, "max_calls_per_day": 100, "max_bytes": 1000, "timeout_s": 20},
    "descriptive": {"title": "Full", "notes": "Uses every field.", "data_classes": ["personal"]},
}

WRONG = {bool: [1, 0, "true", "false", "", None, [], {}],
         int: [True, False, 1.5, "1", None, [], {}],
         str: [1, True, None, [], {}, ["x"]],
         list: ["x", "", {}, None, 1, True],
         dict: ["x", [], None, 1, True, ""]}


def kind(v):
    return bool if isinstance(v, bool) else int if isinstance(v, int) else str if isinstance(v, str) else list if isinstance(v, list) else dict


def paths(v, prefix=()):
    yield prefix, v
    if isinstance(v, dict):
        for k, x in v.items():
            yield from paths(x, prefix + (k,))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from paths(x, prefix + (i,))


def put(doc, path, value):
    d = copy.deepcopy(doc)
    if not path:
        return value
    t = d
    for k in path[:-1]:
        t = t[k]
    t[path[-1]] = value
    return d


def get(doc, path):
    for k in path:
        doc = doc[k]
    return doc


def node_for(schema, instance_path):
    """The schema node for a location of FULL (follows $ref, properties and items)."""
    node = schema
    for k in instance_path:
        while "$ref" in node:
            node = schema["$defs"][node["$ref"].split("/")[-1]]
        node = node["items"] if isinstance(k, int) else node["properties"][k]
    while "$ref" in node:
        node = schema["$defs"][node["$ref"].split("/")[-1]]
    return node


def errors(doc, schema=SCHEMA_V1):
    return [f for f in check_declaration(doc, schema) if f[0] == "error"]


def schema_errors(doc, schema=SCHEMA_V1):
    return [f for f in errors(doc, schema) if f[1] == "E-SCHEMA"]


def all_property_names(node, out=None):
    out = set() if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "properties":
                out.update(v)
            all_property_names(v, out)
    elif isinstance(node, list):
        for v in node:
            all_property_names(v, out)
    return out


def test_the_full_declaration_conforms_and_uses_every_field_of_the_schema():
    assert errors(FULL) == [], errors(FULL)
    names = {k for _, v in paths(FULL) if isinstance(v, dict) for k in v}
    assert names == all_property_names(SCHEMA_V1), names ^ all_property_names(SCHEMA_V1)


def test_both_copies_of_the_schema_judge_the_full_declaration_the_same_way():
    assert SCHEMA_V1 == PUBLISHED
    assert errors(FULL, PUBLISHED) == []


@pytest.mark.parametrize("schema", [SCHEMA_V1, PUBLISHED], ids=["embedded", "published"])
def test_a_value_of_the_wrong_type_is_an_error_at_every_location(schema):
    missed = []
    count = 0
    for path, value in paths(FULL):
        for wrong in WRONG[kind(value)]:
            if kind(wrong) is kind(value):
                continue
            count += 1
            if not schema_errors(put(FULL, path, wrong), schema):
                missed.append((path, wrong))
    assert count > 250 and missed == [], missed


@pytest.mark.parametrize("schema", [SCHEMA_V1, PUBLISHED], ids=["embedded", "published"])
def test_the_values_one_and_the_text_true_never_stand_in_for_the_constant_true(schema):
    for path in (("network", "https_only"),):
        assert get(FULL, path) is True
        for wrong in (1, "true", "True", "yes"):
            assert schema_errors(put(FULL, path, wrong), schema), (path, wrong)
        assert schema_errors(put(FULL, path, True), schema) == []


def test_every_integer_and_string_and_list_bound_is_enforced():
    checked = 0
    for path, value in paths(FULL):
        node = node_for(SCHEMA_V1, path)
        if isinstance(value, int) and not isinstance(value, bool):
            for key, delta in (("minimum", -1), ("maximum", 1)):
                if key in node:
                    checked += 1
                    assert schema_errors(put(FULL, path, node[key] + delta)), (path, key)
                    assert not schema_errors(put(FULL, path, node[key])), (path, key)
        if isinstance(value, str) and "maxLength" in node:
            checked += 1
            assert schema_errors(put(FULL, path, "a" * (node["maxLength"] + 1))), path
        if isinstance(value, str) and "pattern" in node:
            checked += 1
            assert schema_errors(put(FULL, path, "!! not allowed !!")), path
        if isinstance(value, list) and "maxItems" in node:
            checked += 1
            assert schema_errors(put(FULL, path, [value[0]] * (node["maxItems"] + 1))), path
    assert checked >= 12


def test_every_enum_rejects_every_other_value_including_other_types():
    checked = 0
    for path, value in paths(FULL):
        node = node_for(SCHEMA_V1, path)
        if "enum" in node:
            checked += 1
            for wrong in ("zzz", "", 1, True, None, [value]):
                assert schema_errors(put(FULL, path, wrong)), (path, wrong)
            for ok in node["enum"]:
                assert not schema_errors(put(FULL, path, ok)), (path, ok)
    assert checked >= 8


def test_every_object_rejects_an_unknown_field():
    for path, value in paths(FULL):
        if isinstance(value, dict):
            doc = put(FULL, path, dict(value, zz_unknown=1))
            assert schema_errors(doc), path


REQUIRED = {(): ["schema", "id", "version", "kind", "operations"], ("operations", 0): ["name", "effect"],
            ("scope",): ["level"], ("network", "tcp", 0): ["host", "port"], ("operations", 0, "inherits"): ["max_depth", "max_children"]}


def test_every_required_field_is_required():
    for path, keys in REQUIRED.items():
        for k in keys:
            d = copy.deepcopy(FULL)
            del get(d, path)[k]
            assert schema_errors(d), (path, k)


def test_the_list_of_required_fields_is_the_one_in_the_schema():
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "required":
                    found.update(v)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(SCHEMA_V1)
    assert found == {k for keys in REQUIRED.values() for k in keys}
