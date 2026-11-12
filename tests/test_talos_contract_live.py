"""The calls talos makes into eunomia, run as programs against this checkout.

Plan 0097 built it; plan 0117 narrowed the question it asks. talos publishes
`contracts/eunomia-calls.json`; CI clones talos's `main` and points
EUNOMIA_TALOS_CONTRACT at it. Each call is exec'd from this checkout's bin/ --
never imported -- because only an exec fails where talos fails: mode, shebang,
interpreter, argv parsing, exit code. The calls are read from the contract and
never restated here.

THE QUESTION is "did THIS CHANGE break a call talos makes?", not "does this
commit satisfy talos?". They differ whenever talos is ahead of eunomia. On
2026-09-29 talos `67fad31` added `fleet-config-autonomy`; eunomia gained the
`autonomy` setting 18 hours later (#713). For that window every eunomia push
failed with `'autonomy' is not a known setting`, whatever it changed, and seven
PRs stayed red for six days on a failure none of them caused. A re-run could
not clear it.

So a case is judged like this:
  * head exits 0 but the `accept` rule rejects the output -> FAIL, always. The
    change provides the call and provides it wrongly (the PR that first
    implements a talos call). The baseline is not consulted.
  * head exits non-zero -> run the same argv against the BASELINE, the commit
    the change started from (`git merge-base main $GITHUB_SHA`, or `$GITHUB_SHA^1`
    on `main` itself), against the baseline's OWN database seeded by the
    baseline's own CLIs:
      - the baseline cannot answer either (non-zero, or the program does not
        exist there) -> PASS with a `::warning::`; this change did not cause it;
      - the baseline answers -> FAIL; this change broke it;
      - the baseline cannot be resolved, cloned, checked out or seeded -> FAIL,
        naming the step (no evidence the failure predates the change).
A case that passes at head never touches the baseline.

WHAT IT NO LONGER CATCHES: a call `main` already fails. Once main cannot
answer an entry, every later push has a failing baseline and stays green with
the warning. That is the price of not blaming every branch for one bad merge;
catching a broken `main` belongs to the running-pin probe (plan 0095).

Without EUNOMIA_TALOS_CONTRACT the module skips, so a local pytest stays
offline. With it, a contract that cannot be trusted (wrong `v`, no calls, an
`accept` rule this file does not implement) fails rather than passes."""
import atexit
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
REPO = "operator/eunomia"
CONTRACT = os.environ.get("EUNOMIA_TALOS_CONTRACT")


def _list_first_ordinal_1(out, entry):
    data = json.loads(out)
    assert isinstance(data, list) and data, f"expected a non-empty JSON list, got {out[:100]!r}"
    assert data[0]["ordinal"] == 1, f"[0]['ordinal'] is {data[0].get('ordinal')!r}, not 1"


def _object_value_in(out, entry):
    data = json.loads(out)
    assert isinstance(data, dict), f"expected a JSON object, got {out[:100]!r}"
    values = entry["accept"].get("values", entry.get("values"))
    assert values, "the entry names no `values`"
    assert data.get("value") in values, f"value {data.get('value')!r} not in {values}"


def _credits_decision(out, entry):
    """talos plan 0029's rule: `fleet-credits decide` answers one of three bills,
    with a budget in (0, 200] for `credits` and null otherwise."""
    data = json.loads(out)
    assert isinstance(data, dict), f"expected a JSON object, got {out[:100]!r}"
    bill, budget = data.get("bill"), data.get("budget_usd")
    assert bill in ("subscription", "credits", "hold"), f"bill {bill!r} is not a known bill"
    if bill == "credits":
        assert (isinstance(budget, (int, float)) and not isinstance(budget, bool)
                and 0 < budget <= 200), f"budget_usd {budget!r} is not a number in (0, 200]"
    else:
        assert budget is None, f"budget_usd is {budget!r}, not null, for {bill!r}"


def _exit_zero(out, entry):
    """talos plan 0029's rule for `fleet-credits record`: a write whose stdout is not
    part of the contract. `judge` has already required exit 0, so there is nothing
    further to check. The rule is named `exit-zero` in talos's
    contracts/eunomia-calls.json (operator/talos#175, 2ddb17e)."""
    return None


