"""Every script in bin/ is committed executable.

bin/fleet-config sat at 100644 for weeks because every eunomia caller loads it
as a module by path, which needs no exec bit. talos's dispatch-review.py (plan
0010) was the first caller to run it as a program, from the pin; the pin's
checkout copies the committed mode, and every review dispatch on the fleet died
with PermissionError at round 1 (2026-09-28). The mode that matters is the one
in git, not the one in this checkout, so that is what this reads.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _index_modes():
    out = subprocess.run(["git", "ls-files", "-s", "--", "bin/"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        yield meta.split()[0], path


def test_every_shebang_script_in_bin_is_committed_executable():
    modes = list(_index_modes())
    assert modes, "git ls-files saw nothing under bin/ -- the check would pass vacuously"
    bad = [path for mode, path in modes
           if (ROOT / path).read_bytes()[:2] == b"#!" and mode != "100755"]
    assert not bad, (
        f"committed without the exec bit: {bad} -- "
        "fix with `git update-index --chmod=+x <path>`")
