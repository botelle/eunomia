"""The environment guard in conftest.py, proved in both directions.

Asserting that the guard is installed proves nothing: the question is whether a
test can still inherit a variable the previous test wrote. So this runs a real
pytest over a generated pair of files — one writes, the next checks — and runs
it twice, once with the repository's `conftest.py` present and once without.

The second run is the part that matters. A guard nobody has watched fail is the
failure mode this repository keeps re-learning, and this one guards a leak that
by its nature never fails the test that causes it.

`-p no:randomly` pins the order: the leak is order-dependent, and the point here
is the guard, not the seed.
"""
import subprocess
import sys
from pathlib import Path

CONFTEST = Path(__file__).resolve().parent / "conftest.py"

PROBE = "EUNOMIA_ENV_LEAK_PROBE"

_WRITER = f'''import os


def test_writes_the_probe():
    os.environ["{PROBE}"] = "leaked"
'''

_READER = f'''import os


def test_does_not_inherit_the_probe():
    assert "{PROBE}" not in os.environ
'''


def _run(directory):
    """One pytest run over `directory`, in a child process."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:randomly",
         "-p", "no:cacheprovider", "-q", "test_a_writer.py", "test_b_reader.py"],
        cwd=directory, capture_output=True, text=True,
    )


def _layout(directory, *, with_conftest):
    (directory / "test_a_writer.py").write_text(_WRITER, encoding="utf-8")
    (directory / "test_b_reader.py").write_text(_READER, encoding="utf-8")
    if with_conftest:
        (directory / "conftest.py").write_text(
            CONFTEST.read_text(encoding="utf-8"), encoding="utf-8")


def test_the_guard_stops_a_variable_crossing_between_files(tmp_path):
    _layout(tmp_path, with_conftest=True)
    done = _run(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_without_the_guard_the_variable_does_cross(tmp_path):
    """Prove the alarm. If this ever passes, the pair above has stopped
    testing the guard and is passing for some other reason."""
    _layout(tmp_path, with_conftest=False)
    done = _run(tmp_path)
    assert done.returncode != 0, (
        "the writer's variable did not reach the reader even with no guard "
        "installed, so the run above proves nothing:\n" + done.stdout + done.stderr)
    assert "test_does_not_inherit_the_probe" in done.stdout


def test_the_probe_name_is_not_something_the_fleet_reads():
    """The generated files set a variable in THIS process's children only, but
    a name that collided with a real one would make the reader assert against
    live configuration instead of the leak."""
    assert PROBE.startswith("EUNOMIA_ENV_LEAK_")
    root = Path(__file__).resolve().parent.parent
    hits = [p for p in (root / "bin").iterdir()
            if p.is_file() and PROBE in p.read_text(encoding="utf-8", errors="ignore")]
    assert hits == [], hits