def _style_route(out, entry):
    """talos plan 0031's rule for `fleet-style route`: the position, the review it
    names and a list of reasons (plan 0150 D7)."""
    data = json.loads(out)
    assert isinstance(data, dict), f"expected a JSON object, got {out[:100]!r}"
    assert data.get("position") in ("style_reviewer", "code_reviewer"), \
        f"position {data.get('position')!r} is not a known reviewer position"
    assert data.get("review") in ("style", "full"), f"review {data.get('review')!r} is unknown"
    assert isinstance(data.get("reasons"), list), "`reasons` is not a list"


def _evidence_verdict(out, entry):
    """talos plan 0031's rule for `fleet-style verify`: a boolean `ok` and a list
    of `problems` (plan 0150 D7)."""
    data = json.loads(out)
    assert isinstance(data, dict), f"expected a JSON object, got {out[:100]!r}"
    assert isinstance(data.get("ok"), bool), f"`ok` is {data.get('ok')!r}, not a boolean"
    assert isinstance(data.get("problems"), list), "`problems` is not a list"


RULES = {
    "json-style-route": _style_route,
    "json-evidence-verdict": _evidence_verdict,
    "exit-zero": _exit_zero,
    "json-list-first-ordinal-1": _list_first_ordinal_1,
    "json-object-value-in": _object_value_in,
    "json-credits-decision": _credits_decision,
}

# talos contract v2 (plan 0029): named placeholders beyond {repo} and {position}, filled
# with fixed values. The calls run against a temporary FLEET_DB and write nothing that
# matters; `record` with no reservation still writes its row (window_id NULL).
# talos contract v3 (talos plan 0034, 6c38522): adds the lane_reviewer position to
# fleet-models-reviewers, which this plan (0163) gives fleet-models. talos#205 took
# the position back out of the probed list until 0163 is pinned; v stays 3.
# talos contract v4 (talos 3218185, eunomia plan 0178): fleet-models-reviewers may
# carry an optional `access` key (repo | change). json-list-first-ordinal-1 reads
# only `ordinal`, so nothing here changes but the version.
SUPPORTED_VERSIONS = (1, 2, 3, 4)
PLACEHOLDERS = {"{pr}": "1", "{model}": "claude-opus-5-5", "{usd}": "0.01",
                "{session}": "contract-check"}


_fixture = {}


def _style_fixture():
    """The files the `fleet-style` calls need, built once per process: a temporary
    git repository with a base and a head commit, a plan that says `review: style`,
    and the forge-shaped change list. Nothing is declared for REPO, so route answers
    `code_reviewer` and verify answers `ok: false`: both are decisions, exit 0."""
    if not _fixture:
        d = Path(tempfile.mkdtemp(prefix="talos-contract-style-"))
        atexit.register(shutil.rmtree, d, ignore_errors=True)
        repo = d / "repo"
        repo.mkdir()
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.org"],
                     ["config", "user.name", "t"]):
            _git(*args, cwd=repo)
        (repo / "a.txt").write_text("a")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "base", cwd=repo)
        base = _git("rev-parse", "HEAD", cwd=repo)
        (repo / "a.txt").write_text("b")
        _git("commit", "-q", "-am", "head", cwd=repo)
        plan = d / "plan.md"
        plan.write_text(f"---\nid: 0001-contract-check\nstatus: ready\nrepo: {REPO}\n"
                        f"review: style\n---\n")
        changes = d / "changes.json"
        changes.write_text(json.dumps([{"filename": "a.txt", "status": "modified"}]))
        _fixture.update({"{plan_file}": str(plan), "{changes_file}": str(changes),
                         "{git_dir}": str(repo / ".git"), "{base}": base,
                         "{head}": _git("rev-parse", "HEAD", cwd=repo),
                         "{plan_id}": "0001-contract-check"})
    return _fixture


def _rule_name(entry):
    accept = entry.get("accept")
    return accept.get("rule") if isinstance(accept, dict) else accept


