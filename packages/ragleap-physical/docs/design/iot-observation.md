# ragleap-physical: IoT design note (simulated control, an observe-only message feed, and a gated Stage 2)

**Status: DRAFT for review. Docs only. No code for this exists yet.**

Evidence tags: **[verified]** read in this repository (file named); **[plan]** an intention, not built; **[unverified]** believed, not checked; **[open]** a question someone else has to decide.

## 1. Purpose and limits

This note describes how everyday connected devices (a smart plug, a relay, a thermostat) could be handled in `ragleap-physical` in two stages, and what has to be true before a third stage is even discussed. It is a design. Nothing here is built, and nothing in it allows a real device. [plan]

No real device, protocol, product or vendor is claimed. The devices below are models. The failure modes in section 5 are modelling choices, not measurements of any real device or network. What real devices do may differ, and no source about real devices was consulted for this note. [unverified]

Out of scope, always: life-safety systems, medical devices, vehicles on public roads, weapons, and anything that could hurt people without an independent hardware safety stop. A heater behind a switch is exactly the kind of case where this matters: a switch that reports "off" while the heater is on can start a fire. That is why Stage 2 is gated so hard.

Simulation is not reality. Every observation says `simulated: true`. [verified: `docs/safety-invariants.md`, S13]

## 2. What exists today

- **The adapter contract** (`adapters/base.py`). A control adapter says `simulated`, lists its `targets()`, refuses a value that is not a finite number and an unknown target or command (`ValueError`), changes nothing when it refuses, refuses `apply()` while stopped, and never raises from `stop()` or `clear_stop()`. An observe-only adapter has no target and refuses `apply()` and `dry_run()`. [verified]
- **Control adapters:** the arms and the simulated machine tool (`adapters/machine.py`). **Observe-only adapters:** the energy site, the MTConnect replay and the ROS 2 mock. The CSV replay adapter (`adapters/csv_replay.py`) is observe-only as well. [verified]
- **The envelope** (`envelope.py`). It knows two commands, `set_position_target` and `set_tool_offset`, and two limit shapes: a joint (`pos_min`, `pos_max`, `vel_max`, `force_max`) and a tool offset (`offset_min`, `offset_max`, `step_max`, `min_gap_s`). Any other command or key is refused. Violations are rejected, never clamped. [verified]
- **The observation** (`observation.py`). Units are a fixed set: `rad`, `rad/s`, `m`, `m/s`, `N`, `N*m`, `s`, `kg`, `1`, `um`, `W`, `kWh`. There is no unit for temperature. [verified]
- **The rules** (`docs/safety-invariants.md`): the agent never moves a device (S1); an approval binds to the exact change, once, with a named person (S2); limits live only in the bridge and the envelope (S3); reject, never clamp (S4); checks run at propose and again at apply (S5); fail closed (S7); stop needs no approval and never waits (S8); device data is untrusted (S9); only simulated adapters are accepted (S13). [verified]

## 3. The stages

| Stage | What | Real device? | Status |
|---|---|---|---|
| 1a | Simulated IoT control: a plug, a relay and a thermostat with the same contract as the arms, plus a model of IoT failure modes | No | [plan] |
| 1b | An observe-only message feed mock: recorded messages replayed as observations | No | [plan] |
| 2 | Real IoT control | Yes | Design only, and not before the gates in section 7 |

Stage 1 can be built with the standard library alone and needs no network, no dependency and no real device.

## 4. Stage 1a: simulated devices with the same contract as the arms

### 4.1 Two kinds of state

Every simulated device keeps two things apart: its **true state** (what the model does) and its **reported state** (what the last status message says). The adapter's `observe_raw()` shows the reported state, the age of the last report, and counters for messages seen, lost and duplicated. The true state is visible only through an **independent sensor** with its own noise and its own age: a plug's power draw, a thermostat's temperature. Nothing in an observation lets the true state leak around the sensor. [plan]

### 4.2 The devices

