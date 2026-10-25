# ragleap-voice 0.1.0: API and engine interface (draft RFC)

Status: draft for discussion on issue #762. Nothing below exists yet; every item is **planned**.
No code is written before this document is agreed.

## 1. Principles

1. The core package stays standard library only. Engines are optional extras.
2. Inference never touches the network. Only `ragleap-voice models pull` downloads, and only from Hugging Face with hash checks.
3. An engine is given directories, never model names. It cannot fetch anything by itself.
4. No silent fallback. If the chosen engine cannot run, the call fails with a typed error. Nothing is retried on another engine, and a paid cloud service is never chosen automatically.
5. Every result says which engine and model produced it, and the verification status of that model.
6. No claim of quality without a benchmark under `benchmarks/voice/`. Where there is no measurement, the result says so.

## 2. Functions

    from ragleap_voice import transcribe, synthesize, engines

    def transcribe(audio, *, language=None, engine="auto", allow_unverified=False,
                   cache_dir=None, timestamps=False, options=None) -> Transcript: ...

    def synthesize(text, *, language, engine="auto", voice=None, emotion=None,
                   speed=None, allow_unverified=False, cache_dir=None,
                   options=None) -> Speech: ...

    def engines(kind=None) -> list[EngineInfo]: ...   # what is installed, with capabilities

- `audio` is a path, or `bytes` of a WAV file. The core reads WAV with the standard `wave` module. Other formats need ffmpeg, which an engine extra may require; a missing decoder raises `AudioDecodeError`.
- `language` is a code such as `hi`, `ta`, `en`. For `transcribe`, `None` means auto-detect, and only engines that can detect are eligible.
- `options` is a plain dict passed to the engine (for example Parler's pitch, Chatterbox's expressiveness). Unknown keys raise `UnsupportedFeatureError`.
- `cache_dir` defaults to the existing cache root (`RAGLEAP_VOICE_HOME` or `XDG_CACHE_HOME`).

Results are frozen dataclasses with type hints; the package ships `py.typed`.

    Transcript: text, language, segments (start, end, text; empty unless timestamps=True),
                engine, model, model_status ("verified" | "unverified"), notes (tuple of str)
    Speech:     wav (bytes), sample_rate, duration_s, engine, model, model_status,
                voice, synthetic (always True), watermarked (bool), notes
                .save(path)

`notes` carries honest caveats, for example: "no measurement exists for this language" or "emotion prompts are officially supported only for: ...".

## 3. Streaming (planned for Stage 3, not in 0.1.0)

The names `transcribe_stream(chunks, ...)` (yields partial and final transcripts) and `synthesize_stream(text, ...)` (yields audio chunks) are reserved but not exported in 0.1.0. Engines declare `streaming=False` until the Stage 3 benchmark (time to first partial result, time to first audio) exists. Turn-taking and VAD are out of scope here.

## 4. Engine interface and capabilities

    class Capabilities:     # frozen dataclass
        kind: "stt" | "tts"
        languages: frozenset[str]
        clone: bool
        emotion: frozenset[str]            # named controls the engine really has
        emotion_languages: frozenset[str]  # where the model's authors support them
        streaming: bool
        hardware: frozenset[str]           # {"cpu"}, {"cuda"}, ...
        watermarks_output: bool
        isolated: bool                     # must run in a worker process (section 7)

    class Engine(Protocol):
        name: str
        manifest_entries: tuple[str, ...]  # models.toml names it needs, dependencies included
        capabilities: Capabilities
        def load(self, model_dirs: Mapping[str, Path]) -> None: ...
        def run(self, request: Request) -> Result: ...
        def close(self) -> None: ...

Licences are not declared by the engine. They come from `models.toml`, which stays the single authority. An engine cannot claim a model is safe; the manifest and the lock files say so.

## 5. Registration

- Built-in adapters (Whisper, IndicConformer, Parler, Kokoro, Chatterbox) are registered by name and need their extra installed (`pip install "ragleap-voice[parler]"`, and so on). A missing extra raises `EngineNotInstalledError` with the exact command.
- Third-party engines register through the entry point group `ragleap_voice.engines`.
- An entry point runs arbitrary code from whatever package is installed. So a third-party engine is **never** picked by `engine="auto"`. It runs only when the caller names it, and its models must still pass the manifest and lock checks.

## 6. How `engine="auto"` chooses

