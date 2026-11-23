# Changelog

All notable changes to `ragleap-flows` are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-11-23

First release. The PR block replaces 2026-11-20 with the UTC date of that day.

### Added

- `from_json` / `from_dict`: a strict loader for flow files (plain JSON data, no code). Unknown keys, wrong types,
  duplicate keys, NaN, unknown targets, cycles, unreachable steps, paths that read a step that does not always run
  before them and oversize files are refused with `FlowError`.
- Steps: `tool`, `approval`, `branch`. Paths (`$input.x`, `$step.field.0`), operators `eq ne lt le gt ge in exists
  truthy`, `all` and `any`.
- `FlowRunner.run(scope, input)` and `FlowRunner.resume(scope, run_id, decisions)`: every run belongs to a
  `ragleap_security.Scope`; a wrong scope, an unknown run and a damaged state give the same `ResumeError("unknown run")`;
  128-bit run ids; an atomic claim on resume; `store_for(tenant)` for one store per tenant; `rate_limiter`; `audit` with
  no text, fail closed; a taint rule for `outbound` steps after `external` steps or an untrusted input.
- `dispatch=` as an alternative to `tools=`, so tool calls can go through a per-tenant dispatcher.
- Hard limits: 100 steps, 256 KiB flow, 64 KiB input, 64 KiB step output, 1 MiB state, 300 s deadline.
- `scripts/mutation_check.py` (68 mutations, all killed on Python 3.10 and 3.12).
