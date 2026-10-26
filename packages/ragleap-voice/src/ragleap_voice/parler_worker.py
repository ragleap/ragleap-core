"""Worker script of the Indic Parler-TTS engine (docs/design/api.md section 8). Run by the user's own Python environment.

That environment needs torch, transformers and parler-tts (a GitHub library, Apache-2.0, installed at a pinned commit by the
user; it is not covered by the lock files). The script reads only the verified model folders it is given and loads files
from them only. The description tokenizer comes from the locked flan-t5 tokenizer folder, not from the Hub by name.
"""
import importlib.util
import os
import sys
import wave

MODEL, TOKENIZER = "indic-parler-tts", "flan-t5-large-tokenizer"
ENV_NAME = "RAGLEAP_VOICE_PARLER_PYTHON"
DESC = ("A female speaker delivers a slightly expressive and animated speech with a moderate speed and pitch. "
        "The recording is of very high quality, with the speaker's voice sounding clear and very close up.")
_OPTIONS = ("description",)
_state = {}


def load(msg):
    try:
        import torch
        from parler_tts import ParlerTTSForConditionalGeneration
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("the worker environment lacks torch, transformers or parler-tts (%s). Install them into the "
                           "environment named by %s." % (exc, ENV_NAME))
    dirs = msg.get("model_dirs") or {}
    parler, flan = dirs.get(MODEL), dirs.get(TOKENIZER)
    if not parler or not flan:
        raise RuntimeError("the folders of %s and %s are both needed." % (MODEL, TOKENIZER))
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = ParlerTTSForConditionalGeneration.from_pretrained(parler, local_files_only=True).to(dev)
    _state.clear()
    _state.update(dev=dev, model=model,
                  prompt_tok=AutoTokenizer.from_pretrained(parler, local_files_only=True),
                  desc_tok=AutoTokenizer.from_pretrained(flan, local_files_only=True))
    return {}


def run(msg):
    if not _state:
        raise RuntimeError("the model is not loaded.")
    req = msg["request"]
    text = req.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    options = req.get("options") or {}
    unknown = sorted(set(options) - set(_OPTIONS))
    if unknown:
        raise ValueError("unsupported option(s): %s (supported: %s)" % (", ".join(unknown), ", ".join(_OPTIONS)))
    desc = options.get("description", DESC)
    if not isinstance(desc, str) or not desc.strip():
        raise ValueError("description must be a non-empty string")
    dev = _state["dev"]
    d = _state["desc_tok"](desc, return_tensors="pt").to(dev)
    p = _state["prompt_tok"](text, return_tensors="pt").to(dev)
    gen = _state["model"].generate(input_ids=d.input_ids, attention_mask=d.attention_mask,
                                   prompt_input_ids=p.input_ids, prompt_attention_mask=p.attention_mask)
    audio = gen.cpu().numpy().squeeze()
    rate = int(_state["model"].config.sampling_rate)
    pcm = (audio.reshape(-1).astype("float32").clip(-1.0, 1.0) * 32767.0).astype("<i2")
    out = os.path.join(msg["work_dir"], "out.wav")
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return {"wav_path": out, "sample_rate": rate,
            "notes": ["Parler takes no language code: the text itself decides the language.",
                      "naturalness and emotion quality are not measured; generation is slower than real time on a T4.",
                      "device used: %s." % dev]}


def close(msg):
    _state.clear()


def _runtime():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker_runtime.py")
    spec = importlib.util.spec_from_file_location("worker_runtime", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    # the package folder holds modules (api, cache, coverage, ...) that could shadow libraries in this environment
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _here]
    _runtime().serve({"load": load, "run": run, "close": close})