1. Keep engines that are installed, support the language and kind, and have their model pulled.
2. Keep only engines whose models are `verified`. With `allow_unverified=True`, unverified models are also eligible.
3. Rank by a routing table in the repository (data, not code). Each row names the benchmark file that justifies it, for example Hindi, Tamil, Telugu, Malayalam and Kannada speech to text prefer IndicConformer, citing the five-language comparison.
4. For a language with no measurement, rank by manifest order and add a `notes` line saying nothing was measured.
5. If nothing is left, raise `NoEngineError`, listing each candidate and why it was dropped.
6. If the chosen engine fails while running, raise `EngineError`. There is no second attempt on another engine.

Cloud adapters (for example OpenAI, or Sarvam with the user's own key) may exist later as optional extras. They need `engine="name"`, a key from the environment, and they are excluded from `auto` in every case.

## 7. Verified cache folders and offline loading

- The cache layout stays `<root>/models/<name>/<revision>`.
- Before an engine loads, the core resolves each manifest entry the engine needs, including `depends_on` entries, to a directory and checks it with the existing verify code. Parler therefore gets two directories, its own and the locked flan-t5 tokenizer folder, and never loads `google/flan-t5-large` by name.
- The engine process starts with no token variable, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and an empty Hugging Face home, as the Colab demo does.
- Model status:
  - `verified`: allowed by default.
  - `unverified`: refused unless `allow_unverified=True`. The result then carries `model_status="unverified"`.
  - Not pulled: `ModelNotFoundError`, with the `models pull` command.
- Library code is a gap. The `parler-tts` package comes from GitHub at a commit; it is not covered by the lock files. The Parler extra pins that commit, and the gap is stated in the README until it is closed.

## 8. Isolation

The demo runs each TTS engine in its own Python 3.11 environment because they pin conflicting packages, and because some models ship code that runs on load.

What the demo does (`benchmarks/voice/colab_tts_roundtrip.py`, checked 2026-10-25): it is a batch, not a protocol. It writes a jobs file, builds one environment per engine with uv, runs one script per engine with a clean environment and a timeout, and reads WAV files and a `timing.json` back from a folder. It has no stdin/stdout protocol and no long-lived process. The gated Parler repository is downloaded outside the TTS environment, so that environment never sees the token.

Proposal **[plan, nothing is built]**: isolation is a requirement for any engine that has conflicting pins or loads model-supplied code (`trust_remote_code`). Those engines declare `isolated=True` and run as a long-lived worker process, so the model is not reloaded on every call.

- **Protocol.** The parent sends `load`, `run` and `close` as JSON lines over the worker's stdin; the worker answers with JSON lines on stdout. The worker redirects file descriptor 1 to stderr at start and keeps the real stdout only for the protocol, because libraries print to stdout.
- **Audio goes through files.** The worker writes the WAV into a private temporary folder and answers with its path. Audio never travels over the protocol.
- **Logs.** Stderr goes to a log file whose path appears in `EngineError`.
- **Clean environment.** No token variables, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` and an empty temporary Hugging Face home (section 7). The parent verifies the model folders and sends only their paths.
- **Limits.** A timeout per call. A worker that dies, times out or answers badly becomes `EngineError`. `close()` ends the process.
- **Setup is the user's.** The interpreter of the worker environment comes from an option or an environment variable. The core never installs environments for the user; the user installs the engine's extra into the environment the worker uses.
- **Library code.** Libraries installed from GitHub (for example `parler-tts` at a pinned commit, Apache-2.0 per its repository) are not covered by the lock files. The README states this gap.
- **Not a sandbox.** Isolation separates conflicting dependencies and keeps the token out. The worker still runs library code with the user's own permissions, and the documentation must not call it secure.

Engines without conflicting pins or model-supplied code may run in process.

## 9. Errors

    RagleapVoiceError
      ModelNotFoundError        model not pulled
      ModelNotVerifiedError     unverified model without allow_unverified
      LicenceError              manifest entry fails the allowlist
      EngineNotInstalledError   extra missing (message gives the command)
      EngineUnavailableError    for example CUDA needed but not present
      NoEngineError             auto found no eligible engine
      UnsupportedLanguageError
      UnsupportedFeatureError   for example emotion on an engine without it
      AudioDecodeError
      ConsentRequiredError      reserved for cloning (Stage 5, issue #763)
      EngineError               worker crash or failure, with the log path

Every error message says what to do next. No error is swallowed and turned into a default.

## 10. Cloning and emotion

- Cloning is **not** in 0.1.0. The `voice` parameter is reserved. When added, a cloned voice needs a consent record (issue #763); without it the call raises `ConsentRequiredError`. Zero-shot cloning is described as imitation of a sample, not training.
- Emotion is passed as a named control the engine declares. An engine without it raises `UnsupportedFeatureError`. Outside the languages where the authors support emotion, the call works and `notes` says the effect may be weak.
- Every `Speech` is marked `synthetic=True`, and `watermarked` reflects the engine.

## 11. Examples (planned)

    from ragleap_voice import transcribe, synthesize

    t = transcribe("clip.wav", language="ta", engine="auto")
    print(t.text, t.engine, t.model_status)

    s = synthesize("வணக்கம்", language="ta", engine="parler",
                   emotion="happy", allow_unverified=True)
    s.save("hello.wav")

    ragleap-voice transcribe clip.wav --language hi
    ragleap-voice speak "नमस्ते" --language hi --engine parler --out hello.wav

## 12. Open questions

1. **0.1.0 with no verified model.** Every model is unverified today. Either 0.1.0 ships requiring `allow_unverified=True` and says so plainly, or Parler is verified first (listening review, a lawyer's view on the CC BY training data, a second reader). This draft assumes the first.
2. **Cost of verification on every load.** Hashing 3.5 GiB each time is slow. Option: verify fully at `models pull` and `models verify`, and on load check file sizes and a stored stamp. The stamp is only as trustworthy as the disk.
3. **Kokoro and Chatterbox download at run time** and cannot satisfy "engines get directories only". They stay out of 0.1.0 until locked, or run only with `allow_unverified=True` and an explicit note.
4. **Whisper's `model.bin`** needs the justified exception to the pickle-format rule (a separate design) before it can be locked.
5. **Isolation by default.** Is "required for conflicting or model-code engines, optional otherwise" the right line?
6. **Extras names and entry point group.** `ragleap_voice.engines` and extras named after engines: acceptable?

## 13. Requirements from measurements and from ragleap-phone (added 2026-10-21)

Everything here is **planned**. Sources are the sections of `benchmarks/voice/README.md` named in each item, and `packages/ragleap-phone/docs/design/phase0-interface.md`.

1. **A voice-activity gate in front of every recogniser.** In the noise pilot, Whisper large-v3 wrote "Thank you." for silent audio. A recogniser given silence can invent speech, so the streaming API (section 3) must not pass non-speech frames to an engine, and must report the gate's decision. The gate must be configurable, must say in `notes` when it dropped audio, and must never be a silent fallback. Which gate to use is open: the Silero licence was not verified.
2. **Input level is a first-class concern.** More than half of the English FLEURS clips in the noise pilot were recorded very quietly. One denoiser returned silence for the quietest, and the denoiser comparison only became meaningful after scaling every clip to the same level. The core should resample and level-check input in one documented place, and report the level in `notes` when it is far below a speech range. Whether the core normalises level itself or leaves it to the caller is open.
3. **Denoising is an optional stage, off by default.** In the noise pilot, neither RNNoise nor DeepFilterNet3 lowered the word error rate of Whisper large-v3 in any of 30 comparisons, and 22 were worse (white noise and babble at 10 and 5 dB, a simulated telephone channel, three languages, default settings). The API may offer a denoising stage as an optional extra, but must not enable it by default, and its documentation must link to that evidence. Engines other than Whisper large-v3 were measured only without a denoiser (item 3a).
3a. **Engines that are robust on their own are the first route** to "strong speech recognition under noise". Measured without a denoiser (benchmarks/voice/README.md, noise engines section): IndicConformer (Hindi, Tamil) and SunflowerASR (Swahili, Amharic) both lose accuracy under babble at 5 dB and over the simulated telephone path; none was immune.
4. **Telephone audio.** The simulated telephone path (8 kHz mu-law, then back to 16 kHz) cost the three languages between 1.0 and 2.7 percentage points of word error with babble at 10 dB, with overlapping intervals (Whisper large-v3, no denoiser, noise pilot). Engines declare the sample rates they accept, the core resamples with a stated method, and the resampler is part of the benchmark setup, not hidden inside an engine. ragleap-phone's audio contract (8 kHz frames, 20 ms) depends on this.
5. **Engines load once and stay resident.** Loading a 5.75 GiB model per call took 25 to 40 seconds in the noise-engine run, which is unusable for conversation. `load()` runs once per process, `run()` many times, and `close()` frees memory. Isolated workers (section 8) keep their model between requests.
6. **Empty and filler outputs are flagged.** An output that is empty, or equals a known filler phrase on low-energy audio, gets a `notes` entry instead of being returned as if it were speech.

### Open question added

7. **An OpenAI-compatible HTTP endpoint** (`/v1/audio/transcriptions` and `/v1/audio/speech`) as an optional extra, so existing applications, including the older voice channel in this repository, can switch from a paid service by changing one base URL. Does it belong in 0.1.0, or in a later release? It must default to listening on localhost only.
