"""The sourced Taylor parameter sets in examples/wear_study.ipynb: they carry their source, stay inside their tested range, and the toy default is untouched.

Standard library only. The notebook cell is run on its own, so nothing here needs numpy or a notebook runner.
"""
import ast
import contextlib
import io
import json
import math
import re
from pathlib import Path

import pytest

NOTEBOOK = Path(__file__).resolve().parents[1] / "examples" / "wear_study.ipynb"
TOY_LINE = "TAYLOR = dict(C=500.0, n=0.25, t_cut_min=0.1)"
DOI = "https://doi.org/10.3390/pr8070789"
NAMES = ("P20_C45_conventional_lathe", "P20_C45_cnc_lathe")
# The documented numbers, written out here so that a changed constant in the notebook is caught instead of moving the test with it.
EXPECTED = {"P20_C45_conventional_lathe": (568.0, 0.44), "P20_C45_cnc_lathe": (444.0, 0.39)}
V_MIN, V_MAX = 101.25, 768.8


def cells():
    return json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]


def text(cell):
    return "".join(cell["source"])


def index_of(predicate):
    found = [i for i, c in enumerate(cells()) if predicate(c)]
    assert len(found) == 1, found
    return found[0]


def toy_index():
    return index_of(lambda c: c["cell_type"] == "code" and TOY_LINE in text(c))


def run_new_cell():
    """Run the sourced-sets code cell alone. Returns its namespace and what it printed."""
    code = text(cells()[toy_index() + 2])
    ns, out = {}, io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(code, "wear_study.ipynb:4b", "exec"), ns)
    return ns, out.getvalue()


def explanation():
    return text(cells()[toy_index() + 1])


# ---- where the new cells sit, and what did not change
def test_the_new_cells_follow_section_4_and_hold_the_sets():
    i = toy_index()
    md, code = cells()[i + 1], cells()[i + 2]
    assert md["cell_type"] == "markdown" and text(md).startswith("### 4b. Sourced parameter sets")
    assert code["cell_type"] == "code" and "TAYLOR_SOURCED" in text(code)
    assert code["outputs"] == [] and code["execution_count"] is None  # no stored output


def test_the_toy_default_is_unchanged_and_the_new_cell_does_not_replace_it():
    assert sum(text(c).count(TOY_LINE) for c in cells() if c["cell_type"] == "code") == 1  # the text may quote the line; the code defines it once
    code = text(cells()[toy_index() + 2])
    assert not re.search(r"^TAYLOR\s*=", code, re.M)
    ns, _ = run_new_cell()
    assert "TAYLOR" not in ns and set(ns["TAYLOR_SOURCED"]) == set(NAMES)


def test_the_new_code_cell_is_valid_for_python_3_10():
    ast.parse(text(cells()[toy_index() + 2]), feature_version=(3, 10))


# ---- every set carries its source
def test_every_set_carries_its_source_licence_and_says_it_is_derived():
    sets = run_new_cell()[0]["TAYLOR_SOURCED"]
    for name, p in sets.items():
        for key in ("C", "n", "v_min", "v_max", "vb_limit_mm", "derived", "licence", "source", "pair"):
            assert key in p, (name, key)
        assert all(isinstance(p[k], str) and p[k].strip() for k in ("licence", "source", "pair")), name
        assert p["licence"] == "CC BY" and DOI in p["source"], name
        assert p["derived"] is True, name  # the source prints no n and C: they are derived here
        assert all(math.isfinite(p[k]) and p[k] > 0 for k in ("C", "n", "v_min", "v_max", "vb_limit_mm")), name
        assert p["v_min"] < p["v_max"] and 0.0 < p["n"] < 2.0


