# CSV replay adapter (observe only)

`adapters/csv_replay.py` holds `FakeCsvReplayAdapter`. It replays the rows of a recorded CSV file as observations. Pre-alpha, simulation only. There is no machine, meter or network behind it, and the standard library is all it needs.

The CSV replay adapter is observe-only: it has no command and no write path. `targets()` is empty, `apply()` and `dry_run()` refuse, and it is meant for the Observe level of the capability model. Even a Control-level bridge cannot change anything through it.

## An example

The rows below are invented. Nothing in this repository is data from a third party.

```python
from ragleap_physical import Bridge, Capability, Envelope
from ragleap_physical.adapters.csv_replay import Column, CsvSpec, FakeCsvReplayAdapter

NOMINAL = {
    "envelope_version": 0, "allowed_commands": ["set_position_target"],
    "targets": {"nominal": {"pos_min": -1.0, "pos_max": 1.0, "vel_max": 1.0, "force_max": 1.0}},
    "rate": {"max_commands_per_s": 1}, "episode": {"max_sim_time_s": 60, "max_steps": 1000},
    "on_violation": "reject",
}

# The file counts milliseconds. What it holds is declared here, and nothing else is accepted.
SPEC = CsvSpec(
    time_column="t_ms", time_scale=0.001,
    columns=[Column("temp", "temp", "1", minimum=0.0, maximum=100.0), Column("speed", "speed", "1", minimum=0.0, maximum=1000.0)],
    ignore=["note"],
)
DATA = "t_ms,temp,speed,note\n0,20.5,100,a\n500,20.6,110,b\n1500,21.0,120,c\n"

adapter = FakeCsvReplayAdapter(DATA, SPEC)
adapter.advance(1.0, 100)  # one simulated second into the file
obs = Bridge(adapter, Envelope(NOMINAL), org_id="org_demo", max_capability=Capability.OBSERVE).observe()
print({s.id: s.value for s in obs.sensors})
```

The envelope above limits nothing, because the adapter has no target. The guard is the Observe level: the bridge refuses to propose anything.

To read a local file, `read_csv_file(path)` returns its bytes. It reads at most the byte cap and refuses a link, a directory, a pipe and anything larger. The adapter itself never opens a path.

## What the parser refuses

The file is untrusted text. It is checked as a whole before the first row is replayed, and a file that fails any check is refused.

- **Declared columns only.** `CsvSpec` names the time column and every column that becomes a sensor, with its sensor id, unit, scale and value range. A header that is neither declared nor listed in `ignore` is refused, and so is a declared column that is missing.
- **Declared ranges.** Every value, after scaling, must lie between its declared minimum and maximum. `nan`, `inf` and a number too large for a float are refused.
- **Plain text and plain numbers.** Printable ASCII only (one leading byte order mark is accepted), lines end with `\n` or `\r\n`, no quotes, no blank line, no empty cell, no row with a different number of fields. Numbers are plain decimals such as `12`, `-0.5` or `1e-3`. Signs like `+1`, padding, `1_000` and hexadecimal text are refused.
- **Time moves forward.** The time column must strictly increase.
- **Caps that the caller cannot raise.** Each file is at most 5,000,000 bytes, 100,000 data rows and 64 header cells. A line has at most 4,096 characters, a field at most 32, and the file spans at most 10,000,000 seconds. A caller may ask for lower byte and row caps.
- **Messages never repeat file text.** An error names the row number and the declared column name, nothing else.

## A starting point for milling signals: [unverified]

`MILLING_MACHINE_SIGNALS` in the same module describes the signals that a machine's own control records during milling: the time, the position control deviation of two axes, the tool position, and the spindle torque.

[unverified] It was written from the signal table in the paper below. It has not been run on a real file of that dataset, and no claim is made that it works with that data. The ranges are loose sanity bounds chosen here, not values taken from the dataset. The paper's table lists the same name three times for one group of signals, so the names of the columns marked "ignored on purpose" are an inference. Only the signals that come from the machine's control are described. The force sensor signals have their own time base and are not part of this description.

The dataset is distributed as HDF5 files, which this adapter does not read. To use it you would export the machine-side signals of one run to CSV with the header names above. No file of the dataset is part of this repository, and the repository holds only a short invented file (`tests/fixtures/synthetic_signals.csv`) with the same header names.

### Attribution

Dataset: "Multivariate time series data of milling processes with varying tool wear and machine tools", Mendeley Data, DOI [10.17632/zpxs87bjt8](https://doi.org/10.17632/zpxs87bjt8). The creators are named at that DOI. The licence stated on the record is [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (as stated when this guide was written; check the record before relying on it). Describing the data in the paper: Data in Brief 50 (2023) 109574, DOI [10.1016/j.dib.2023.109574](https://doi.org/10.1016/j.dib.2023.109574).

Nothing from the dataset is copied here. The profile uses only the names and units of the signals as the paper lists them.

## Limits

- Simulation only. No real machine and no real file of a third-party dataset was used.
- The adapter replays numbers. It does not know what a number means, and it checks no physics beyond the declared ranges.
- Only one time base per file. Files that mix two sampling rates must be split or resampled first, outside this package.
- Reading HDF5 is not supported. A new dependency would need a licence and supply-chain review first.