def _load(path=None):
    contract = json.loads(Path(path or CONTRACT).read_text())
    if contract.get("v") not in SUPPORTED_VERSIONS:
        raise AssertionError(
            f"contract v is {contract.get('v')!r}; this test implements v {', '.join(map(str, SUPPORTED_VERSIONS))}")
    calls = contract.get("calls")
    if not calls:
        raise AssertionError("the contract has no calls; a job that checks nothing is not green")
    cases = []
    for entry in calls:
        if _rule_name(entry) not in RULES:
            raise AssertionError(
                f"{entry.get('id')}: accept rule {_rule_name(entry)!r} is not implemented here")
        for position in entry.get("positions") or [None]:
            cases.append((entry, position))
    return cases


def _cases():
    if not CONTRACT:
        return []
    try:
        return _load()
    except Exception as e:  # surfaced as one failing test, not a collection error
        return [pytest.param(None, None, id=f"contract-unusable: {e}")]


def _render(entry, position):
    out = []
    for a in entry["argv"]:
        a = a.replace("{repo}", REPO).replace("{position}", position or "")
        for name, value in PLACEHOLDERS.items():
            a = a.replace(name, value)
        if "{" in a:
            for name, value in _style_fixture().items():
                a = a.replace(name, value)
        out.append(a)
    return out


class BaselineError(Exception):
    """The baseline could not be had; the message names the step that failed."""


def _git(*args, cwd=None):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def choose_baseline(clone, head_sha):
    """The commit the change started from: the merge-base with main, or the
    first parent when the change IS main's tip (a push to main, or a branch
    with nothing of its own)."""
    base = _git("merge-base", "main", head_sha, cwd=clone)
    if base == _git("rev-parse", head_sha, cwd=clone):
        base = _git("rev-parse", f"{head_sha}^1", cwd=clone)
    return base


class Baseline:
    """A checkout of the baseline commit with its own seeded database, built
    lazily and at most once per run -- success or failure. Nothing is touched
    unless a case fails at head."""

    def __init__(self, source, head_sha, workdir, seed_runner):
        self.source, self.head_sha = source, head_sha
        self.workdir, self.seed_runner = Path(workdir), seed_runner
        self.clones = 0
        self._result = None

    def get(self):
        """(sha, bin dir, env) or BaselineError naming the failed step."""
        if self._result is None:
            try:
                self._result = self._build()
            except BaselineError as e:
                self._result = e
        if isinstance(self._result, BaselineError):
            raise self._result
        return self._result

    def _step(self, name, fn):
        try:
            return fn()
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            raise BaselineError(f"{name}: {e}") from e

    def _build(self):
        if not self.head_sha:
            raise BaselineError("determine: GITHUB_SHA is unset")
        if not self.source:
            raise BaselineError("clone: no eunomia source (FORGEJO_REPOS_ROOT is unset)")
        clone = self.workdir / "eunomia-baseline"
        self.clones += 1
        # full history: merge-base needs it. clone is read-only against the source.
        self._step("clone", lambda: _git("clone", "--branch", "main", self.source, str(clone)))
        sha = self._step("determine", lambda: choose_baseline(clone, self.head_sha))
        self._step("clone", lambda: _git("remote", "remove", "origin", cwd=clone))
        self._step("checkout", lambda: _git("checkout", "--detach", sha, cwd=clone))
        db = self.workdir / "baseline-fleet.db"
        env = dict(os.environ, FLEET_DB=str(db))
        if not self.seed_runner:
            raise BaselineError("seed: SEED_RUNNER is unset")
        for cmd in (["fleet-db-migrate"],
                    ["fleet-models", "set", "default", "code_reviewer", self.seed_runner],
                    ["fleet-models", "set", "default", "plan_reviewer", self.seed_runner]):
            r = self._step("seed", lambda: subprocess.run(
                [str(clone / "bin" / cmd[0]), *cmd[1:]], env=env,
                capture_output=True, text=True, timeout=60))
            if r.returncode != 0:
                raise BaselineError(f"seed: {' '.join(cmd[:2])} exit={r.returncode} "
                                    f"stderr={r.stderr[:200]!r}")
        # A baseline that predates plan 0150 does not know `style_reviewer` and refuses
        # the row. That refusal is not a failed baseline: the call that needs the row
        # then fails there too, which is what makes it tolerable (plan 0150 D7).
        subprocess.run([str(clone / "bin" / "fleet-models"), "set", "default",
                        "style_reviewer", self.seed_runner], env=env,
                       capture_output=True, text=True, timeout=60)
        return sha, clone / "bin", env


