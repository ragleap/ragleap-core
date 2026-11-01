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
```

Report vulnerabilities privately through the repository's Security tab (see `SECURITY.md`).
