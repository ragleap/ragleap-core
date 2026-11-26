import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ragleap_voice import Registry  # noqa: E402
from ragleap_voice.api import _transcribe  # noqa: E402
from ragleap_voice.errors import ModelNotVerifiedError  # noqa: E402
from test_transcribe import FakeStt, make_env, wav_bytes  # noqa: E402


def test_candidates_are_ordered_by_name_not_by_registration():
    reg = Registry()
    reg.register(FakeStt("b", entries=("other-model",), languages=("en",)))
    reg.register(FakeStt("a", entries=("fake-model",), languages=("en",)))
    assert [e.name for e in reg.auto_candidates("stt", "en")] == ["a", "b"]


def test_auto_uses_the_first_by_name_and_refuses_it_when_unverified_without_trying_another(tmp_path):
    mpath, cache = make_env(tmp_path, status="unverified")
    first = FakeStt("a", entries=("fake-model",), languages=("en",), text="from-a")
    second = FakeStt("b", entries=("other-model",), languages=("en",), text="from-b")
    reg = Registry()
    reg.register(second)
    reg.register(first)
    with pytest.raises(ModelNotVerifiedError):
        _transcribe(wav_bytes(), reg, mpath, language="en", engine="auto", cache_dir=cache)
    assert first.loaded is None and second.loaded is None
    res = _transcribe(wav_bytes(), reg, mpath, language="en", engine="auto", allow_unverified=True, cache_dir=cache)
    assert res.text == "from-a" and second.loaded is None


def test_a_verified_first_pick_runs_and_the_other_engine_is_not_loaded(tmp_path):
    mpath, cache = make_env(tmp_path, status="verified")
    first = FakeStt("a", entries=("fake-model",), languages=("en",), text="from-a")
    second = FakeStt("b", entries=("other-model",), languages=("en",), text="from-b")
    reg = Registry()
    reg.register(first)
    reg.register(second)
    res = _transcribe(wav_bytes(), reg, mpath, language="en", engine="auto", cache_dir=cache)
    assert res.text == "from-a" and second.loaded is None
