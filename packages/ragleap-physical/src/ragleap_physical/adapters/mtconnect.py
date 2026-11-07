"""An observe-only MOCK for MTConnect streams, standard library only: it replays recorded "streams" XML documents as
observations.

It never connects to anything. There is no network code, no URL and no command: the documents are given to the
constructor as bytes or text. This mock is observe-only: it only reads and sends nothing. (MTConnect Part 5, Interfaces,
v1.6.0: machines coordinate by each publishing request and response states that the other reads; the standard gives no
direct way for one machine to command another. Some agents can be set to accept pushed data, for example the reference
agent's AllowPut, default false, not re-checked since 2026-10-30; that is an agent setting, not a command to a machine.)
The XML is treated as untrusted device data:

* only UTF-8, at most ``max_bytes``, at most ``MAX_ELEMENTS`` elements;
* any ``<!`` markup other than a comment or CDATA is refused (so no DTD and no entity), before the parser sees it;
* a sensor is made only for a ``dataItemId`` that the caller listed in ``items``. Ids and text from the device never
  become sensor ids or free text. Anything else is counted in ``skipped_items``;
* numbers must match a strict decimal pattern. ``UNAVAILABLE``, ``nan``, ``inf``, ``1_0`` and series are skipped.

The element layout (``MTConnectStreams`` > ``Header`` and ``Samples`` / ``Events`` / ``Condition`` groups) was checked
only against the section titles of MTConnect Part 3 v1.6.0 and the Condition model of v2.3 to v2.7 (element names
Normal / Warning / Fault / Unavailable; attributes dataItemId, sequence, nativeCode, conditionId). It was NOT checked
against a real agent: the only documents it has seen are the invented ones in the tests. Units are not in a streams
document, so the caller maps each ``dataItemId`` to a unit (and a scale) taken from the device's own description.

Conditions: one activation is identified by ``(dataItemId, conditionId)``; the standard says conditionId defaults to
nativeCode and that several concurrent activations may exist on one data item. The latest element (by sequence) of
each activation gives its state. A Normal that carries neither a conditionId nor a nativeCode clears only activations
that also carry neither (how real agents treat such a Normal is NOT verified: this errs towards reporting more
faults, not fewer). Unavailable means "state unknown" and is counted on its own, never as "no fault".
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence, Union

from ..observation import UNITS, ident
from .base import Adapter, DryRun

ALLOWED_UNITS = frozenset(UNITS) | {"W", "kWh"}  # W and kWh are approved by A
MAX_BYTES = 1_000_000
MAX_ELEMENTS = 50_000
MAX_ITEMS = 100  # mapped data items; plus four meta sensors stays far under the observation cap of 256
MAX_DOCUMENTS = 100_000
META_IDS = frozenset({"fault_count", "warning_count", "unavailable_count", "skipped_items", "data_age_s"})
_TIME_RE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z\Z")
_NUM_RE = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_BAD_MARKUP = re.compile(r"<!(?!--|\[CDATA\[)")
_LEVELS = ("Normal", "Warning", "Fault", "Unavailable")


class StreamError(ValueError):
    """The document was refused. Nothing from it was used."""


@dataclass(frozen=True)
class Item:
    """How to turn one dataItemId into a sensor: the unit it will have, and a factor applied to the number."""

    unit: str
    scale: float = 1.0


@dataclass(frozen=True)
class Parsed:
    created: datetime
    values: Mapping[str, float]  # dataItemId -> value, already scaled
    faults: int
    warnings: int
    unavailable: int
    skipped: int


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _check_items(items: Any) -> None:
    if not isinstance(items, Mapping) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError(f"items must be a mapping of 1 to {MAX_ITEMS} entries")
    for key, item in items.items():
        ident(key, "dataItemId")
        if key in META_IDS:
            raise ValueError("a dataItemId must not use a reserved sensor id")
        if not isinstance(item, Item) or item.unit not in ALLOWED_UNITS:
            raise ValueError("each item must be an Item with an allowed unit")
        if isinstance(item.scale, bool) or not isinstance(item.scale, (int, float)) \
                or not math.isfinite(item.scale) or item.scale <= 0:
            raise ValueError("scale must be a finite number above zero")


def _time(text: Any) -> datetime:
    m = _TIME_RE.match(text) if isinstance(text, str) else None
    if m is None:
        raise StreamError("creationTime must look like 2026-10-28T10:00:00.000Z")
    y, mo, d, h, mi, s = (int(g) for g in m.groups()[:6])
    micro = int(((m.group(7) or "") + "000000")[:6])
    try:
        return datetime(y, mo, d, h, mi, s, micro, tzinfo=timezone.utc)
    except ValueError:
        raise StreamError("creationTime is not a real date") from None


def _activation(el: Any) -> str:
    """The identity of one condition activation inside its data item: conditionId, else nativeCode, else none."""
    for name in ("conditionId", "nativeCode"):
        v = el.get(name)
        if isinstance(v, str) and v:
            return v
    return ""


def _seq(text: Any) -> int:
    return int(text) if isinstance(text, str) and text.isascii() and text.isdigit() and len(text) <= 18 else -1


def _value(text: Any, item: Optional[Item]) -> Optional[float]:
    if item is None or not isinstance(text, str) or not _NUM_RE.match(text.strip()):
        return None
    v = float(text.strip()) * item.scale
    return v if math.isfinite(v) else None


def parse_streams(document: Union[bytes, str], items: Mapping[str, Item], *, max_bytes: int = MAX_BYTES) -> Parsed:
    """Parse one streams document. Raises StreamError if it is refused."""
    _check_items(items)
    if isinstance(document, (bytes, bytearray)):
        try:
            text = bytes(document).decode("utf-8")
        except UnicodeDecodeError:
            raise StreamError("document is not UTF-8") from None
    elif isinstance(document, str):
        text = document
    else:
        raise StreamError("document must be bytes or text")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise StreamError("document is not valid text") from None
    if size > max_bytes:
        raise StreamError("document is too large")
    if _BAD_MARKUP.search(text):
        raise StreamError("document has markup that is not accepted (DTD or entity)")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise StreamError("document is not well-formed XML") from None
    if sum(1 for _ in root.iter()) > MAX_ELEMENTS:
        raise StreamError("document has too many elements")
    if _local(root.tag) != "MTConnectStreams":
        raise StreamError("root element must be MTConnectStreams")
    header = next((e for e in root if _local(e.tag) == "Header"), None)
    if header is None:
        raise StreamError("Header is missing")
    created = _time(header.get("creationTime"))

    latest: dict = {}  # dataItemId -> (sequence, order, value)
    cond: dict = {}  # (dataItemId, activation) -> (sequence, order, level)
    skipped = order = 0
    for group in root.iter():
        kind = _local(group.tag)
        if kind == "Events":
            skipped += len(group)  # event text is free text from the device: never used
            continue
        if kind not in ("Samples", "Condition"):
            continue
        for el in group:
            order += 1
            did, seq = el.get("dataItemId"), _seq(el.get("sequence"))
            if kind == "Samples":
                item = items.get(did) if isinstance(did, str) else None
                val = _value(el.text, item)
                if val is None:
                    skipped += 1
                elif did not in latest or (seq, order) > latest[did][:2]:
                    latest[did] = (seq, order, val)
            else:
                level = _local(el.tag)
                key = (did, _activation(el))
                if not isinstance(did, str) or level not in _LEVELS:
                    skipped += 1
                elif key not in cond or (seq, order) > cond[key][:2]:
                    cond[key] = (seq, order, level)
    levels = [c[2] for c in cond.values()]
    return Parsed(created, {k: v[2] for k, v in latest.items()}, levels.count("Fault"), levels.count("Warning"),
                  levels.count("Unavailable"), skipped)


class FakeMTConnectAdapter(Adapter):
    """Replays a list of streams documents. Observe only: no entities, sensors only, no command.

    Time is the Header creationTime of each document, relative to the first one. Between two documents the last
    values hold, and ``data_age_s`` says how old they are.
    """

    def __init__(self, documents: Sequence[Union[bytes, str]], items: Mapping[str, Item], *, seed: int = 0,
                 max_bytes: int = MAX_BYTES) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        if not 1 <= len(documents) <= MAX_DOCUMENTS:
            raise ValueError(f"documents must be a list of 1 to {MAX_DOCUMENTS} documents")
        _check_items(items)
        parsed = []
        for i, doc in enumerate(documents):
            try:
                parsed.append(parse_streams(doc, items, max_bytes=max_bytes))
            except StreamError as exc:
                raise ValueError(f"document {i} was refused: {exc}") from None
        self._times = [(p.created - parsed[0].created).total_seconds() for p in parsed]
        if any(b < a for a, b in zip(self._times, self._times[1:])):
            raise ValueError("documents must not go back in time")
        self._parsed, self._items, self.seed = parsed, dict(items), seed
        self.idx, self.t, self.stopped = 0, 0.0, False
        digest = [hashlib.sha256(d if isinstance(d, (bytes, bytearray)) else d.encode("utf-8")).hexdigest() for d in documents]
        spec = {"items": {k: [v.unit, v.scale] for k, v in sorted(items.items())}, "documents": digest}
        self._model_sha = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()

    @property
    def simulated(self) -> bool:
        return True

    def targets(self) -> Optional[list]:
        return []  # nothing here can move

    def observe_raw(self) -> Mapping[str, Any]:
        p = self._parsed[self.idx]
        sensors = [{"id": k, "kind": "sample", "value": p.values[k], "unit": self._items[k].unit}
                   for k in sorted(p.values)]
        sensors += [{"id": "fault_count", "kind": "count", "value": float(p.faults), "unit": "1"},
                    {"id": "warning_count", "kind": "count", "value": float(p.warnings), "unit": "1"},
                    {"id": "unavailable_count", "kind": "count", "value": float(p.unavailable), "unit": "1"},
                    {"id": "skipped_items", "kind": "count", "value": float(p.skipped), "unit": "1"},
                    {"id": "data_age_s", "kind": "timer", "value": self.t - self._times[self.idx], "unit": "s"}]
        return {
            "source": {"adapter": "fake", "adapter_version": "0.0.1", "engine_version": "none",
                       "model_id": "mtconnect_replay", "model_sha256": self._model_sha},
            "sim_time_s": self.t, "step": self.idx, "seed": self.seed, "entities": [], "sensors": sensors,
        }

    def dry_run(self, target: str, command: str, value: float, until_sim_time_s: float, max_steps: int) -> DryRun:
        raise ValueError("this adapter is observe-only: it has no command")

    def apply(self, target: str, command: str, value: float) -> None:
        raise ValueError("this adapter is observe-only: it has no command")

    def advance(self, to_sim_time_s: float, max_steps: int) -> bool:
        n = 0
        while (self.idx + 1 < len(self._times) and self._times[self.idx + 1] <= to_sim_time_s + 1e-9
               and n < max_steps and not self.stopped):
            self.idx += 1
            n += 1
            self.t = self._times[self.idx]
        caught_up = not (self.idx + 1 < len(self._times) and self._times[self.idx + 1] <= to_sim_time_s + 1e-9)
        if caught_up and not self.stopped:
            self.t = max(self.t, to_sim_time_s)  # the last values hold until the next document
        return self.t >= to_sim_time_s - 1e-9

    def stop(self) -> None:
        self.stopped = True

    def clear_stop(self) -> None:
        self.stopped = False
