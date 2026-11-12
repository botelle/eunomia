"""The owner's one page per protected PR, when everything else holds (plan 0214).

Same fake forge and fixtures as test_fleet_merge.py: only check 4 (protected
paths) changes behaviour here, so the doubles are reused rather than restated.
"""
import pytest

from test_fleet_merge import HEAD, OWN, PLAN, REPO, env, findings, merge, put_findings, run  # noqa: F401


def protected(env, path="bin/fleet-merge"):
    env.files.append({"filename": path})


def test_protected_pr_with_everything_else_holding_pages_once(env):
    protected(env)
    out = run(env)
    assert env.merges == []
    assert "check 4 protected paths" in out
    assert len(env.pages) == 1
    title, body = env.pages[0]
    assert title == "Ready for your merge"
    assert f"{REPO}#7 (plan {PLAN}) at {HEAD[:12]}: touches bin/fleet-merge." in body
    assert "Every other check holds." in body
    assert f"{merge.FORGEJO_URL}/{REPO}/pulls/7" in body


def test_second_pass_at_the_same_head_sends_nothing(env):
    protected(env)
    run(env)
    assert len(env.pages) == 1
    run(env)
    assert len(env.pages) == 1


def test_a_new_head_pages_again(env):
    protected(env)
    run(env)
    assert len(env.pages) == 1

    new_head = "c" * 40
    env.pr["head"] = {"sha": new_head}
    env.contents[(OWN, new_head)] = env.contents[(OWN, HEAD)]
    for r in env.reviews:
        r["commit_id"] = new_head
    put_findings(env.tmp, findings(head=new_head), head=new_head)
    run(env)
    assert len(env.pages) == 2
    assert new_head[:12] in env.pages[1][1]


@pytest.mark.parametrize("breakage", ["reviews", "ci", "findings", "mergeable"])
def test_protected_pr_with_another_check_failing_does_not_page(env, breakage):
    protected(env)
    if breakage == "reviews":
        env.reviews = env.reviews[:1]
        check = "6 reviews"
    elif breakage == "ci":
        env.statuses[1] = dict(env.statuses[1], status="failure")
        check = "8 ci"
    elif breakage == "findings":
        for p in (env.tmp / "work").rglob("findings.json"):
            p.unlink()
        check = "9 integration tests"
    else:
        env.pr["mergeable"] = False
        check = "10 mergeable"
    out = run(env)
    assert env.merges == [] and env.pages == []
    assert f"check {check}" in out


def test_protected_pr_failing_plan_diff_is_refused_at_check3_not_paged(env):
    protected(env)
    env.contents[(OWN, HEAD)] = env.contents[(OWN, HEAD)].replace("done", "draft")
    out = run(env)
    assert env.merges == [] and env.pages == []
    assert "check 3 plan diff" in out


def test_failed_delivery_leaves_no_marker_and_the_next_pass_pages(env, monkeypatch):
    protected(env)
    monkeypatch.setattr(merge.watch, "notify", lambda *a, **k: False)
    run(env)
    assert env.pages == []
    monkeypatch.setattr(merge.watch, "notify",
                        lambda title, body, label=None: env.pages.append((title, body)) or True)
    run(env)
    assert len(env.pages) == 1


def test_protected_pr_is_never_merged(env):
    protected(env)
    run(env)
    assert env.merges == []
    assert env.writes == 0
