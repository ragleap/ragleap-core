import hashlib
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
from ragleap_voice import EngineError, RagleapVoiceError, UnsupportedLanguageError  # noqa: E402
from ragleap_voice.cache import model_dir  # noqa: E402
from ragleap_voice.errors import ModelNotVerifiedError  # noqa: E402
from ragleap_voice.locks import lock_digest  # noqa: E402
from ragleap_voice.synthesizer import Synthesizer  # noqa: E402
from ragleap_voice.zonos3_engine import MODEL  # noqa: E402
from test_transcribe import REV  # noqa: E402
from test_zonos3_engine import FAKE_SERVER  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake server is started through a shell wrapper")

FILES = {"gguf/backbone-ptq.gguf": b"x", "gguf/zac-keep2.gguf": b"x", "gguf/spk-encoder.gguf": b"x",
         "gguf/voices/Hindi/clip.wav": b"w"}
TOML = """[[model]]
name = "%(name)s"
kind = "tts"
source_url = "https://huggingface.co/example/fake"
revision = "%(rev)s"
sha256 = "%(digest)s"
code_license = "MIT"
weights_license = "Apache-2.0"
languages = ["hi", "te"]
capabilities = []
min_hardware = "unknown"
status = "unverified"
default = false
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "fake_server.py").write_text(FAKE_SERVER)
    wrapper = bindir / "zonos3-server"
    wrapper.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, bindir / "fake_server.py"))
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    lock = {"schema": 1, "model": MODEL, "revision": REV, "source_url": "https://huggingface.co/example/fake",
            "files": [{"path": p, "sha256": hashlib.sha256(c).hexdigest(), "size": len(c)} for p, c in sorted(FILES.items())]}
    (tmp_path / "locks").mkdir()
    (tmp_path / "locks" / (MODEL + ".json")).write_text(json.dumps(lock))
    mpath = tmp_path / "models.toml"
    mpath.write_text(TOML % {"name": MODEL, "rev": REV, "digest": lock_digest(lock)})
    cache = tmp_path / "cache"
    folder = model_dir(MODEL, REV, cache)
    for p, c in FILES.items():
        (folder / p).parent.mkdir(parents=True, exist_ok=True)
        (folder / p).write_bytes(c)
    return types.SimpleNamespace(tmp=tmp_path, bin=bindir, manifest=mpath, cache=cache)


@pytest.fixture
def make(env):
    made = []

    def go():
        s = Synthesizer(manifest=env.manifest, base_dir=str(env.tmp))
        made.append(s)
        return s

    yield go
    for s in made:
        try:
            s.close()
        except Exception:
            pass


def say(s, env, text="namaste", language="hi", **kw):
    return s.synthesize(text, language=language, allow_unverified=True, cache_dir=env.cache, **kw)


def pid(env):
    return int((env.bin / "pid.txt").read_text())


def alive(p):
    try:
        os.kill(p, 0)
        return True
    except ProcessLookupError:
        return False


def gone(p):
    for _ in range(50):
        if not alive(p):
            return True
        time.sleep(0.1)
    return False


def test_the_server_is_started_once_across_calls(env, make):
    s = make()
    r1 = say(s, env)
    p1 = pid(env)
    r2 = say(s, env, text="phir milenge")
    assert pid(env) == p1 and alive(p1)
    assert r1.wav[:4] == b"RIFF" and r1.engine == "zonos3" and r1.synthetic is True
    assert r1.model_status == "unverified" and r2.wav[:4] == b"RIFF"
    s.close()
    assert gone(p1)


def test_both_languages_share_one_server(env, make):
    s = make()
    say(s, env, language="hi")
    p1 = pid(env)
    say(s, env, text="namaskaram", language="te", options={"seed": 5})
    body = json.loads((env.bin / "last.json").read_text())
    assert pid(env) == p1
    assert body["language"] == "te" and body["speaker_embedding_id"] == "default_c3bb44b62cf13a5c" and body["seed"] == 5


def test_a_refusal_keeps_the_server(env, make):
    s = make()
    say(s, env)
    p1 = pid(env)
    with pytest.raises(UnsupportedLanguageError):
        say(s, env, language="en")
    say(s, env)
    assert pid(env) == p1 and alive(p1)


def test_an_unverified_model_is_refused_without_the_flag_and_nothing_starts(env, make):
    s = make()
    with pytest.raises(ModelNotVerifiedError):
        s.synthesize("namaste", language="hi", cache_dir=env.cache)
    assert not (env.bin / "pid.txt").exists()


def test_a_worker_error_stops_the_server_and_the_next_call_starts_it_again(env, make):
    s = make()
    say(s, env)
    p1 = pid(env)
    with pytest.raises(EngineError):
        say(s, env, options={"color": 1})
    assert gone(p1)
    r = say(s, env)
    assert r.wav[:4] == b"RIFF" and pid(env) != p1


def test_closed_object_rejects_calls_and_close_is_idempotent(env, make):
    s = make()
    say(s, env)
    s.close()
    s.close()
    with pytest.raises(RagleapVoiceError):
        say(s, env)


def test_context_manager_stops_the_server(env):
    with Synthesizer(manifest=env.manifest, base_dir=str(env.tmp)) as s:
        say(s, env)
        p1 = pid(env)
        assert alive(p1)
    assert gone(p1)
    with pytest.raises(RagleapVoiceError):
        say(s, env)


def test_a_closed_object_cannot_be_entered_again(env):
    s = Synthesizer(manifest=env.manifest, base_dir=str(env.tmp))
    s.close()
    with pytest.raises(RagleapVoiceError):
        s.__enter__()
