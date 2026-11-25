import json
import os
import pathlib
import stat
import sys
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ragleap_voice import EngineError, Request, UnsupportedFeatureError  # noqa: E402
from ragleap_voice.zonos3_engine import LANGUAGES, MODEL, make_zonos3_engine  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake server is started through a shell wrapper")

FAKE_SERVER = '''
import http.server, io, json, os, sys, wave
here = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[1:]
root = os.path.dirname(argv[0])
if os.path.exists(os.path.join(root, "die")):
    sys.stderr.write("fake server: dying" + chr(10))
    sys.exit(3)
json.dump(argv, open(os.path.join(here, "args.json"), "w"))
open(os.path.join(here, "pid.txt"), "w").write(str(os.getpid()))
IDS = ["default_f76cf78a50cf8496", "default_c3bb44b62cf13a5c"]


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v2/health":
            self._send(200, b'{"status":"ok"}')
        elif self.path == "/v2/speakers":
            ids = [] if os.path.exists(os.path.join(root, "novoices")) else IDS
            self._send(200, json.dumps({"speakers": [{"id": i} for i in ids]}).encode())
        else:
            self._send(404, b"{}")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        json.dump(body, open(os.path.join(here, "last.json"), "w"))
        if "boom" in body.get("text", ""):
            self._send(400, json.dumps({"detail": "Failed to read default voice."}).encode())
            return
        buf = io.BytesIO()
        w = wave.open(buf, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(bytes(8820))
        w.close()
        self._send(200, buf.getvalue(), "audio/wav")


http.server.ThreadingHTTPServer(("127.0.0.1", int(argv[argv.index("--port") + 1])), H).serve_forever()
'''


@pytest.fixture
def box(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "fake_server.py").write_text(FAKE_SERVER)
    wrapper = bindir / "zonos3-server"
    wrapper.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, bindir / "fake_server.py"))
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    root = tmp_path / "model"
    (root / "gguf" / "voices").mkdir(parents=True)
    for name in ("backbone-ptq.gguf", "zac-keep2.gguf", "spk-encoder.gguf"):
        (root / "gguf" / name).write_bytes(b"x")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return types.SimpleNamespace(tmp=tmp_path, bin=bindir, root=root)


@pytest.fixture
def start(box):
    made = []

    def go(**kw):
        eng = make_zonos3_engine(base_dir=str(box.tmp), **kw)
        made.append(eng)
        eng.load({MODEL: box.root})
        return eng

    yield go
    for eng in made:
        try:
            eng.close()
        except Exception:
            pass


def tts(text="namaste", language="hi", **opt):
    return Request(kind="tts", language=language, text=text, options=opt)


def last(box):
    return json.loads((box.bin / "last.json").read_text())


def test_capabilities():
    eng = make_zonos3_engine()
    caps = eng.capabilities
    assert eng.name == "zonos3" and eng.manifest_entries == (MODEL,)
    assert caps.kind == "tts" and caps.languages == LANGUAGES == {"hi", "te"}
    assert caps.isolated is True and caps.clone is False and caps.emotion == frozenset()
    assert caps.hardware == frozenset({"cpu"})


def test_refuses_speed_emotion_voice_and_other_languages():
    eng = make_zonos3_engine()
    for kw in ({"speed": 1.2}, {"emotion": "happy"}, {"voice": "x"}):
        with pytest.raises(UnsupportedFeatureError):
            eng.run(Request(kind="tts", language="hi", text="namaste", **kw))
    for lang in ("en", None):
        with pytest.raises(UnsupportedFeatureError):
            eng.run(Request(kind="tts", language=lang, text="namaste"))


