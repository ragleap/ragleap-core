# RagLeap capability declaration (schema ragleap.capability/1, draft)

**Status: in design (draft).** This is Phase 0 of the integration-contract plan. The schema, this guide and a local checker are published so that contributors can build against one contract early. **Nothing in the RagLeap core reads a declaration yet, and no behaviour of the approval gate changes because of this document.** The schema stays "draft" until the 13 data-source connectors have passed the checker (Phase 2); inside version 1 only additive changes will be made.

## Purpose

An integration (an action tool, a data-source connector or an MCP server) writes one JSON document that says what it can do: its operations, whether each one reads or changes something, which hosts it talks to, which secrets it needs and how much it may use. The core is meant to enforce its own rules from that document. A contributor can check the document locally before opening a pull request.

## Trust statement

Until process isolation is available, every connector that runs inside the RagLeap process is trusted code. It can read the same database and secrets as the application. Such connectors are accepted only as reviewed code merged into the repository. Capability declarations and the central gate protect against mistakes and against actions the connector is not meant to take; they do not contain a malicious in-process connector.

## A declaration at a glance

See `docs/capability-examples/` for three complete examples: an action (`crm-action.json`), a data source (`sql-datasource.json`) and an MCP server (`mcp-server.json`).

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | Always `ragleap.capability/1`. |
| `id` | yes | Lower-case dotted name, for example `example.crm`. |
| `version` | yes | Semantic version of the declaration, `major.minor.patch`. |
| `kind` | yes | `action`, `datasource` or `mcp`. |
| `operations` | yes | One or more operations (below). Names must be unique. |
| `scope` | no | `level` is `organization` or `user`. A `user` scope needs `user_filter_field`, the field that holds the user id. |
| `network` | no | `hosts`: public host names (no wildcards, no IP addresses, no internal names). `https_only`: must be `true` when present. `egress`: how far the connector reaches: `fixed_hosts` (the hosts listed here), `owner_configured` (targets the owner configures by name; no host list) or `runtime_chosen` (the address is chosen while the run goes on; no host list; carries the obligations in "Egress classes" below). Without it the class is `fixed_hosts` when hosts or `tcp` are listed, otherwise none. `tcp`: reserved (below). |
| `secrets` | no | Names of settings the connector needs, UPPER_SNAKE_CASE. Names that belong to the core (such as `DATABASE_URL`) cannot be declared. |
| `budgets` | no | `max_calls_per_run`, `max_calls_per_day`, `max_bytes`, `timeout_s`. The core's own ceilings apply on top. |
| `descriptive` | no | `title`, `notes`, `data_classes`. **Not enforced** (below). |

Each **operation** has:

| Field | Required | Values |
|---|---|---|
| `name` | yes | lower_snake_case |
| `effect` | yes | `read` (changes nothing), `write` (changes data in a system the owner controls, such as files or a database), `external_send` (sends or changes something outside the organization, such as an email, a webhook or a record at a third party), `execute` (runs code or commands) |
| `approval` | no | `always` or `per_mode` (default). There is no `never`: the core decides when approval is forced. |
| `taint` | no | `none` (default), `tainting` (its result is untrusted content, so later outbound actions in the same run need approval), `outbound` (it is an outbound action that is subject to that rule), or `both` |
| `args` | no | A reference name for the argument schema (descriptive for now). |
| `grant` | no | `explicit`: the operation is denied unless the owner grants it by name. Nothing is granted by default. |
| `inherits` | no | Limits of the runs this operation starts: `max_depth` (1 to 3), `max_children` (1 to 8), `child_tokens`, `tree_tokens`, `tools: parent_subset`, `taint: both_ways`. Needs `grant: explicit`. |

## Design rules (not enforced yet)

These are the rules the core is meant to apply once it reads declarations (Phase 1). They are stated here so that nobody builds on a wrong assumption:

- A declaration can **narrow** what is allowed, never widen it. The owner's configuration and the core's own floor always win.
- An operation that is not declared is denied.
- File writes, file deletes and access requests always need approval, whatever the declaration says.
- Declared budgets are clamped to the core's ceilings.
- Only calls and bytes measured by the host count toward limits; usage reported by a connector is informational.

## Egress classes and obligations (not enforced yet)

