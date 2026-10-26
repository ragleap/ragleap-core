import importlib.util
import pathlib
import struct
import sys
import types
import wave

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ragleap_voice import EngineError, EngineNotInstalledError, Request, UnsupportedFeatureError, engines  # noqa: E402
from ragleap_voice.parler_engine import INTERPRETER_ENV, LANGUAGES, SCRIPT, make_parler_engine  # noqa: E402

PARLER, FLAN = "/models/parler", "/models/flan"


class FakeAudio:
    def __init__(self, values):
        self.values = list(values)

    def cpu(self):
        return self

    def numpy(self):
        return self

    def squeeze(self):
        return self

    def reshape(self, *_):
        return self

    def astype(self, _t):
        return self

    def clip(self, lo, hi):
        return FakeAudio(min(max(v, lo), hi) for v in self.values)

    def __mul__(self, k):
        return FakeAudio(v * k for v in self.values)

    def tobytes(self):
        return struct.pack("<%dh" % len(self.values), *[int(v) for v in self.values])


class Enc:
    def __init__(self, text, path):
        self.input_ids = ("ids", path, text)
        self.attention_mask = ("mask", path, text)

    def to(self, _dev):
        return self


class FakeTok:
    def __init__(self, path):
        self.path = path

    def __call__(self, text, return_tensors=None):
        return Enc(text, self.path)


class FakeModel:
    config = types.SimpleNamespace(sampling_rate=24000)
    last = None

    def to(self, dev):
        self.dev = dev
        return self

    def generate(self, **kw):
        FakeModel.last = kw
        return types.SimpleNamespace(cpu=lambda: FakeAudio([0.0, 0.5, -0.5, 2.0]))


CALLS = []


class FakeParler:
    @classmethod
    def from_pretrained(cls, path, **kw):
        CALLS.append(("model", path, kw))
        return FakeModel()


class FakeAuto:
    @classmethod
    def from_pretrained(cls, path, **kw):
        CALLS.append(("tok", path, kw))
        return FakeTok(path)


@pytest.fixture
def worker(monkeypatch):
    del CALLS[:]
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "parler_tts", types.SimpleNamespace(ParlerTTSForConditionalGeneration=FakeParler))
    monkeypatch.setitem(sys.modules, "transformers", types.SimpleNamespace(AutoTokenizer=FakeAuto))
    spec = importlib.util.spec_from_file_location("parler_worker_under_test", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def loaded(mod):
    mod.load({"model_dirs": {"indic-parler-tts": PARLER, "flan-t5-large-tokenizer": FLAN}})
    return mod


def msg(tmp_path, text="namaste", **options):
    return {"work_dir": str(tmp_path), "request": {"kind": "tts", "text": text, "options": options}}


def test_registered_without_emotion_and_isolated():
    info = [i for i in engines("tts") if i.name == "parler"]
    assert len(info) == 1 and not info[0].third_party
    caps = info[0].capabilities
    assert caps.languages == LANGUAGES == {"hi", "ta", "te", "ml", "kn", "en"}
    assert caps.isolated is True and caps.emotion == frozenset() and caps.clone is False
    assert info[0].manifest_entries == ("indic-parler-tts", "flan-t5-large-tokenizer")


def test_no_interpreter_set(monkeypatch):
    monkeypatch.delenv(INTERPRETER_ENV, raising=False)
    with pytest.raises(EngineNotInstalledError) as info:
        make_parler_engine().load({})
    assert INTERPRETER_ENV in str(info.value)


def test_unsupported_parameters_are_refused_before_any_worker_starts():
    eng = make_parler_engine()
    for kw in ({"speed": 1.2}, {"emotion": "happy"}, {"voice": "someone"}):
        with pytest.raises(UnsupportedFeatureError):
            eng.run(Request(kind="tts", language="hi", text="namaste", **kw))
    with pytest.raises(EngineError):
        eng.run(Request(kind="tts", language="hi", text="namaste"))


def test_load_reads_only_the_given_folders(worker):
    loaded(worker)
    assert ("model", PARLER, {"local_files_only": True}) in CALLS
    assert ("tok", PARLER, {"local_files_only": True}) in CALLS
    assert ("tok", FLAN, {"local_files_only": True}) in CALLS
    assert len(CALLS) == 3


def test_load_errors(worker, monkeypatch):
    with pytest.raises(RuntimeError):
        worker.load({"model_dirs": {"indic-parler-tts": PARLER}})
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(RuntimeError) as info:
        loaded(worker)
    assert INTERPRETER_ENV in str(info.value)


def test_run_writes_a_wav_and_uses_the_right_tokenizers(worker, tmp_path):
    loaded(worker)
    res = worker.run(msg(tmp_path))
    assert res["sample_rate"] == 24000 and any("not measured" in n for n in res["notes"])
    with wave.open(res["wav_path"], "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 24000, 4)
        assert struct.unpack("<4h", w.readframes(4)) == (0, 16383, -16383, 32767)
    kw = FakeModel.last
    assert kw["input_ids"] == ("ids", FLAN, worker.DESC)
    assert kw["prompt_input_ids"] == ("ids", PARLER, "namaste")


def test_description_option(worker, tmp_path):
    loaded(worker)
    worker.run(msg(tmp_path, description="A calm voice."))
    assert FakeModel.last["input_ids"] == ("ids", FLAN, "A calm voice.")


def test_run_errors(worker, tmp_path):
    with pytest.raises(RuntimeError):
        worker.run(msg(tmp_path))
    loaded(worker)
    for bad in (msg(tmp_path, text="  "), msg(tmp_path, pitch=1), msg(tmp_path, description=""), msg(tmp_path, description=5)):
        with pytest.raises(ValueError):
            worker.run(bad)


def test_real_script_reports_a_missing_environment(tmp_path, monkeypatch):
    if importlib.util.find_spec("parler_tts") is not None:
        pytest.skip("parler_tts is installed here")
    eng = make_parler_engine(interpreter=sys.executable, base_dir=str(tmp_path))
    with pytest.raises(EngineError) as info:
        eng.load({"indic-parler-tts": tmp_path, "flan-t5-large-tokenizer": tmp_path})
    assert INTERPRETER_ENV in str(info.value)