def test_roundtrip_and_server_command(box, start):
    eng = start()
    res = eng.run(tts("namaste", "hi"))
    assert res.wav[:4] == b"RIFF" and res.sample_rate == 44100
    assert last(box) == {"text": "namaste", "language": "hi", "speaker_embedding_id": "default_f76cf78a50cf8496",
                         "stream": False, "format": "wav", "seed": 1}
    res = eng.run(tts("namaskaram", "te", seed=7))
    body = last(box)
    assert body["speaker_embedding_id"] == "default_c3bb44b62cf13a5c" and body["seed"] == 7 and body["language"] == "te"
    assert any("Sravya" in n for n in res.notes)
    a = json.loads((box.bin / "args.json").read_text())
    g = str(box.root / "gguf")
    assert a[0] == g + "/backbone-ptq.gguf" and "--cpu" in a and "--gpu" not in a
    assert a[a.index("--host") + 1] == "127.0.0.1"
    assert a[a.index("--zac") + 1] == g + "/zac-keep2.gguf"
    assert a[a.index("--spk-encoder") + 1] == g + "/spk-encoder.gguf"
    assert a[a.index("--tts-default-voices-dir") + 1] == g + "/voices"


@pytest.mark.parametrize("bad", [{"seed": "1"}, {"seed": True}, {"seed": -1}, {"seed": 2 ** 31}, {"color": 1}])
def test_bad_options_are_refused_and_the_worker_survives(start, bad):
    eng = start()
    p1 = pid_of(eng)
    with pytest.raises(UnsupportedFeatureError):
        eng.run(tts(**bad))
    assert eng.run(tts()).wav[:4] == b"RIFF" and pid_of(eng) == p1


def test_server_error_is_reported_without_the_text(start):
    eng = start()
    with pytest.raises(EngineError) as e:
        eng.run(tts("boom"))
    assert "400" in str(e.value) and "boom" not in str(e.value)


def test_missing_binary(box, monkeypatch):
    empty = box.tmp / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    eng = make_zonos3_engine(base_dir=str(box.tmp))
    with pytest.raises(EngineError) as e:
        eng.load({MODEL: box.root})
    assert "PATH" in str(e.value)


def test_incomplete_folder(box, start):
    (box.root / "gguf" / "zac-keep2.gguf").unlink()
    with pytest.raises(EngineError) as e:
        start()
    assert "zac-keep2.gguf" in str(e.value)


def test_server_that_exits_fails_the_load(box, start):
    (box.root / "gguf" / "die").write_text("")
    with pytest.raises(EngineError) as e:
        start()
    assert "exited" in str(e.value)


def test_server_without_the_stock_voices_fails_the_load(box, start):
    (box.root / "gguf" / "novoices").write_text("")
    with pytest.raises(EngineError) as e:
        start()
    assert "stock voice" in str(e.value)


def test_close_stops_the_server(box, start):
    eng = start()
    pid = int((box.bin / "pid.txt").read_text())
    os.kill(pid, 0)
    eng.close()
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail("the server is still running after close()")


def pid_of(eng):
    return eng._worker._proc.pid


def test_options_and_text_length_are_checked_before_the_worker_is_asked():
    eng = make_zonos3_engine()  # never loaded: a check that reached the worker would say so
    for opts in ({"color": 1}, {"seed": "1"}, {"seed": True}, {"seed": -1}, {"seed": 2 ** 31}):
        with pytest.raises(UnsupportedFeatureError):
            eng.run(Request(kind="tts", language="hi", text="namaste", options=opts))
    with pytest.raises(UnsupportedFeatureError):
        eng.run(Request(kind="tts", language="hi", text="x" * 2001))
    with pytest.raises(EngineError) as e:
        eng.run(Request(kind="tts", language="hi", text="namaste"))
    assert "not loaded" in str(e.value)


def test_the_engine_checks_agree_with_the_worker_script():
    import importlib.util
    from ragleap_voice import zonos3_engine as ze
    spec = importlib.util.spec_from_file_location("zonos3_worker_under_test", str(ze.SCRIPT))
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    assert w.MAX_CHARS == ze.MAX_CHARS and tuple(w._OPTIONS) == tuple(ze._OPTIONS)
