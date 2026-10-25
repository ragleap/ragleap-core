"""transcribe(), the first public function of the planned API (docs/design/api.md sections 2, 6, 7 and 9).

Standard library only. The engine is handed directories of verified model files and never fetches anything itself.
Nothing here swallows an error and turns it into a default.
"""
import pathlib
from collections.abc import Mapping

from .cache import cache_root, model_dir
from .engine import Request, Result
from .errors import (EngineError, LicenceError, ModelNotFoundError, ModelNotVerifiedError, NoEngineError,
                     RagleapVoiceError, UnsupportedFeatureError, UnsupportedLanguageError)
from .licences import check_licence
from .locks import LockError, load_lock, lock_digest, validate_lock, verify_dir
from .manifest import ManifestError, default_manifest_path, load_manifest
from .registry import _DEFAULT as _DEFAULT_REGISTRY
from .results import Transcript
from .wavio import read_wav

_PULL_HINT = "pull the model first: ragleap-voice models pull (see its --help for the arguments)."


def _pick_engine(registry, name, language):
    if name != "auto":
        eng = registry.get(name)
        caps = eng.capabilities
        if caps.kind != "stt":
            raise UnsupportedFeatureError("engine %r is a %s engine, not a speech-to-text engine." % (name, caps.kind),
                                          hint="choose an engine from engines('stt').")
        if language is None:
            if not caps.detects_language:
                raise UnsupportedFeatureError("engine %r cannot detect the language." % name,
                                              hint="pass language=, for example language='en'.")
        elif language not in caps.languages:
            raise UnsupportedLanguageError("engine %r does not declare language %r." % (name, language),
                                           hint="see engines('stt') for the languages each engine declares.")
        return eng
    stt = [i for i in registry.info("stt") if not i.third_party]
    if language is None:
        names = [i.name for i in stt if i.capabilities.detects_language]
        if not names:
            raise NoEngineError("no built-in speech-to-text engine can detect the language.",
                                hint="pass language=, or name an engine with engine='name'.")
        return registry.get(names[0])
    candidates = registry.auto_candidates("stt", language)
    if candidates:
        return candidates[0]
    if not stt:
        raise NoEngineError("no built-in speech-to-text engine is registered.",
                            hint="engines arrive as optional extras; install the one you need.")
    raise UnsupportedLanguageError("no built-in speech-to-text engine declares language %r." % language,
                                   hint="see engines('stt'); you can also name an engine explicitly.")


def _needed_models(names, by_name):
    """The manifest entries an engine needs, dependencies first, each once."""
    order, seen = [], set()

    def visit(n, trail):
        if n in seen:
            return
        if n in trail:
            raise ModelNotFoundError("dependency cycle in the manifest: " + " -> ".join(trail + [n]))
        entry = by_name.get(n)
        if entry is None:
            raise ModelNotFoundError("model %r is not in the manifest." % n,
                                     hint="the engine and the manifest disagree; this is a bug in the engine or the manifest.")
        for dep in entry.depends_on:
            visit(dep, trail + [n])
        seen.add(n)
        order.append(entry)

    for n in names:
        visit(n, [])
    return order


