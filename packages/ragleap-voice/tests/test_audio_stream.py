import array
import random

import pytest

from ragleap_voice import audio


def _signal(n, seed):
    rng = random.Random(seed)
    x = [rng.randint(-3000, 3000) for _ in range(n)]
    for i in range(0, n, 97):  # transients, including full-scale clipping
        x[i] = 32767 if (i // 97) % 2 else -32768
    return array.array("h", x)


def _run(cls, x, sizes):
    s = cls()
    out = array.array("h")
    pos = 0
    for size in sizes:
        out.extend(s.process(x[pos:pos + size]))
        pos += size
        if pos >= len(x):
            break
    if pos < len(x):
        out.extend(s.process(x[pos:]))
    out.extend(s.finish())
    return out, s


def _irregular(seed):
    rng = random.Random(seed)
    while True:
        yield rng.choice([0, 0, 1, 2, 3, 7, 31, 160, 333, 500])


CASES = [(audio.Down2Stream, audio.down2), (audio.Up2Stream, audio.up2)]


@pytest.mark.parametrize("cls,one_shot", CASES)
@pytest.mark.parametrize("n", [0, 1, 2, 14, 15, 16, 30, 31, 32, 321, 1000, 4001])
def test_chunked_equals_one_shot(cls, one_shot, n):
    x = _signal(n, n)
    want = one_shot(x)
    for sizes in (iter(lambda: 1, 0), iter(lambda: 160, 0), iter(lambda: 320, 0), _irregular(n)):
        got, _ = _run(cls, x, sizes)
        assert got == want


@pytest.mark.parametrize("cls,one_shot", CASES)
def test_single_chunk_and_whole_signal(cls, one_shot):
    x = _signal(2500, 5)
    s = cls()
    got = s.process(x)
    got.extend(s.finish())
    assert got == one_shot(x)


@pytest.mark.parametrize("cls,one_shot", CASES)
def test_finish_is_idempotent_and_process_after_finish_raises(cls, one_shot):
    s = cls()
    s.process(_signal(100, 1))
    s.finish()
    assert len(s.finish()) == 0
    with pytest.raises(ValueError):
        s.process(_signal(10, 2))


@pytest.mark.parametrize("cls,one_shot", CASES)
def test_history_is_bounded(cls, one_shot):
    s = cls()
    x = _signal(320, 3)
    for _ in range(300):
        s.process(x)
        assert len(s._buf) < audio._L


@pytest.mark.parametrize("cls,one_shot", CASES)
def test_independent_chunks_differ_but_stream_does_not(cls, one_shot):
    x = _signal(1280, 9)
    naive = array.array("h")
    for i in range(0, len(x), 320):
        naive.extend(one_shot(x[i:i + 320]))
    got, _ = _run(cls, x, iter(lambda: 320, 0))
    assert got == one_shot(x)
    assert naive != got  # the problem the stream objects solve
