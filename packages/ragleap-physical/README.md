# ragleap-physical

**Status: pre-alpha. Simulation only. Nothing here controls real hardware.**

A small library that connects RagLeap AI employees to open-source simulators, so people can build and test
safely. Observing comes first. Every action goes through a proposal, an approval and a safety envelope that
lives in the bridge, in one place. The model cannot change the limits.

## What works today (and what does not)

- Observation and proposal formats (v0), the four capability levels, the safety envelope, approvals bound to
  the exact proposal, dry run, outcome check, and a stop that needs no approval. Tested with a tiny
  built-in one-joint simulator (standard library only).
- Not built: workspace limits, a second simulator, real hardware, Java and npm clients.
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
pytest
python examples/demo_fake.py
```

Report vulnerabilities privately through the repository's Security tab (see `SECURITY.md`).
