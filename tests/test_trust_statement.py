"""The trust statement for in-process connectors (Phase 0, P0-F): one wording, in the docs, CONTRIBUTING and the PR template."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


DOC = _read("docs/capability-schema.md")
CONTRIBUTING = _read("CONTRIBUTING.md")
TEMPLATE = _read(".github/PULL_REQUEST_TEMPLATE.md")
STATEMENT = next(line for line in DOC.splitlines() if line.startswith("Until process isolation is available"))
KEY = "every connector that runs inside the RagLeap process is trusted code"


def test_the_statement_in_the_docs_is_the_full_wording():
    assert KEY in STATEMENT
    assert "they do not contain a malicious in-process connector" in STATEMENT
    assert "accepted only as reviewed code merged into the repository" in STATEMENT


def test_contributing_has_the_exact_statement_from_the_docs_once():
    assert CONTRIBUTING.count(STATEMENT) == 1
    assert CONTRIBUTING.count("## Adding a connector, tool or integration") == 1


def test_the_template_has_the_key_sentence_and_the_checks():
    assert TEMPLATE.count(KEY) == 1
    assert "ragleap_conformance.py" in TEMPLATE and "ragleap_boundary_scan.py" in TEMPLATE
    assert TEMPLATE.count("- [ ]") >= 6          # the three original boxes and the three connector boxes


def test_the_contributing_section_points_at_files_that_exist():
    section = CONTRIBUTING.split("## Adding a connector, tool or integration", 1)[1].split("\n## ", 1)[0]
    for rel in ("docs/capability-schema.md", "scripts/ragleap_conformance.py", "scripts/ragleap_boundary_scan.py",
                "core/autonomy.py", "core/integrations"):
        assert rel in section, rel
        assert os.path.exists(os.path.join(ROOT, rel)), rel


def test_the_section_says_the_format_is_a_draft_and_makes_no_claim_of_its_own():
    section = CONTRIBUTING.split("## Adding a connector, tool or integration", 1)[1].split("\n## ", 1)[0]
    assert "draft" in section
    # the words appear only inside the sentence that tells contributors NOT to use them
    for word in ("secure", "certified", "production-ready"):
        for m in re.finditer(word, section):
            assert 'Do not describe a change as' in section[:m.start()].rsplit("\n", 1)[-1], word


def test_the_pages_build_cannot_be_broken_by_these_files():
    for text in (CONTRIBUTING, TEMPLATE):
        assert "{{" not in text and "{%" not in text
        assert not re.match(r"---\s*\n", text)
