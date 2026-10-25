"""Isolated worker engines (docs/design/api.md section 8). Standard library only.

A worker engine runs in a separate Python process, normally from an environment the user set up (the core never installs
environments). The parent sends JSON lines (load, run, close); audio travels as files in a private folder. This separates
conflicting dependencies and keeps tokens out of the worker. It is NOT a security sandbox: the worker runs library code with
the user's own permissions.
"""
import json
import os
import pathlib
import queue
import shutil
import subprocess
import tempfile
import threading
import time

from .engine import Result
from .errors import EngineError, EngineNotInstalledError, UnsupportedFeatureError
from .results import Segment

INTERPRETER_ENV = "RAGLEAP_VOICE_WORKER_PYTHON"
MAX_LINE = 4 * 1024 * 1024
MAX_WAV = 512 * 1024 * 1024
_KEEP_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES")
_EOF, _LONG = object(), object()


def runtime_path():
    """Path of worker_runtime.py, for a worker script to load with importlib."""
    return str(pathlib.Path(__file__).with_name("worker_runtime.py"))


def _read_lines(stream, out):
    try:
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                out.put(_EOF)
                return
            if len(line) > MAX_LINE and not line.endswith(b"\n"):
                out.put(_LONG)
                return
            out.put(line)
    except (OSError, ValueError):
        out.put(_EOF)


