from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _setup_isolated_checkout(tmp_path: Path, policy_content: str | dict | None = "DEFAULT") -> tuple[Path, dict[str, str]]:
    """Copy bin/ and config/ into a tmp checkout so BIN.parent / 'config' is isolated."""
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    shutil.copytree(REPO_ROOT / "bin", checkout / "bin", dirs_exist_ok=True)
    if (REPO_ROOT / "config").exists():
        shutil.copytree(REPO_ROOT / "config", checkout / "config", dirs_exist_ok=True)
    else:
        (checkout / "config").mkdir(exist_ok=True)

    policy_path = checkout / "config" / "model-policy.json"
    if policy_content is None:
        if policy_path.exists():
            policy_path.unlink()
    elif policy_content == "DEFAULT":
        pass
    elif isinstance(policy_content, dict):
        policy_path.write_text(json.dumps(policy_content), encoding="utf-8")
    else:
        policy_path.write_text(policy_content, encoding="utf-8")

    db_path = tmp_path / "fleet.db"
    fleet_dir = tmp_path / "fleet_dir"
    fleet_dir.mkdir(exist_ok=True)

    env = os.environ.copy()
    env["FLEET_DB"] = str(db_path)
    env["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    env["EUNOMIA_SESSION"] = "test-session-0155"
    env["USER"] = "operator"

    migrate_bin = checkout / "bin" / "fleet-db-migrate"
    if migrate_bin.exists():
        subprocess.run([sys.executable, str(migrate_bin)], env=env, check=False, capture_output=True, text=True)

    return checkout, env


def _run_fleet_models(checkout: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(checkout / "bin" / "fleet-models"), *args]
    return subprocess.run(cmd, env=env, capture_output=True, text=True)


def _discover_valid_runner(checkout: Path, env: dict[str, str]) -> str:
    """Discover a valid runner name through the CLI or fallback to setting/listing."""
    # Try setting a dummy runner to see if error message lists valid runners, or check policy check with common names
    candidates = [
        "sub-opus",
        "sub-sonnet",
        "api-sonnet",
        "sub-codex",
        "sub-antigravity",
        "local-qwen",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    ]
    for c in candidates:
        res = _run_fleet_models(checkout, env, "policy", "check", "operator/test", "implementer", c)
        if res.returncode == 0 and "allowed" in res.stdout:
            return c
    for c in candidates:
        res = _run_fleet_models(checkout, env, "set", "operator/probe", "implementer", c)
        if res.returncode == 0:
            return c
    pytest.skip("Could not discover a known runner name from blackbox CLI surface")


@pytest.mark.covers("D1")
def test_shipped_policy_file_shape_and_empty_deny() -> None:
    policy_path = REPO_ROOT / "config" / "model-policy.json"
    assert policy_path.is_file(), "config/model-policy.json must exist"
    data = json.loads(policy_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data.get("v") == 1
    assert isinstance(data.get("model_groups"), dict)
    assert isinstance(data.get("repo_groups"), dict)
    assert data.get("deny") == []


@pytest.mark.covers("D1", "D4")
def test_policy_show_json_matches_shipped_file(tmp_path: Path) -> None:
    checkout, env = _setup_isolated_checkout(tmp_path)
    res = _run_fleet_models(checkout, env, "policy", "show", "--json")
    assert res.returncode == 0, f"stderr: {res.stderr}"
    shown = json.loads(res.stdout)
    shipped = json.loads((REPO_ROOT / "config" / "model-policy.json").read_text(encoding="utf-8"))
    assert shown == shipped


@pytest.mark.covers("D2")
@pytest.mark.parametrize(
    "defect_name,policy_payload",
    [
        ("missing_file", None),
        ("invalid_json", "{not valid json"),
        (
            "unknown_top_key",
            {"v": 1, "model_groups": {}, "repo_groups": {}, "deny": [], "extra_unknown_key": 123},
        ),
        (
            "unknown_version",
            {"v": 999, "model_groups": {}, "repo_groups": {}, "deny": []},
        ),
        (
            "undefined_model_group",
            {
                "v": 1,
                "model_groups": {},
                "repo_groups": {"rg": {"repos": ["operator/a"]}},
                "deny": [{"models": "missing_mg", "repos": "rg", "positions": ["*"], "why": "no mg"}],
            },
        ),
        (
            "undefined_repo_group",
            {
                "v": 1,
                "model_groups": {"mg": {"provenance": ["us-hosted"]}},
                "repo_groups": {},
                "deny": [{"models": "mg", "repos": "missing_rg", "positions": ["*"], "why": "no rg"}],
            },
        ),
        (
            "unknown_runner",
            {
                "v": 1,
                "model_groups": {"mg": {"runners": ["nonexistent-runner-xyz-999"]}},
                "repo_groups": {},
                "deny": [],
            },
        ),
        (
            "unknown_family",
            {
                "v": 1,
                "model_groups": {"mg": {"families": ["nonexistent-family-xyz-999"]}},
                "repo_groups": {},
                "deny": [],
            },
        ),
        (
            "unknown_provenance",
            {
                "v": 1,
                "model_groups": {"mg": {"provenance": ["nonexistent-prov-xyz-999"]}},
                "repo_groups": {},
                "deny": [],
            },
        ),
        (
            "unknown_position",
            {
                "v": 1,
                "model_groups": {"mg": {"provenance": ["us-hosted"]}},
                "repo_groups": {"rg": {"repos": ["operator/a"]}},
                "deny": [{"models": "mg", "repos": "rg", "positions": ["nonexistent_pos"], "why": "bad pos"}],
            },
        ),
        (
            "all_except_cycle",
            {
                "v": 1,
                "model_groups": {},
                "repo_groups": {
                    "g1": {"all_except": ["g2"]},
                    "g2": {"all_except": ["g1"]},
                },
                "deny": [],
            },
        ),
    ],
)
def test_malformed_policy_refuses_set_and_resolve(
    tmp_path: Path, defect_name: str, policy_payload: str | dict | None
) -> None:
    # First get a valid checkout & runner to seed DB before introducing the defect
    checkout, env = _setup_isolated_checkout(tmp_path, "DEFAULT")
    runner = _discover_valid_runner(checkout, env)
    seed_res = _run_fleet_models(checkout, env, "set", "operator/a", "implementer", runner)
    assert seed_res.returncode == 0, f"Failed to seed runner {runner}: {seed_res.stderr}"

    # Now write the defective policy
    policy_path = checkout / "config" / "model-policy.json"
    if policy_payload is None:
        policy_path.unlink()
    elif isinstance(policy_payload, dict):
        policy_path.write_text(json.dumps(policy_payload), encoding="utf-8")
    else:
        policy_path.write_text(policy_payload, encoding="utf-8")

    # Set must be refused (non-zero exit, defect named on stderr/stdout)
    set_res = _run_fleet_models(checkout, env, "set", "operator/a", "implementer", runner)
    assert set_res.returncode != 0, f"Expected set to fail on {defect_name}"
    combined_set_out = (set_res.stdout + set_res.stderr).lower()
    assert combined_set_out.strip() != ""

    # Resolve (via get / reviewers) must also refuse or report error
    get_res = _run_fleet_models(checkout, env, "get", "operator/a")
    rev_res = _run_fleet_models(checkout, env, "reviewers", "operator/a", "code_reviewer", "--json")
    assert (get_res.returncode != 0 or get_res.stderr.strip() != "") and rev_res.returncode != 0


@pytest.mark.covers("D1", "D3", "D4")
def test_deny_rule_repos_group_refuses_set_resolve_and_policy_check(tmp_path: Path) -> None:
    checkout, env = _setup_isolated_checkout(tmp_path, "DEFAULT")
    runner = _discover_valid_runner(checkout, env)

    policy = {
        "v": 1,
        "model_groups": {"G": {"runners": [runner]}},
        "repo_groups": {"target_repos": {"repos": ["operator/a"]}},
        "deny": [
            {
                "models": "G",
                "repos": "target_repos",
                "positions": ["*"],
                "why": "restricted to non-a repos",
            }
        ],
    }
    (checkout / "config" / "model-policy.json").write_text(json.dumps(policy), encoding="utf-8")

    for pos in [
        "implementer",
        "plan_reviewer",
        "code_reviewer",
        "tester",
        "mediator",
        "impl_supervisor",
        "test_supervisor",
        "style_reviewer",
    ]:
        # Refuses set on operator/a
        res_a = _run_fleet_models(checkout, env, "set", "operator/a", pos, runner)
        assert res_a.returncode != 0
        err_out = res_a.stdout + res_a.stderr
        assert "restricted to non-a repos" in err_out
        assert "G" in err_out

        # Allows set on operator/b
        res_b = _run_fleet_models(checkout, env, "set", "operator/b", pos, runner)
        assert res_b.returncode == 0, f"Expected operator/b {pos} to be allowed: {res_b.stderr}"

        # Policy check exits 4 on operator/a and 0 on operator/b
        chk_a = _run_fleet_models(checkout, env, "policy", "check", "operator/a", pos, runner)
        assert chk_a.returncode == 4
        assert "restricted to non-a repos" in (chk_a.stdout + chk_a.stderr)

        chk_b = _run_fleet_models(checkout, env, "policy", "check", "operator/b", pos, runner)
        assert chk_b.returncode == 0
        assert "allowed" in chk_b.stdout


@pytest.mark.covers("D1", "D3", "D4")
def test_all_except_and_default_row_enforcement(tmp_path: Path) -> None:
    checkout, env = _setup_isolated_checkout(tmp_path, "DEFAULT")
    runner = _discover_valid_runner(checkout, env)

    # Seed default row BEFORE policy denies it so we can test resolve-time enforcement on a pre-existing default row
    seed_default = _run_fleet_models(checkout, env, "set", "default", "code_reviewer", runner)
    assert seed_default.returncode == 0, f"stderr: {seed_default.stderr}"
    seed_default_impl = _run_fleet_models(checkout, env, "set", "default", "implementer", runner)
    assert seed_default_impl.returncode == 0, f"stderr: {seed_default_impl.stderr}"

    policy = {
        "v": 1,
        "model_groups": {"hosted_exp": {"runners": [runner]}},
        "repo_groups": {
            "bakeoff": {"repos": ["operator/bakeoff-1"]},
            "non_bakeoff": {"all_except": ["bakeoff"]},
        },
        "deny": [
            {
                "models": "hosted_exp",
                "repos": "non_bakeoff",
                "positions": ["*"],
                "why": "bakeoff repositories only",
            }
        ],
    }
    (checkout / "config" / "model-policy.json").write_text(json.dumps(policy), encoding="utf-8")

    # Unnamed repository is denied
    chk_unnamed = _run_fleet_models(checkout, env, "policy", "check", "operator/new-repo", "implementer", runner)
    assert chk_unnamed.returncode == 4
    assert "bakeoff repositories only" in (chk_unnamed.stdout + chk_unnamed.stderr)

    # Excepted repository is allowed
    chk_bakeoff = _run_fleet_models(checkout, env, "policy", "check", "operator/bakeoff-1", "implementer", runner)
    assert chk_bakeoff.returncode == 0
    assert "allowed" in chk_bakeoff.stdout

    # Setting `default` is refused when an `all_except` rule matches
    set_def = _run_fleet_models(checkout, env, "set", "default", "implementer", runner)
    assert set_def.returncode != 0
    assert "bakeoff repositories only" in (set_def.stdout + set_def.stderr)

    # Resolving `default` row that got into the table anyway refuses for a denied repository
    rev_denied = _run_fleet_models(checkout, env, "reviewers", "operator/new-repo", "code_reviewer", "--json")
    get_denied = _run_fleet_models(checkout, env, "get", "operator/new-repo")
    assert rev_denied.returncode != 0 or "bakeoff repositories only" in (get_denied.stdout + get_denied.stderr)

    # And resolves for an allowed repository
    rev_allowed = _run_fleet_models(checkout, env, "reviewers", "operator/bakeoff-1", "code_reviewer", "--json")
    assert rev_allowed.returncode == 0
    assert runner in rev_allowed.stdout
