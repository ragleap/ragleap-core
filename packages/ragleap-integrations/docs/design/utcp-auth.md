# UTCP: authentication and real-world manuals (design note)

Status: proposal, written 2026-11-08. It asks the maintainer for decisions and changes no code. What ragleap-integrations 0.5.0 does is not decided by this note. It follows [utcp.md](utcp.md), which designed and describes the 0.4.0 client.

## Why this note

0.4.0 was published on 2026-11-05 with a UTCP client that refuses any tool whose template declares `auth`, and any URL with a query string. On 2026-11-07 and 2026-11-08 it was checked, read-only and without a credential, against a real public manual (Unified.to's). The result is that nothing from that manual can be offered. This note records what was observed, what the code does with it, and the decisions that would change that, with their risks.

## Observations (live, read-only, no credential, no tool call)

- 2026-11-07, about 15:39 UTC: two GET requests to the manual's `/utcp` URL, made by `scripts/utcp_live.py` from the published 0.4.0 wheel. HTTP 200, 237,586 bytes, `utcp_version` 1.0.1, 33 tools, all with call template type `http`. `connect_utcp()` offered none of the 20 tools tried (the script's default limit); each was skipped with the reason "the template declares auth".
- 2026-11-08, about 13:05 UTC: one GET and a structure-only summary (counts of shapes; no descriptions printed). HTTP 200, 242,911 bytes, **35 tools**. The manual had changed in a day, so every number about it is point-in-time.
- The shape was identical for all 35 tools on 2026-11-08:

| Field | Value on all 35 tools |
|---|---|
| call template type, method | `http`, `POST` |
| template keys | `auth`, `body_field`, `call_template_type`, `content_type`, `http_method`, `name`, `url` |
| URL | `https`, same host as the manual, path `/tools/<name>/call`, a query string with only the parameter `format`, no braces |
| auth block | `auth_type` `api_key`, `location` `header`, `var_name` `Authorization`, `api_key` exactly `Bearer ${NAME}` for one fixed variable name |
| body | `body_field` `arguments`, `content_type` `application/json` |
| absent | static headers, `header_fields`, `query_params`, `allowed_communication_protocols` |

## What 0.4.0 does with that shape

Read from `src/ragleap_integrations/utcp.py` on `main` at commit 32ba21c5 (594 lines). `_check_tool` skips a tool, in this order, when: the template is missing or is not `http`; it has a field outside `_KNOWN_TEMPLATE_FIELDS`; the method is missing or unsupported; it declares `auth`; it has `header_fields`; `inputs` is not an object schema or is too large; `body_field` is not an input property, is not allowed for the method, or the content type is not JSON. The URL check (`_check_url`, which needs the properties of `inputs`) and the header check come after the auth rule: a placeholder, a non-https or non-public URL, a query string or fragment, a parameter outside the path, a URL outside the approved origins.

- **Blocker 1, observed:** the `auth` rule. It fired for every tool tried. Because it is reached only after the known-fields rule, the template keys of this manual (including `name` and `auth`) are accepted by that rule. This is inferred from the order of the checks and the observed reason; only the first line of the known-fields set was read.
- **Blocker 2, read in the code, not yet observed:** `_check_url` refuses a query string ("url has a query string or a fragment"), and every URL of this manual carries `?format=...`. It runs after the `auth` rule, so it has never been the reported reason. Fixing auth alone would only move the refusal here.
- **Possible blocker 3, unknown:** `body_field` is `arguments`, and 0.4.0 requires it to be a property of the tool's `inputs`. Whether these manuals define such a property has not been observed.

## Decision 1: the auth block

0.4.0 already sends the owner's configured token as `Authorization: Bearer <token>` to approved origins (shown for discovery at `utcp.py` lines 405 and 406; stated for calls in the 0.4.0 changelog; the call path was not part of the lines read for this note).

- **A. Keep refusing.** No change, no risk; nothing from a manual that declares auth can be used.
- **B. Accept exactly one shape.** `auth_type` `api_key`, `location` `header`, `var_name` exactly `Authorization`, `api_key` exactly `Bearer ${NAME}` for an identifier-like NAME. The client sends the owner's token as it already does and never looks NAME up anywhere (no environment, no substitution). Anything else, including a missing token, stays skipped with a stated reason. This adds no capability: it removes a refusal where the manual declares what the client does anyway.
- **C. Other api_key forms** (another header, a query-string key, a cookie). Not recommended now: a key in a URL ends up in logs and caches, and other locations widen what a manual can make the client send.
- **D. basic and oauth2.** Separate, later slices. oauth2 needs token acquisition from a token endpoint, the largest new network surface, and would need its own note.

