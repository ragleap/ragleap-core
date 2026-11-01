"""Observation format v0. Device data is untrusted: no free text, finite numbers, size caps."""
from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ValidationError

SCHEMA = "ragleap.physical.observation/0"
ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
ACTOR_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,64}$")  # like ID_RE plus @ and :, for people and roles
UNITS = frozenset({"rad", "rad/s", "m", "m/s", "N", "N*m", "s", "kg", "1", "um", "W", "kWh"})
MAX_ENTITIES = 256
MAX_SENSORS = 256
READ_LIMIT = 1024  # at most this many raw items are even looked at, accepted or not
SOURCE_KEYS = ("adapter", "adapter_version", "engine_version", "model_id", "model_sha256")


def ident(value: Any, what: str = "id") -> str:
    """Return ``value`` if it is a safe identifier, else raise ValidationError."""
    if not isinstance(value, str) or not ID_RE.fullmatch(value):  # fullmatch: "$" would accept a trailing newline
        raise ValidationError(f"{what} is not a safe identifier")
    return value


def actor(value: Any, what: str = "actor") -> str:
    """Return ``value`` if it is a safe actor id such as person:alice or employee:ops."""
    if not isinstance(value, str) or not ACTOR_RE.fullmatch(value):
        raise ValidationError(f"{what} is not a safe actor id")
    return value


def finite(value: Any, what: str = "number") -> float:
    """Return ``value`` as a finite float. Booleans, NaN and infinity are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{what} is not a number")
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float
        raise ValidationError(f"{what} is too large") from None
    if not math.isfinite(number):
        raise ValidationError(f"{what} is not finite")
    return number


def unit(value: Any) -> str:
    """Return ``value`` if it is an allowed unit."""
    if not isinstance(value, str) or value not in UNITS:
        raise ValidationError("unit is not allowed")
    return value


def _bounded(items: Any):
    """At most READ_LIMIT raw items are examined. Returns (items, was_cut). Anything that is not a list counts as empty."""
    if not isinstance(items, (list, tuple)):
        return [], False
    got = list(itertools.islice(iter(items), READ_LIMIT + 1))  # iterate with a hard stop: never trust len() or slicing
    return got[:READ_LIMIT], len(got) > READ_LIMIT


def _drop_duplicates(items: list):
    """An id that appears twice is ambiguous (which one is real?), so ALL items with that id are dropped."""
    counts: dict = {}
    for it in items:
        counts[it.id] = counts.get(it.id, 0) + 1
    kept = [it for it in items if counts[it.id] == 1]
    return kept, len(items) - len(kept)


@dataclass(frozen=True)
class Entity:
    id: str
    kind: str
    pos: float
    vel: float
    pos_unit: str
    vel_unit: str


@dataclass(frozen=True)
class Sensor:
    id: str
    kind: str
    value: float
    unit: str


@dataclass(frozen=True)
class Observation:
    """One observation. ``simulated`` is always True and ``trust`` always 'untrusted'."""

    observation_id: str
    org_id: str
    source: Mapping[str, str]
    sim_time_s: float
    step: int
    seed: int
    entities: tuple
    sensors: tuple
    truncated: bool = False
    dropped: int = 0
    simulated: bool = True
    trust: str = "untrusted"

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any], *, org_id: str, observation_id: str) -> "Observation":
        """Build from adapter output. Unknown keys are ignored. Bad items are dropped and counted."""
        ident(org_id, "org_id")
        ident(observation_id, "observation_id")
        src_raw = raw.get("source")
        if not isinstance(src_raw, Mapping):
            raise ValidationError("source missing")
        source = {k: ident(src_raw.get(k), f"source.{k}") for k in SOURCE_KEYS}
        step, seed = raw.get("step"), raw.get("seed")
        if isinstance(step, bool) or not isinstance(step, int) or step < 0:
            raise ValidationError("step must be a non-negative integer")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValidationError("seed must be an integer")
        sim_time = finite(raw.get("sim_time_s"), "sim_time_s")
        if sim_time < 0:
            raise ValidationError("sim_time_s must not be negative")
        dropped = 0
        truncated = False
        entities: list = []
        sensors: list = []
        ent_items, ent_cut = _bounded(raw.get("entities"))
        sen_items, sen_cut = _bounded(raw.get("sensors"))
        truncated = ent_cut or sen_cut
        for item in ent_items:
            if len(entities) >= MAX_ENTITIES:
                truncated = True
                break
            try:
                entities.append(Entity(ident(item["id"]), ident(item["kind"]), finite(item["pos"]),
                                       finite(item["vel"]), unit(item["pos_unit"]), unit(item["vel_unit"])))
            except (ValidationError, KeyError, TypeError):
                dropped += 1
        for item in sen_items:
            if len(sensors) >= MAX_SENSORS:
                truncated = True
                break
            try:
                sensors.append(Sensor(ident(item["id"]), ident(item["kind"]), finite(item["value"]), unit(item["unit"])))
            except (ValidationError, KeyError, TypeError):
                dropped += 1
        entities, lost_e = _drop_duplicates(entities)
        sensors, lost_s = _drop_duplicates(sensors)
        return cls(observation_id, org_id, source, sim_time, step, seed, tuple(entities), tuple(sensors),
                   truncated, dropped + lost_e + lost_s)

    def entity(self, entity_id: str) -> Entity:
        """Return the entity with this id or raise ValidationError."""
        for e in self.entities:
            if e.id == entity_id:
                return e
        raise ValidationError("entity not found")

    def to_dict(self) -> dict:
        """JSON-ready dict following the v0 schema."""
        return {
            "schema": SCHEMA, "observation_id": self.observation_id, "org_id": self.org_id,
            "simulated": self.simulated, "source": dict(self.source), "sim_time_s": self.sim_time_s,
            "step": self.step, "seed": self.seed,
            "entities": [dict(id=e.id, kind=e.kind, pos=e.pos, vel=e.vel, pos_unit=e.pos_unit, vel_unit=e.vel_unit)
                         for e in self.entities],
            "sensors": [dict(id=s.id, kind=s.kind, value=s.value, unit=s.unit) for s in self.sensors],
            "truncated": self.truncated, "dropped": self.dropped, "trust": self.trust,
        }
