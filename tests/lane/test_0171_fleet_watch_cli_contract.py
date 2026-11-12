"""Public CLI checks only; overlap scenario coverage requires a declared harness."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.covers()
@pytest.mark.parametrize(
    "arguments",
    [
        ["--lane-test-unknown-option"],
        ["--meeting-job"],
        ["--meeting-job", "a", "b", "c", "d"],
        ["--meeting-job", "a", "b", "c", "d", "e", "f"],
    ],
    ids=["unknown-option", "missing-meeting-values", "four-meeting-values", "six-meeting-values"],
)
def test_bad_usage_exits_two(tmp_path, arguments):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.defpath]),
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "LANG": "C",
        "EUNOMIA_FLEET_DIR": str(tmp_path / "fleet"),
        "FLEET_DB": str(tmp_path / "fleet.db"),
    }
    result = subprocess.run(
        [str(ROOT / "bin" / "fleet-watch"), *arguments],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.stderr.strip(), "Bad usage must produce an argparse diagnostic"