def _exec(bin_dir, entry, position, env):
    """Exec the program as talos would. A missing program or a timeout is a
    non-zero answer, not an error: it is a call that cannot be answered."""
    argv = [str(Path(bin_dir) / entry["program"])] + _render(entry, position)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
    except FileNotFoundError:
        return argv, subprocess.CompletedProcess(argv, 127, "", "program does not exist")
    except subprocess.TimeoutExpired:
        return argv, subprocess.CompletedProcess(argv, 124, "", "timed out")
    return argv, r


def _accepts(out, entry):
    try:
        RULES[_rule_name(entry)](out, entry)
    except (AssertionError, ValueError, KeyError, TypeError) as e:
        return str(e) or type(e).__name__
    return None


def judge(entry, position, head_bin, baseline, talos_sha="unknown"):
    """Pass (None), tolerate (the warning line), or raise AssertionError."""
    argv, r = _exec(head_bin, entry, position, os.environ)
    where = (f"{entry['id']}: argv={argv[1:]} exit={r.returncode} "
             f"stderr={r.stderr[:300]!r}")
    if r.returncode == 0:
        # wrong answer from a program that answers: never tolerated.
        why = _accepts(r.stdout, entry)
        assert why is None, f"{where}: {why}"
        return None
    try:
        sha, base_bin, env = baseline.get()
    except BaselineError as e:
        raise AssertionError(f"{where}: baseline unavailable, failing closed ({e})") from e
    _, b = _exec(base_bin, entry, position, env)
    if b.returncode == 0 and _accepts(b.stdout, entry) is None:
        raise AssertionError(f"{where}: baseline {sha} answers this call; this change broke it")
    return (f"::warning::talos-contract: {entry['id']} fails at baseline {sha} as well "
            f"(talos {talos_sha}); this change did not cause it. Either talos is ahead of "
            f"eunomia, or eunomia main already fails this call.")


def _talos_sha():
    try:
        return _git("rev-parse", "HEAD", cwd=Path(CONTRACT).parent)
    except Exception:
        return "unknown"


CASES = _cases()
_ran = []
_tolerated = []


