import test from "node:test";
import assert from "node:assert/strict";
import {
  configure_json_schema_validator,
  parse_and_validate,
  parse_and_validate_object,
} from "../dist/structured.js";

test("valid JSON is parsed and checked against the top-level type", () => {
  configure_json_schema_validator();
  const result = parse_and_validate('{"name":"test"}', { type: "object" });

  assert.deepEqual(result.parsed, { name: "test" });
  assert.equal(result.is_valid, true);
  assert.equal(result.validation_method, "basic_type_check_only");
});

test("fallback does not claim to validate nested required fields", () => {
  const result = parse_and_validate(
    '{"other":"value"}',
    { type: "object", required: ["name"] },
  );

  assert.equal(result.is_valid, true);
  assert.equal(result.validation_method, "basic_type_check_only");
});

test("fallback catches top-level type mismatches", () => {
  const result = parse_and_validate('["not","an","object"]', {
    type: "object",
  });

  assert.deepEqual(result.parsed, ["not", "an", "object"]);
  assert.equal(result.is_valid, false);
});

test("malformed JSON returns null and false", () => {
  const result = parse_and_validate("not valid JSON{{{", { type: "object" });

  assert.equal(result.parsed, null);
  assert.equal(result.is_valid, false);
});

test("already-parsed objects are supported", () => {
  const result = parse_and_validate_object({ name: "test" }, {
    type: "object",
  });

  assert.deepEqual(result.parsed, { name: "test" });
  assert.equal(result.is_valid, true);
});

test("registered validator performs full validation and failures preserve parsed data", () => {
  configure_json_schema_validator((value, schema) => {
    if (schema.required?.includes("name") &&
        !(value && typeof value === "object" && "name" in value)) {
      throw new Error("Required property 'name' is missing");
    }
  });

  const result = parse_and_validate('{"other":"value"}', {
    type: "object",
    required: ["name"],
  });

  assert.deepEqual(result.parsed, { other: "value" });
  assert.equal(result.is_valid, false);
  assert.equal(result.validation_method, "jsonschema");

  configure_json_schema_validator();
});

test("validator can be reset", () => {
  configure_json_schema_validator();
  const result = parse_and_validate('{"x":1}', { type: "object" });
  assert.equal(result.validation_method, "basic_type_check_only");
});
