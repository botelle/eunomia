"""fleet-orphans — a branch with work and no pull request is named daily (plan 0020).

The fake forge is keyed on `Fake.__call__(method, path, token=None)`, the
same shape `fleet-watch`'s and `fleet-repo`'s tests use. Since plan 0057
migrated this tool onto `fleetforge.Forge`, that dispatcher is wrapped by
`_ForgeDouble` — the same seam plan 0051 introduced for `fleet-watch`
(`tests/test_fleet_watch._ForgeDouble`) — which reconstructs the exact
(method, path) each `Forge` verb would put on the wire and forwards it to the
Fake, so the Fake's own routing table needed no changes; only the injection
point (`_wire`, below) moved from `om._api` to `om._forge`.
"""
import importlib.machinery
import importlib.util
import json
import re
import urllib.parse
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def om():
    return _load("fleet_orphans", BIN / "fleet-orphans")


# --- a small, self-consistent fake Forgejo -----------------------------------


def _blank_repo(default_branch):
    return {
        "default_branch": default_branch,
        "default_status": 200,
        "branches": [],                 # list of PAGES (each a list of branch dicts)
        "pulls": [],                    # list of PAGES
        "pulls_fail": False,
        "branches_fail": False,
        "commits": {},                  # sha -> list of PAGES
        "commits_fail_sha": set(),      # shas whose commit walk 500s
        "commits_path": {},             # (path, sha) -> list of PAGES
        "commits_path_fail": set(),     # (path, sha) that 500 instead of paging
        "contents": {},                 # (path, ref) -> blob sha string, or absent
        "contents_fail": set(),         # (path, ref) that 500 instead of 200/404
    }


class Fake:
    def __init__(self):
        self.calls = []
        self.repos = {}
        self.search_pages = None        # list of PAGES of repo-search entries

    def repo(self, full_name, default_branch="main"):
        self.repos[full_name] = _blank_repo(default_branch)
        return self.repos[full_name]

    def __call__(self, method, path, token=None):
        self.calls.append((method, path))
        assert method == "GET", "fleet-orphans must issue GET only"
        assert token == "tok"
        url = urllib.parse.urlsplit(path)
        qs = urllib.parse.parse_qs(url.query)
        page = int(qs.get("page", ["1"])[0])

        if url.path == "/repos/search":
            if self.search_pages is None:
                return 200, {"data": []}
            return 200, {"data": self._page(self.search_pages, page)}

        m = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", url.path)
        if not m:
            return 404, None
        repo = f"{m.group(1)}/{m.group(2)}"
        rest = m.group(3) or ""
        fx = self.repos.get(repo)
        if fx is None:
            return 404, None

        if rest == "":
            return fx["default_status"], (
                {"default_branch": fx["default_branch"]}
                if fx["default_status"] == 200 else None)

        if rest == "/branches":
            if fx["branches_fail"]:
                return 500, None
            return 200, self._page(fx["branches"], page)

        if rest == "/pulls":
            if fx["pulls_fail"]:
                return 500, None
            return 200, self._page(fx["pulls"], page)

        if rest == "/commits":
            sha = qs.get("sha", [None])[0]
            path_param = qs.get("path", [None])[0]
            if path_param:
                p = urllib.parse.unquote(path_param)
                if (p, sha) in fx["commits_path_fail"]:
                    return 500, None
                return 200, self._page(fx["commits_path"].get((p, sha), []), page)
            if sha in fx["commits_fail_sha"]:
                return 500, None
            return 200, self._page(fx["commits"].get(sha, []), page)

        if rest.startswith("/contents/"):
            filepath = urllib.parse.unquote(rest[len("/contents/"):])
            ref = qs.get("ref", [None])[0]
            if (filepath, ref) in fx["contents_fail"]:
                return 500, None
            blob = fx["contents"].get((filepath, ref))
            if blob is None:
                return 404, None
            return 200, {"sha": blob}

        return 404, None

    @staticmethod
    def _page(pages, page):
        return pages[page - 1] if 0 < page <= len(pages) else []


