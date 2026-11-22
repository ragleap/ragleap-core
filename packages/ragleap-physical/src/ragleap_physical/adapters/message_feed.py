"""An observe-only replay adapter for recorded messages, standard library only.

It replays a recorded file of messages as observations. It never connects to anything: there is no network code, no URL, no
subscription and no command. The file's bytes are given to the constructor (``read_feed_file`` reads a local file, with a size
cap, for you). The adapter has no target, ``apply()`` and ``dry_run()`` refuse, and it is meant for the Observe level of the
capability model.

The text is treated as untrusted device data and the parser refuses what it does not understand:

* one message per line, each a flat JSON object with exactly three keys: ``topic`` (text), ``time_s`` (a number of seconds) and
  ``payload`` (a number, or one of the words declared for that topic). Repeated keys, ``NaN``, ``Infinity``, nesting, booleans,
  null and extra or missing keys are refused;
* the caller declares every topic it accepts in a ``FeedSpec``, with the sensor id, unit, scale and value range of its payload and
  the number each declared word stands for. A message for any other topic is refused;
* caps (all with a hard upper limit that the caller cannot raise): bytes, messages, topics, characters per line, topic and
  payload, and the time span. Every value must lie inside its declared range after scaling;
* printable ASCII only, with ``\\n`` or ``\\r\\n`` line endings and no blank line;
* time must strictly increase from one message to the next;
* an error message never repeats text from the file, only the line number and the declared sensor id.

Nothing is guessed: a file that fails any check is refused as a whole, before the first message is replayed. What an observation
shows, for each declared topic, is the last value received up to the replay time and its age. A topic with no message yet is
missing: it has no value and no age in the observation, it is never shown as zero, and ``topics_missing`` counts it. The replay is
a model, not a device: the same file always gives the same observations.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Union

from ..observation import ident
from .base import Adapter, DryRun
from .csv_replay import (ALLOWED_UNITS, MAX_BYTES, MAX_LINE_CHARS, MAX_SCALE, MAX_SPAN_S, META_IDS, CsvError, _BAD_TEXT_RE,
                         _LONE_CR_RE, _count, _number, read_csv_file)

MAX_MESSAGES = 100_000  # hard caps: the caller may ask for less, never for more
MAX_TOPICS = 64
MAX_TOPIC_CHARS = 64
MAX_PAYLOAD_CHARS = 32  # a number's text, or a word
MAX_WORDS = 8  # declared words per topic
MAX_WORD_CHARS = 16
MAX_SENSOR_ID_CHARS = 48  # leaves room for the "_age_s" suffix
MISSING_COUNT_ID = "topics_missing"
KEYS = frozenset({"topic", "time_s", "payload"})
_TOPIC_RE = re.compile(r"^[A-Za-z0-9_./-]{1,64}\Z")
_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,15}\Z")
_ABS_TIME_MAX = 1.0e12


class FeedError(CsvError):
    """The file or its description was refused. The message never repeats text from the file."""


@dataclass(frozen=True)
class Topic:
    """One topic that becomes a sensor: ``payload * scale`` is in ``unit`` and must lie in ``[minimum, maximum]``.

    ``words`` maps each accepted word to the number it stands for (already in ``unit``, not scaled).
    """

    topic: str
    sensor_id: str
    unit: str
    minimum: float
    maximum: float
    scale: float = 1.0
    words: Mapping[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not isinstance(self.topic, str) or not _TOPIC_RE.fullmatch(self.topic):
            raise FeedError("a topic must be 1 to 64 characters of letters, digits, '_', '.', '/' or '-'")
        try:
            ident(self.sensor_id, "sensor_id")
        except ValueError:
            raise FeedError("a sensor id is not a safe identifier") from None
        if len(self.sensor_id) > MAX_SENSOR_ID_CHARS:
            raise FeedError("a sensor id is longer than %d characters" % MAX_SENSOR_ID_CHARS)
        if self.sensor_id in META_IDS or self.sensor_id == MISSING_COUNT_ID or self.sensor_id.endswith("_age_s"):
            raise FeedError("a sensor id is reserved")
        if not isinstance(self.unit, str) or self.unit not in ALLOWED_UNITS:
            raise FeedError("a unit is not one the observation accepts")
        low, high, scale = _number(self.minimum, "minimum"), _number(self.maximum, "maximum"), _number(self.scale, "scale")
        if not low < high:
            raise FeedError("minimum must be below maximum")
        if scale == 0.0 or abs(scale) > MAX_SCALE:
            raise FeedError("scale must not be zero and must be at most %g in size" % MAX_SCALE)
        words = {} if self.words is None else self.words
        if not isinstance(words, Mapping) or len(words) > MAX_WORDS:
            raise FeedError("words must be a mapping of at most %d entries" % MAX_WORDS)
        clean = {}
        for word, number in words.items():
            if not isinstance(word, str) or not _WORD_RE.fullmatch(word):
                raise FeedError("a declared word is not valid")
            number = _number(number, "a word's number")
            if not low <= number <= high:
                raise FeedError("a word's number is outside the declared range")
            clean[word] = number
        for field, number in (("minimum", low), ("maximum", high), ("scale", scale)):
            object.__setattr__(self, field, number)
        object.__setattr__(self, "words", tuple(sorted(clean.items())))


@dataclass(frozen=True)
class FeedSpec:
    """What the file is allowed to contain."""

    topics: Sequence[Topic]

    def __post_init__(self) -> None:
        if isinstance(self.topics, (str, bytes)) or not isinstance(self.topics, (list, tuple)):
            raise FeedError("topics must be a list or a tuple")
        topics = tuple(self.topics)
        if not 1 <= len(topics) <= MAX_TOPICS or not all(isinstance(t, Topic) for t in topics):
            raise FeedError("topics must hold 1 to %d Topic objects" % MAX_TOPICS)
        if len({t.topic for t in topics}) != len(topics):
            raise FeedError("topics must be unique")
        if len({t.sensor_id for t in topics}) != len(topics):
            raise FeedError("sensor ids must be unique")
        object.__setattr__(self, "topics", topics)

    def fingerprint(self) -> dict:
        return {"topics": [[t.topic, t.sensor_id, t.unit, t.scale, t.minimum, t.maximum, list(t.words)] for t in self.topics]}


@dataclass(frozen=True)
class Parsed:
    times: tuple  # seconds, relative to the first message, strictly increasing
    topics: tuple  # index into ``spec.topics``, one per message
    values: tuple  # the scaled value of each message, in the topic's unit


def _lines(data: Union[bytes, bytearray, str], max_bytes: int) -> list:
    if isinstance(data, (bytes, bytearray)):
        if len(data) > max_bytes:
            raise FeedError("the data is larger than %d bytes" % max_bytes)
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            raise FeedError("the data is not UTF-8") from None
    elif isinstance(data, str):
        if len(data) > max_bytes or len(data.encode("utf-8")) > max_bytes:
            raise FeedError("the data is larger than %d bytes" % max_bytes)
        text = data
    else:
        raise FeedError("the data must be bytes or text")
    if text.startswith("\ufeff"):
        text = text[1:]
    if _BAD_TEXT_RE.search(text):
        raise FeedError("the data holds a character that is not printable ASCII")
    if _LONE_CR_RE.search(text):
        raise FeedError("a carriage return must be followed by a line feed")
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # one final line end is fine
    if not lines or any(line == "" for line in lines):
        raise FeedError("the data must have at least one message and no blank line")
    if any(len(line) > MAX_LINE_CHARS for line in lines):
        raise FeedError("a line is longer than %d characters" % MAX_LINE_CHARS)
    return lines


def _no_repeat(pairs: list) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise FeedError("a key is repeated")
        out[key] = value
    return out


def _refuse_constant(_name: str) -> Any:
    raise FeedError("NaN and Infinity are not accepted")


def _plain_int(text: str) -> int:
    if len(text) > MAX_PAYLOAD_CHARS:
        raise FeedError("a number is longer than %d characters" % MAX_PAYLOAD_CHARS)
    return int(text)


def _plain_float(text: str) -> float:
    if len(text) > MAX_PAYLOAD_CHARS:
        raise FeedError("a number is longer than %d characters" % MAX_PAYLOAD_CHARS)
    return float(text)


def _message(line: str, n: int) -> dict:
    try:
        obj = json.loads(line, object_pairs_hook=_no_repeat, parse_constant=_refuse_constant,
                         parse_int=_plain_int, parse_float=_plain_float)
    except FeedError as err:
        raise FeedError(f"line {n}: {err}") from None
    except (ValueError, RecursionError):
        raise FeedError(f"line {n}: not a valid JSON object") from None
    if not isinstance(obj, dict) or set(obj) != KEYS:
        raise FeedError(f"line {n}: a message must be an object with exactly the keys topic, time_s and payload")
    return obj


def parse_feed(data: Union[bytes, bytearray, str], spec: FeedSpec, *, max_bytes: int = MAX_BYTES,
               max_messages: int = MAX_MESSAGES) -> Parsed:
    """Parse and check the whole file against ``spec``. Raises ``CsvError`` (a ``FeedError``) for anything it does not understand."""
    if not isinstance(spec, FeedSpec):
        raise FeedError("spec must be a FeedSpec")
    max_bytes, max_messages = _count(max_bytes, "max_bytes", MAX_BYTES), _count(max_messages, "max_messages", MAX_MESSAGES)
    lines = _lines(data, max_bytes)
    if len(lines) > max_messages:
        raise FeedError("the file must have 1 to %d messages" % max_messages)
    by_topic = {t.topic: i for i, t in enumerate(spec.topics)}
    times, which, values = [], [], []
    first = last = 0.0
    for n, line in enumerate(lines, start=1):
        obj = _message(line, n)
        topic, t, payload = obj["topic"], obj["time_s"], obj["payload"]
        if not isinstance(topic, str) or len(topic) > MAX_TOPIC_CHARS or topic not in by_topic:
            raise FeedError(f"line {n}: the topic is not one that was declared")
        i = by_topic[topic]
        top = spec.topics[i]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(float(t)) or abs(float(t)) > _ABS_TIME_MAX:
            raise FeedError(f"line {n}, {top.sensor_id}: time_s is not a usable number")
        t = float(t)
        if n == 1:
            first = t
        elif t <= last:
            raise FeedError(f"line {n}, {top.sensor_id}: time does not increase")
        last = t
        if t - first > MAX_SPAN_S:
            raise FeedError(f"line {n}: the file spans more than {MAX_SPAN_S:g} seconds")
        if isinstance(payload, str):
            if len(payload) > MAX_WORD_CHARS or dict(top.words).get(payload) is None:
                raise FeedError(f"line {n}, {top.sensor_id}: the payload is not a declared word")
            v = dict(top.words)[payload]
        elif isinstance(payload, bool) or not isinstance(payload, (int, float)):
            raise FeedError(f"line {n}, {top.sensor_id}: the payload must be a number or a declared word")
        else:
            try:
                v = float(payload) * top.scale
            except OverflowError:
                raise FeedError(f"line {n}, {top.sensor_id}: the payload is too large") from None
        if not math.isfinite(v) or not top.minimum <= v <= top.maximum:
            raise FeedError(f"line {n}, {top.sensor_id}: outside the declared range")
        times.append(t - first)
        which.append(i)
        values.append(v)
    return Parsed(tuple(times), tuple(which), tuple(values))


def read_feed_file(path: Any, max_bytes: int = MAX_BYTES) -> bytes:
    """Read a local regular file, at most ``max_bytes`` (same rules as the CSV replay: no link, no directory, no pipe)."""
    return read_csv_file(path, max_bytes)


class FakeMessageFeedAdapter(Adapter):
    """Replays the messages of one recorded file. Observe only: no entity, sensors only, no command.

    Time is ``time_s``, relative to the first message. Between two messages the last values hold, and each sensor's age says how
    old its value is. A topic with no message yet has no sensor in the observation (it is missing, not zero).
    """

    def __init__(self, data: Union[bytes, bytearray, str], spec: FeedSpec, *, seed: int = 0, max_bytes: int = MAX_BYTES,
                 max_messages: int = MAX_MESSAGES) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        parsed = parse_feed(data, spec, max_bytes=max_bytes, max_messages=max_messages)
        self._spec, self._p, self.seed = spec, parsed, seed
        self.n, self.t, self.stopped = 0, 0.0, False
        self._last: list = [None] * len(spec.topics)  # (value, time) of the last message per topic
        raw = bytes(data) if isinstance(data, (bytes, bytearray)) else data.encode("utf-8")
        model = {"spec": spec.fingerprint(), "data": hashlib.sha256(raw).hexdigest()}
        self._model_sha = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
        self._replay(0.0, len(parsed.times))  # the first message is at time 0

    @property
    def simulated(self) -> bool:
        return True

    def targets(self) -> Optional[list]:
        return []  # nothing here can move

    def missing(self) -> list:
        """Topics that have not had a message yet."""
        return [t.topic for t, last in zip(self._spec.topics, self._last) if last is None]

    def _replay(self, to: float, max_steps: int) -> int:
        k = 0
        times = self._p.times
        while self.n < len(times) and times[self.n] <= to + 1e-9 and k < max_steps:
            self._last[self._p.topics[self.n]] = (self._p.values[self.n], times[self.n])
            self.n += 1
            k += 1
            self.t = times[self.n - 1]
        return k

    def _pending(self, to: float) -> bool:
        return self.n < len(self._p.times) and self._p.times[self.n] <= to + 1e-9

    def observe_raw(self) -> Mapping[str, Any]:
        sensors = []
        absent = 0
        for top, last in zip(self._spec.topics, self._last):
            if last is None:
                absent += 1
                continue
            sensors.append({"id": top.sensor_id, "kind": "sample", "value": last[0], "unit": top.unit})
            sensors.append({"id": top.sensor_id + "_age_s", "kind": "timer", "value": self.t - last[1], "unit": "s"})
        sensors.append({"id": MISSING_COUNT_ID, "kind": "sample", "value": float(absent), "unit": "1"})
        return {
            "source": {"adapter": "fake", "adapter_version": "0.0.1", "engine_version": "none",
                       "model_id": "message_feed", "model_sha256": self._model_sha},
            "sim_time_s": self.t, "step": self.n, "seed": self.seed, "entities": [], "sensors": sensors,
        }

    def dry_run(self, target: str, command: str, value: float, until_sim_time_s: float, max_steps: int) -> DryRun:
        raise ValueError("this adapter is observe-only: it has no command")

    def apply(self, target: str, command: str, value: float) -> None:
        raise ValueError("this adapter is observe-only: it has no command")

    def advance(self, to_sim_time_s: float, max_steps: int) -> bool:
        to = _number(to_sim_time_s, "to_sim_time_s")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 0:
            raise ValueError("max_steps must be a non-negative integer")
        if not self.stopped:
            self._replay(to, max_steps)
            if not self._pending(to):
                self.t = max(self.t, to)  # the last values hold until the next message
        return self.t >= to - 1e-9

    def stop(self) -> None:
        self.stopped = True

    def clear_stop(self) -> None:
        self.stopped = False
