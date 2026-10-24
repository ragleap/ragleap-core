import ast
import dataclasses
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import ragleap_voice  # noqa: E402
from ragleap_voice import registry as registry_mod  # noqa: E402
from ragleap_voice import (Capabilities, EngineError, EngineNotInstalledError, RagleapVoiceError, Registry,  # noqa: E402
                           Request, Result, Segment, Speech, Transcript)

SRC = ROOT / "src" / "ragleap_voice"
ERROR_NAMES = ["ModelNotFoundError", "ModelNotVerifiedError", "LicenceError", "EngineNotInstalledError",
               "EngineUnavailableError", "NoEngineError", "UnsupportedLanguageError", "UnsupportedFeatureError",
               "AudioDecodeError", "ConsentRequiredError", "EngineError"]


class Fake:
    def __init__(self, name="fake-stt", kind="stt", languages=("en",)):
        self.name = name
        self.manifest_entries = ("fake-model",)
        self.capabilities = Capabilities(kind=kind, languages=frozenset(languages))
        self.loaded = self.closed = False

    def load(self, model_dirs):
        self.loaded = True

    def run(self, request):
        return Result(text="hello", language=request.language)

    def close(self):
        self.closed = True


# ---- errors ------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ERROR_NAMES)
def test_every_designed_error_exists_and_derives_from_the_base(name):
    cls = getattr(ragleap_voice, name)
    assert issubclass(cls, RagleapVoiceError)
    with pytest.raises(RagleapVoiceError):
        raise cls("x")


def test_error_message_and_hint():
    assert str(RagleapVoiceError("It broke.")) == "It broke."
    e = RagleapVoiceError("It broke.", hint="try again")
    assert str(e) == "It broke. Next: try again" and e.hint == "try again" and e.message == "It broke."


def test_engine_error_carries_a_log_path():
    assert EngineError("crash", log_path="/tmp/x.log").log_path == "/tmp/x.log"
    assert EngineError("crash").log_path is None


# ---- results -----------------------------------------------------------------------------------
def transcript(**kw):
    base = dict(text="hi", language="en", engine="e", model="m", model_status="unverified")
    base.update(kw)
    return Transcript(**base)


def speech(**kw):
    base = dict(wav=b"RIFF", sample_rate=16000, duration_s=1.0, engine="e", model="m", model_status="unverified")
    base.update(kw)
    return Speech(**base)


def test_results_require_engine_model_and_status():
    with pytest.raises(TypeError):
        Transcript(text="hi", language="en")
    with pytest.raises(TypeError):
        Speech(wav=b"x", sample_rate=16000, duration_s=1.0)


@pytest.mark.parametrize("make", [transcript, speech])
def test_bad_model_status_is_refused(make):
    with pytest.raises(ValueError):
        make(model_status="trusted")


def test_notes_are_coerced_to_a_tuple_and_a_bare_string_is_refused():
    assert transcript(notes=["a", "b"]).notes == ("a", "b")
    with pytest.raises(TypeError):
        transcript(notes="a note")


def test_segments_are_checked():
    assert transcript(segments=[Segment(0.0, 1.0, "hi")]).segments == (Segment(0.0, 1.0, "hi"),)
    with pytest.raises(TypeError):
        transcript(segments=["hi"])
    with pytest.raises(ValueError):
        Segment(2.0, 1.0, "x")


def test_results_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        transcript().text = "changed"


def test_speech_is_always_synthetic():
    assert speech().synthetic is True
    with pytest.raises(ValueError):
        speech(synthetic=False)
    with pytest.raises(ValueError):
        speech(sample_rate=0)


def test_speech_save_refuses_to_overwrite_unless_asked(tmp_path):
    p = tmp_path / "out.wav"
    assert speech().save(p) == p and p.read_bytes() == b"RIFF"
    with pytest.raises(FileExistsError):
        speech(wav=b"NEW!").save(p)
    speech(wav=b"NEW!").save(p, overwrite=True)
    assert p.read_bytes() == b"NEW!"


# ---- capabilities and requests -----------------------------------------------------------------
def test_capabilities_are_validated_and_coerced():
    c = Capabilities(kind="stt", languages=["en", "hi"])
    assert c.languages == frozenset({"en", "hi"}) and c.hardware == frozenset({"cpu"})
    with pytest.raises(ValueError):
        Capabilities(kind="video")
    with pytest.raises(TypeError):
        Capabilities(kind="stt", languages="en")


def test_requests_are_validated():
    with pytest.raises(ValueError):
        Request(kind="stt")
    with pytest.raises(ValueError):
        Request(kind="tts", text="   ")
    assert Request(kind="stt", audio=b"RIFF").options == {}
    assert Request(kind="tts", text="hello", options={"pitch": "low"}).options == {"pitch": "low"}


# ---- registry ----------------------------------------------------------------------------------
def test_register_get_and_info():
    r = Registry()
    r.register(Fake("b-stt")); r.register(Fake("a-tts", kind="tts"))
    assert r.get("b-stt").name == "b-stt"
    assert [i.name for i in r.info()] == ["a-tts", "b-stt"]
    assert [i.name for i in r.info("stt")] == ["b-stt"]
    with pytest.raises(ValueError):
        r.info("video")


def test_bad_registrations_are_refused():
    r = Registry()
    r.register(Fake("one"))
    with pytest.raises(ValueError):
        r.register(Fake("one"))
    with pytest.raises(ValueError):
        r.register(Fake("Bad Name"))
    broken = Fake("two"); broken.run = None
    with pytest.raises(TypeError):
        r.register(broken)
    no_caps = Fake("three"); no_caps.capabilities = "fast"
    with pytest.raises(TypeError):
        r.register(no_caps)


def test_unknown_engine_names_the_registered_ones():
    r = Registry(); r.register(Fake("known"))
    with pytest.raises(EngineNotInstalledError) as exc:
        r.get("missing")
    assert "known" in str(exc.value) and "Next:" in str(exc.value)


def test_auto_candidates_filter_by_kind_and_language_and_never_return_third_party():
    r = Registry()
    r.register(Fake("b", languages=("en", "hi"))); r.register(Fake("a", languages=("en",)))
    r.register(Fake("speaker", kind="tts", languages=("en",))); r.register(Fake("plugin", languages=("en",)), third_party=True)
    assert [e.name for e in r.auto_candidates("stt", "en")] == ["a", "b"]
    assert [e.name for e in r.auto_candidates("stt", "hi")] == ["b"]
    assert r.auto_candidates("stt", "ta") == []
    assert r.get("plugin").name == "plugin"
    assert [i.third_party for i in r.info("stt") if i.name == "plugin"] == [True]


def test_default_registry_functions(monkeypatch):
    monkeypatch.setattr(registry_mod, "_DEFAULT", Registry())
    assert ragleap_voice.engines() == []
    registry_mod.register_engine(Fake("solo"))
    assert [i.name for i in ragleap_voice.engines("stt")] == ["solo"]


# ---- package rules -----------------------------------------------------------------------------
def test_package_is_marked_typed_and_exports_resolve():
    assert (SRC / "py.typed").is_file()
    for name in ragleap_voice.__all__:
        assert hasattr(ragleap_voice, name), name


def test_new_modules_import_only_the_standard_library():
    for module in ("errors", "results", "engine", "registry"):
        for node in ast.walk(ast.parse((SRC / (module + ".py")).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for n in names:
                assert n in sys.stdlib_module_names, (module, n)
