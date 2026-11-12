"""Tests for `eunomia/plan-lint`'s git-trees-and-blobs read (plan 0216).

`tests/test_fleet_deps.py`'s plan-lint tests already cover the decide-logic
end to end with the git-trees-based `FakeForge`. This file pins the
properties plan 0216 exists for specifically: the read cost stays a small
constant regardless of how many plans a repository has, the blob cache is
keyed by content (never by path), and it is bounded and self-healing.
"""
import base64
import hashlib
import importlib.machinery
import importlib.util
import os
import shutil
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


REPO = "operator/x"
ROOT_SHA = "deadbeef-head"


def _sha(text):
    return "b" + hashlib.sha1((text or "").encode()).hexdigest()[:16]


class TreeForge:
    """A forge double serving `plans/` and `governance.yaml` through the git
    trees/blobs API only. `list_contents` and `get_contents` — the slow
    contents-API path this plan replaces — raise if called at all, which is
    itself the property `test_the_slow_contents_listing_is_never_called`
    checks for."""

    def __init__(self, plans, governance=None, governance_status=None,
                 root_sha=ROOT_SHA, truncated_root=False, truncated_plans=False,
                 root_status=200, plans_status=200, blob_status=None):
        self.plans = dict(plans)                # {"name.md": text}
        self.governance = governance
        self.governance_status = governance_status
        self.root_sha = root_sha
        self.truncated_root = truncated_root
        self.truncated_plans = truncated_plans
        self.root_status = root_status
        self.plans_status = plans_status
        self.blob_status = dict(blob_status or {})     # sha -> forced status
        self.tree_reads = []
        self.blob_reads = []

    def list_contents(self, *a, **k):
        raise AssertionError("list_contents must never be called")

    def get_contents(self, *a, **k):
        raise AssertionError("get_contents must never be called")

    def get_git_tree(self, repo, sha):
        self.tree_reads.append(sha)
        if sha == "plans-tree":
            if self.plans_status != 200:
                return self.plans_status, None
            entries = [{"path": name, "type": "blob", "sha": _sha(text)}
                       for name, text in self.plans.items()]
            return 200, {"tree": entries, "truncated": self.truncated_plans}
        if sha == self.root_sha:
            if self.root_status != 200:
                return self.root_status, None
            entries = []
            if self.plans is not None:
                entries.append({"path": "plans", "type": "tree", "sha": "plans-tree"})
            if self.governance is not None or self.governance_status is not None:
                gsha = "gov" if self.governance_status is None else "gov-bad"
                entries.append({"path": "governance.yaml", "type": "blob", "sha": gsha})
            return 200, {"tree": entries, "truncated": self.truncated_root}
        return 404, None

    def get_git_blob(self, repo, sha):
        self.blob_reads.append(sha)
        if sha in self.blob_status:
            return self.blob_status[sha], None
        if sha == "gov-bad":
            return self.governance_status, None
        if sha == "gov":
            return 200, {"content": base64.b64encode(self.governance.encode()).decode()}
        for text in self.plans.values():
            if sha == _sha(text):
                return 200, {"content": base64.b64encode(text.encode()).decode()}
        return 404, None


def _load_deps(tmp_path):
    return _load("fleet_deps_tree_test", BIN / "fleet-deps")


@pytest.fixture
def deps(tmp_path, monkeypatch):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    return _load_deps(tmp_path)


def test_the_slow_contents_listing_is_never_called(deps):
    forge = TreeForge({"0001-a.md": "text-a", "0002-b.md": "text-b"})
    plans_dir, err = deps._read_plan_dir(forge, REPO, ROOT_SHA)
    assert err is None
    assert sorted(p.name for p in plans_dir.glob("*.md")) == ["0001-a.md", "0002-b.md"]
    assert (plans_dir / "0001-a.md").read_text() == "text-a"


def _two_hundred_plans():
    return {f"{i:04d}-plan.md": f"text-{i:04d}-v1" for i in range(200)}


def test_three_tree_reads_and_one_blob_read_on_a_warm_cache(deps):
    plans = _two_hundred_plans()
    forge = TreeForge(plans)
    # Warm the cache for every plan except one.
    stale_name = "0100-plan.md"
    for name, text in plans.items():
        if name != stale_name:
            deps._blob_cache_write(_sha(text), text)
    plans[stale_name] = "text-0100-v2"           # the one cold blob
    forge = TreeForge(plans)

    plans_dir, err = deps._read_plan_dir(forge, REPO, ROOT_SHA)
    assert err is None
    assert len(forge.tree_reads) == 3
    assert forge.blob_reads == [_sha("text-0100-v2")]
    assert (plans_dir / stale_name).read_text() == "text-0100-v2"


