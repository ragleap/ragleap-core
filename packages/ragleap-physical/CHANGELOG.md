# Changelog

All notable changes are listed here. Format: Keep a Changelog.

## [Unreleased]

## [0.0.5] - 2026-11-08

### Added
- An explicit-state model of the approval flow (`benchmarks/approval_model.py`) and tests that replay every short event sequence (one proposal,
  up to five events) through the model and through the real Bridge (`tests/test_approval_model.py`). It is a hand-written abstract model, not a
  proof about the code; time, concurrency and adapters are not modelled, and several proposals are outside the compared domain.
- Adapter conformance tests (`tests/test_adapter_conformance.py`): one contract that every adapter must pass (observation shape, stop and
  clear-stop, stable entities, a pure dry run, refusals that change nothing, a Bridge that can stop and clear). The refusal contract is
  written in the `Adapter` docstring.

### Fixed
- One-joint and two-link stand-in arms, and the simulated machine tool: `apply()` and `dry_run()` refuse NaN, infinity, text, booleans and
  `None` with `ValueError` and change nothing. Values are now stored as floats. (The Bridge already rejected these values, so this
  matters only when an adapter is called directly.)
- The offline ROS 2 mock: `clear_stop()` no longer raises, so a Bridge over it can be stopped and then cleared.
- MTConnect wording: the README, the industry guide and the adapter docstrings no longer say that MTConnect is "not purely read-only".
  The Interfaces part (Part 5) lets machines coordinate through published request and response states and, as read, gives no direct way for
  one machine to command another; some agents can be set to accept pushed data (AllowPut), which was not re-checked since 2026-10-30. Our
  adapters are observe-only. The guide's test now requires the new wording. (The 0.0.4 page on PyPI still shows the old sentence.)

## [0.0.4] - 2026-11-06

### Added
- `Item` (the MTConnect item description) can be imported from `ragleap_physical.adapters`; an import test covers it
  (reported by a reviewer on a clean install of 0.0.3).
- Six review tests for the energy-site adapter, kept as regression tests (`tests/test_energy_adapter_review.py`).
- Review tests for the three-joint arm and the MuJoCo servo adapter (`tests/test_arm3_review.py`, `tests/test_arm3_mujoco_review.py`,
  `examples/robots/test_arm3_hardening.py`).
- PowerShell steps in `docs/first-hour.md` (#1177, by @Anurag07-crypto, whose Windows run showed the gap).

### Fixed
- Three-joint arm (`FakeArm3Adapter`): after a stop the arm can be observed again (the stop used to reset the velocities to two numbers, so
  `observe_raw()` raised `IndexError`); `apply()` and `dry_run()` refuse NaN, infinity, text, booleans and `None`; time and step inputs and
  constructor parameters are validated; the model fingerprint now depends on the parameters.
- MuJoCo servo adapter: a target outside the actuator range is refused with `ValueError` and is no longer silently clamped by MuJoCo; NaN, infinity,
  text, booleans and `None` are refused. `examples/robots/arm3.xml` actuator ranges are now -3.1 to 3.1 rad, like the other models.
  Not yet run on a real MuJoCo install at the time of writing.
- The date of 0.0.3 in this file was 2026-11-03; the upload was on 2026-11-04.

## [0.0.3] - 2026-11-04

### Added
- A second runnable demo, `examples/two_link_workspace_demo.py`: a two-link arm with one accepted and one refused workspace move
  (thanks to @bhuvanm0981-ctrl).
- Package metadata, so the PyPI page shows them: project links (homepage, documentation, source, issues, changelog, roadmap), an author,
  keywords and more classifiers.

### Fixed
- The operator console and the MCP server now read and discard a small request body before they refuse a request, so a client gets the answer and
  not a connection reset. A Windows test run reported `WinError 10053` on a refused request (thanks to @Anurag07-crypto). The reset could not be
  reproduced on Linux; a test checks that the body is drained. Not yet verified on Windows.

## [0.0.2] - 2026-11-02

### Added
- Operator console (`ragleap_physical.console`): a named person approves the exact proposal, applies it and can stop. Loopback only,
  one token per person. New read-only `Bridge.pending_proposals()`.
- Observe-only energy site mock and MTConnect document reader (simulation only, strict input limits), a first-hour guide, an
  industry guide with runnable examples, and Hindi and Tamil README drafts (not reviewed by native speakers).
- The wear study notebook has a pluggable wear model (Taylor's equation, with assumptions stated).

## [0.0.1] - 2026-11-02

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
- Three-joint arm model (`FakeArm3Adapter`, MuJoCo XML), offline ROS 2 observe-only mock (`MockROS2ObserveAdapter`), repeatability
  experiment in `benchmarks/`. Tests in `examples/robots/` and `benchmarks/` now run by default.

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
