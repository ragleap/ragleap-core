import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ragleap_voice import Capabilities, EngineError, EngineNotInstalledError, Registry, Request, Segment, UnsupportedFeatureError  # noqa: E402
from ragleap_voice.api import _transcribe  # noqa: E402
from ragleap_voice.cache import model_dir  # noqa: E402
from ragleap_voice.synth import _synthesize  # noqa: E402
from ragleap_voice.worker import INTERPRETER_ENV, WorkerEngine, runtime_path  # noqa: E402
from test_transcribe import REV, make_env, wav_bytes  # noqa: E402,F401

FAKE = '''
import importlib.util, json, os, sys, time, wave
spec = importlib.util.spec_from_file_location("worker_runtime", __RT__)
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)
state = {}

def load(msg):
    state["dirs"] = msg["model_dirs"]
    print("noise on stdout during load")
    sys.stderr.write("worker log line" + chr(10))
    return {}

def run(msg):
    req = msg["request"]
    mode = req["options"].get("mode")
    print("noise on stdout during run")
    if mode == "crash":
        sys.stderr.write("about to crash" + chr(10))
        sys.stderr.flush()
        os._exit(3)
    if mode == "sleep":
        time.sleep(60)
    if mode == "error":
        raise ValueError("boom")
    if mode == "env":
        keys = ("HF_TOKEN", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HOME", "HOME")
        return {"text": json.dumps({k: os.environ.get(k) for k in keys})}
    if mode == "dirs":
        return {"text": json.dumps(state["dirs"])}
    if mode == "escape":
        return {"wav_path": req["options"]["path"]}
    if mode == "badsegments":
        return {"text": "x", "segments": [[2.0, 1.0, "bad"]]}
    if req["kind"] == "tts":
        out = os.path.join(msg["work_dir"], "out.wav")
        w = wave.open(out, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\\x00\\x00" * 1600)
        w.close()
        return {"wav_path": out, "sample_rate": 16000, "notes": ["worker note"]}
    size = os.path.getsize(req["audio_path"])
    return {"text": "heard %d bytes" % size, "segments": [[0.0, 1.0, "hi"]], "language": req["language"]}

rt.serve({"load": load, "run": run})
'''.replace("__RT__", repr(runtime_path()))

BAD = '''
import sys
sys.stdin.readline()
sys.stdout.write("not json" + chr(10))
sys.stdout.flush()
sys.stdin.readline()
'''


def make_engine(tmp_path, script=FAKE, kind="stt", **kw):
    p = tmp_path / "fake_worker.py"
    p.write_text(script)
    caps = Capabilities(kind=kind, languages=frozenset({"en"}), isolated=True)
    kw.setdefault("interpreter", sys.executable)
    kw.setdefault("base_dir", str(tmp_path))
    return WorkerEngine("fakeworker", ("fake-model",), caps, str(p), **kw)


def started(tmp_path, **kw):
    eng = make_engine(tmp_path, **kw)
    eng.load({"fake-model": tmp_path})
    return eng


def req(kind="stt", **opt):
    if kind == "stt":
        return Request(kind="stt", language="en", audio=wav_bytes(), options=opt)
    return Request(kind="tts", language="en", text="hello", options=opt)


def test_stt_roundtrip_and_stdout_noise_stays_out_of_the_protocol(tmp_path):
    eng = started(tmp_path)
    work = eng._worker.work_dir
    r = eng.run(req())
    assert r.text == "heard %d bytes" % len(wav_bytes()) and r.language == "en"
    assert r.segments == (Segment(0.0, 1.0, "hi"),)
    assert list(eng._worker.io_dir.iterdir()) == []
    eng.close()
    assert not work.exists()


def test_tts_roundtrip_reads_and_removes_the_wav(tmp_path):
    eng = started(tmp_path, kind="tts")
    r = eng.run(req("tts"))
    assert r.wav[:4] == b"RIFF" and r.sample_rate == 16000 and "worker note" in r.notes
    assert list(eng._worker.io_dir.iterdir()) == []
    eng.close()