| Device | Target | Command (absolute, never relative) | True behaviour | Independent sensor |
|---|---|---|---|---|
| Smart plug | `plug_1` | set the switch to on (1) or off (0); any other value is refused | follows the command after a delay with seeded jitter | power draw in `W` |
| Relay | `relay_1` | the same | the same | none: with no independent sensor the outcome cannot be verified, and the model says so |
| Thermostat | `thermostat_1` | set a setpoint | the room temperature follows a first-order model; the heater switches with a hysteresis around the setpoint | temperature, with noise |

Rules for every command: absolute only (no toggle, no "raise by"), because a command that arrives twice must not do twice as much (section 5). Each command carries the proposal's id as its idempotency key. [plan]

### 4.3 Meeting the arm contract

| Contract item | How a simulated device meets it |
|---|---|
| `simulated` is true | always |
| `targets()` lists every target | the devices above, one target each |
| non-finite value, unknown target or command: `ValueError`, nothing changes | same checks as `adapters/machine.py` |
| `apply()` while stopped: `ValueError`, nothing changes | same |
| `stop()` and `clear_stop()` never raise; the observation keeps its entities | same |
| `dry_run()` predicts on a copy | the model is stepped on a copy with the faults of the real run switched off, and says it did so |
| the observation is sanitised by the bridge | same; device text and numbers are untrusted (S9) |

What "stop" means for a device is open (see Q4). [open]

### 4.4 The limits

A switch needs a minimum time between changes and a cap on changes per window, because rapid switching wears and heats real parts. A setpoint needs a range, a step and a minimum gap. The second shape matches the existing offset limits (`offset_min`, `offset_max`, `step_max`, `min_gap_s`) closely. The first does not match either existing shape. How to express these is a change to the envelope, which belongs to the maintainers (Q2, Q3). [open]

## 5. A model of IoT failure modes

All faults are **simulation-only fault injection**. They are drawn from the adapter's seeded random generator with explicit, named rates, are off by default, and give the same result for the same seed. They are implemented as a wrapper that sits between the bridge and a device model and obeys the adapter contract itself, so the device models stay simple. [plan]

| Failure mode | What the model does | What a safe design must do |
|---|---|---|
| A device reports "on" but is not | After a fault event the true state stops following commands while the report keeps following the command. | Never treat the reported state as the outcome. The outcome check must use an independent sensor, and an unverifiable outcome counts as failed (S7). |
| A lost message | A command or a status report is dropped. | A command without an acknowledgement is "unknown", not "done". The proposal is not retried without a new approval. |
| A replayed message | An old status report, or an old command, is delivered again later with its original timestamp. | A report older than the newest one seen is ignored and counted. A replayed command is not applied again (idempotency key). |
| A stale reading | The last report is old. | An observation carries the age of every reading. A reading older than a declared bound is not used for an outcome check. |
| A command that arrives twice | The same command is delivered twice. | The second delivery is ignored and counted. This is why commands are absolute. |

Each row becomes a test with a deliberate-break proof: remove the guard, a test must fail. [plan]

## 6. Stage 1b: an observe-only message feed mock

A replay adapter for recorded messages, in the style of the MTConnect and CSV replays. It has no network, no URL, no dependency and no command. [plan]

- **Input.** A text file of recorded messages, one per line. Each message is a flat object with exactly three keys: a topic, a time in seconds, and a payload that is either a number or one of a few declared words. The caller declares each topic it accepts, with the sensor id, the unit, the scale and the value range of its payload, and what each declared word means as a number. A topic that is not declared is refused. [plan]
- **Strict parsing, like the CSV replay.** Caps on bytes, messages, topics, topic length and payload length, with hard upper limits the caller cannot raise. Printable ASCII only. Strict numbers (no `nan`, no `inf`). No repeated keys. Time must increase. Values must lie in their declared range. The whole file is refused if any line fails, before the first message is replayed. An error names the line and the declared topic, never text from the file. [plan]
- **What an observation shows.** For each declared topic, the last value received up to the replay time, and the age of that value. A topic with no message yet is shown as missing, not as zero. [plan]
- **Replay and staleness.** A recorded replay can contain old or repeated messages on purpose, so the same failure modes as in section 5 can be shown without any device model. [plan]
- **Reuse.** The strict number and name checks can be shared with the CSV replay by importing them from that module, which keeps the rules in one place. [plan]

