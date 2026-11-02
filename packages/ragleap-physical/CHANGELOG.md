# Changelog

All notable changes are listed here. Format: Keep a Changelog.

## [Unreleased]

### Added
- Observation and proposal formats v0, capability levels, safety envelope, bridge with approvals,
  dry run, outcome check and stop.
- Built-in deterministic one-joint adapter for tests.
- Four model-facing tools (`physical_list_sources`, `physical_get_observation`, `physical_get_envelope`,
  `physical_propose_action`) and a minimal MCP server (JSON-RPC over HTTP, loopback by default).
- Optional MuJoCo adapter (observe-only).
- MuJoCo servo adapter (simulated control behind the bridge: dry run on a copy, apply, hold on stop).
- Phase 3 demo notebook (`examples/phase3_demo.ipynb`).
- Workspace limits (x, y, z box) in the envelope, with a runtime monitor; limits on every joint, not only the target;
  the envelope must list every joint the adapter can move.
- Two-link arm: built-in stand-in (`FakeArmAdapter`) and a MuJoCo model (`ARM2_XML`) with end-effector positions.
- Tool-wear compensation study (`examples/wear_study.ipynb`): a toy model with invented numbers, settings tuned on separate seeds,
  hard scenarios and paired statistics. It makes no claim about real machines.
- Simulated machine tool (`FakeMachineToolAdapter`) and the `set_tool_offset` command (range, step and minimum-gap limits
  re-checked at apply time). Tool change is not a command.

### Changed (hardening, found by independent review)
- Identifiers and actor ids no longer accept a trailing newline (`fullmatch`); a huge integer is a `ValidationError`, not an `OverflowError`.
- Observations examine at most 1024 raw items, valid or not; items whose id appears twice are all dropped.
- `reason` must be printable single-line text (no control, bidi or invisible characters).
- The MCP server answers 400 to deeply nested JSON instead of failing silently.
- Dry-run predictions: speed and force must be finite, non-negative numbers (per joint and in the aggregate); an empty
  `per_joint` no longer skips the aggregate check; predicted positions must be finite.
- Dry-run predictions must also be consistent: inverted position bounds are refused, and an aggregate smaller than a per-joint value is refused.
- Workspace check: an incomplete tracked x/y/z point is refused instead of ignored.
- `Bridge` refuses a TTL or chunk size that is not a finite number above zero, and caps that are not positive whole numbers.
- The requester can no longer approve its own proposal (refused and audited).
- Units `W` and `kWh` accepted. `Envelope.observe_only()` and `Bridge(adapter, None, ...)` for observe and analyse only.
- Found by two independent adversarial review rounds and by the lead's own review. AI-assisted.
- Three-joint arm model (`FakeArm3Adapter`, MuJoCo XML), offline ROS 2 observe-only mock (`MockROS2ObserveAdapter`), repeatability
  experiment in `benchmarks/`. Tests in `examples/robots/` and `benchmarks/` now run by default.
- Operator console (`ragleap_physical.console`): a named person approves the exact proposal, applies it and can stop. Loopback only,
  one token per person. New read-only `Bridge.pending_proposals()`.
- Observe-only energy site mock and MTConnect document reader (simulation only, strict input limits), a first-hour guide, an
  industry guide with runnable examples, and Hindi and Tamil README drafts (not reviewed by native speakers).
- The wear study notebook has a pluggable wear model (Taylor's equation, with assumptions stated).
