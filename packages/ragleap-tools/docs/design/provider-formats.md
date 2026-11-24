# Current provider tool formats and the Python floor (proposal for ragleap-tools 0.7.0)

Status: proposal, written 2026-11-24. Nothing in this note is implemented, and
the open decisions at the end have not been taken by the maintainer.

## Problem

`ragleap-tools` emits tool schemas for three providers: `Tool.to_openai_schema()`,
`Tool.to_gemini_schema()` and `Tool.to_anthropic_schema()`, and the same three on
`Toolkit`, which return lists. They were written for the formats of 2025. Their
output has never been compared with the providers' current documentation and has
never been sent to a provider (the 0.5.0 changelog and the packages site both say
so).

Reading the providers' current documentation on 2026-11-23 and 2026-11-24 shows these
differences:

1. OpenAI. The function-calling guide documents a flat tool for the Responses API:
   `type`, `name`, `description`, `parameters` and `strict` at one level. Our
   `to_openai_schema()` emits the older Chat Completions shape, with the definition
   nested under a `function` key. The same guide states that the newest GPT-6
   models require the Responses API for tool calling, so for those models our output
   cannot be used as it is. A search excerpt of the guide also says that Responses
   treats tools as strict unless the definition says `strict: false`; that sentence
   must be re-read on the full page before it is relied on (see Verification status).
2. Gemini. The current function-calling guide defines a function as a flat tool
   (`type: "function"`, `name`, `description`, `parameters`) used through a new
   Interactions API. Our `to_gemini_schema()` emits a bare declaration without
   `type`, which matches the older `generateContent` declarations.
3. Anthropic. Our output matches the current format (`name`, `description`,
   `input_schema`). Two optional fields are missing: a top-level `strict: true`
   (grammar-constrained sampling) and `input_examples`. The guide also says that
   forced tool choice (`tool_choice` of `any` or `tool`) returns a 400 error on the
   newest Claude models; that concerns the caller's request, not our schemas, and
   belongs in the documentation.
4. Strict modes and our schemas. Read in code (2026-11-24): the schemas we generate
   never set `additionalProperties`, list optional parameters outside `required`,
   and keep `default` for optional parameters. The strict modes of OpenAI and
   Anthropic ask for stricter schemas (OpenAI's own examples set
   `additionalProperties: false` and list every property in `required`), so strict
   mode cannot be switched on by a flag alone: the schema the provider sees has to
   be transformed.
5. Python. Python 3.10 reached end of life in October 2026 (3.10.22 was its final
   release) and 3.15 has been released, so the supported versions are 3.11 to 3.15.
   `requires-python` is still `>=3.10` and CI runs 3.10, 3.11 and 3.12.

Goal: additive adapters for the current formats, an explicit and checked strict
transform, and a supported Python range, without changing any existing output.

## Design (proposal)

### 1. New and extended adapters (existing output stays byte-for-byte the same)

On `Tool`, and the same on `Toolkit` returning a list:

- `to_openai_responses_schema(*, strict=False)` returns
  `{"type": "function", "name", "description", "parameters", "strict"}`. `strict` is
  always present and explicit, so the result never depends on a provider default.
  With `strict=True` the `parameters` are the result of `strict_schema()` below.
- `to_gemini_tool_schema()` returns `{"type": "function", "name", "description",
  "parameters"}` for the Interactions API. `to_gemini_schema()` is unchanged.
- `to_anthropic_schema(*, strict=False, input_examples=None)`. With the defaults the
  output is identical to today's. `strict=True` adds `"strict": true` and uses
  `strict_schema()`. `input_examples` (a list of argument dicts) adds
  `"input_examples"`; every example is checked with `validate_arguments()` first and
  an invalid one raises `ToolDefinitionError`.
- `to_openai_schema(*, strict=False)` (Chat Completions) gains the same keyword, with
  the unchanged output as the default; `strict=True` puts `"strict": true` inside the
  `function` object.

On `Toolkit` the Anthropic method takes only `strict` (examples belong to one tool).

### 2. `strict_schema(parameters)` (new module `ragleap_tools.strict`, pure function)

Returns a transformed deep copy and never touches the original:

- every object gets `additionalProperties: false`;
- `required` lists every property, in the original order;
- a property that was optional becomes nullable (the exact form, a type list or
  `anyOf` with `{"type": "null"}`, is chosen after reading the providers' documents);
- `default` is removed (the handler keeps its own default);
- a keyword outside the supported subset raises `ToolDefinitionError`. The subset is
  pinned in the code and the tests after the providers' structured-output pages are
  read. Nothing is dropped or rewritten silently.

Why this is safe for our own checking: the toolkit keeps the ORIGINAL parameters.
`Toolkit.prepare` already drops a `null` sent for an optional, non-required
parameter (`drop_null_optionals`, any tool, not only `@tool` ones) and validates
against the original schema. A model that follows the transformed schema and sends
`null` for an optional parameter therefore reaches the handler as if it had omitted
it. A test per built-in tool will prove this instead of assuming it.

### 3. Python floor and CI

- `requires-python = ">=3.11"`, the classifiers list 3.11 to 3.15.
- The CI matrix for `ragleap-tools` (and, in its own change, `ragleap-integrations`
  and `ragleap-agents`) becomes 3.11, 3.12, 3.13, 3.14 and 3.15.
- Users on 3.10 stay on 0.6.0, which keeps working. The changelog says so.

### 4. Release

0.7.0 (a minor version: additive API plus a supported-range change). The per-tenant
toolkit hub that the governed-toolkit note planned for 0.7.0 moves to 0.8.0.

### 5. Not in 0.7.0

Parsing a provider's tool-call response into `Toolkit.call` arguments (the host owns
the loop), OpenAI namespaces, deferred loading and tool search, OpenAI `custom`
(free-text) tools, provider SDK types, and any call to a provider.

