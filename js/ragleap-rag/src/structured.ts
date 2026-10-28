export type ValidationMethod = "jsonschema" | "basic_type_check_only";

export interface ValidationResult<T = unknown> {
  parsed: T | null;
  is_valid: boolean;
  validation_method: ValidationMethod;
}

export type JsonSchemaValidator = (
  value: unknown,
  schema: Record<string, unknown>,
) => void;

let jsonSchemaValidator: JsonSchemaValidator | undefined;

/**
 * Register a synchronous JSON Schema validator adapter.
 * If none is registered, only the top-level type is checked.
 * The adapter should throw when validation fails.
 */
export function configure_json_schema_validator(
  validator?: JsonSchemaValidator,
): void {
  jsonSchemaValidator = validator;
}

function basicTypeCheck(
  value: unknown,
  schema: Record<string, unknown>,
): boolean {
  const expected = schema.type;

  // Match Python's fallback: unknown or missing types pass.
  switch (expected) {
    case "object":
      return value !== null && typeof value === "object" && !Array.isArray(value);
    case "array":
      return Array.isArray(value);
    case "string":
      return typeof value === "string";
    case "number":
      // Python isinstance(True, (int, float)) is True.
      return typeof value === "number" || typeof value === "boolean";
    case "boolean":
      return typeof value === "boolean";
    default:
      return true;
  }
}

export function parse_and_validate(
  raw_text: string,
  schema: Record<string, unknown>,
): ValidationResult {
  const method: ValidationMethod = jsonSchemaValidator
    ? "jsonschema"
    : "basic_type_check_only";

  let parsed: unknown;

  try {
    if (typeof raw_text !== "string") {
      throw new TypeError("Expected a JSON string");
    }
    parsed = JSON.parse(raw_text);
  } catch {
    return { parsed: null, is_valid: false, validation_method: method };
  }

  return validateParsed(parsed, schema, method);
}

export function parse_and_validate_object<T>(
  obj: T,
  schema: Record<string, unknown>,
): ValidationResult<T> {
  const method: ValidationMethod = jsonSchemaValidator
    ? "jsonschema"
    : "basic_type_check_only";

  return validateParsed(obj, schema, method) as ValidationResult<T>;
}

function validateParsed<T>(
  parsed: T,
  schema: Record<string, unknown>,
  method: ValidationMethod,
): ValidationResult<T> {
  if (!jsonSchemaValidator) {
    return {
      parsed,
      is_valid: basicTypeCheck(parsed, schema),
      validation_method: "basic_type_check_only",
    };
  }

  try {
    jsonSchemaValidator(parsed, schema);
    return { parsed, is_valid: true, validation_method: "jsonschema" };
  } catch (error) {
    console.warn("Structured output failed schema validation:", error);
    return { parsed, is_valid: false, validation_method: method };
  }
}
