import threading
import time

from ragleap_voice import Capabilities, Registry, api
from ragleap_voice.api import _locked_run


class Eng:
    name = "dup"
    manifest_entries = ()
    capabilities = Capabilities(kind="stt", languages=frozenset({"en"}))

    def load(self, dirs):
        pass

    def run(self, request):
        return "ok"

    def close(self):
        pass


class SlowDict(dict):
    def __contains__(self, key):
        found = dict.__contains__(self, key)
        time.sleep(0.1)  # widens the check-then-set window
        return found


def test_two_threads_registering_one_name_exactly_one_wins():
    reg = Registry()
    reg._items = SlowDict()
    out = []

    def go():
        try:
            reg.register(Eng())
            out.append("ok")
        except ValueError:
            out.append("dup")

    threads = [threading.Thread(target=go) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(out) == ["dup", "ok"]


def test_fallback_locks_are_released_after_use():
    class Slotted:
        __slots__ = ("p",)

        def load(self, dirs):
            self.p = 1

        def run(self, request):
            return "ok"

        def close(self):
            pass

    assert _locked_run(Slotted(), {}, None) == ("ok", None)
    assert api._FALLBACK_LOCKS == {}
