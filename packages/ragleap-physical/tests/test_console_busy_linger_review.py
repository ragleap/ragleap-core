"""Break-it review of the busy refusal of the operator console (a refused connection gets its 503 and is then handed to at most
eight helper threads that run the bounded lingering close). Standard library only, loopback only.

Linux cannot show the Windows reset itself (WinError 10053). These tests check what the design promises and what it must not cost: the busy refusal
is lingered and counted, a flood of silent refused clients never delays a stop, helpers neither leak threads or sockets nor outlive the linger time, a
client that sends more than one mebibyte is cut off, the helper cap holds, and a helper that cannot start does not leak its slot. The numbers below are
written out here (8 helpers, 0.5 s, 1 MiB); none is read from the module, so a changed constant is caught instead of moving the test with it.
Each test was run against a stand-in of the design with deliberate breaks, and against the merged code."""
import contextlib
import os
import socket
import subprocess
import sys
import threading
import time
from unittest import mock

import pytest

from ragleap_physical import console

from helpers import make
from test_console import ALICE, Running as ConsoleRunning, call

MIB = 1 << 20
HELPERS = 8  # the cap on helper threads
STOP_BOUND_S = 8.0  # wide on purpose: shared runners are slow; a stop that waited behind lingering refusals would take far longer


def wait_for(predicate, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def open_fds():
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def request(port, body=b""):
    head = ("POST /v1/approve HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
            % (port, ALICE, len(body))).encode()
    return head + body


def connect(port):
    return socket.create_connection(("127.0.0.1", port), timeout=10)


def silent(port, n):
    """n connections that send the start of a request and then say nothing and read nothing."""
    out = []
    for _ in range(n):
        c = connect(port)
        c.sendall(b"POST /v1/apply HTTP/1.1\r\n")
        out.append(c)
        time.sleep(0.01)  # paced: the console's listen queue holds only a few connections, and a client in the same process slows the accept loop
    return out


def close_all(socks):
    for c in socks:
        try:
            c.close()
        except OSError:
            pass


def read_until_end(c, seconds=10.0):
    """Read until the server ends the connection. Returns (data, how) with how in eof, reset, timeout, error."""
    c.settimeout(seconds)
    data = b""
    try:
        while True:
            chunk = c.recv(65536)
            if not chunk:
                return data, "eof"
            data += chunk
    except socket.timeout:
        return data, "timeout"
    except ConnectionResetError:
        return data, "reset"
    except OSError:
        return data, "error"


def probe_status(base):
    try:
        return call(base, "/v1/pending")[0]
    except OSError:
        return None


@contextlib.contextmanager
def busy_console(cap=2):
    """A console whose connection slots are all held by unfinished requests, so that the next connection is refused as busy."""
    old = (console.MAX_CONNECTIONS, console.READ_TIMEOUT_S)
    console.MAX_CONNECTIONS, console.READ_TIMEOUT_S = cap, 30.0
    try:
        b, _, _ = make()
        with ConsoleRunning(b) as s:
            port = s.server.server_address[1]
            held = silent(port, cap)
            try:
                assert wait_for(lambda: probe_status(s.base) == 503, 5.0), "the slots did not fill"
                time.sleep(0.8)  # the probe may have been lingered: let that end before any baseline is taken
                yield s, port, held
            finally:
                close_all(held)
    finally:
        console.MAX_CONNECTIONS, console.READ_TIMEOUT_S = old


# ---- the busy refusal is lingered, bounded and given back
def test_a_busy_refusal_is_lingered_and_counted():
    with busy_console() as (s, port, held):
        calls0, bytes0 = s.server.lingered_calls, s.server.lingered_bytes
        c = connect(port)
        try:
            c.sendall(request(port, bytes(5000)))
            assert c.recv(64).startswith(b"HTTP/1.0 503")
            assert wait_for(lambda: s.server.lingered_calls >= calls0 + 1, 5.0), "the busy refusal did not linger"
            assert s.server.lingered_bytes - bytes0 >= 5000, "the lingering close did not read what the client sent"
        finally:
            c.close()


def test_a_refused_client_that_sends_more_than_one_mebibyte_is_cut_off():
    with busy_console() as (s, port, held):
        calls0, bytes0 = s.server.lingered_calls, s.server.lingered_bytes
        c = connect(port)
        sent = 0
        try:
            c.settimeout(10)
            chunk = bytes(65536)
            try:
                while sent < 32 * MIB:
                    c.sendall(chunk)
                    sent += len(chunk)
            except OSError:
                pass
            assert wait_for(lambda: s.server.lingered_calls >= calls0 + 1, 5.0), "the busy refusal did not linger"
            assert s.server.lingered_bytes - bytes0 <= MIB + 65536, "the lingering close read more than its byte limit"
            assert sent < 32 * MIB, "the client was never cut off"
        finally:
            c.close()


def test_the_helper_slots_come_back_after_a_flood():
    with busy_console() as (s, port, held):
        for round_ in range(2):
            calls0 = s.server.lingered_calls
            flood = silent(port, 12)  # more refusals than helpers
            try:
                assert wait_for(lambda: s.server.lingered_calls > calls0, 5.0), "round %d: no refusal lingered" % round_
            finally:
                close_all(flood)
            time.sleep(1.0)  # every helper is done after the linger time


def test_a_helper_thread_that_cannot_start_does_not_leak_a_helper_slot():
    with busy_console() as (s, port, held):
        refused0 = s.server.refused_busy
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")):
            flood = silent(port, 12)  # more than the helper cap: a leaked slot per failure would use them all
            try:
                assert wait_for(lambda: s.server.refused_busy >= refused0 + 12, 5.0)
            finally:
                close_all(flood)
        calls0 = s.server.lingered_calls
        again = silent(port, 3)
        try:
            assert wait_for(lambda: s.server.lingered_calls >= calls0 + 1, 5.0), "after the failures no refusal lingered: a helper slot leaked"
        finally:
            close_all(again)


def test_a_busy_refusal_ends_cleanly_for_a_client_that_sent_a_body():
    # Measured on Linux: main already ends cleanly for a 20,000-byte body (100 of 100 trials), so this cannot show the Windows reset. It guards the fix.
    with busy_console() as (s, port, held):
        c = connect(port)
        try:
            c.sendall(request(port, bytes(20000)))
            data, how = read_until_end(c)
            assert data.startswith(b"HTTP/1.0 503"), (data[:40], how)
            assert how == "eof", how
        finally:
            c.close()


# ---- what a busy refusal must not cost
def test_a_busy_connection_is_answered_with_503_and_counted_as_refused():
    with busy_console() as (s, port, held):
        refused0 = s.server.refused_busy
        assert probe_status(s.base) == 503
        assert s.server.refused_busy >= refused0 + 1


def test_a_flood_of_silent_refused_connections_does_not_delay_a_stop():
    with busy_console() as (s, port, held):
        refused0 = s.server.refused_busy
        started = time.monotonic()
        flood = []
        status = None
        try:
            flood = silent(port, 40)  # forty refusals: lingering them one by one in the accept loop would take about twenty seconds
            assert wait_for(lambda: s.server.refused_busy >= refused0 + 40, STOP_BOUND_S), "the refusals were not all processed: the accept loop was held up"
            held[0].close()  # one slot comes back, and no queued flood connection can take it
            while time.monotonic() - started < 3 * STOP_BOUND_S:
                try:
                    status = call(s.base, "/v1/stop", {})[0]
                except OSError:
                    status = None
                if status == 200:
                    break
                time.sleep(0.05)
        finally:
            close_all(flood)
        took = time.monotonic() - started
        assert status == 200, "the stop was never answered"
        assert took < STOP_BOUND_S, "the stop waited %.1f s behind refused connections" % took


def test_helpers_do_not_leak_threads_or_sockets_after_a_flood():
    with busy_console() as (s, port, held):
        threads0, fds0 = threading.active_count(), open_fds()
        flood = silent(port, 40)
        time.sleep(1.0)
        close_all(flood)
        assert wait_for(lambda: threading.active_count() <= threads0, 8.0), "threads left over: %d against %d" % (threading.active_count(), threads0)
        if fds0 is not None:
            assert wait_for(lambda: open_fds() <= fds0, 8.0), "descriptors left over: %d against %d" % (open_fds(), fds0)


def test_a_refused_client_that_never_reads_does_not_hold_a_helper_beyond_the_linger_time():
    with busy_console() as (s, port, held):
        threads0, refused0 = threading.active_count(), s.server.refused_busy
        flood = silent(port, HELPERS)  # they stay connected and read nothing
        try:
            assert wait_for(lambda: s.server.refused_busy >= refused0 + HELPERS, 5.0)
            time.sleep(0.25)
            assert wait_for(lambda: threading.active_count() <= threads0, 4.0), "helper threads outlived the linger time while the clients stayed connected"
        finally:
            close_all(flood)


def test_no_more_than_eight_helpers_run_at_once():
    release, lock, state = threading.Event(), threading.Lock(), {"now": 0, "max": 0}

    def stuck(connection, *args, **kwargs):
        """A lingering close that does not finish until the test lets it: it counts how many run at the same time."""
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        try:
            release.wait(8.0)
        finally:
            with lock:
                state["now"] -= 1
        return 0

    with busy_console() as (s, port, held):
        refused0, flood = s.server.refused_busy, []
        try:
            with mock.patch.object(console, "linger", stuck):
                flood = silent(port, 30)
                assert wait_for(lambda: s.server.refused_busy >= refused0 + 30, 5.0)
                time.sleep(0.3)
            if state["max"] == 0:
                pytest.skip("busy refusals do not call console.linger yet: there are no helpers on this tree")
            assert state["max"] <= HELPERS, "%d helpers lingered at the same time (the plan allows 8)" % state["max"]
        finally:
            release.set()
            close_all(flood)


def test_while_every_helper_is_stuck_further_refusals_are_closed_at_once_and_a_stop_is_still_answered():
    release = threading.Event()

    def stuck(connection, *args, **kwargs):
        release.wait(8.0)
        return 0

    with busy_console() as (s, port, held):
        refused0, flood = s.server.refused_busy, []
        started = time.monotonic()
        status = None
        try:
            with mock.patch.object(console, "linger", stuck):
                flood = silent(port, 30)  # far more than eight: the rest must not wait for a helper
                assert wait_for(lambda: s.server.refused_busy >= refused0 + 30, 5.0), "refusals waited for a free helper: the accept loop was held up"
            held[0].close()
            while time.monotonic() - started < 3 * STOP_BOUND_S:
                try:
                    status = call(s.base, "/v1/stop", {})[0]
                except OSError:
                    status = None
                if status == 200:
                    break
                time.sleep(0.05)
            assert status == 200, "the stop was never answered"
            assert time.monotonic() - started < STOP_BOUND_S
        finally:
            release.set()
            close_all(flood)


def test_when_every_helper_is_busy_a_refusal_is_still_answered_and_closed_at_once():
    with busy_console() as (s, port, held):
        refused0 = s.server.refused_busy
        flood = silent(port, 12)  # keeps the helpers busy for the linger time
        try:
            assert wait_for(lambda: s.server.refused_busy >= refused0 + 12, 5.0)
            c = connect(port)
            try:
                c.sendall(request(port))
                data, how = read_until_end(c, 3.0)
                assert data.startswith(b"HTTP/1.0 503"), (data[:40], how)
                assert how in ("eof", "reset"), "the refusal was not closed at once: " + how
            finally:
                c.close()
        finally:
            close_all(flood)


def test_shutting_the_console_down_does_not_wait_for_lingering_helpers():
    old = (console.MAX_CONNECTIONS, console.READ_TIMEOUT_S)
    console.MAX_CONNECTIONS, console.READ_TIMEOUT_S = 1, 30.0
    flood, held = [], []
    try:
        b, _, _ = make()
        server = console.make_console(b, {ALICE: "person:alice"})
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        port = server.server_address[1]
        held = silent(port, 1)
        try:
            flood = silent(port, 30)
            assert wait_for(lambda: server.refused_busy >= 20, 5.0)
            started = time.monotonic()
            server.shutdown()
            server.server_close()
            assert time.monotonic() - started < 5.0, "the shutdown waited for the refused connections"
            thread.join(5.0)
            assert not thread.is_alive()
        finally:
            close_all(flood)
            close_all(held)
    finally:
        console.MAX_CONNECTIONS, console.READ_TIMEOUT_S = old


BURST_CLIENT = (
    "import socket, sys, time\n"
    "port, n = int(sys.argv[1]), int(sys.argv[2])\n"
    "started, socks = time.monotonic(), []\n"
    "for _ in range(n):\n"
    "    c = socket.create_connection(('127.0.0.1', port), timeout=60)\n"
    "    c.sendall(b'POST /v1/apply HTTP/1.1\\r\\n')\n"
    "    socks.append(c)\n"
    "print(round(time.monotonic() - started, 2))\n"
)


def test_a_burst_of_busy_refusals_from_another_process_is_accepted_within_ten_seconds():
    # A tight burst of 200 connections from another process overflowed the standard library's listen queue of 5: each dropped connection waited about
    # a second for the kernel's retry (32.77 s in total on one machine). With the console's queue of 128 the same burst takes well under a second.
    with busy_console() as (s, port, held):
        done = subprocess.run([sys.executable, "-c", BURST_CLIENT, str(port), "200"], capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr[-300:]
        assert float(done.stdout.strip()) < 10.0, "the burst waited for the kernel's retry of dropped connections: %s s" % done.stdout.strip()
        assert wait_for(lambda: probe_status(s.base) == 503, 5.0), "the console stopped answering after the burst"
