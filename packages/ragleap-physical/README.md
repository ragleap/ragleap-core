# ragleap-physical

**Status: pre-alpha. Simulation only. Nothing here controls real hardware.**

A small library that connects RagLeap AI employees to open-source simulators, so people can build and test
safely. Observing comes first. Every action goes through a proposal, an approval and a safety envelope that
lives in the bridge, in one place. The model cannot change the limits.

## What works today (and what does not)

- Observation and proposal formats (v0), the four capability levels, the safety envelope, approvals bound to
  the exact proposal, dry run, outcome check, and a stop that needs no approval. Tested with a tiny
  built-in one-joint simulator (standard library only).
- Exactly four tools for an AI agent: list sources, get an observation, get the envelope, propose an action.
  None of them can approve, apply, stop or change a limit. A small MCP server (standard library, loopback by
  default, optional bearer token) serves them. Tested against the official MCP Python SDK (`mcp>=1.28,<2`).
  RagLeap's own MCP client was not read or tested: it may need changes (for example HTTPS).
- MuJoCo adapters as an optional extra: one observe-only, and one servo adapter that follows
  `set_position_target` in simulation, only through the bridge.
- Workspace limits: a box in x, y, z that every tracked point must stay inside, checked on the starting state,
  on the dry run, and again on the real motion in short slices. If a workspace is set and the adapter cannot
  report positions, everything is refused. A two-link arm model (built-in stand-in and MuJoCo) exercises it.
- A simulated machine tool (tool wear): the AI can only adjust a tool offset, in micrometres, inside an envelope (range,
  step size, minimum gap between changes). Changing the tool is a human action and is not a command. It is a model with
  invented numbers and says nothing about a real machine.
- Hardened after independent review by two other AI sessions (see the CHANGELOG): stricter input and prediction checks, no self-approval.
- A three-joint arm model (MuJoCo XML in `examples/robots/`, plus a built-in stand-in), an offline ROS 2 observe-only mock that
  needs no ROS install, and a repeatability experiment in `benchmarks/` (exact replay within one installed environment;
  cross-machine repeatability is not claimed).
- An operator console (`ragleap_physical.console`): the human side of the bridge. A named person sees the pending proposals,
  approves the exact digest, applies, and can stop. Loopback only, one token per person, and the approver's name comes from the
  token, never from the request. It is not a tool: the AI cannot reach it.
- Two observe-only industrial mocks, with no network and no real meter or machine: a simulated energy site and an MTConnect
  document reader with strict limits (it refuses DTDs and entities, and caps size). These adapters are observe-only: they only
  read the documents you give them and send nothing. See `docs/industry/observe-only-adapters.md` and the first-hour guide
  `docs/first-hour.md`.
- Not built: a second simulator, real hardware, Java and npm clients.
- Simulation is not reality. Do not read a passing simulation test as proof for a real machine.

## Out of scope, always

Life-safety systems, medical devices, vehicles on public roads, weapons, and anything that could hurt people
without an independent hardware safety stop.

## Docs

- Plan, decisions, safety rules and conventions: [docs/HANDOVER.md](docs/HANDOVER.md)
- Design (formats, safety envelope, threat model): [docs/design/phase0-interface.md](docs/design/phase0-interface.md)

## Try it

```
pip install -e ".[test]"          # core only, standard library
pip install -e ".[mujoco,test]"   # with the optional MuJoCo adapter
pip install -e ".[test,interop]"  # also runs the MCP SDK interop test
pytest
python examples/demo_fake.py
python examples/two_link_workspace_demo.py
```

Report vulnerabilities privately through the repository's Security tab (see `SECURITY.md`).

## Other languages (drafts, not reviewed by native speakers)
English (this file) is the original: if a translation differs, trust the English text. The Hindi and Tamil drafts are older and shorter than this file. The Spanish text was written by a native speaker, with translation tools, and has not been read by a second one.

[English (original)](https://github.com/ragleap/ragleap-core/blob/main/packages/ragleap-physical/README.md) · [Hindi](https://github.com/ragleap/ragleap-core/blob/main/packages/ragleap-physical/README.hi.md) · [Tamil](https://github.com/ragleap/ragleap-core/blob/main/packages/ragleap-physical/README.ta.md) · [Chinese, Simplified](https://github.com/ragleap/ragleap-core/blob/main/packages/ragleap-physical/README.zh-CN.md) · [Spanish](https://github.com/ragleap/ragleap-core/blob/main/packages/ragleap-physical/README.es.md)