_READ_VERBS = {"search_repos", "get_repo", "list_branches", "list_pulls",
              "list_commits", "get_contents"}


class _ForgeDouble:
    """`fleetforge.Forge`-shaped double over a `Fake`'s existing
    `(method, path, token=None)` dispatcher — the same seam plan 0051
    introduced for `fleet-watch` (`tests/test_fleet_watch._ForgeDouble`).
    Each verb below reconstructs the exact (method, path) the real
    `fleetforge.Forge` verb would put on the wire, so `Fake`'s routing table
    above needed no changes for this migration; only the injection point
    (`_wire`) moved from `om._api` to `om._forge`.

    `verbs_used` records which of the six verbs fleet-orphans actually calls
    (plan 0057 D1) — `_ForgeDouble` implements only those, so a stray call to
    a write verb (`post_status`, `create_issue_comment`, `edit_pull_body`)
    raises `AttributeError` here rather than silently reaching a real forge."""

    def __init__(self, dispatch, token=None):
        self._dispatch = dispatch
        self.token = token
        self.verbs_used = []

    def _call(self, verb, path):
        self.verbs_used.append(verb)
        return self._dispatch("GET", path, token=self.token)

    def search_repos(self, page=1, limit=50):
        return self._call("search_repos", f"/repos/search?limit={limit}&page={page}")

    def get_repo(self, repo):
        return self._call("get_repo", f"/repos/{repo}")

    def list_branches(self, repo, page=1, limit=50):
        return self._call("list_branches",
                          f"/repos/{repo}/branches?limit={limit}&page={page}")

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        q = f"state={state}&page={page}&limit={limit}"
        if sort:
            q += f"&sort={sort}"
        return self._call("list_pulls", f"/repos/{repo}/pulls?{q}")

    def list_commits(self, repo, sha=None, path=None, page=1, limit=50):
        q = [f"limit={limit}"]
        if sha:
            q.append(f"sha={urllib.parse.quote(sha, safe='')}")
        if path:
            q.append(f"path={urllib.parse.quote(path)}")
        q.append(f"page={page}")
        return self._call("list_commits", f"/repos/{repo}/commits?{'&'.join(q)}")

    def get_contents(self, repo, path, ref=None):
        q = f"?ref={urllib.parse.quote(ref, safe='')}" if ref else ""
        return self._call("get_contents",
                          f"/repos/{repo}/contents/{urllib.parse.quote(path)}{q}")


def _c(sha, created, files=None, parents=None):
    c = {"sha": sha, "created": created, "files": files or []}
    if parents is not None:
        c["parents"] = [{"sha": p} for p in parents]
    return c


def _br(name, sha):
    return {"name": name, "commit": {"id": sha}}


def _pr(state, head_repo, head_ref, merged=False, closed_at=None):
    return {"state": state, "merged": merged, "closed_at": closed_at,
           "head": {"ref": head_ref, "repo": {"full_name": head_repo}}}


OLD = "2020-01-01T00:00:00Z"
OLD2 = "2020-01-02T00:00:00Z"