def _resolve_models(eng, manifest_path, cache_dir, allow_unverified):
    """Return ({model name: verified directory}, "verified" or "unverified", names of unverified models)."""
    try:
        mpath = pathlib.Path(manifest_path) if manifest_path else default_manifest_path()
        entries, _ = load_manifest(mpath)
    except ManifestError as exc:
        raise ModelNotFoundError("cannot read the model manifest: %s" % exc)
    order = _needed_models(eng.manifest_entries, {e.name: e for e in entries})
    for e in order:
        for label, expr in (("code", e.code_license), ("weights", e.weights_license)):
            ok, reason = check_licence(expr)
            if not ok:
                raise LicenceError("model %r: %s licence %r fails the allowlist (%s)." % (e.name, label, expr, reason),
                                   hint="only allowlisted licences may run; see docs/mission-and-security.md.")
    unverified = [e.name for e in order if e.status != "verified"]
    if unverified and not allow_unverified:
        raise ModelNotVerifiedError("model(s) not verified: %s." % ", ".join(unverified),
                                    hint="pass allow_unverified=True to run them anyway; the result will say so.")
    root = pathlib.Path(cache_dir) if cache_dir is not None else cache_root()
    dirs = {}
    for e in order:
        if not e.revision:
            raise ModelNotFoundError("model %r has no pinned revision in the manifest, so it cannot be located." % e.name,
                                     hint="the manifest entry must be pinned before the model can run.")
        try:
            d = model_dir(e.name, e.revision, root)
        except LockError as exc:
            raise ModelNotFoundError(str(exc))
        if not d.is_dir():
            raise ModelNotFoundError("model %r is not pulled (expected %s)." % (e.name, d), hint=_PULL_HINT)
        lock_path = mpath.parent / "locks" / (e.name + ".json")
        if not lock_path.is_file():
            raise ModelNotVerifiedError("model %r has no lock file, so its files cannot be verified." % e.name)
        try:
            lock = load_lock(lock_path)
        except LockError as exc:
            raise ModelNotVerifiedError("model %r: %s." % (e.name, exc))
        problems = validate_lock(lock, e.name, e.revision)
        if not problems and (e.sha256 or e.status == "verified") and lock_digest(lock) != e.sha256:
            problems = ["the lock digest does not match the manifest"]
        if not problems:
            problems = verify_dir(lock, d)
        if problems:
            more = "" if len(problems) <= 3 else " (and %d more)" % (len(problems) - 3)
            raise ModelNotVerifiedError("model %r failed verification: %s%s." % (e.name, "; ".join(problems[:3]), more),
                                        hint="pull the model again; do not run files that fail the lock.")
        dirs[e.name] = d
    return dirs, ("verified" if not unverified else "unverified"), unverified


def _transcribe(audio, registry, manifest_path, *, language=None, engine="auto", allow_unverified=False,
                cache_dir=None, timestamps=False, options=None):
    if language is not None and (not isinstance(language, str) or not language.strip()):
        raise ValueError("language must be None or a code such as 'hi', 'ta', 'en'")
    if not isinstance(engine, str) or not engine:
        raise ValueError("engine must be 'auto' or an engine name")
    if options is not None and not isinstance(options, Mapping):
        raise TypeError("options must be a dict or None")
    data, _info = read_wav(audio)
    eng = _pick_engine(registry, engine, language)
    dirs, status, unverified = _resolve_models(eng, manifest_path, cache_dir, allow_unverified)
    request = Request(kind="stt", language=language, audio=data, timestamps=bool(timestamps), options=dict(options or {}))
    closing = None
    try:
        try:
            eng.load(dirs)
            res = eng.run(request)
        finally:
            try:
                eng.close()
            except Exception as exc:
                closing = type(exc).__name__
    except RagleapVoiceError:
        raise
    except Exception as exc:
        raise EngineError("engine %r failed: %s: %s" % (eng.name, type(exc).__name__, exc),
                          hint="the original error is chained; run the engine on its own to investigate.") from exc
    if not isinstance(res, Result) or not isinstance(res.text, str):
        raise EngineError("engine %r returned no text result." % eng.name, hint="this is a bug in the engine.")
    notes = list(res.notes)
    if unverified:
        notes.append("unverified model(s) used because allow_unverified=True: %s." % ", ".join(unverified))
    if closing:
        notes.append("engine close() raised %s." % closing)
    if timestamps and not res.segments:
        notes.append("segments were requested but the engine returned none.")
    return Transcript(text=res.text, language=res.language or language, engine=eng.name,
                      model=", ".join(eng.manifest_entries), model_status=status,
                      segments=res.segments if timestamps else (), notes=tuple(notes))


def transcribe(audio, *, language=None, engine="auto", allow_unverified=False, cache_dir=None, timestamps=False,
               options=None):
    """Speech to text. audio is a path or the bytes of a PCM WAV file. Returns a Transcript."""
    return _transcribe(audio, _DEFAULT_REGISTRY, None, language=language, engine=engine,
                       allow_unverified=allow_unverified, cache_dir=cache_dir, timestamps=timestamps, options=options)