| Class | Host list | What the core must apply once it enforces declarations |
|---|---|---|
| `fixed_hosts` | required | Only the listed hosts, cut back to the owner's list. Denied when the owner turns egress off. Metadata and link-local addresses blocked. |
| `owner_configured` | none | The owner picks the target by name. Denied when egress is off. Metadata and link-local addresses blocked. |
| `runtime_chosen` | none | The address is chosen while the run goes on. Denied when egress is off. Metadata, link-local, loopback and private addresses blocked after DNS resolution as well as before. Every redirect hop checked again. The resolved URL and the status written to the audit event. |

The core's own tools carry a class that a declaration cannot lower: `fetch_page` is `runtime_chosen`, the senders are `owner_configured`. The checks themselves arrive with the shared HTTP client (Phase 1); until then the class and its obligations are only recorded in the effective policy.

## Explicit grants and inheritance (not enforced yet)

An operation with `grant: explicit` is denied unless the owner grants it by name; nothing is granted by default. `delegate` always needs a grant, and a declaration cannot remove that. `inherits` states the limits of the runs an operation starts: a child may use only tools its parent may use (`tools: parent_subset`; the core does not cut the list down yet), and taint travels both ways (`taint: both_ways`: a child starts with the parent's taint and hands its own back). `inherits` needs `grant: explicit`.

## `descriptive` is not enforced

Everything under `descriptive` (including `data_classes`) is for people: it is shown in the dashboard and listed by the checker. **No core behaviour depends on it, now or in version 1.** Do not treat it as a security control.

## `network.tcp` is reserved

Some connectors (SQL databases) need a raw connection to a host and port. The field and a `tcp` service of the future host interface are reserved now so that connector code does not have to change later. While a connector runs inside the RagLeap process, a declared `tcp` egress **cannot be enforced**; the checker says so with `W-TCP-NOT-ENFORCEABLE`. Only the isolated host (a later phase) will enforce it.

## Checking a declaration

The checker runs offline and needs only Python 3:

```
python scripts/ragleap_conformance.py my-declaration.json
python scripts/ragleap_conformance.py my-declaration.json --source my_connector.py --strict
python scripts/ragleap_conformance.py --print-schema
```

Exit code 0 means it conforms, 1 means errors (or warnings with `--strict`), 2 means a file could not be read. `--json` prints machine-readable findings. With `--source` it also lints connector code: direct use of the network (`requests`, `httpx`, `aiohttp`, `socket`, `urllib.request`, `http.client`), of a subprocess, of `os.environ` and `os.getenv`, and dynamic imports is an error, because those must go through the host services of a later phase.

| Code | Level | Meaning |
|---|---|---|
| `E-SCHEMA` | error | The document does not match the JSON Schema `docs/capability-schema-v1.schema.json` (unknown field, wrong type, wildcard host, `https_only` not true, value out of range and so on). |
| `E-OP-DUPLICATE` | error | Two operations have the same name. |
| `E-APPROVAL-NEVER` | error | `approval: never` was used. It does not exist. |
| `E-SCOPE-USER-FIELD` | error | A `user` scope has no `user_filter_field`. |
| `E-NET-INTERNAL-HOST` | error | A host is an IP address or an internal name. |
| `E-NET-REQUIRED` | error | An `external_send` operation declares no host, no `tcp` target and no `owner_configured` or `runtime_chosen` egress class. |
| `E-NET-EGRESS-HOSTS` | error | Egress `fixed_hosts` lists no host, or `owner_configured` or `runtime_chosen` lists one. |
| `E-INHERITS-NEEDS-GRANT` | error | An operation declares `inherits` without `grant: explicit`. |
| `E-SECRET-PROTECTED` | error | A core-owned secret was declared. |
| `E-SRC-DIRECT-ACCESS` | error | Connector source uses the network, a subprocess, the environment or a dynamic import directly. |
| `W-TCP-NOT-ENFORCEABLE` | warning | `network.tcp` is declared; it cannot be enforced in-process. |
| `W-DATA-CLASS-UNKNOWN` | warning | A `descriptive.data_classes` entry is not in the known list. |
| `W-SRC-DRIVER` | warning | Connector source imports a database or cloud driver that opens its own connections. |
| `I-DESCRIPTIVE-ONLY` | information | `data_classes` is descriptive and not enforced. |
| `I-NO-BUDGETS` | information | No budgets were declared; the core's own limits apply. |

## Versioning

The schema string carries the major version. The core will refuse an unknown major version. Within version 1 only additive, optional fields will be added. While the status is "draft", fields may still change before the first connector set has passed the checker.