def _wire(om, monkeypatch, fake, tmp_path):
    """Wires the fake forge/token/state dir, and points the DEFAULT config
    path (fleet-repo's own `CONF` global, which `read_conf(None)` falls back
    to) at a file listing exactly the fake's repos — the real
    config/repos.conf must never leak into a test that calls `om.main([])`
    with no `--conf`.

    `om._forge` is the builder `bin/fleet-orphans`'s `run()` calls with the
    fetched token (plan 0057 D4, mirroring `fleet-watch`'s `_forge`/`_wire`
    seam) — one `_ForgeDouble` instance is reused across a run, its bound
    `token` refreshed on every `_forge(token)` call. `fake.forge_double` is
    exposed so a test can read `.verbs_used` afterward."""
    forge = _ForgeDouble(fake)
    fake.forge_double = forge

    def _forge_builder(token):
        forge.token = token
        return forge

    monkeypatch.setattr(om, "_forge", _forge_builder)
    monkeypatch.setattr(om, "_token", lambda: ("tok", ""))
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / ".fleet"))
    monkeypatch.delenv("FLEET_NTFY_URL", raising=False)
    conf = tmp_path / "_auto_repos.conf"
    conf.write_text("".join(f"repo: {r} dispatch\n" for r in fake.repos))
    monkeypatch.setattr(om.repo_mod, "CONF", conf)
    return conf


def _fork(fx, branch_sha, base_sha, base_created, branch_files=None):
    """Wire the common shape: branch forked from base_sha, one extra commit."""
    fx["commits"][branch_sha] = [[
        _c(branch_sha, OLD2, files=branch_files), _c(base_sha, OLD, files=[]),
    ]]


# --- classification ----------------------------------------------------------


