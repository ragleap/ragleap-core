import csv
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ragleap_voice.whisper_engine import LANGUAGES  # noqa: E402

MIN_CLIPS, MAX_UPPER = 60, 15.0  # the proposed rule in docs/design/coverage-matrix.md section 7


def measured_rows():
    with open(ROOT / "coverage" / "stt.csv", encoding="utf-8", newline="") as f:
        return [r for r in csv.DictReader(f) if r["engine"] == "whisper-large-v3" and r["tier"] == "2"]


def rule_languages():
    out = set()
    for r in measured_rows():
        try:
            n, upper = int(r["n_clips"]), float(r["ci_high"])
        except ValueError:
            continue
        if r["metric"] in ("WER", "CER") and n >= MIN_CLIPS and upper <= MAX_UPPER:
            out.add(r["language"])
    return out


def test_declared_languages_follow_the_rule():
    assert LANGUAGES == rule_languages(), sorted(LANGUAGES ^ rule_languages())


def test_every_declared_language_has_a_measured_row():
    have = {r["language"] for r in measured_rows()}
    assert LANGUAGES <= have, sorted(LANGUAGES - have)


def test_near_misses_and_indian_languages_stay_out():
    assert not {"da", "hu", "ar", "ko", "he", "fa", "sw", "hi", "ta", "te", "ml", "kn"} & LANGUAGES


def test_the_set_is_the_31_of_the_first_sweep():
    assert len(LANGUAGES) == 31