def test_the_sets_are_the_documented_numbers():
    sets = run_new_cell()[0]["TAYLOR_SOURCED"]
    for name, (c, n) in EXPECTED.items():
        p = sets[name]
        assert (p["C"], p["n"], p["v_min"], p["v_max"], p["vb_limit_mm"]) == (c, n, V_MIN, V_MAX, 0.3), name
    md = explanation()
    for number in ("568", "444", "0.44", "0.39", "101.25", "768.8", "0.3 mm"):
        assert number in md, number
    for name, (c, n) in EXPECTED.items():  # the table row of each set must say what the code says
        rows = [line for line in md.splitlines() if line.startswith("| `%s`" % name)]
        assert len(rows) == 1, name
        cols = [x.strip() for x in rows[0].strip("|").split("|")]
        assert cols[2] == "101.25 to 768.8" and float(cols[3]) == n and float(cols[4]) == c, (name, cols)


def test_the_stated_ranges_are_the_spread_between_the_two_lathes():
    sets = run_new_cell()[0]["TAYLOR_SOURCED"]
    ns_, cs = [p["n"] for p in sets.values()], [p["C"] for p in sets.values()]
    assert (min(ns_), max(ns_)) == (0.39, 0.44) and (min(cs), max(cs)) == (444.0, 568.0)
    assert "n from 0.39 to 0.44, C from 444 to 568 m/min" in re.sub(r"\s+", " ", explanation())


# ---- the tool life, and the range guard
def test_tool_life_follows_the_taylor_law():
    ns, _ = run_new_cell()
    life = ns["sourced_tool_life_min"]
    for name, (c, n) in EXPECTED.items():
        for v in (V_MIN, 150.0, 250.0, 500.0, V_MAX):
            assert math.isclose(life(name, v), (c / v) ** (1.0 / n), rel_tol=1e-12), (name, v)
    assert math.isclose(life("P20_C45_conventional_lathe", 250.0), 6.5, rel_tol=0.02)
    assert math.isclose(life("P20_C45_cnc_lathe", 250.0), 4.4, rel_tol=0.02)
    assert life("P20_C45_cnc_lathe", 150.0) > life("P20_C45_cnc_lathe", 300.0)  # faster cutting, shorter life


def test_a_speed_outside_the_tested_range_is_refused():
    ns, _ = run_new_cell()
    life = ns["sourced_tool_life_min"]
    for name in NAMES:
        life(name, V_MIN)  # both ends are inside
        life(name, V_MAX)
        for bad in (V_MIN - 0.01, V_MAX + 0.01, 0.0, -5.0, 20.0, 1500.0, float("nan"), float("inf"), float("-inf"), True, "250", None, [250.0]):
            with pytest.raises(ValueError):
                life(name, bad)
    with pytest.raises(KeyError):
        life("no_such_set", 250.0)


# ---- the text says what it must
def test_the_text_says_it_is_still_a_toy_in_each_place_it_must():
    md, i = explanation(), toy_index()
    assert md.split("\n")[0].endswith("(still a toy)")
    assert "**The notebook is still a toy**" in md
    assert "Still a toy: it says nothing about a real machine, a real tool or a real process." in run_new_cell()[1]


def test_the_text_names_the_source_the_licence_and_what_was_derived():
    md = re.sub(r"\s+", " ", explanation())
    for phrase in ("Prediction of Cutting Material Durability by T = f(vc) Dependence for Turning Processes", DOI, "licence CC BY.", "ln V on ln T",
                   "The source does not print n and C", "derived here", "not taken from the source", "refuses a speed outside them",
                   "does not hold over its whole speed range", "The source gives no scatter"):
        assert phrase in md, phrase
    for name in NAMES:
        assert "`%s`" % name in md


def test_the_new_cell_prints_the_comparison_with_the_toy_default():
    _, out = run_new_cell()
    assert "toy default" in out and "tool life in minutes" in out
    row = [line for line in out.splitlines() if line.split() and line.split()[0] == "250"]
    assert len(row) == 1 and "16.0 (160)" in row[0] and "6.5 (64)" in row[0] and "4.4 (43)" in row[0], row