Risks of B: the owner's token is used whatever NAME says, so the owner must know the token they configure for a manual is the one its tools expect; and a manual can declare the shape without needing it, which changes nothing about what is sent. **Recommendation: B**, with the observed block as a test fixture and one refusal test per near miss (other header name, other location, other value text, two placeholders, `basic`, `oauth2`, no token configured).

## Decision 2: the query string in tool URLs

- **A. Keep refusing.** Then no tool of this manual can be offered, whatever is decided in Decision 1.
- **B. Accept a fixed set of static parameters** (initially `format`) with plain-token values, merged with the arguments; a collision with an input property skips the tool. The manual decides nothing, but the set is fixed in code for every owner.
- **C. Accept any static query string** without placeholders. Not recommended: a manual could then choose parameters that change server behaviour (debug, redirect or callback style parameters), even though the origin is owner-approved.
- **D. The owner lists the allowed static parameter names** per server in the configuration (for example one name, `format`). The default is empty, which is today's behaviour. Nothing the manual says changes what is sent unless the owner allowed it.

**Recommendation: D**, because it keeps the rule of this package that the owner decides and the manual only supplies arguments. Placeholders, fragments and collisions stay refused in every option.

## Needed before any implementation

1. A structure-only look at `inputs` for the manual's tools (is `body_field` an input property?). One more read-only GET, no credential.
2. The decision of whether to obtain a test credential for a read-only tool call. Without one, a tool call against a real server stays impossible and the claim "no tool call against a real UTCP server has been verified" stays true. A call performs a real operation on the vendor's API, so it is the maintainer's decision, not a default.
3. A reading of the normative UTCP specification (still not read; only plugin documentation, a security page and reference clients were).

## Security notes

- The UTCP security page (read through a search result on 2026-11-07; not the normative specification) cautions that variable substitution introduces injection risks. Nothing in this note adds substitution or environment lookups.
- Both recommendations only remove a refusal where the declaration matches what the client does already, or where the owner explicitly allowed it.
- A manual is untrusted text and a snapshot: the tool list and shapes above can change between two reads, as this one did.

## Verification status

- Observed live, read-only and without a credential: the two checks above.
- Read in code: `utcp.py` on `main` at 32ba21c5, the lines quoted above.
- Documentation only: the vendor's description of its manual and the UTCP security page.
- Not verified: any tool call against a real UTCP server, the normative specification, the full known-fields set, whether `body_field` is an input property in these manuals.

## Open decisions for the maintainer

1. Decision 1: A, B (recommended), or later.
2. Decision 2: A, B, D (recommended), or later.
3. Whether to obtain a test credential for one read-only call.
4. Whether to link this note from the "Open decisions" section of `utcp.md` (this note does not edit it).

## Addendum 2026-11-09: what the `inputs` check found

One more read-only GET (no credential, no tool call, counts only) was made on 2026-11-09, about 03:19 UTC, with a structure-only script. HTTP 200, 320,876 bytes, **37 tools**. The manual keeps changing: 33 tools on 2026-11-07, 35 on 2026-11-08, 37 on 2026-11-09 (237,586, 242,911 and 320,876 bytes).

- For all 37 tools `inputs` is an object schema with exactly one property, and that property is the template's own `body_field` (`arguments`), of type `object` and listed as required. `additionalProperties` is absent on all of them.
- So "Possible blocker 3" above is resolved: the `body_field` rule of 0.4.0 is satisfied by every tool of this manual. The two blockers that remain are the auth rule and the query-string rule, as described above.
- Size: the `inputs` schemas range from 221 to 14,799 characters (median 528). The 0.4.0 check skips a schema larger than `max_schema_chars` (line 283 of `utcp.py`). The default for the MCP client is 10,000 characters and `UtcpConfig` extends `McpConfig`, so the same default is expected here (not read). At least the largest tool would then be skipped with "inputs schema is too large" until the limit is raised, whatever is decided above. This is an expected, stated skip, not a blocker for the other tools. How many tools exceed 10,000 characters was not counted.
- What a model would supply is therefore one nested object, `arguments`, which 0.4.0 would send as the JSON body. Whether that is useful to a model was not assessed.
- Still not verified: any tool call against a real UTCP server, the normative specification, the full known-fields set.
