"""The guide in docs/industry/ is executed here, so its examples cannot go out of date."""
import contextlib
import io
import pathlib
import re

DOC = pathlib.Path(__file__).resolve().parents[1] / "docs" / "industry" / "observe-only-adapters.md"


def examples():
    text = DOC.read_text(encoding="utf-8")  # a missing guide fails the test on purpose
    return re.findall(r"```python\n(.*?)```\n```text\n(.*?)```", text, re.S)


def test_the_guide_has_its_two_examples():
    assert len(examples()) == 2


def test_every_example_runs_and_prints_exactly_what_the_guide_shows():
    for n, (code, expected) in enumerate(examples(), start=1):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile(code, f"{DOC.name} example {n}", "exec"), {"__name__": "guide_example"})
        assert out.getvalue() == expected, f"example {n} printed {out.getvalue()!r}"


def test_the_guide_states_its_limits_and_has_no_addresses():
    text = DOC.read_text(encoding="utf-8")
    for phrase in ("simulation only", "not verified", "invented", "not an MTConnect client", "observe-only", "no direct way", "AllowPut"):
        assert phrase.lower() in text.lower(), phrase
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text)
