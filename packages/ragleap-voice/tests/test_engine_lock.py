import threading
import time

from ragleap_voice import api
from ragleap_voice.api import _locked_run


class Shared:
    """Fake engine whose state lives on the instance, like WhisperEngine."""

    def __init__(self):
        self.path = None
        self.calls = 0
        self.first_in = threading.Event()
        self.release = threading.Event()

    def load(self, dirs):
        self.path = "p"

    def run(self, request):
        self.calls += 1
        if self.calls == 1:
            self.first_in.set()
            self.release.wait(5)
        if self.path is None:
            raise RuntimeError("engine was closed under a running call")
        return "ok"

    def close(self):
        self.path = None


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def scenario():
    eng = Shared()
    out = {}

    def call(key):
        try:
            out[key] = _locked_run(eng, {}, None)[0]
        except Exception as exc:
            out[key] = type(exc).__name__

    first = threading.Thread(target=call, args=("first",))
    second = threading.Thread(target=call, args=("second",))
    first.start()
    assert eng.first_in.wait(5)
    second.start()
    time.sleep(0.2)  # lets the second thread finish if nothing holds it back
    eng.release.set()
    first.join()
    second.join()
    return out


def test_two_threads_on_one_engine_do_not_corrupt_each_other():
    assert scenario() == {"first": "ok", "second": "ok"}


def test_the_race_is_real_without_the_lock(monkeypatch):
    monkeypatch.setattr(api, "_engine_lock", lambda eng: _NoLock())
    assert scenario()["first"] == "RuntimeError"


def test_different_engines_do_not_block_each_other():
    slow, fast = Shared(), Shared()
    fast.calls = 1  # skip the blocking branch
    th = threading.Thread(target=lambda: _locked_run(slow, {}, None))
    th.start()
    assert slow.first_in.wait(5)
    assert _locked_run(fast, {}, None)[0] == "ok"  # returns while slow is still inside run()
    slow.release.set()
    th.join()


def test_engine_without_weakref_support_still_works():
    class Slotted:
        __slots__ = ("p",)

        def load(self, dirs):
            self.p = 1

        def run(self, request):
            return "ok"

        def close(self):
            pass

    assert _locked_run(Slotted(), {}, None) == ("ok", None)