## 7. Stage 2: real IoT control (design only)

Stage 2 is **not** part of any plan to build. This section records what would have to be true before anyone could ask for it.

All of these gates are required, and none of them is implied by another:

1. **An independent hardware protection** that limits the harm of any command, working without software (for a heater, for example, a thermal cut-out that no command can override).
2. **A written risk review** of the exact device, the exact load and the exact failure modes of section 5.
3. **An outside tester** who is not the author, with a report.
4. **Verification by an independent sensor** for every command: the outcome is checked through a reading the device itself does not produce, and a reading that is missing or too old counts as failure.
5. **TLS and authentication** on every link, with a separate write credential that observe-only adapters never receive.
6. **The maintainer's explicit go**, in writing, for each device and each load.

The note adds requirements that follow from the failure modes. They are design requirements, not claims about any product: an allow-list of exact targets with envelope limits; absolute commands with idempotency keys; fail closed on an unknown or stale state; the write path off by default; and a physical stop outside software. [plan]

### 7.1 The decision on S13

S13 says only simulated adapters are accepted, and the bridge refuses an adapter that does not say it is simulated. Any Stage 2 needs a decision on S13. **That decision belongs to the maintainer.** This note does not assume either answer. Two readings, with what each costs: [open]

- **S13 stays as it is.** Real adapters are never accepted by this package. IoT stays at Stage 1 for good. Nothing else changes.
- **S13 becomes a gated rule.** A real adapter is accepted only under conditions that are written down. Code can enforce only part of them: it can refuse by default, require the operator's own configuration to name the adapter, and refuse an adapter that does not declare its hardware protection. It cannot check a risk review or an outside test. Those would remain paperwork, and a gate that is only paperwork is a weaker gate.

## 8. What would change in code, and who owns it

This note changes no code. If it is accepted, the work splits like this:

- **New files only:** `adapters/iot_sim.py` (devices and the fault wrapper), `adapters/message_feed.py`, their tests with deliberate-break proofs, and a guide in `docs/industry/`.
- **Changes in core files** (reviewed by the maintainers, not part of a new-files-only step): new command and limit shapes in `envelope.py` (Q2); a temperature unit in `observation.py` (Q3); an outcome check that can name a sensor and use its age (Q5).

## 9. Open questions

1. **Q1. S13.** Keep it, or replace it by a gated rule? (Section 7.1.) Maintainer.
2. **Q2. How are a switch and a setpoint expressed in the envelope?** New commands with their own limit shapes, or the existing offset shape for setpoints and a new shape for switches? Maintainers.
3. **Q3. Temperature.** The unit list has no temperature. Add one, and which? Maintainers.
4. **Q4. What does "stop" mean for a device?** Hold the last state and refuse further commands, or go to a safe state? The safe state depends on what is plugged in (off is not safe for everything), so the answer cannot be one rule. It may have to be declared per device and per load. Maintainer.
5. **Q5. Outcome by sensor.** [unverified] whether the current outcome check can name a sensor, not only an entity, and use its age. If it cannot, that is a change in the bridge. Maintainers.
6. **Q6. Freshness bounds.** Where does the bound on a reading's age live: in the envelope (so only the operator sets it) or in the adapter? The first fits S3. Maintainers.
7. **Q7. The fault wrapper.** A wrapper that obeys the adapter contract, or faults built into each device model? The wrapper is the proposal here. Reviewers.

## 10. What is not claimed

- No real device, protocol, product or vendor.
- No measurement of any real network or device. The failure modes are modelling choices.
- No statement that any Stage 1 behaviour carries over to a real device.
- No claim that this design is complete or that it covers all kinds of devices.