@pytest.fixture(scope="module", autouse=True)
def _seed_style_reviewer():
    """`ci.yml` seeds only `code_reviewer` and `plan_reviewer`, so without this a
    `style_reviewer` call would exit 3 at head and at baseline and pass with a
    warning, never tested. Only against a database CI named: never the live one."""
    seed, db = os.environ.get("SEED_RUNNER"), os.environ.get("FLEET_DB")
    if CONTRACT and seed and db:
        r = subprocess.run([str(BIN / "fleet-models"), "set", "default", "style_reviewer", seed],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"seeding style_reviewer failed: {r.stderr[:200]!r}"
        # plan 0163: so talos's `reviewers {repo} lane_reviewer` call is exercised,
        # not tolerated as exit 3.
        r = subprocess.run([str(BIN / "fleet-models"), "set", "default", "lane_reviewer", "1", seed],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"seeding lane_reviewer failed: {r.stderr[:200]!r}"


@pytest.fixture(scope="module")
def baseline():
    root = os.environ.get("FORGEJO_REPOS_ROOT")
    with tempfile.TemporaryDirectory(prefix="talos-contract-baseline-") as d:
        yield Baseline(f"file://{root}/operator/eunomia.git" if root else None,
                       os.environ.get("GITHUB_SHA"), d, os.environ.get("SEED_RUNNER"))


@pytest.mark.skipif(not CONTRACT, reason="EUNOMIA_TALOS_CONTRACT unset: offline run")
@pytest.mark.parametrize("entry,position", CASES,
                         ids=lambda v: v.get("id") if isinstance(v, dict) else str(v))
def test_talos_call(entry, position, baseline, capsys):
    if entry is None:
        pytest.fail("the contract cannot be trusted (see the test id)")
    _ran.append(1)
    warning = judge(entry, position, BIN, baseline, _talos_sha())
    # a passing test's output is captured; the runner needs the line at column 0.
    with capsys.disabled():
        if warning:
            _tolerated.append(entry["id"])
            print(f"\n{warning}", flush=True)
        if len(_ran) == len(CASES) and _tolerated:
            print(f"\ntalos-contract: {len(_tolerated)} of {len(CASES)} calls tolerated "
                  f"(also failing at baseline): {', '.join(_tolerated)}", flush=True)


# ---- offline: this file's own reading of a v1 and a v2 contract (plan 0146 D4a) -------


def _contract_file(tmp_path, v, calls):
    p = tmp_path / "eunomia-calls.json"
    p.write_text(json.dumps({"v": v, "calls": calls}))
    return p


_V1_CALL = {"id": "fleet-models-list", "program": "fleet-models",
            "argv": ["list", "{repo}", "--json"], "positions": ["code_reviewer"],
            "accept": {"rule": "json-list-first-ordinal-1"}}
_DECIDE = {"id": "fleet-credits-decide", "program": "fleet-credits",
           "argv": ["decide", "--peek", "--repo", "{repo}", "--pr", "{pr}", "--model",
                    "{model}", "--json"], "accept": {"rule": "json-credits-decision"}}
_RECORD = {"id": "fleet-credits-record", "program": "fleet-credits",
           "argv": ["record", "--repo", "{repo}", "--pr", "{pr}", "--model", "{model}",
                    "--usd", "{usd}", "--session", "{session}"],
           "accept": {"rule": "exit-zero"}}


def test_a_v1_file_loads_unchanged(tmp_path):
    cases = _load(_contract_file(tmp_path, 1, [_V1_CALL]))
    assert [(e["id"], pos) for e, pos in cases] == [("fleet-models-list", "code_reviewer")]
    assert _render(_V1_CALL, "code_reviewer") == ["list", REPO, "--json"]


def test_an_unknown_version_is_refused(tmp_path):
    with pytest.raises(AssertionError, match="v 1, 2, 3, 4"):
        _load(_contract_file(tmp_path, 5, [_V1_CALL]))


# talos's v4 entry for fleet-models-reviewers, verbatim from contracts/eunomia-calls.json
# (trimmed to the key under test).
_REVIEWERS_V4 = {"id": "fleet-models-reviewers", "program": "fleet-models",
                 "argv": ["reviewers", "{repo}", "{position}", "--json"],
                 "positions": ["plan_reviewer", "code_reviewer", "style_reviewer", "lane_reviewer"],
                 "accept": {"rule": "json-list-first-ordinal-1",
                            "optional_keys": {"access": "string: repo | change (eunomia plan 0178; "
                                                        "absent means repo)"}},
                 "source": "resolve_reviewers"}
_ACCESS_DOMAIN = {"repo", "change"}


def test_a_v4_reviewers_entry_loads_for_every_position(tmp_path):
    cases = _load(_contract_file(tmp_path, 4, [_REVIEWERS_V4]))
    assert [(e["id"], pos) for e, pos in cases] == \
        [("fleet-models-reviewers", pos) for pos in _REVIEWERS_V4["positions"]]


def test_the_access_eunomia_emits_stays_in_talos_v4_domain(tmp_path):
    """The real `fleet-models reviewers` against a fresh database seeded as ci.yml does:
    every record's `access` is one of talos's declared values, and the output is accepted."""
    env = dict(os.environ, FLEET_DB=str(tmp_path / "fleet.db"), EUNOMIA_FLEET_DIR=str(tmp_path))
    for cmd in (["fleet-db-migrate"], ["fleet-models", "set", "default", "code_reviewer", "sub-fable"]):
        r = subprocess.run([str(BIN / cmd[0]), *cmd[1:]], env=env, capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, f"seed {cmd[:2]}: {r.stderr[:200]!r}"
    _, r = _exec(BIN, _REVIEWERS_V4, "code_reviewer", env)
    assert r.returncode == 0, r.stderr[:300]
    assert _accepts(r.stdout, _REVIEWERS_V4) is None
    records = json.loads(r.stdout)
    assert {rec["access"] for rec in records} <= _ACCESS_DOMAIN


def test_a_v2_file_runs_the_two_fleet_credits_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    cases = _load(_contract_file(tmp_path, 2, [_DECIDE, _RECORD]))
    assert [e["id"] for e, _ in cases] == ["fleet-credits-decide", "fleet-credits-record"]
    assert _render(_RECORD, None)[-8:] == ["--pr", "1", "--model", "claude-opus-5-5",
                                           "--usd", "0.01", "--session", "contract-check"]
    decide, record = (e for e, _ in cases)
    assert judge(decide, None, BIN, None) is None
    assert judge(record, None, BIN, None) is None


@pytest.mark.parametrize("out,ok", [
    ('{"bill":"credits","budget_usd":12.5}', True),
    ('{"bill":"subscription","budget_usd":null}', True),
    ('{"bill":"hold","budget_usd":null}', True),
    ('{"bill":"credits","budget_usd":0}', False),
    ('{"bill":"credits","budget_usd":200.01}', False),
    ('{"bill":"credits","budget_usd":null}', False),
    ('{"bill":"hold","budget_usd":5}', False),
    ('{"bill":"free","budget_usd":null}', False),
])
def test_the_credits_decision_rule(out, ok):
    assert (_accepts(out, _DECIDE) is None) is ok



# ---- plan 0150 D7: the two fleet-style rules and their placeholders -------------------

_STYLE_ROUTE = {"id": "fleet-style-route", "program": "fleet-style",
                "argv": ["route", "{repo}", "--pr", "{pr}", "--plan-file", "{plan_file}",
                         "--changes", "{changes_file}", "--git-dir", "{git_dir}", "--base",
                         "{base}", "--head", "{head}", "--override", "none", "--json"],
                "accept": {"rule": "json-style-route"}}
_STYLE_VERIFY = {"id": "fleet-style-verify", "program": "fleet-style",
                 "argv": ["verify", "{repo}", "--pr", "{pr}", "--plan-id", "{plan_id}",
                          "--git-dir", "{git_dir}", "--base", "{base}", "--head", "{head}",
                          "--json"],
                 "accept": {"rule": "json-evidence-verdict"}}


@pytest.mark.parametrize("out,ok", [
    ('{"position":"style_reviewer","review":"style","reasons":[]}', True),
    ('{"position":"code_reviewer","review":"full","reasons":["x"]}', True),
    ('{"position":"tester","review":"full","reasons":[]}', False),
    ('{"position":"code_reviewer","review":"light","reasons":[]}', False),
    ('{"position":"code_reviewer","review":"full","reasons":"x"}', False),
    ('{"position":"code_reviewer","review":"full"}', False),
    ('[]', False),
])
def test_the_style_route_rule(out, ok):
    assert (_accepts(out, _STYLE_ROUTE) is None) is ok


@pytest.mark.parametrize("out,ok", [
    ('{"ok":true,"problems":[]}', True),
    ('{"ok":false,"problems":["a"]}', True),
    ('{"ok":"yes","problems":[]}', False),
    ('{"ok":false,"problems":"a"}', False),
    ('{"ok":false}', False),
])
def test_the_evidence_verdict_rule(out, ok):
    assert (_accepts(out, _STYLE_VERIFY) is None) is ok


def test_a_v2_file_carries_the_two_style_entries_and_runs_them(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    cases = _load(_contract_file(tmp_path, 2, [_STYLE_ROUTE, _STYLE_VERIFY]))
    assert [e["id"] for e, _ in cases] == ["fleet-style-route", "fleet-style-verify"]
    rendered = _render(_STYLE_ROUTE, None)
    assert "{" not in " ".join(rendered)
    assert Path(rendered[rendered.index("--plan-file") + 1]).read_text().count("review: style") == 1
    route, verify = (e for e, _ in cases)
    assert judge(route, None, BIN, None) is None
    assert judge(verify, None, BIN, None) is None
