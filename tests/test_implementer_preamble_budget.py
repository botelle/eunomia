from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PREAMBLE_PATH = REPO_ROOT / "bin" / "implementer-preamble.md"

HEADING = "## You test within your budget, and leave your neighbours alone"


def _text() -> str:
    assert PREAMBLE_PATH.is_file(), "bin/implementer-preamble.md must exist"
    return PREAMBLE_PATH.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Collapse hard-wrapped newlines so prose phrases match regardless of
    where the Markdown source happens to wrap a line."""
    return " ".join(text.split())


def test_budget_section_sits_after_you_cannot_wait_and_before_how_to_finish() -> None:
    text = _text()
    cannot_wait = text.index(
        "## You cannot wait, and nothing unpushed survives you"
    )
    how_to_finish = text.index("## How to finish")
    assert HEADING in text
    budget_index = text.index(HEADING)
    assert cannot_wait < budget_index < how_to_finish


def test_rule_hard_time_limit_and_process_group_kill() -> None:
    text = _flat(_text())
    assert "90 minutes" in text
    assert "wrapper kills your whole process group" in text


def test_rule_run_targeted_tests_not_the_whole_suite() -> None:
    text = _flat(_text())
    assert (
        "Run the tests that cover what you changed, not the whole suite" in text
    )
    assert "CI runs the full suite on your pull request" in text
    assert "Name the tests you ran in your final message" in text


def test_rule_scratch_files_go_in_a_mktemp_dir_not_a_fixed_tmp_name() -> None:
    text = _flat(_text())
    assert "Write scratch files to a directory of your own" in text
    assert "mktemp -d" in text
    assert "Never use a fixed name under `/tmp` or `$TMPDIR`" in text


def test_rule_never_signal_a_process_you_did_not_start() -> None:
    text = _flat(_text())
    assert "Never signal a process you did not start" in text
    assert "never by `pkill` or a pattern match on the process list" in text
