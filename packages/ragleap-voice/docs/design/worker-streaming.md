# Worker-side streaming (plan, not scheduled)

Status: plan only. Nothing here is implemented, measured or scheduled. No latency claim is made.

## Why

`transcribe_stream` and `synthesize_stream` are adapters around batch calls (see `streaming.md`). The isolated worker answers one request with one result, so an engine that runs in the worker cannot emit partial output. This note records what a worker-side stream would need.

## What the worker does today

- Messages are single JSON lines. The parent sends `{"id", "op", ...}`; the worker answers `{"id", "ok", "result" or "error"}`. [verified: `worker_runtime.py`]
- The worker redirects file descriptor 1 to stderr at start and answers through a saved copy of the real stdout, so library prints cannot corrupt the protocol. [verified: `os.dup2(2, 1)` in `worker_runtime.py`]
- The parent sends the ops `load`, `run` and `close`; an unknown op gets an error answer. [verified: `worker.py`, `worker_runtime.py`]
- Audio goes in as a file path (`audio_path`) and comes back as a file path (`wav_path`) inside a private `io/` folder. [verified: `worker.py`]
- Defaults: `load_timeout` 900 s, `run_timeout` 300 s. A request that is not answered in time kills the worker. [verified: `worker.py`]
- Only the Parler engine uses the worker. [unverified]
- The worker is process isolation, not a security sandbox. [unverified]

## What would have to change

1. Several messages per request. Today one request gets one answer. A stream needs events that share the request `id`, plus a final message that closes the request.
2. New ops (names are a proposal): `stream_open`, `stream_feed`, `stream_close`, and a cancel path that does not kill the process.
3. A timeout rule per event, not per request, so a long stream is not killed while it is making progress.
4. A bound on buffered audio in the parent and in the worker, as `max_bytes` does for `transcribe_stream`.
5. Audio chunks as files in `io/`, or a framed binary channel. File-per-chunk is simple but adds disk work for every chunk; this has not been measured.

## Open questions

- Whether the engines in use can emit partial output at all. Whisper runs in-process and is batch only. Parler's streaming support is not established.
- Whether event ordering needs a sequence number inside the worker protocol, as the public `StreamEvent` has.
- How a cancelled stream releases GPU memory.

## Out of scope

Latency or quality claims, new engines, changes to the public API, and any claim that the worker is a security boundary.
