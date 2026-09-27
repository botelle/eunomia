"""An ADR on `main` is Accepted, because merge is acceptance (docs/adr/README.md).

`main` is protected, so nothing can rewrite the status word after a merge. This
test is what holds the README's rule true: a record that says `Proposed` fails
the suite on the branch and on `main` alike — the open pull request is the
proposal, so the word is never written.
Measured 2026-09-21: all ten records said `Proposed`, every one merged.
"""
import pathlib
import re

ADR_DIR = pathlib.Path(__file__).resolve().parent.parent / "docs" / "adr"
STATUS_RE = re.compile(r"^- \*\*Status:\*\* (?P<word>.+?)\s*$", re.M)
ON_MAIN = ("Accepted", "Deprecated")


def _records():
    files = sorted(p for p in ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))
    assert files, f"no ADRs found under {ADR_DIR} — wrong checkout?"
    return files


def _status(path):
    m = STATUS_RE.search(path.read_text(encoding="utf-8"))
    assert m, f"{path.name}: no `- **Status:** <word>` line"
    return m.group("word")


def test_every_adr_carries_a_status_line():
    for path in _records():
        _status(path)


def test_no_adr_on_disk_is_proposed():
    proposed = [p.name for p in _records() if _status(p) == "Proposed"]
    assert not proposed, (
        "these records say Proposed; a merged ADR is Accepted, set it in the PR "
        "(docs/adr/README.md): " + ", ".join(proposed))


def test_status_word_is_from_the_closed_set():
    for path in _records():
        word = _status(path)
        ok = word in ON_MAIN or word.startswith("Superseded by ADR-")
        assert ok, f"{path.name}: status {word!r} is not one of {ON_MAIN} or 'Superseded by ADR-NNNN'"