def test_two_hundred_blob_reads_on_a_cold_cache(deps):
    plans = _two_hundred_plans()
    forge = TreeForge(plans)
    plans_dir, err = deps._read_plan_dir(forge, REPO, ROOT_SHA)
    assert err is None
    assert len(forge.tree_reads) == 3
    assert len(forge.blob_reads) == 200


def test_a_changed_blob_sha_for_the_same_path_is_read_again_not_stale(deps):
    forge1 = TreeForge({"0001-a.md": "first text"})
    d1, err1 = deps._read_plan_dir(forge1, REPO, ROOT_SHA)
    assert err1 is None
    assert (d1 / "0001-a.md").read_text() == "first text"

    forge2 = TreeForge({"0001-a.md": "second text"})
    d2, err2 = deps._read_plan_dir(forge2, REPO, ROOT_SHA)
    assert err2 is None
    assert (d2 / "0001-a.md").read_text() == "second text"


def test_a_failed_root_tree_read_is_pending_naming_the_call(deps):
    forge = TreeForge({"0001-a.md": "x"}, root_status=500)
    plans_dir, err = deps._read_plan_dir(forge, REPO, ROOT_SHA)
    assert plans_dir is None
    assert "root tree" in err and "500" in err


def test_a_failed_plans_tree_read_is_pending_naming_the_call(deps):
    forge = TreeForge({"0001-a.md": "x"}, plans_status=503)
    plans_dir, err = deps._read_plan_dir(forge, REPO, ROOT_SHA)
    assert plans_dir is None
    assert "plans/ tree" in err and "503" in err


def test_a_truncated_tree_is_pending(deps):
    forge = TreeForge({"0001-a.md": "x"}, truncated_plans=True)
    plans_dir, err = deps._read_plan_dir(forge, REPO, ROOT_SHA)
    assert plans_dir is None
    assert "plans/ tree" in err


def test_a_failed_blob_read_is_pending_naming_the_path(deps):
    sha = _sha("x")
    forge = TreeForge({"0001-a.md": "x"}, blob_status={sha: 502})
    plans_dir, err = deps._read_plan_dir(forge, REPO, ROOT_SHA)
    assert plans_dir is None
    assert "plans/0001-a.md" in err and "502" in err


def test_no_plans_directory_is_an_empty_dir_not_an_error(deps):
    forge = TreeForge({})
    forge.plans = None                      # no `plans` entry in the root tree at all
    plans_dir, err = deps._read_plan_dir(forge, REPO, ROOT_SHA)
    assert err is None
    assert list(plans_dir.glob("*.md")) == []


def test_governance_yaml_absent(deps):
    forge = TreeForge({"0001-a.md": "x"})
    plans_dir, err = deps._read_plan_dir(forge, REPO, ROOT_SHA)
    assert err is None
    assert not (plans_dir.parent / "governance.yaml").exists()


def test_governance_yaml_present(deps):
    forge = TreeForge({"0001-a.md": "x"}, governance="spec_watch: []\n")
    plans_dir, err = deps._read_plan_dir(forge, REPO, ROOT_SHA)
    assert err is None
    assert (plans_dir.parent / "governance.yaml").read_text() == "spec_watch: []\n"


def test_governance_yaml_unreadable_is_pending(deps):
    forge = TreeForge({"0001-a.md": "x"}, governance_status=500)
    plans_dir, err = deps._read_plan_dir(forge, REPO, ROOT_SHA)
    assert plans_dir is None
    assert "governance.yaml" in err and "500" in err


# ------------------------------------------------------------- the blob cache
def test_cache_prune_keeps_a_blob_read_today_and_removes_one_untouched_for_31_days(deps):
    deps._blob_cache_write("fresh-sha", "fresh text")
    deps._blob_cache_write("stale-sha", "stale text")
    old = time.time() - 31 * 24 * 3600
    stale_path = deps._blob_cache_path("stale-sha")
    os.utime(stale_path, (old, old))

    deps._prune_blob_cache()

    assert deps._blob_cache_path("fresh-sha").exists()
    assert not stale_path.exists()


def test_cache_read_bumps_mtime_so_a_hit_is_never_pruned_as_stale(deps):
    deps._blob_cache_write("sha-x", "text")
    old = time.time() - 31 * 24 * 3600
    path = deps._blob_cache_path("sha-x")
    os.utime(path, (old, old))

    assert deps._blob_cache_read("sha-x") == "text"   # a read bumps mtime to now
    deps._prune_blob_cache()
    assert path.exists()


