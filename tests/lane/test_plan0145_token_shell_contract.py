"""Blackbox checks using the declared fleet-deps/fleet-merge executables."""

import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
PROGRAMS = [
    ("fleet-deps", "FLEET_DEPS_TOKEN_CMD"),
    ("fleet-merge", "FLEET_MERGE_TOKEN_CMD"),
]


def isolated_env(tmp_path, token_key, command):
    home = tmp_path / "home"
    home.mkdir()
    return {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.defpath]),
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "LANG": "C",
        "FLEET_FORGEJO_URL": "http://127.0.0.1:1",
        token_key: command,
    }


def invoke(program, arguments, env, tmp_path):
    return subprocess.run(
        [str(ROOT / "bin" / program), *arguments],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.covers("D3")
@pytest.mark.parametrize("program,token_key", PROGRAMS)
def test_token_command_supports_arguments_and_shell_redirection(
    tmp_path, program, token_key
):
    # Redirect the output so execution is observable without supplying a token.
    # The declared empty-token refusal keeps this test before forge mutations.
    marker = tmp_path / "helper output.txt"
    command = "printf '%s' 'tok' > " + shlex.quote(str(marker))
    result = invoke(
        program, ["once"], isolated_env(tmp_path, token_key, command), tmp_path
    )
    assert marker.is_file(), (
        f"Configured shell command was not executed: rc={result.returncode}, "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    assert marker.read_text() == "tok"
    assert result.returncode == 2, (
        "A helper producing no token must refuse the run", result.stdout, result.stderr
    )
    if program == "fleet-deps":
        assert result.stdout == ""


@pytest.mark.covers()
@pytest.mark.parametrize("program,token_key", PROGRAMS)
@pytest.mark.parametrize("arguments", [[], ["not-a-declared-subcommand"]])
def test_missing_or_unknown_subcommand_exits_two(
    tmp_path, program, token_key, arguments
):
    result = invoke(
        program, arguments, isolated_env(tmp_path, token_key, "printf ''"), tmp_path
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