def test_orphan_differing_blob_and_old_tip(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", OLD2, files=[{"filename": "plans/0001-foo.md", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("plans/0001-foo.md", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("plans/0001-foo.md", "c1")] = "blobB"
    fx["contents"][("plans/0001-foo.md", "m1")] = "blobM"
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_ORPHANS
    assert "operator/x feat/thing orphan c1" in out
    assert "documents=plans/0001-foo.md" in out


def test_stale_when_every_blob_equal(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", OLD2, files=[{"filename": "plans/0001-foo.md", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("plans/0001-foo.md", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("plans/0001-foo.md", "c1")] = "blobSame"
    fx["contents"][("plans/0001-foo.md", "m1")] = "blobSame"
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_OK
    assert "operator/x feat/thing stale c1" in out

    # printed on a SECOND run too -- stale is never deduped/suppressed
    rc2 = om.main([])
    out2 = capsys.readouterr().out
    assert rc2 == om.EXIT_OK
    assert "operator/x feat/thing stale c1" in out2


def test_offset_timestamp_is_parsed_so_a_true_orphan_is_reported(
        om, monkeypatch, tmp_path, capsys):
    """Forgejo serialises timestamps in the SERVER's offset, not always a
    trailing Z (fleet-watch's `_ts_utc` docstring). A Z-only strptime parses
    every commit date as None, which reads every tip as ageless and
    silently downgrades every real orphan to quiet (r2 finding)."""
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", "2020-01-02T00:00:00-04:00",
           files=[{"filename": "plans/0001-foo.md", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("plans/0001-foo.md", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("plans/0001-foo.md", "c1")] = "blobB"
    fx["contents"][("plans/0001-foo.md", "m1")] = "blobM"
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_ORPHANS
    assert "operator/x feat/thing orphan c1" in out


def test_young_tip_not_listed(om, monkeypatch, tmp_path, capsys):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", now, files=[{"filename": "plans/0001-foo.md", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("plans/0001-foo.md", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("plans/0001-foo.md", "c1")] = "blobB"
    fx["contents"][("plans/0001-foo.md", "m1")] = "blobM"
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_OK
    assert "feat/thing" not in out


def test_open_pr_covers_the_branch(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", OLD2, files=[{"filename": "x.py", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("x.py", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("x.py", "c1")] = "blobB"
    fx["contents"][("x.py", "m1")] = "blobM"
    fx["pulls"] = [[_pr("open", "operator/x", "feat/thing")]]
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_OK
    assert "feat/thing" not in out


def test_closed_unmerged_pr_with_no_later_push_covers(om, monkeypatch, tmp_path):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", OLD2, files=[{"filename": "x.py", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("x.py", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("x.py", "c1")] = "blobB"
    fx["contents"][("x.py", "m1")] = "blobM"
    fx["pulls"] = [[_pr("closed", "operator/x", "feat/thing", merged=False,
                        closed_at="2021-01-01T00:00:00Z")]]
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    assert rc == om.EXIT_OK


def test_closed_unmerged_pr_with_later_push_does_not_cover(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    # c1 is AFTER the PR's closed_at
    fx["commits"]["feat/thing"] = [[
        _c("c1", "2022-01-01T00:00:00Z", files=[{"filename": "x.py", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("x.py", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("x.py", "c1")] = "blobB"
    fx["contents"][("x.py", "m1")] = "blobM"
    fx["pulls"] = [[_pr("closed", "operator/x", "feat/thing", merged=False,
                        closed_at="2021-01-01T00:00:00Z")]]
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_ORPHANS
    assert "feat/thing orphan" in out


def test_merged_pr_never_deleted_is_stale_not_silent(om, monkeypatch, tmp_path, capsys):
    """The shape this fleet produces most: a PR merged, branch left behind."""
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", OLD2, files=[{"filename": "x.py", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("x.py", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("x.py", "c1")] = "blobSame"
    fx["contents"][("x.py", "m1")] = "blobSame"
    fx["pulls"] = [[_pr("closed", "operator/x", "feat/thing", merged=True,
                        closed_at="2020-06-01T00:00:00Z")]]
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_OK
    assert "feat/thing stale" in out


def test_commits_after_merge_is_orphan_once_old(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c2")]]
    # c2 is a NEW commit after the merged PR's tip c1
    fx["commits"]["feat/thing"] = [[
        _c("c2", OLD2, files=[{"filename": "y.py", "status": "added"}]),
        _c("c1", OLD, files=[{"filename": "x.py", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("y.py", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("y.py", "c2")] = "blobY"
    fx["contents"][("y.py", "m1")] = None
    fx["pulls"] = [[_pr("closed", "operator/x", "feat/thing", merged=True,
                        closed_at="2019-06-01T00:00:00Z")]]
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_ORPHANS
    assert "feat/thing orphan" in out


def test_branch_that_merged_main_back_in_is_not_falsely_stale(
        om, monkeypatch, tmp_path, capsys):
    """A branch that merges the default branch back into itself pulls the
    default branch's own commits into its ancestry. `branch_commits` is
    date-ordered, not topological, and the default branch keeps moving
    while the branch's own commits sit still -- so the merge-base (main's
    own commit) can sort ahead of the branch's own, earlier commit in that
    list. Stopping at the merge-base's first LIST POSITION then breaks
    right after the (typically fileless) merge commit itself, before ever
    reaching the branch's real commit: an empty touched-path set, so the
    branch was reported `stale` while its content never reached main (r2
    finding). Walking each commit's own `parents` from the tip fixes it."""
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "x1")]]
    fx["commits"]["feat/thing"] = [[
        _c("x1", "2020-01-04T00:00:00Z", files=[], parents=["b1", "m1"]),
        _c("m1", "2020-01-03T00:00:00Z", files=[], parents=["a1"]),
        _c("b1", "2020-01-02T00:00:00Z",
           files=[{"filename": "plans/orphan.md", "status": "added"}],
           parents=["a1"]),
        _c("a1", OLD, files=[], parents=[]),
    ]]
    fx["commits"]["main"] = [[
        _c("m1", "2020-01-03T00:00:00Z", files=[]),
        _c("a1", OLD, files=[]),
    ]]
    fx["contents"][("plans/orphan.md", "x1")] = "blobP"
    # main never touched plans/orphan.md -- no commits_path entry for it
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_ORPHANS
    assert "operator/x feat/thing orphan x1" in out
    assert "documents=plans/orphan.md" in out


def test_deleted_path_lands_when_absent_on_both_sides(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", OLD2, files=[{"filename": "gone.py", "status": "removed"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    # absent at branch tip (deleted) AND absent on main -> landed
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_OK
    assert "feat/thing stale" in out


def test_unrelated_histories_is_exit2_compare(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[_c("c1", OLD2), _c("z9", OLD)]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    err = capsys.readouterr().err
    assert rc == om.EXIT_UNKNOWN
    assert "compare" in err


# --- pagination ----------------------------------------------------------------


def test_pagination_reads_to_a_confirmed_empty_page(om, monkeypatch, tmp_path, capsys):
    """A fixture on two pages; a tool that reads one page misses page two."""
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [
        [_br("main", "m1"), _br("feat/a", "ca")],
        [_br("feat/b", "cb")],
    ]
    for branch, sha in (("feat/a", "ca"), ("feat/b", "cb")):
        fx["commits"][branch] = [[_c(sha, OLD2), _c("m1", OLD)]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert "feat/a" in out and "feat/b" in out


def test_paginate_does_not_stop_on_a_short_page(om):
    """The property plan 0020 §3's boundary protects, stated directly: a
    stubbed forge returning a SHORT page (fewer than `PAGE_SIZE` items)
    followed by a NON-EMPTY page must not end the scan. `fleetforge.paged()`
    would stop right there (its `EXHAUSTED` rule is "a page shorter than
    page_size ends the scan") — which is exactly why `_paginate` is a local
    loop rather than a call to `paged()` (plan 0057 D3, docs/forge.md). This
    must fail if `_paginate` is ever rewritten to use `paged()` or to infer
    "the end" from page length instead of an empty page."""
    pages = {1: [{"n": 1}], 2: [{"n": 2}, {"n": 3}], 3: []}

    def page_fn(n):
        return 200, pages.get(n, [])

    assert om._paginate(page_fn) == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_commit_pages_does_not_stop_on_a_short_page(om):
    """Same property as test_paginate_does_not_stop_on_a_short_page, for the
    other local pagination loop `_commit_pages` (commits, not branches/pulls)
    — a stubbed forge returning a short page followed by a non-empty one
    must still reach the second page."""
    pages = {1: [{"sha": "a"}], 2: [{"sha": "b"}, {"sha": "c"}], 3: []}

    class _StubForge:
        def list_commits(self, repo, sha=None, path=None, page=1, limit=50):
            return 200, pages.get(page, [])

    out = list(om._commit_pages("owner/repo", _StubForge(), sha="deadbeef"))
    assert out == [[{"sha": "a"}], [{"sha": "b"}, {"sha": "c"}]]


def test_a_short_page_still_costs_a_confirming_request(om, monkeypatch, tmp_path):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1")]]     # one short page; no other branches
    _wire(om, monkeypatch, fake, tmp_path)
    om.main([])
    branch_calls = [c for c in fake.calls if "/branches" in c[1]]
    assert len(branch_calls) == 2, "must fetch the confirming empty page too"


def test_three_pages_where_the_second_is_a_full_page(om, monkeypatch, tmp_path):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1")], [_br(f"feat/{i}", f"c{i}") for i in range(2)], []]
    for i in range(2):
        fx["commits"][f"feat/{i}"] = [[_c(f"c{i}", OLD2), _c("m1", OLD)]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    _wire(om, monkeypatch, fake, tmp_path)
    om.main([])
    branch_calls = [c for c in fake.calls if "/branches" in c[1]]
    assert len(branch_calls) == 3


def test_a_pulls_page_that_fails_to_parse_is_exit2_and_prints_nothing(
        om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["pulls_fail"] = True
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_UNKNOWN
    assert "feat/thing" not in out


def test_a_branches_page_that_fails_prints_no_classification(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches_fail"] = True
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_UNKNOWN
    assert "operator/x " not in out.replace("scanning 1 repositor", "")


def test_default_branch_unreadable_is_exit2(om, monkeypatch, tmp_path):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["default_status"] = 500
    _wire(om, monkeypatch, fake, tmp_path)
    assert om.main([]) == om.EXIT_UNKNOWN


# --- unprintable names ---------------------------------------------------------


def test_unprintable_branch_name_is_reported_and_not_classified(
        om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("../etc/passwd", "cbad")]]
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_UNKNOWN
    assert om.UNPRINTABLE in out
    assert not any("/commits" in c[1] and "cbad" in c[1] for c in fake.calls)
    assert not any("../etc/passwd" in c[1] for c in fake.calls)


def test_a_shell_metachar_branch_name_is_unprintable(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/`rm -rf`", "cbad")]]
    _wire(om, monkeypatch, fake, tmp_path)

    om.main([])
    out = capsys.readouterr().out
    assert om.UNPRINTABLE in out


def test_a_repo_name_that_fails_validation_is_skipped_under_all(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fake.search_pages = [[{"full_name": "operator/ok"}, {"full_name": "Operator/Bad"}]]
    fx = fake.repo("operator/ok")
    fx["branches"] = [[_br("main", "m1")]]
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main(["--all"])
    err = capsys.readouterr().err
    assert rc == om.EXIT_UNKNOWN
    assert "search" in err
    assert not any("Operator/Bad" in c[1] for c in fake.calls)


# --- config ----------------------------------------------------------------


def test_conf_row_rejected_is_exit2_reason_conf(om, monkeypatch, tmp_path, capsys):
    conf = tmp_path / "repos.conf"
    conf.write_text("repo: operator/ok dispatch\nnonsense line\n")
    fake = Fake()
    fx = fake.repo("operator/ok")
    fx["branches"] = [[_br("main", "m1")]]
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main(["--conf", str(conf)])
    err = capsys.readouterr().err
    assert rc == om.EXIT_UNKNOWN
    assert "conf" in err


def test_empty_conf_reports_zero_not_clean(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    _wire(om, monkeypatch, fake, tmp_path)
    rc = om.main(["--conf", str(tmp_path / "nope.conf")])
    out = capsys.readouterr().out
    assert rc == om.EXIT_OK
    assert "scanning 0 repositor" in out


# --- --all / visibility -------------------------------------------------------


def test_visibility_loss_under_all_is_exit2(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fake.search_pages = [[{"full_name": "operator/x"}, {"full_name": "operator/y"}]]
    fake.repo("operator/x")["branches"] = [[_br("main", "m1")]]
    fake.repo("operator/y")["branches"] = [[_br("main", "m1")]]
    _wire(om, monkeypatch, fake, tmp_path)
    assert om.main(["--all"]) == om.EXIT_OK

    fake2 = Fake()
    fake2.search_pages = [[{"full_name": "operator/x"}]]     # operator/y vanished
    fake2.repo("operator/x")["branches"] = [[_br("main", "m1")]]
    _wire(om, monkeypatch, fake2, tmp_path)
    rc = om.main(["--all"])
    err = capsys.readouterr().err
    assert rc == om.EXIT_UNKNOWN
    assert "visibility" in err and "operator/y" in err


# --- dedupe / paging -----------------------------------------------------------


def test_paging_dedupes_on_repo_and_sha_then_pages_on_a_new_tip(
        om, monkeypatch, tmp_path, capsys):
    def make_fake(tip_sha):
        fake = Fake()
        fx = fake.repo("operator/x")
        fx["branches"] = [[_br("main", "m1"), _br("feat/thing", tip_sha)]]
        fx["commits"]["feat/thing"] = [[
            _c(tip_sha, OLD2, files=[{"filename": "x.py", "status": "modified"}]),
            _c("m1", OLD, files=[]),
        ]]
        fx["commits"]["main"] = [[_c("m1", OLD)]]
        fx["commits_path"][("x.py", "main")] = [[_c("m1", OLD)]]
        fx["contents"][("x.py", tip_sha)] = "blobB"
        fx["contents"][("x.py", "m1")] = "blobM"
        return fake

    fake1 = make_fake("c1")
    _wire(om, monkeypatch, fake1, tmp_path)
    om.main([])
    first = capsys.readouterr().err
    assert "NOTIFY" in first and "c1" in first

    fake1b = make_fake("c1")
    _wire(om, monkeypatch, fake1b, tmp_path)
    om.main([])
    second = capsys.readouterr().err
    assert "NOTIFY" not in second, "a rescan of a known tip must be silent"

    fake2 = make_fake("c2")
    _wire(om, monkeypatch, fake2, tmp_path)
    om.main([])
    third = capsys.readouterr().err
    assert "NOTIFY" in third and "c2" in third, "a new tip is a new finding"


def test_exit2_reason_pages_once_per_day(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["default_status"] = 500
    _wire(om, monkeypatch, fake, tmp_path)

    om.main([])
    first = capsys.readouterr().err
    assert "NOTIFY" in first

    fake2 = Fake()
    fx2 = fake2.repo("operator/x")
    fx2["default_status"] = 500
    _wire(om, monkeypatch, fake2, tmp_path)
    om.main([])
    second = capsys.readouterr().err
    assert "NOTIFY" not in second, "same reason, same day: silent"


def test_unprintable_branch_finding_dedupes_and_stores_the_marker(
        om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("../bad", "cbad")]]
    _wire(om, monkeypatch, fake, tmp_path)
    om.main([])
    state = json.loads((tmp_path / ".fleet" / "orphans.state.json").read_text())
    assert any(f["branch"] == om.UNPRINTABLE and f["sha"] == "cbad"
              for f in state["findings"])


# --- --dry-run -----------------------------------------------------------------


def test_dry_run_sends_and_writes_nothing_but_reports_the_real_exit_code(
        om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", OLD2, files=[{"filename": "x.py", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("x.py", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("x.py", "c1")] = "blobB"
    fx["contents"][("x.py", "m1")] = "blobM"
    _wire(om, monkeypatch, fake, tmp_path)

    state_path = tmp_path / ".fleet" / "orphans.state.json"
    assert not state_path.exists()
    rc = om.main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == om.EXIT_ORPHANS
    assert "feat/thing orphan" in out
    assert not state_path.exists(), "a dry run must write no state"

    # and a REAL run afterwards still pages -- the dry run consumed nothing
    fake2 = Fake()
    fx2 = fake2.repo("operator/x")
    fx2["branches"] = fx["branches"]
    fx2["commits"] = fx["commits"]
    fx2["commits_path"] = fx["commits_path"]
    fx2["contents"] = fx["contents"]
    _wire(om, monkeypatch, fake2, tmp_path)
    om.main([])
    err = capsys.readouterr().err
    assert "NOTIFY" in err


def test_dry_run_state_byte_identical_when_state_preexists(om, monkeypatch, tmp_path):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1")]]
    _wire(om, monkeypatch, fake, tmp_path)
    om.main([])                                     # create real state
    state_path = tmp_path / ".fleet" / "orphans.state.json"
    before = state_path.read_bytes()

    om.main(["--dry-run"])
    assert state_path.read_bytes() == before


# --- state file shape -----------------------------------------------------------


def test_state_file_holds_only_the_three_collections(om, monkeypatch, tmp_path):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1")]]
    _wire(om, monkeypatch, fake, tmp_path)
    om.main([])
    state = json.loads((tmp_path / ".fleet" / "orphans.state.json").read_text())
    assert set(state) == {"findings", "visible", "exit2"}


# --- PR-map correctness --------------------------------------------------------


def test_fork_pr_with_same_head_ref_does_not_mask(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", OLD2, files=[{"filename": "x.py", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("x.py", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("x.py", "c1")] = "blobB"
    fx["contents"][("x.py", "m1")] = "blobM"
    fx["pulls"] = [[_pr("open", "someone-else/x", "feat/thing")]]   # a FORK's PR
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_ORPHANS
    assert "feat/thing orphan" in out


def test_null_head_repo_masks_nothing(om, monkeypatch, tmp_path, capsys):
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", OLD2, files=[{"filename": "x.py", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("x.py", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("x.py", "c1")] = "blobB"
    fx["contents"][("x.py", "m1")] = "blobM"
    fx["pulls"] = [[{"state": "open", "merged": False, "closed_at": None,
                     "head": {"ref": "feat/thing", "repo": None}}]]
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_ORPHANS
    assert "feat/thing orphan" in out


def test_historical_but_not_tip_blob_match_is_stale(om, monkeypatch, tmp_path, capsys):
    """A branch whose blob equals an OLDER main blob of the path, not the tip's."""
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m2"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", OLD2, files=[{"filename": "x.py", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    # main advanced past the merge-base: m2 (tip, different blob) -> m1 (merge-base)
    fx["commits"]["main"] = [[_c("m2", "2020-02-01T00:00:00Z"), _c("m1", OLD)]]
    fx["commits_path"][("x.py", "main")] = [[
        _c("m2", "2020-02-01T00:00:00Z"), _c("m1", OLD)]]
    fx["contents"][("x.py", "c1")] = "blobM"          # matches the OLDER commit
    fx["contents"][("x.py", "m2")] = "blobOther"
    fx["contents"][("x.py", "m1")] = "blobM"
    _wire(om, monkeypatch, fake, tmp_path)

    rc = om.main([])
    out = capsys.readouterr().out
    assert rc == om.EXIT_OK
    assert "feat/thing stale" in out


# --- GET-only ------------------------------------------------------------------


def test_every_request_is_a_get(om, monkeypatch, tmp_path):
    """The pre-migration `_api` seam asserted this itself, on every call
    (`Fake.__call__`'s own `assert method == "GET"`) — still true here since
    `_ForgeDouble` hardcodes "GET" into every one of the six verbs it
    forwards. See test_every_forge_call_is_a_read_verb below for the
    verb-level half of this property that the raw HTTP method can no longer
    carry once the seam moved from `om._api` to `om._forge` (plan 0057 D4)."""
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", OLD2, files=[{"filename": "x.py", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("x.py", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("x.py", "c1")] = "blobB"
    fx["contents"][("x.py", "m1")] = "blobM"
    _wire(om, monkeypatch, fake, tmp_path)
    om.main([])
    assert fake.calls
    assert all(m == "GET" for m, _ in fake.calls)


def test_every_forge_call_is_a_read_verb(om, monkeypatch, tmp_path):
    """fleet-orphans holds a write-capable PAT (module docstring) but never
    exercises that scope: `_ForgeDouble` implements only the six read verbs
    it calls, so a stray write call would `AttributeError` in this fixture
    rather than silently reach a real forge in production. This is the
    verb-level half of the GET-only property `test_every_request_is_a_get`
    checks at the wire-method level — the two used to be the same assertion
    before the seam moved from `om._api` to `om._forge` (plan 0057 D4)."""
    fake = Fake()
    fx = fake.repo("operator/x")
    fx["branches"] = [[_br("main", "m1"), _br("feat/thing", "c1")]]
    fx["commits"]["feat/thing"] = [[
        _c("c1", OLD2, files=[{"filename": "x.py", "status": "modified"}]),
        _c("m1", OLD, files=[]),
    ]]
    fx["commits"]["main"] = [[_c("m1", OLD)]]
    fx["commits_path"][("x.py", "main")] = [[_c("m1", OLD)]]
    fx["contents"][("x.py", "c1")] = "blobB"
    fx["contents"][("x.py", "m1")] = "blobM"
    _wire(om, monkeypatch, fake, tmp_path)
    om.main([])
    assert fake.forge_double.verbs_used
    assert set(fake.forge_double.verbs_used) <= _READ_VERBS
