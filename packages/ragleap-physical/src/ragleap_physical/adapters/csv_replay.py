"""An observe-only replay adapter for time series stored in a CSV file, standard library only.

It replays the rows of a recorded file as observations. It never connects to anything: there is no network code, no URL and
no command. The file's bytes are given to the constructor (``read_csv_file`` reads a local file, with a size cap, for you).
The adapter has no target, ``apply()`` and ``dry_run()`` refuse, and it is meant for the Observe level of the capability model.

The text is treated as untrusted device data and the parser refuses what it does not understand:

* the caller declares, in a ``CsvSpec``, the time column and every column that becomes a sensor, with its sensor id, unit, scale
  and value range. A header that is neither declared nor listed in ``ignore`` is refused, and so is a missing declared column;
* caps (all with a hard upper limit that the caller cannot raise): bytes, rows, columns, characters per line and per field, and
  the time span. Every declared value must lie inside its declared range after scaling;
* printable ASCII only, with ``\\n`` or ``\\r\\n`` line endings. No quotes, no blank lines, no empty cell, no ragged row;
* numbers must match a strict decimal pattern. ``nan``, ``inf``, ``1_0``, ``+1``, hexadecimal text and padded numbers are refused;
* time must strictly increase;
* an error message never repeats text from the file, only the row number and the column's declared name.

Nothing is guessed: a file that fails any check is refused as a whole, before the first row is replayed. The replay is a model,
not a machine. Rows are shown at their file time (relative to the first row) and the last values hold between two rows.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Union

from ..observation import UNITS, ident
from .base import Adapter, DryRun

MAX_BYTES = 5_000_000  # hard caps: the caller may ask for less, never for more
MAX_ROWS = 100_000
MAX_COLUMNS = 64  # header cells, ignored ones included; the observation holds at most 256 sensors
MAX_LINE_CHARS = 4096
MAX_FIELD_CHARS = 32
MAX_SPAN_S = 1.0e7  # about 115 days of file time
MAX_SCALE = 1.0e12
META_IDS = frozenset({"data_age_s"})
ALLOWED_UNITS = frozenset(UNITS)
_NUM_RE = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}\Z")
_BAD_TEXT_RE = re.compile(r"[^\x20-\x7e\r\n]")  # anything but printable ASCII and line ends
_LONE_CR_RE = re.compile(r"\r(?!\n)")


class CsvError(ValueError):
    """The file or its description was refused. The message never repeats text from the file."""


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CsvError(f"{what} must be a number")
    try:
        number = float(value)
    except OverflowError:
        raise CsvError(f"{what} is too large") from None
    if not math.isfinite(number):
        raise CsvError(f"{what} must be finite")
    return number


def _count(value: Any, what: str, hard: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= hard:
        raise CsvError(f"{what} must be an integer from 1 to {hard}")
    return value


@dataclass(frozen=True)
class Column:
    """One column that becomes a sensor: ``value in the file * scale`` is in ``unit`` and must lie in ``[minimum, maximum]``."""

    name: str  # the header text of the file
    sensor_id: str  # the id the observation will use
    unit: str
    minimum: float  # declared range, after scaling
    maximum: float
    scale: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME_RE.fullmatch(self.name):
            raise CsvError("a column name must be 1 to 64 characters of letters, digits, '_', '.' or '-'")
        try:
            ident(self.sensor_id, "sensor_id")
        except ValueError:
            raise CsvError("a sensor id is not a safe identifier") from None
        if self.sensor_id in META_IDS:
            raise CsvError("a sensor id is reserved")
        if not isinstance(self.unit, str) or self.unit not in ALLOWED_UNITS:
            raise CsvError("a unit is not one the observation accepts")
        low, high, scale = _number(self.minimum, "minimum"), _number(self.maximum, "maximum"), _number(self.scale, "scale")
        if not low < high:
            raise CsvError("minimum must be below maximum")
        if scale == 0.0 or abs(scale) > MAX_SCALE:
            raise CsvError("scale must not be zero and must be at most %g in size" % MAX_SCALE)
        for field, number in (("minimum", low), ("maximum", high), ("scale", scale)):
            object.__setattr__(self, field, number)


@dataclass(frozen=True)
class CsvSpec:
    """What the file is allowed to contain. ``time_scale`` is seconds per file unit (0.001 when the file counts milliseconds)."""

    time_column: str
    columns: Sequence[Column]
    time_scale: float = 1.0
    ignore: Sequence[str] = ()  # headers that may be present and are skipped on purpose; every one must be listed

    def __post_init__(self) -> None:
        if not isinstance(self.time_column, str) or not _NAME_RE.fullmatch(self.time_column):
            raise CsvError("the time column name is not valid")
        scale = _number(self.time_scale, "time_scale")
        if not 0.0 < scale <= 1.0e6:
            raise CsvError("time_scale must be above 0 and at most 1e6")
        for seq, what in ((self.columns, "columns"), (self.ignore, "ignore")):
            if isinstance(seq, (str, bytes)) or not isinstance(seq, (list, tuple)):
                raise CsvError(f"{what} must be a list or a tuple")
        columns, ignore = tuple(self.columns), tuple(self.ignore)
        if not 1 <= len(columns) <= MAX_COLUMNS - 1 or not all(isinstance(c, Column) for c in columns):
            raise CsvError("columns must hold 1 to %d Column objects" % (MAX_COLUMNS - 1))
        if any(not isinstance(n, str) or not _NAME_RE.fullmatch(n) for n in ignore):
            raise CsvError("an ignored header is not a valid name")
        names = [self.time_column] + [c.name for c in columns] + list(ignore)
        if len(names) != len(set(names)) or len(names) > MAX_COLUMNS:
            raise CsvError("header names must be unique, and there are at most %d of them" % MAX_COLUMNS)
        if len({c.sensor_id for c in columns}) != len(columns):
            raise CsvError("sensor ids must be unique")
        object.__setattr__(self, "time_scale", scale)
        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "ignore", ignore)

    def fingerprint(self) -> dict:
        return {"time": [self.time_column, self.time_scale], "ignore": sorted(self.ignore),
                "columns": [[c.name, c.sensor_id, c.unit, c.scale, c.minimum, c.maximum] for c in self.columns]}


@dataclass(frozen=True)
class Parsed:
    times: tuple  # seconds, relative to the first row, strictly increasing
    rows: tuple  # one tuple of floats per row, in the order of ``spec.columns``


def _lines(data: Union[bytes, bytearray, str], max_bytes: int) -> list:
    if isinstance(data, (bytes, bytearray)):
        if len(data) > max_bytes:
            raise CsvError("the data is larger than %d bytes" % max_bytes)
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            raise CsvError("the data is not UTF-8") from None
    elif isinstance(data, str):
        if len(data) > max_bytes or len(data.encode("utf-8")) > max_bytes:
            raise CsvError("the data is larger than %d bytes" % max_bytes)
        text = data
    else:
        raise CsvError("the data must be bytes or text")
    if text.startswith("\ufeff"):
        text = text[1:]  # one byte order mark is what spreadsheets write; nothing else non-ASCII is accepted
    if _BAD_TEXT_RE.search(text):
        raise CsvError("the data holds a character that is not printable ASCII")
    if _LONE_CR_RE.search(text):
        raise CsvError("a carriage return must be followed by a line feed")
    if '"' in text:
        raise CsvError("quoted fields are not accepted")
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # one final line end is fine
    if not lines or any(line == "" for line in lines):
        raise CsvError("the data must have a header and no blank line")
    if any(len(line) > MAX_LINE_CHARS for line in lines):
        raise CsvError("a line is longer than %d characters" % MAX_LINE_CHARS)
    return lines


def _field(text: str, row: int, name: str) -> float:
    if len(text) > MAX_FIELD_CHARS or not _NUM_RE.fullmatch(text):
        raise CsvError(f"row {row}, column {name}: not a plain decimal number")
    return float(text)


def parse_csv(data: Union[bytes, bytearray, str], spec: CsvSpec, *, max_bytes: int = MAX_BYTES, max_rows: int = MAX_ROWS) -> Parsed:
    """Parse and check the whole file against ``spec``. Raises ``CsvError`` for anything it does not understand."""
    if not isinstance(spec, CsvSpec):
        raise CsvError("spec must be a CsvSpec")
    max_bytes, max_rows = _count(max_bytes, "max_bytes", MAX_BYTES), _count(max_rows, "max_rows", MAX_ROWS)
    lines = _lines(data, max_bytes)
    header = lines[0].split(",")
    if len(header) > MAX_COLUMNS:
        raise CsvError("the header has more than %d columns" % MAX_COLUMNS)
    if any(not _NAME_RE.fullmatch(h) for h in header) or len(set(header)) != len(header):
        raise CsvError("the header holds an invalid or repeated name")
    known = {spec.time_column} | {c.name for c in spec.columns} | set(spec.ignore)
    if not set(header) <= known:
        raise CsvError("the header holds a column that was neither declared nor ignored on purpose")
    index = {name: i for i, name in enumerate(header)}
    for name in [spec.time_column] + [c.name for c in spec.columns]:
        if name not in index:
            raise CsvError(f"the declared column {name} is not in the header")
    body = lines[1:]
    if not 1 <= len(body) <= max_rows:
        raise CsvError("the file must have 1 to %d data rows" % max_rows)
    times, rows, first, last = [], [], 0.0, 0.0
    for n, line in enumerate(body, start=1):
        cells = line.split(",")
        if len(cells) != len(header):
            raise CsvError(f"row {n}: wrong number of fields")
        if any(len(c) > MAX_FIELD_CHARS for c in cells):
            raise CsvError(f"row {n}: a field is longer than {MAX_FIELD_CHARS} characters")
        t = _field(cells[index[spec.time_column]], n, spec.time_column) * spec.time_scale
        if not math.isfinite(t):
            raise CsvError(f"row {n}, column {spec.time_column}: time is not finite")
        if n == 1:
            first = t
        elif t <= last:
            raise CsvError(f"row {n}, column {spec.time_column}: time does not increase")
        last = t
        if t - first > MAX_SPAN_S:
            raise CsvError(f"row {n}: the file spans more than {MAX_SPAN_S:g} seconds")
        values = []
        for c in spec.columns:
            v = _field(cells[index[c.name]], n, c.name) * c.scale
            if not math.isfinite(v) or not c.minimum <= v <= c.maximum:
                raise CsvError(f"row {n}, column {c.name}: outside the declared range")
            values.append(v)
        times.append(t - first)
        rows.append(tuple(values))
    return Parsed(tuple(times), tuple(rows))


def read_csv_file(path: Union[str, "os.PathLike[str]"], max_bytes: int = MAX_BYTES) -> bytes:
    """Read a local regular file, at most ``max_bytes``. No symbolic link, no directory, no pipe, nothing larger is read."""
    max_bytes = _count(max_bytes, "max_bytes", MAX_BYTES)
    if not isinstance(path, (str, os.PathLike)):
        raise CsvError("the path must be a string or a path object")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError):
        raise CsvError("the file could not be opened (it must be a regular file, not a link)") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CsvError("the path is not a regular file")
        if info.st_size > max_bytes:
            raise CsvError("the file is larger than %d bytes" % max_bytes)
        chunks, left = [], max_bytes + 1  # one byte more than allowed, to notice a file that grew
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        data = b"".join(chunks)
    except OSError:
        raise CsvError("the file could not be read") from None
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise CsvError("the file is larger than %d bytes" % max_bytes)
    return data


class FakeCsvReplayAdapter(Adapter):
    """Replays the rows of one CSV file. Observe only: no entity, sensors only, no command.

    Time is the time column, relative to the first row. Between two rows the last values hold, and ``data_age_s`` says how old
    they are.
    """

    def __init__(self, data: Union[bytes, bytearray, str], spec: CsvSpec, *, seed: int = 0, max_bytes: int = MAX_BYTES,
                 max_rows: int = MAX_ROWS) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        parsed = parse_csv(data, spec, max_bytes=max_bytes, max_rows=max_rows)
        self._spec, self._times, self._rows, self.seed = spec, parsed.times, parsed.rows, seed
        self.idx, self.t, self.stopped = 0, 0.0, False
        raw = bytes(data) if isinstance(data, (bytes, bytearray)) else data.encode("utf-8")
        model = {"spec": spec.fingerprint(), "data": hashlib.sha256(raw).hexdigest()}
        self._model_sha = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()

    @property
    def simulated(self) -> bool:
        return True

    def targets(self) -> Optional[list]:
        return []  # nothing here can move

    def observe_raw(self) -> Mapping[str, Any]:
        row = self._rows[self.idx]
        sensors = [{"id": c.sensor_id, "kind": "sample", "value": v, "unit": c.unit} for c, v in zip(self._spec.columns, row)]
        sensors.append({"id": "data_age_s", "kind": "timer", "value": self.t - self._times[self.idx], "unit": "s"})
        return {
            "source": {"adapter": "fake", "adapter_version": "0.0.1", "engine_version": "none",
                       "model_id": "csv_replay", "model_sha256": self._model_sha},
            "sim_time_s": self.t, "step": self.idx, "seed": self.seed, "entities": [], "sensors": sensors,
        }

    def dry_run(self, target: str, command: str, value: float, until_sim_time_s: float, max_steps: int) -> DryRun:
        raise ValueError("this adapter is observe-only: it has no command")

    def apply(self, target: str, command: str, value: float) -> None:
        raise ValueError("this adapter is observe-only: it has no command")

    def advance(self, to_sim_time_s: float, max_steps: int) -> bool:
        to = _number(to_sim_time_s, "to_sim_time_s")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 0:
            raise ValueError("max_steps must be a non-negative integer")
        n = 0
        while (self.idx + 1 < len(self._times) and self._times[self.idx + 1] <= to + 1e-9
               and n < max_steps and not self.stopped):
            self.idx += 1
            n += 1
            self.t = self._times[self.idx]
        caught_up = not (self.idx + 1 < len(self._times) and self._times[self.idx + 1] <= to + 1e-9)
        if caught_up and not self.stopped:
            self.t = max(self.t, to)  # the last values hold until the next row
        return self.t >= to - 1e-9

    def stop(self) -> None:
        self.stopped = True

    def clear_stop(self) -> None:
        self.stopped = False


# A starting point for the signals a machine's own control records during milling. [unverified]
# Built only from the signal table of the data paper cited in docs/industry/csv-replay-adapter.md: no file of that dataset has been
# loaded with it, and no claim is made that it works with that data. The ranges are loose sanity bounds chosen here, not values
# taken from the dataset. Only the machine-side signals are included (their own time base). The force sensor signals have another
# time base and are not part of this description. Columns that the table lists for some machines only are ignored on purpose.
MILLING_MACHINE_SIGNALS = CsvSpec(
    time_column="time_machine",
    columns=(
        Column("position_control_deviation_axis_x", "pos_dev_x", "um", -10_000.0, 10_000.0),
        Column("position_control_deviation_axis_y", "pos_dev_y", "um", -10_000.0, 10_000.0),
        Column("tool_position_x", "tool_x", "m", -1.0, 1.0, 0.001),  # the file counts millimetres
        Column("tool_position_y", "tool_y", "m", -1.0, 1.0, 0.001),
        Column("tool_position_z", "tool_z", "m", -1.0, 1.0, 0.001),
        Column("torque_spindle", "spindle_torque", "N*m", -50.0, 50.0),
    ),
    ignore=("torque_axis_x", "torque_axis_y", "torque_axis_z", "force_axis_x", "force_axis_y", "force_axis_z"),
)
