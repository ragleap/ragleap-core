import asyncio

import pytest

from ragleap_phone import Adapter, Call, Capabilities, Event, Frame, new_call_id


def _caps(**kw):
    base = dict(transport="fake", codecs=("pcm16",), sample_rates=(16000,), dtmf=True, barge_in=True, max_concurrent_calls=2)
    base.update(kw)
    return Capabilities(**base)


def test_capabilities_accepts_valid_values():
    assert _caps().max_concurrent_calls == 2


@pytest.mark.parametrize("kw", [
    dict(transport=""), dict(transport=5), dict(codecs=()), dict(codecs=["pcm16"]), dict(codecs=("",)),
    dict(sample_rates=()), dict(sample_rates=(0,)), dict(sample_rates=(8000.0,)), dict(sample_rates=(True,)),
    dict(dtmf=1), dict(barge_in="yes"), dict(max_concurrent_calls=0), dict(max_concurrent_calls=True),
])
def test_capabilities_refuses_bad_values(kw):
    with pytest.raises(ValueError):
        _caps(**kw)


def test_event_kinds():
    assert Event("call.started", 1).data is None
    assert Event("error", 0, "tunnel closed").data == "tunnel closed"
    with pytest.raises(ValueError):
        Event("ring", 0)
    with pytest.raises(ValueError):
        Event("call.ended", -1)
    with pytest.raises(ValueError):
        Event("call.ended", True)


@pytest.mark.parametrize("digit", list("0123456789*#ABCD"))
def test_dtmf_digits_are_accepted(digit):
    assert Event("dtmf", 0, digit).data == digit


@pytest.mark.parametrize("bad", [None, "", "12", "E", "a", 5])
def test_dtmf_needs_one_digit(bad):
    with pytest.raises(ValueError):
        Event("dtmf", 0, bad)


def test_call_ids_are_made_here_and_differ():
    a, b = new_call_id(), new_call_id()
    assert a != b and len(a) == 16 and int(a, 16) >= 0


def test_the_interfaces_cannot_be_used_directly():
    with pytest.raises(TypeError):
        Adapter()
    with pytest.raises(TypeError):
        Call("inbound")


class FakeCall(Call):
    def __init__(self, direction, frames):
        super().__init__(direction)
        self._frames = frames
        self.sent = []
        self.cleared = 0
        self.hung_up = False
        self._events = [Event("call.started", 0), Event("call.ended", 1)]

    async def audio_in(self):
        for f in self._frames:
            yield f

    async def send_audio(self, frame):
        self.sent.append(frame)

    async def clear_playback(self):
        self.cleared += 1

    async def hangup(self):
        self.hung_up = True

    @property
    def events(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()


class FakeAdapter(Adapter):
    name = "fake"
    capabilities = _caps()

    def __init__(self, calls):
        self._calls = calls
        self.closed = False

    async def run(self, on_call):
        for c in self._calls:
            await on_call(c)

    async def close(self):
        self.closed = True


def test_a_fake_adapter_delivers_calls_with_distinct_ids():
    frames = [Frame(bytes(640), 16000, i) for i in range(3)]
    calls = [FakeCall("inbound", frames), FakeCall("outbound", [])]
    seen = []

    async def on_call(call):
        got = [f async for f in call.audio_in()]
        evs = [e.kind async for e in call.events]
        await call.send_audio(frames[0])
        await call.clear_playback()
        await call.hangup()
        seen.append((call.id, call.direction, len(got), evs))

    async def main():
        ad = FakeAdapter(calls)
        await ad.run(on_call)
        await ad.close()
        return ad

    ad = asyncio.run(main())
    assert ad.closed and ad.name == "fake" and ad.capabilities.transport == "fake"
    assert seen[0][1:] == ("inbound", 3, ["call.started", "call.ended"])
    assert seen[1][1:] == ("outbound", 0, ["call.started", "call.ended"])
    assert seen[0][0] != seen[1][0]
    assert calls[0].sent == [frames[0]] and calls[0].cleared == 1 and calls[0].hung_up


def test_a_call_id_cannot_be_chosen_by_the_caller():
    with pytest.raises(TypeError):
        FakeCall("inbound", [], call_id="remote-chosen")


def test_bad_direction_is_refused():
    with pytest.raises(ValueError):
        FakeCall("sideways", [])