def test_an_unreadable_cache_file_is_treated_as_a_miss(deps):
    path = deps._blob_cache_path("sha-y")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("cached")
    path.chmod(0o000)
    try:
        assert deps._blob_cache_read("sha-y") is None
    finally:
        path.chmod(0o644)

    forge = TreeForge({"0001-a.md": "fresh from the forge"})
    text, err = deps._blob_text(forge, REPO, _sha("fresh from the forge"),
                                "plans/0001-a.md")
    assert err is None
    assert text == "fresh from the forge"


def test_a_missing_cache_dir_is_created(deps):
    assert not deps._blob_cache_dir().exists()
    deps._prune_blob_cache()
    assert deps._blob_cache_dir().is_dir()


class OnceForge(TreeForge):
    """`TreeForge` plus the handful of verbs `fleet-deps.once()` needs to run
    a whole pass over one repo's open PRs — just enough to prove the prune
    runs once per PASS, not once per PR."""

    def __init__(self, prs, **kw):
        super().__init__(kw.pop("plans", {"0001-a.md": "x", "0002-b.md": "y"}), **kw)
        self.prs = prs                   # [{"number", "body", "sha", "files"}]
        self.posts = []
        self.login = "mergebot"

    def get_user(self):
        return 200, {"login": self.login}

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        if page > 1:
            return 200, []
        return 200, [{"number": p["number"], "body": p["body"],
                      "base": {"ref": "main"}, "head": {"sha": p["sha"]}}
                     for p in self.prs]

    def list_pull_files(self, repo, n, page_cap=20, limit=50):
        for p in self.prs:
            if p["number"] == n:
                return 200, [{"filename": f} for f in p.get("files", [])]
        return 200, []

    def list_statuses(self, repo, sha, page=1, limit=50):
        return 200, []

    def post_status(self, repo, sha, state, description, context):
        self.posts.append((sha, state, description, context))
        return 201, {}


def test_prune_runs_at_most_once_per_pass(tmp_path, monkeypatch):
    mod = _load_deps(tmp_path)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(mod, "_prune_blob_cache", lambda *a, **k: calls.append(1))
    fake = OnceForge([
        {"number": 1, "body": "adds a plan", "sha": "sha1", "files": ["plans/0001-a.md"]},
        {"number": 2, "body": "adds another plan", "sha": "sha2", "files": ["plans/0002-b.md"]},
    ])
    monkeypatch.setattr(mod, "_forge", lambda token: fake)
    monkeypatch.setattr(mod.watch, "_forge", lambda token: fake)
    monkeypatch.setattr(mod, "_token", lambda: ("tok", ""))
    monkeypatch.setattr(mod.watch, "conf_repos", lambda: ([REPO], [REPO]))
    monkeypatch.setattr(mod.watch, "notify", lambda *a, **k: True)

    mod.once()
    assert calls == [1]
    mod.once()
    assert calls == [1, 1]


# --------------------------------------------------------- prove the alarm
_KEY_LINE = "    key = sha"


def test_keying_the_cache_by_path_would_go_stale(tmp_path, monkeypatch):
    """Patch a scratch copy so the cache is keyed by `what` (a path) instead
    of `sha` (the blob's own content), and show that the "not stale" test
    above — run against the patched copy — fails. If this ever passes, the
    real test has stopped testing the thing it claims to."""
    scratch_bin = tmp_path / "bin"
    shutil.copytree(BIN, scratch_bin)
    patched_path = scratch_bin / "fleet-deps"
    source = patched_path.read_text(encoding="utf-8")
    assert source.count(_KEY_LINE) == 1, (
        "the cache-key line fleet-deps._blob_text uses has moved or changed; "
        "update _KEY_LINE before trusting this test")
    patched_path.write_text(source.replace(_KEY_LINE, "    key = what"),
                            encoding="utf-8")

    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / "fleet-dir"))
    mod = _load("fleet_deps_keyed_by_path", patched_path)

    forge1 = TreeForge({"0001-a.md": "first text"})
    d1, err1 = mod._read_plan_dir(forge1, REPO, ROOT_SHA)
    assert err1 is None
    assert (d1 / "0001-a.md").read_text() == "first text"

    forge2 = TreeForge({"0001-a.md": "second text"})
    d2, err2 = mod._read_plan_dir(forge2, REPO, ROOT_SHA)
    assert err2 is None
    with pytest.raises(AssertionError):
        assert (d2 / "0001-a.md").read_text() == "second text"
    # what actually happened: the stale, first-read text came back.
    assert (d2 / "0001-a.md").read_text() == "first text"