class WorkerProcess:
    def __init__(self, command, *, base_dir=None):
        self._command = [str(c) for c in command]
        self._base = base_dir
        self._proc = None
        self._thread = None
        self._q = queue.Queue()
        self._next_id = 0
        self._failed = False
        self.work_dir = None
        self.io_dir = None
        self.log_path = None

    def start(self):
        if self._proc is not None:
            raise EngineError("the worker was already started.")
        work = pathlib.Path(tempfile.mkdtemp(prefix="ragleap-voice-worker-", dir=self._base))
        for sub in ("home", "hf", "cache", "tmp", "io"):
            (work / sub).mkdir()
        self.work_dir, self.io_dir, self.log_path = work, work / "io", work / "worker.log"
        env = {k: os.environ[k] for k in _KEEP_ENV if k in os.environ}
        env.update({"HOME": str(work / "home"), "HF_HOME": str(work / "hf"), "XDG_CACHE_HOME": str(work / "cache"),
                    "TMPDIR": str(work / "tmp"), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                    "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        log = open(str(self.log_path), "wb")
        try:
            self._proc = subprocess.Popen(self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                          env=env, cwd=str(work), close_fds=True)
        except OSError as exc:
            shutil.rmtree(str(work), ignore_errors=True)
            raise EngineNotInstalledError("cannot start the worker %r: %s." % (self._command[0], exc.strerror or exc),
                                          hint="set the interpreter of the engine's environment (see the engine's documentation).")
        finally:
            log.close()
        self._thread = threading.Thread(target=_read_lines, args=(self._proc.stdout, self._q), daemon=True)
        self._thread.start()

    def _fail(self, message, hint=None):
        self._failed = True
        return EngineError("%s (worker log: %s)" % (message, self.log_path),
                           hint=hint or "read the worker log for the library's own error message.", log_path=str(self.log_path))

    def _kill(self):
        p = self._proc
        if p is None:
            return
        if p.poll() is None:
            p.kill()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def request(self, op, timeout, **payload):
        if self._proc is None or self._proc.poll() is not None:
            raise self._fail("the worker is not running.")
        self._next_id += 1
        mid = self._next_id
        msg = dict(payload)
        msg.update({"id": mid, "op": op, "work_dir": str(self.io_dir)})
        try:
            self._proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except (OSError, ValueError):
            self._kill()
            raise self._fail("could not send %r to the worker." % op)
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self._q.get(timeout=max(deadline - time.monotonic(), 0.0))
            except queue.Empty:
                self._kill()
                raise self._fail("the worker did not answer %r within %g s and was stopped." % (op, timeout))
            if line is _EOF:
                try:
                    code = self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    code = None
                    self._kill()
                raise self._fail("the worker exited unexpectedly (exit code %s)." % code)
            if line is _LONG:
                self._kill()
                raise self._fail("the worker sent a line longer than %d bytes." % MAX_LINE)
            try:
                ans = json.loads(line)
                if not isinstance(ans, dict) or ans.get("id") != mid:
                    raise ValueError("unexpected answer")
            except ValueError:
                self._kill()
                raise self._fail("the worker sent an answer that is not valid for this protocol.")
            if not ans.get("ok"):
                raise self._fail("the worker reported an error: %s" % str(ans.get("error"))[:500])
            result = ans.get("result")
            return result if isinstance(result, dict) else {}

    def close(self):
        p, self._proc = self._proc, None
        if p is None:
            return
        if p.poll() is None:
            try:
                p.stdin.write(b'{"id": 0, "op": "close"}\n')
                p.stdin.flush()
            except OSError:
                pass
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            p.stdout.close()
        except OSError:
            pass
        if not self._failed and self.work_dir is not None:
            shutil.rmtree(str(self.work_dir), ignore_errors=True)


class WorkerEngine:
    """An Engine whose work happens in a worker process. The worker script serves load and run (see worker_runtime.py)."""

    def __init__(self, name, manifest_entries, capabilities, script, *, interpreter=None, interpreter_env=INTERPRETER_ENV,
                 load_timeout=900.0, run_timeout=300.0, base_dir=None):
        if not getattr(capabilities, "isolated", False):
            raise ValueError("a worker engine must declare capabilities.isolated=True")
        self.name = name
        self.manifest_entries = tuple(manifest_entries)
        self.capabilities = capabilities
        self._script = os.path.abspath(str(script))
        self._interpreter = interpreter
        self._interpreter_env = interpreter_env
        self._load_timeout, self._run_timeout, self._base = float(load_timeout), float(run_timeout), base_dir
        self._worker = None
        self._n = 0

    def load(self, model_dirs):
        interp = self._interpreter or os.environ.get(self._interpreter_env)
        if not interp:
            raise EngineNotInstalledError("no Python interpreter is set for the %r worker." % self.name,
                                          hint="set %s to the python of the environment where the engine's extra is installed."
                                          % self._interpreter_env)
        worker = WorkerProcess([interp, self._script], base_dir=self._base)
        worker.start()
        try:
            worker.request("load", self._load_timeout, model_dirs={k: str(v) for k, v in model_dirs.items()})
        except BaseException:
            worker.close()
            raise
        self._worker = worker

    def run(self, request):
        w = self._worker
        if w is None:
            raise EngineError("the worker engine %r was not loaded." % self.name, hint="call load() before run().")
        fields = {"kind": request.kind, "language": request.language, "text": request.text, "voice": request.voice,
                  "emotion": request.emotion, "speed": request.speed, "timestamps": request.timestamps,
                  "options": dict(request.options)}
        try:
            json.dumps(fields)
        except (TypeError, ValueError):
            raise UnsupportedFeatureError("options of an isolated engine must contain only JSON values.")
        in_path = None
        if request.audio is not None:
            self._n += 1
            in_path = w.io_dir / ("in-%d.wav" % self._n)
            in_path.write_bytes(request.audio)
            fields["audio_path"] = str(in_path)
        try:
            res = w.request("run", self._run_timeout, request=fields)
        finally:
            if in_path is not None:
                try:
                    in_path.unlink()
                except OSError:
                    pass
        return self._result(res)

    def _result(self, res):
        w = self._worker

        def bad(why):
            return w._fail("the worker of %r answered with an invalid result: %s." % (self.name, why))

        text = res.get("text")
        if text is not None and not isinstance(text, str):
            raise bad("text is not a string")
        segs = []
        try:
            for s in res.get("segments") or []:
                segs.append(Segment(float(s[0]), float(s[1]), str(s[2])))
        except (TypeError, ValueError, IndexError, KeyError):
            raise bad("segments are malformed")
        notes = res.get("notes") or []
        if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
            raise bad("notes are malformed")
        rate, dur, lang = res.get("sample_rate"), res.get("duration_s"), res.get("language")
        if rate is not None and (isinstance(rate, bool) or not isinstance(rate, int)):
            raise bad("sample_rate is not an integer")
        if dur is not None and (isinstance(dur, bool) or not isinstance(dur, (int, float))):
            raise bad("duration_s is not a number")
        if lang is not None and not isinstance(lang, str):
            raise bad("language is not a string")
        wav = None
        wp = res.get("wav_path")
        if wp is not None:
            if not isinstance(wp, str):
                raise bad("wav_path is not a string")
            try:
                real = pathlib.Path(wp).resolve()
                real.relative_to(w.io_dir.resolve())
            except (OSError, ValueError, RuntimeError):
                raise bad("wav_path is outside the worker's private folder")
            try:
                if real.stat().st_size > MAX_WAV:
                    raise bad("the audio file is too large")
                wav = real.read_bytes()
                real.unlink()
            except OSError:
                raise bad("the audio file cannot be read")
        return Result(text=text, segments=tuple(segs), wav=wav, sample_rate=rate, duration_s=dur, language=lang,
                      notes=tuple(notes))

    def close(self):
        w, self._worker = self._worker, None
        if w is not None:
            w.close()
