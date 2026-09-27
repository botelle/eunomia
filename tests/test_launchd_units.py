"""Every shipped launchd unit runs a PINNED path, not the shared clone.

`~/dev/eunomia` is a shared checkout: several sessions work in it and the branch
it sits on is whatever the last one left. Until 2026-09-07 the PreToolUse
credential guard ran from there, and so did `fleet-collect` and
`fleet-leak-watch` — every 120 seconds, unattended. That clone was observed on
three different branches in a single afternoon.

A scheduled job is the worst place for this defect, because nobody is watching
when it fires. The rule is ADR-0005's, applied to deployment rather than to a
reviewer: address a commit, not a directory.

These parse through `plutil -convert` rather than plistlib directly: launchd
tolerates `--` inside XML comments, which XML forbids and Python's expat
refuses. plutil is the authority for a file launchd reads, and it strips the
comments on the way.

Not every plist here uses `--`, and the ones that do are tracked rather than
assumed: `test_units_parse_strictly` names them and fails if that set grows OR
shrinks without the list being updated. An earlier version of this docstring
said "every plist here uses it", which was never true and is what let
`test_the_dispatch_unit_carries_no_throttle_keys` read two units with plistlib
directly and look like luck.
"""
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

UNITS = sorted((Path(__file__).resolve().parent.parent / "launchd").glob("*.plist"))


def _load(p):
    """Parse a plist through `plutil`, skipping where it does not exist.

    The skip is on shutil.which, NOT on a returncode. `subprocess.run` RAISES
    FileNotFoundError when the binary is absent, so a guard written as
    `if out.returncode != 0: skip` only fires when plutil exists AND fails --
    exactly backwards. That shipped, and turned the cihost-linux lane red with
    8 FileNotFoundErrors while the cihost (macOS) lane stayed green.

    plutil rather than plistlib because launchd tolerates `--` inside XML
    comments, which expat refuses; plutil is the authority for a file launchd
    reads, and strips comments. Which units still need that leniency is pinned
    by `test_units_parse_strictly`, not assumed here.
    """
    if shutil.which("plutil") is None:
        pytest.skip("plutil is macOS-only; these units are only read by launchd")
    out = subprocess.run(["plutil", "-convert", "xml1", "-o", "-", str(p)],
                         capture_output=True)
    if out.returncode != 0:
        pytest.skip(f"plutil could not read {getattr(p, 'name', p)}")
    return plistlib.loads(out.stdout)


# Units whose header comment still contains a literal `--`. XML forbids it
# inside a comment; launchd and plutil accept it, expat does not.
#
# The set is empty. The last entry was pin-watch, whose remaining dashes were in
# runbook lines rather than prose. Two different things were done to them, and
# the distinction is worth keeping because only one of them is safe by default:
#
#   - the fetch-and-detach pair was DEDUPLICATED. docs/secret-guard.md already
#     carried it, so the plist's copy was removed and the comment points there.
#     Nothing was lost, and one owner replaced two.
#   - the dry-run verification and the `checkout` one-commit-back step were
#     REWORDED into prose — the thing this comment previously said could not be
#     done. It holds here only because the prose keeps every operative detail and
#     a checkout of HEAD~1 on an already-detached pin stays detached, so no
#     reader performs a different operation. That is a judgement per line, not a
#     general licence: a flag whose removal changes the operation must move to a
#     doc instead.
#
# One residual: someone following the pointer to secret-guard.md for the DRY-RUN
# reproduction will not find it there. The nearest copy is docs/plan-dispatch.md,
# without the env details.
#
# This set is asserted EXACTLY, both directions. Growing it is a regression.
# Shrinking it without editing this line leaves a stale exemption that would
# hide the next one, which is how `--` reached three units unnoticed.
LENIENT_ONLY = set()   # emptied: pin-watch parses strictly since the block
                       # above; see it for what was done to each dash.


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_units_parse_strictly(unit):
    """Every unit parses under a strict XML parser, except the tracked ones.

    `plutil -lint` passes all five, so it cannot see this class of defect at
    all: it is the lenient parser. The failure mode is not launchd refusing to
    run the job — it will run it — but any tool that reaches for the obvious
    plistlib.load getting an ExpatError from a file that "lints clean", which
    already cost one author a debugging session while writing fleet-watch.
    """
    try:
        plistlib.load(unit.open("rb"))
    except Exception as exc:
        assert unit.name in LENIENT_ONLY, (
            f"{unit.name} no longer parses strictly: {exc}\n"
            "`--` is illegal inside an XML comment. plutil and launchd accept "
            "it, expat does not. Use an em dash for prose; if it is a literal "
            "command that must stay verbatim, add it to LENIENT_ONLY with the "
            "reason.")
        return
    assert unit.name not in LENIENT_ONLY, (
        f"{unit.name} parses strictly now, but is still listed in "
        "LENIENT_ONLY. Remove it: a stale exemption silently covers the next "
        "unit that regains a `--`.")


