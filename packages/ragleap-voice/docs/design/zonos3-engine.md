# A ZONOS3-Mini engine for ragleap-voice: design note (proposal, written 2026-11-20, no code)

Status: a proposal. Nothing here is built, no manifest entry exists, and the model is not verified and not a default. Written from what was read of engine.py, registry.py, parler_engine.py, models.toml and api.md, from the measurements in benchmarks/voice/README.md, and from docs/zonos3-mini-licence-notes.md. Tags: [read] seen in the code or docs, [measured] in our own runs, [proposal], [not read].

## 1. What exists today

- Engine contract [read]: engine.py defines Capabilities (kind, languages, hardware, isolated; api.md section 4 also lists emotion), EngineInfo and the Engine protocol. register_engine(engine, third_party=False) registers an engine; a third-party engine is never chosen by engine="auto".
- The Parler engine [read]: make_parler_engine() builds Capabilities(kind="tts", ..., hardware cpu and cuda, isolated=True) and a ParlerEngine on WorkerEngine (worker.py), which runs a script in an interpreter environment the user builds.
- Manifest entries [read] carry name, kind, source_url, revision, sha256 (of the lock), code_license, weights_license, languages, capabilities, min_hardware, status, default and depends_on. Licences come only from models.toml, which is a protected file: a change to it is a pull request for the package owner.
- Principles of api.md [read]: the core stays standard library only; engines are optional; an engine is given directories and never fetches anything; no silent fallback; every result names the engine, the model and its verification status.
- [not read]: worker.py beyond its class line (how WorkerEngine starts and talks to its script), and the full field list of Capabilities.

## 2. What ZONOS3-Mini is

- Licences [read, via the model card and repository; see zonos3-mini-licence-notes.md]: code MIT, weights Apache-2.0; NOTICE item 4 lists Unicode CLDR data under Unicode-3.0. [not read]: NOTICE items 1 to 3 and those after 4, the vendored licences, the submodule licences.
- Pins used in every measurement: code 96d9e0ec1e39083ed336ac78f24e3dbc93721c0f, weights d2d142a86b1e6aeb4252679673c86ecd86177bf8.
- Files and SHA-256 [measured; identical on a GPU runtime and on a laptop]: gguf/backbone-ptq.gguf, 663150624 bytes, 66fceb6d73a8c30bde80249e4112bb308380ab7f3384fa11c8e4048115e4ceec; gguf/zac-keep2.gguf, 452325440 bytes, 834e5b70b88294afc3c702a478487e0664dcc6b574c16fc6167d72444aabeafd; gguf/spk-encoder.gguf, 45385728 bytes, 9539f340b70be246005403b9c254732e180e85e0b3c57fb1973f0d6bc6c59bdc; plus the folder gguf/voices (27 speakers were listed by the server).
- It is a native server (C++, ggml) with an HTTP API, not a Python library. The request used in the measurements: POST /v2/generate with text, language (a plain code such as hi), speaker_embedding_id, stream false, format wav and seed; the answer is a WAV.
- Measured (details and limits in benchmarks/voice/README.md): on a T4 the real-time factor was 0.13 to 0.23; on one laptop CPU (4 cores, 8 threads, no GPU) 3.35 pooled over 10 clips (Hindi 3.70, Telugu 2.97), so slower than real time there. Round trip with a recogniser (5 sentences per language, 2 voices, one run): Hindi word error 13.0 and 10.2 percent, Telugu 10.1 and 10.1 percent (character error 0.8 to 4.3); 8 of 50 clips read back badly, 7 of them in Tamil, Malayalam and Kannada, which have no stock voice. Nobody has listened to the clips, and the recogniser may favour Parler (same organisation); no confidence interval, one run.

## 3. Proposal for a first version