## Rejected alternatives

- Change what the existing three methods return. Breaking, and the old shapes are
  still documented by the providers.
- A single `to_schema(provider=...)`. A string switch is easier to get wrong and
  harder to type-check than explicit methods.
- Detect the provider from a model name. Hidden behaviour that goes stale.
- Depend on a provider SDK for its types. The package has no required dependencies.
- `strict=True` by default. A schema that is not strict-compatible would be refused
  by the provider, and a default must never change what existing callers send.
- Best-effort strict transform that drops what it cannot express. It would change
  what the model is told without saying so.

## Security notes

- The adapters are pure functions of a schema: no network, no files, no secrets.
- A provider's strict mode is not a security boundary. `Toolkit.prepare` keeps
  validating every call against the original schema, and `GovernedToolkit` keeps its
  checks. Provider-side strictness is defence in depth that reduces malformed calls.
- `input_examples` are owner-written text that is sent to the provider with every
  request. They must not hold secrets or personal data. They are validated against
  the schema and otherwise left as written.
- A transformed schema changes what the model may send (`null` for optional
  parameters). The tests cover that path end to end through `Toolkit.call`.

## Limitations (planned)

- No provider has accepted any of the new shapes in a live request. The changelog and
  the packages site will say so until a live check is run and dated.
- The strict subset follows the providers' documents at the time of writing and will
  need to follow their changes.
- Gemini's accepted schema subset (its documents mention an OpenAPI-style subset) is
  not yet compared with our generated schemas, which keep `default` and integer
  `enum`.
- `to_openai_responses_schema()` does not use namespaces or deferred loading, so a
  large toolkit still sends every schema on every request.

## Open decisions for the maintainer

Each has a recommendation; "recommend and go" takes them all.

1. Default of the new `strict` keyword. Recommend `False`, always emitted
   explicitly.
2. Strict transform on an unsupported keyword: raise (recommended) or drop.
3. Add `to_gemini_tool_schema()` now (recommended) or wait for the older shape to be
   retired.
4. Raise the Python floor to 3.11 in 0.7.0 (recommended) or in 0.8.0.
5. CI matrix: 3.11 to 3.15 all required (recommended). Python 3.15 is a final
   release, so it is not marked as an allowed failure.
6. Live verification: a script under `scripts/` that sends one real request per
   provider with a key the maintainer supplies, never stored in the repository,
   results dated in the changelog. Recommend yes, run by the maintainer after the
   code is merged and before the release; without it the release says "not
   live-verified".
7. Add the `strict` keyword to the existing Chat Completions method
   (`to_openai_schema`). Recommend yes: small, symmetric, unchanged by default.
8. Version number: 0.7.0 (recommended) or 0.6.1.
9. Slicing: this slice as 0.7.0 and the per-tenant hub as 0.8.0 (recommended), then
   the UTCP manual exporter.

## Verification status

- Read in code (public repository, commit e2b791d7, 2026-11-24): the three adapter
  methods in `base.py` and `toolkit.py`; `drop_null_optionals` and the validator
  keywords in `_schema.py`; `Toolkit.prepare`; `requires-python`, the classifiers
  and the CI matrix (3.10, 3.11, 3.12).
- Observed (a sandbox run on 2026-11-23 or 2026-11-24): the schema generated for a sample
  `@tool` with a required string, a `Literal` with a default, an integer with a
  default and an optional list has no `additionalProperties`, keeps `default`, and
  lists only the required parameter in `required`. The schema of the `calculator`
  tool was printed. The other eleven built-in schemas were not all inspected.
- Documentation only (read on 2026-11-23 and 2026-11-24): OpenAI function-calling guide
  (https://developers.openai.com/api/docs/guides/function-calling), Gemini
  function-calling guide (https://ai.google.dev/gemini-api/docs/function-calling),
  Anthropic define-tools and strict-tool-use pages
  (https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools and
  https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use),
  and secondary reports of the Python 3.15 release and the end of life of 3.10.
  Sources disagree on the exact day of the 3.15.0 release (1 or 9 October 2026).
- Not verified: that any provider accepts the proposed output; the exact strict
  subsets and nullable forms of OpenAI, Anthropic and Gemini (the structured-output
  pages were not read); the sentence about Responses treating tools as strict by
  default (seen only in a search excerpt); that all twelve built-in schemas can be
  transformed; whether any caller depends on the current Gemini shape. No
  prototype exists and no test was run for this proposal.
