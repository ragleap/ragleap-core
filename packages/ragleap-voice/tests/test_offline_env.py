import os
import threading

from ragleap_voice.whisper_engine import _offline_env


def test_overlapping_opens_keep_the_setting_and_restore_cleanly(monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    a_in, b_in, a_out = threading.Event(), threading.Event(), threading.Event()
    seen = {}

    def first():
        with _offline_env():
            a_in.set()
            b_in.wait(5)
        a_out.set()

    def second():
        a_in.wait(5)
        with _offline_env():
            b_in.set()
            a_out.wait(5)
            seen["inside"] = os.environ.get("HF_HUB_OFFLINE")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert seen["inside"] == "1"
    assert "HF_HUB_OFFLINE" not in os.environ
    assert "TRANSFORMERS_OFFLINE" not in os.environ


def test_caller_values_are_restored_and_exceptions_do_not_leak(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    try:
        with _offline_env():
            assert os.environ["HF_HUB_OFFLINE"] == "1"
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert os.environ["HF_HUB_OFFLINE"] == "0"
    assert "TRANSFORMERS_OFFLINE" not in os.environ