- Scope: text to speech, Hindi and Telugu only, stock voices only (Ananya for hi, Sravya for te, ids in a fixed table inside the engine), status unverified, default false, never chosen by engine="auto", and it runs only with allow_unverified=True. Tamil, Malayalam and Kannada are left out because of the bad clips and the missing stock voices.
- Connection (proposed): the operator builds the server from the pinned commit and starts it on 127.0.0.1; the engine only connects to a URL it is given, using the standard library, and refuses any non-loopback host unless allow_non_loopback is set (the same rule as ragleap-phone). Reasons not to start the server from our code: a built binary cannot be hash-pinned like a model file, building needs a Rust toolchain and cmake (7 minutes on the laptop CPU, over an hour with CUDA on a T4), and we would be running an unverified binary. models verify can still check the weight files of the folder the operator gives the server.
- What the engine sends: text, language, a speaker id from the fixed table, a seed, stream false, format wav. It rejects any other speaker id. No speaker audio, no speaker embedding upload and no cloning in the first version: the server accepts them, and they need the consent design (#763) first.
- Server surface to be aware of [read in the usage text, not tested]: the server can also serve a browser studio and project and media endpoints, and can start helper Python workers; the command line used in the measurements enabled none of these on purpose, and whether those endpoints answer without their options was not tested.
- Capabilities (proposed): kind tts, languages hi and te, hardware cpu and cuda with the CPU speed stated in the documentation, no emotion control declared (the server has controls that were not measured). Results carry engine, model and model_status "unverified".
- Speed statement for the documentation (from the measurements): useful in real time only on a GPU; on a laptop CPU it was about 3 times slower than real time.
- Streaming: the request has a stream field and the server lists stream decode modes; neither was tested. The first version uses stream false.

## 4. Open before any manifest entry or code

1. Read worker.py and decide whether the engine uses WorkerEngine or a plain loopback client. The server is already a separate process, so the isolation a worker gives may add little; that needs the code read.
2. Someone who reads Hindi and Telugu listens to the clips; for the other three languages a decision on whether to say anything at all.
3. Build the lock with the existing pull tool (plan_lock) for the three files and the voices folder, and read the rest of the NOTICE and the submodule licences.
4. Decide on cloning and consent (#763) before any speaker audio or embedding is accepted.
5. The manifest entry itself is a pull request to a protected file, for the owner.

## 5. Questions for the package owner

- Is an engine that talks to an operator-run local server acceptable, given that the model files can be verified but the server binary cannot?
- Should the first version carry Hindi and Telugu only?
- Is a GPU-only recommendation acceptable for a pre-alpha engine, with the CPU figure stated?

## 6. Addendum after reading engine.py and worker.py (2026-11-20): the proposal in section 3 changes

What was read: the Engine protocol (load(model_dirs), run(request), close()), Capabilities (kind, languages, clone, emotion, emotion_languages, streaming, hardware, watermarks_output, isolated, detects_language), Request (kind, language, audio, text, voice, emotion, speed, timestamps, options), Result (text, segments, wav, sample_rate, duration_s, language, notes) and worker.py. WorkerEngine runs a Python script in a separate process, sends it JSON lines (load, run, close), moves audio as files in a private folder, limits the worker's environment to PATH, LANG, LC_ALL, LC_CTYPE, SYSTEMROOT, LD_LIBRARY_PATH and CUDA_VISIBLE_DEVICES, and says in its own docstring that it is not a security sandbox. Timeouts: 900 s to load, 300 s per run; limits of 4 MiB per line and 512 MiB per WAV.

The problem with section 3: with a server started by the operator, nothing ties the folder that models verify checks to the files the server actually loads. The capability list printed by the server in the smoke run showed keys only and no file path, so the engine could not prove which files are in use.

Revised proposal (still a proposal): use WorkerEngine with a small standard-library worker script that starts the server from the verified folder in load (on 127.0.0.1, with no option that enables the studio, projects or helper workers), calls it over HTTP in run, and stops it in close. Gains: the files the server loads are the files that were verified; the worker owns the server's lifetime; the environment the server inherits is the restricted one unless the script passes another; the 900 s load timeout fits the measured start-up times on a laptop CPU (36 s to over 120 s). Costs and open points:

- The server binary is still unverified and still built by the operator; this does not change.
- The binary path does not reach the worker through model_dirs and the worker environment is restricted; how to pass it (a constructor argument like interpreter, or a subclass as the Parler engine may do) needs parler_engine.py and worker_runtime.py, which were not read.
- Port choice: the server accepts --port 0 for an ephemeral port, but how the worker learns the port was not tested; picking a free port in the worker leaves a small race.
- run_timeout of 300 s: a 10 s sentence took about 35 to 40 s on the laptop CPU, so very long texts need a stated limit.
- What isolated=True promises (api.md section 8) was not read; this proposal does not rely on it yet.

Still true: Hindi and Telugu only, stock voices only, unverified, never chosen by auto, no cloning before the consent design. The questions of section 5 stand; the first one becomes: is an engine that starts an operator-built server binary from a verified folder acceptable, given that the binary itself cannot be verified.

## 7. What parler_engine.py, worker_runtime.py and the rest of worker.py show (read 2026-11-20)

Read: parler_engine.py (first 70 lines), worker_runtime.py (structure), api.md section 8, worker.py lines 49 to 100 and 173 to 280, and the format policy in locks.py and pull.py. Not read: the rest of locks.py and pull.py (so whether plan_lock has another check for GGUF files is open), the full parler_worker.py, and how the manifest checks a worker engine's declared hardware.

- The Parler engine is a thin subclass of WorkerEngine whose run() refuses speed, emotion and voice before calling the parent (voice is reserved: cloning needs consent, #763). A Zonos engine would do the same.
- Worker protocol: a worker script calls serve(handlers) from worker_runtime.py (52 lines); each handler takes a message and returns a dict; the operations are load, run and close. The worker answers a run with wav_path (it must stay inside the worker's private folder), sample_rate, duration_s and language; the parent caps a WAV at 512 MiB.
- The worker's environment is built from PATH, LANG, LC_ALL, LC_CTYPE, SYSTEMROOT, LD_LIBRARY_PATH and CUDA_VISIBLE_DEVICES, plus a private HOME, HF_HOME, XDG_CACHE_HOME and TMPDIR, HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1, PYTHONUNBUFFERED=1 and PYTHONDONTWRITEBYTECODE=1. load() sends the worker only model_dirs. So a variable such as ZONOS3_SERVER_BIN would not reach the worker.
- The interpreter comes from the interpreter argument or from an environment variable; a worker script that uses only the standard library can run with sys.executable, so no separate environment would be needed for Zonos.
- Format policy: the unsafe suffixes are .pt .pth .bin .pkl .pickle .ckpt .joblib and .dill, with one closed exception (ctranslate2 on .bin). .gguf is not among them, so the three weight files can be locked without a policy change; what a GGUF file can contain beyond tensors was not checked.
- A WorkerEngine must declare capabilities.isolated=True (ValueError otherwise). api.md section 8 names conflicting pins and model-supplied code as the reasons; for Zonos the reason would be a native binary that cannot be verified. The owner should decide whether that is an acceptable use of the flag.

Ways to give the worker the path of the server binary (a proposal, ranked):

1. The operator puts the server binary on PATH, and the worker script finds it with shutil.which. No change to the core. The binary stays unverified and the operator is responsible for it.
2. A small, explicit extension of the worker framework: an engine declares extra environment variable names that may pass through, or load() carries an engine-specific configuration dictionary. A core change, for the owner.
3. A binary copied into the verified model folder. Not recommended: a binary is not a model file and no lock would cover it.

Recommendation, not a decision: a first version with option 1, Hindi and Telugu only, stock voices only, unverified, never chosen by auto, GPU recommended with the measured CPU figure stated. Size: an engine class like ParlerEngine, a worker script of about the size of parler_worker.py using serve(), a manifest entry (a protected file, a pull request for the owner) and a lock for the three files and the voices folder. Nothing is built.
