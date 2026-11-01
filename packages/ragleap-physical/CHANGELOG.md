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