def test_there_are_units_to_check():
    """A glob that matches nothing would make every test below vacuously pass."""
    assert UNITS, "no launchd units found"


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_no_unit_executes_the_shared_clone(unit):
    d = _load(unit)
    args = d.get("ProgramArguments") or []
    offenders = [a for a in args if "/dev/eunomia/" in str(a)]
    assert not offenders, (
        f"{unit.name} runs {offenders} from the shared checkout — its branch is "
        "whatever a session last left there. Point it at the pinned worktree "
        "(~/.local/share/pins/eunomia) and let fleet-watch report drift.")


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_every_unit_has_a_label_and_a_log(unit):
    """A unit whose output goes nowhere fails silently, which is how the
    scheduled jobs above went a day without anyone noticing what they ran."""
    d = _load(unit)
    assert d.get("Label"), f"{unit.name} has no Label"
    assert d.get("StandardOutPath") or d.get("StandardErrorPath"), (
        f"{unit.name} sends its output nowhere")


RETIRED_PIN_PATHS = (
    "~/.local/share/fleet-guard",
    "/Users/operator/.local/share/fleet-guard",
    "~/.local/share/techne-skills",
)


def test_no_tracked_file_names_a_retired_pin_path():
    """The install instructions are the control against a second pin.

    On 2026-09-07 the eunomia pin moved to ~/.local/share/pins/eunomia, and
    every reference was updated -- except in two files that did not exist on
    main yet, because they were in an open PR. When that PR merged, main told
    readers to run `worktree add --detach ~/.local/share/fleet-guard`, which is
    exactly the command that had already produced a second, unwatched guard
    earlier the same day.

    Nothing caught it: test_no_unit_executes_the_shared_clone only forbids
    /dev/eunomia/ in ProgramArguments, so a stale *pin* name passes, and it
    reads plists rather than docs. Two PRs open at once against different files
    is the normal case here, so a grep over what is tracked is the check that
    survives it.
    """
    root = Path(__file__).resolve().parent.parent
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout.split("\0")

    # This file names the retired paths in order to forbid them, and its own
    # history section quotes the install line that caused the incident. Naming
    # a path in the check that bans it is not an instruction to follow it.
    self_rel = str(Path(__file__).resolve().relative_to(root))

    offenders = []
    for rel in filter(None, tracked):
        if rel == self_rel:
            continue
        try:
            body = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(body.splitlines(), 1):
            for dead in RETIRED_PIN_PATHS:
                if dead in line:
                    offenders.append(f"{rel}:{lineno}: {dead}")

    assert not offenders, (
        "a retired pin path is still in the tree:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe pins live under ~/.local/share/pins/<repo>. An install block "
          "naming a retired path is not a stale comment -- a reader who follows "
          "it creates a second worktree that no FLEET_PINS list watches."
    )


def test_the_dispatch_unit_carries_no_throttle_keys():
    """fleet-watch must NOT set ProcessType/LowPriorityIO/Nice. It spawns.

    All three are inherited by the orchestrators spawn() starts and none can be
    undone by an unprivileged child: measured on opshost, a real launchd job with
    ProcessType Background reports darwinbg 0 while sitting at priority 4, so
    the clamp is the process TYPE and setpriority cannot clear it; nice cannot
    be lowered by an unprivileged process at all. Re-adding these would put
    every dispatched implementer session in the background band, I/O-throttled,
    while `nice` still reported 0 -- invisible to every tool that reports
    priority.

    The sibling watchers keep the keys and should: they spawn nothing. This
    asserts the difference rather than the absence, so that a well-meaning
    "make the units consistent" change fails here with the reason.

    plistlib directly, not the plutil helper above: this assertion is about a
    file the repo controls, so it should run on the Linux lane too, where
    plutil does not exist and the helper would skip. That is safe only while
    both units named here parse strictly, which is not luck:
    `test_units_parse_strictly` asserts it, and fails if either regains a `--`.
    """
    import plistlib
    unit = Path(__file__).resolve().parent.parent / "launchd"
    d = plistlib.load((unit / "org.eunomia.fleet-watch.plist").open("rb"))
    present = [k for k in ("ProcessType", "LowPriorityIO", "Nice") if k in d]
    assert not present, (
        f"org.eunomia.fleet-watch.plist sets {present}; the dispatch unit spawns "
        "orchestrators that would inherit the throttle and could not clear it")

    sibling = plistlib.load(
        (unit / "org.eunomia.fleet-broker-tunnel.plist").open("rb"))
    assert sibling.get("ProcessType") == "Background", (
        "the non-spawning siblings are still expected to be polite; if that "
        "changed deliberately, this test is the record that needs updating")