def test_clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "t" + str(os.getpid()))
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    eng = started(tmp_path)
    seen = json.loads(eng.run(req(mode="env")).text)
    assert seen["HF_TOKEN"] is None
    assert seen["HF_HUB_OFFLINE"] == "1" and seen["TRANSFORMERS_OFFLINE"] == "1"
    assert pathlib.Path(seen["HF_HOME"]) == eng._worker.work_dir / "hf"
    assert pathlib.Path(seen["HOME"]) == eng._worker.work_dir / "home"
    eng.close()


def test_model_dirs_are_sent_as_strings(tmp_path):
    eng = started(tmp_path)
    assert json.loads(eng.run(req(mode="dirs")).text) == {"fake-model": str(tmp_path)}
    eng.close()


def test_worker_crash_keeps_the_log(tmp_path):
    eng = started(tmp_path)
    log = eng._worker.log_path
    with pytest.raises(EngineError) as info:
        eng.run(req(mode="crash"))
    assert "exit code 3" in str(info.value) and str(log) in str(info.value)
    eng.close()
    text = log.read_text()
    assert "about to crash" in text and "worker log line" in text


def test_timeout_kills_the_worker(tmp_path):
    eng = started(tmp_path, run_timeout=1.0)
    proc = eng._worker._proc
    with pytest.raises(EngineError) as info:
        eng.run(req(mode="sleep"))
    assert "did not answer" in str(info.value) and proc.poll() is not None
    eng.close()


def test_handler_error_is_reported_and_the_worker_survives(tmp_path):
    eng = started(tmp_path)
    with pytest.raises(EngineError) as info:
        eng.run(req(mode="error"))
    assert "ValueError: boom" in str(info.value)
    assert eng.run(req()).text.startswith("heard")
    eng.close()


def test_garbage_answer_fails_the_load(tmp_path):
    with pytest.raises(EngineError):
        started(tmp_path, script=BAD)


def test_bad_results_are_refused(tmp_path):
    eng = started(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    with pytest.raises(EngineError) as info:
        eng.run(req(mode="escape", path=str(outside)))
    assert "outside" in str(info.value) and outside.exists()
    with pytest.raises(EngineError):
        eng.run(req(mode="badsegments"))
    eng.close()


def test_interpreter_errors(tmp_path, monkeypatch):
    with pytest.raises(EngineNotInstalledError):
        make_engine(tmp_path, interpreter=str(tmp_path / "no-such-python")).load({})
    monkeypatch.delenv(INTERPRETER_ENV, raising=False)
    with pytest.raises(EngineNotInstalledError) as info:
        make_engine(tmp_path, interpreter=None).load({})
    assert INTERPRETER_ENV in str(info.value)
    monkeypatch.setenv(INTERPRETER_ENV, sys.executable)
    eng = make_engine(tmp_path, interpreter=None)
    eng.load({"fake-model": tmp_path})
    eng.close()


def test_argument_and_state_checks(tmp_path):
    with pytest.raises(ValueError):
        WorkerEngine("x", (), Capabilities(kind="stt"), "script.py")
    with pytest.raises(EngineError):
        make_engine(tmp_path).run(req())
    eng = started(tmp_path)
    with pytest.raises(UnsupportedFeatureError):
        eng.run(req(bad=object()))
    eng.close()
    eng.close()


def test_through_transcribe_and_synthesize(tmp_path):
    env = make_env(tmp_path)
    reg = Registry()
    reg.register(make_engine(tmp_path))
    t = _transcribe(wav_bytes(), reg, env[0], cache_dir=env[1], language="en", options={"mode": "dirs"})
    assert json.loads(t.text) == {"fake-model": str(model_dir("fake-model", REV, env[1]))}
    t = _transcribe(wav_bytes(), reg, env[0], cache_dir=env[1], language="en")
    assert t.text.startswith("heard") and t.engine == "fakeworker" and t.model_status == "verified"
    reg2 = Registry()
    reg2.register(make_engine(tmp_path, kind="tts"))
    s = _synthesize("hello", reg2, env[0], language="en", cache_dir=env[1])
    assert s.sample_rate == 16000 and abs(s.duration_s - 0.1) < 1e-9 and s.wav[:4] == b"RIFF" and s.synthetic is True
