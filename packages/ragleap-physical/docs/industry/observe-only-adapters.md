# Observe-only adapters: energy site and MTConnect replay

Two simulated adapters. They only **read**: `targets()` is empty, and `apply()` and `dry_run()` refuse. Pre-alpha, simulation only. There is no real meter, agent or machine behind them, and no network code.

- `adapters/energy.py`: `FakeEnergySiteAdapter`, a seeded model of a site with four meters (invented numbers).
- `adapters/mtconnect.py`: `FakeMTConnectAdapter`, which replays recorded MTConnect "streams" XML documents that you give it as text or bytes. It is not an MTConnect client.

MTConnect is a standard for publishing equipment data. Its Interfaces part (Part 5; v1.6.0 is the version checked) lets two machines coordinate by each publishing request and response states that the other reads; the standard gives no direct way for one machine to command another. Some agents can be set to accept pushed data values or assets (the reference agent's `AllowPut`, default false; not re-checked since 2026-10-30). That is an agent setting, not a command to a machine, and what it could reach on a real installation is not verified. These adapters are observe-only: this replay only reads the documents you give it and sends nothing.

## One thing to know first
An observe-only bridge still needs a valid envelope, because `Envelope` requires one target and one command. The adapters have neither, so the envelope below limits nothing. The real guard is `Capability.OBSERVE`: a proposal is refused.

## Example 1: eight hours at an energy site
```python
from ragleap_physical import Bridge, Capability, Envelope
from ragleap_physical.adapters.energy import FakeEnergySiteAdapter

NOMINAL = {
    "envelope_version": 0, "allowed_commands": ["set_position_target"],
    "targets": {"nominal": {"pos_min": -1.0, "pos_max": 1.0, "vel_max": 1.0, "force_max": 1.0}},
    "rate": {"max_commands_per_s": 1}, "episode": {"max_sim_time_s": 60, "max_steps": 1000},
    "on_violation": "reject",
}

site = FakeEnergySiteAdapter(seed=1)
site.advance(8 * 3600, 100_000)  # eight simulated hours
obs = Bridge(site, Envelope(NOMINAL), org_id="org_demo", max_capability=Capability.OBSERVE).observe()
print(len(obs.sensors) + obs.dropped, "sensors reported")
print("shift active:", [s.value for s in obs.sensors if s.id == "shift_active"])
```
```text
11 sensors reported
shift active: [1.0]
```
Power and energy sensors use the units `W` and `kWh`. Until the core lists them in `observation.UNITS`, the bridge drops them and counts them in `obs.dropped` (10 of the 11 today). That is why the example adds `dropped` back.

## Example 2: one MTConnect document
```python
from ragleap_physical import Bridge, Capability, Envelope
from ragleap_physical.adapters.mtconnect import FakeMTConnectAdapter, Item

NOMINAL = {
    "envelope_version": 0, "allowed_commands": ["set_position_target"],
    "targets": {"nominal": {"pos_min": -1.0, "pos_max": 1.0, "vel_max": 1.0, "force_max": 1.0}},
    "rate": {"max_commands_per_s": 1}, "episode": {"max_sim_time_s": 60, "max_steps": 1000},
    "on_violation": "reject",
}
STREAM = """<MTConnectStreams><Header creationTime="2026-10-30T10:00:00Z"/><Streams><DeviceStream>
<ComponentStream><Samples><Position dataItemId="x_pos" sequence="1">12.5</Position></Samples>
<Events><Execution dataItemId="exec">ACTIVE</Execution></Events>
<Condition><Fault dataItemId="sys" sequence="2" nativeCode="E1"/></Condition>
</ComponentStream></DeviceStream></Streams></MTConnectStreams>"""

adapter = FakeMTConnectAdapter([STREAM], {"x_pos": Item("m", 0.001)})  # the document is in mm, the observation in m
obs = Bridge(adapter, Envelope(NOMINAL), org_id="org_demo", max_capability=Capability.OBSERVE).observe()
print({s.id: round(s.value, 6) for s in obs.sensors})
```
```text
{'x_pos': 0.0125, 'fault_count': 1.0, 'warning_count': 0.0, 'unavailable_count': 0.0, 'skipped_items': 1.0, 'data_age_s': 0.0}
```
Reading the output: the position came from the list of data items you allowed, converted from mm to m. The one fault is counted. The event text (`ACTIVE`) is free text from the device and is never used; it is counted in `skipped_items`.

## What the MTConnect replay does with untrusted input
- Only UTF-8, at most 1 MB and 50,000 elements; any DTD or entity is refused before parsing.
- A sensor exists only for a `dataItemId` you listed in `items`. Units are not in a streams document, so you give each id its unit and scale.
- Numbers must be plain decimals. `UNAVAILABLE`, `nan` and similar are skipped.
- A fault is one condition activation (`conditionId`, else `nativeCode`); several can be active on one data item. `Unavailable` conditions are counted in `unavailable_count`, never as "no fault".

## Limits
- The XML layout was checked against the section titles of MTConnect Part 3 v1.6.0 and the Condition model v2.3 to v2.7, not against a real agent.
- How real agents treat a `Normal` without codes is not verified; this code does not let it clear a coded fault.
- The energy numbers are invented.
- These adapters say nothing about any real machine.
