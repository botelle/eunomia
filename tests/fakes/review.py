#!/usr/bin/env python3
"""A scripted stand-in for the review dispatcher (plan 0099 D2).

Run as `FLEET_REVIEW_CMD`, so the orchestrator invokes it with the real argv
(`<repo> <pr> --post --no-followups`), and mopsus invokes it with
`--probe-limit --model <m>` or `<repo> <pr> --post`.

Env:
  FAKE_REVIEW_SCRIPT  JSON list of actions, one per review round, in order.
                      An entry `{"action": "probe", "code": N}` is NOT a round:
                      it answers `--probe-limit`.
  FAKE_FORGE_URL      the fake forge (tests/fakes/forge.py)
  FAKE_REVIEW_TOKEN   the reviewer account's token on that forge
  FLEET_DB            where `quota` writes its `review_attempt` row

Sidecars, next to the script: `<script>.state` (rounds taken, heads seen) and
`<script>.calls` (one JSON argv per invocation, for a row to assert on).

THE VERDICT GOES TO THE FORGE. What this prints means nothing, and `post_nothing`
prints `VERDICT: APPROVE` to prove it: a fake that approved on stdout would
reproduce the 2026-09-07 failure inside the test that exists to catch it.

An exhausted script exits 98 and an unknown action 99, both loudly. A fake that
quietly approved when it ran out of script would let a loop that reviews one
round too many pass.
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

# Talos's `MACHINE-READABLE FINDINGS` section. The orchestrator reads only the
# fenced block, so the heading is the human-facing half; tests/test_review_
# scenarios.py round-trips every body this builds through the orchestrator's own
# `_FENCE` and `_SEV_LINE`, so this cannot drift from the parser unnoticed.
FINDINGS_HEADING = "MACHINE-READABLE FINDINGS"


def findings_body(findings, prose="Review of the change."):
    """A review body carrying `findings`, a list of (severity, text) pairs."""
    lines = [f"{sev}: {text}" for sev, text in findings]
    return "\n".join([prose, "", FINDINGS_HEADING, "```findings", *lines, "```", ""])


def _api(method, path, body=None):
    req = urllib.request.Request(
        os.environ["FAKE_FORGE_URL"].rstrip("/") + "/api/v1" + path,
        data=None if body is None else json.dumps(body).encode(), method=method)
    req.add_header("Authorization", "token " + os.environ["FAKE_REVIEW_TOKEN"])
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def _head(repo, pr):
    return _api("GET", f"/repos/{repo}/pulls/{pr}")["head"]["sha"]


def _post_review(repo, pr, event, body, commit_id):
    _api("POST", f"/repos/{repo}/pulls/{pr}/reviews",
         {"event": event, "body": body, "commit_id": commit_id})


def _quota_row(repo, pr, head):
    """The row talos writes when a review stops at a usage limit, with the
    columns mopsus's `_HELD_QUERY` reads. Nothing in eunomia writes this table."""
    path = os.environ["FLEET_DB"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    con = sqlite3.connect(path)
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS review_attempt (
              repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT,
              model TEXT, outcome TEXT NOT NULL, session_uuid TEXT,
              started_at TEXT, stopped_at TEXT, ordinal INTEGER, position TEXT)""")
        con.execute(
            "INSERT INTO review_attempt (repo, pr, head_sha, model, outcome, "
            "session_uuid, started_at, stopped_at, ordinal, position) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (repo, int(pr), head, "claude-opus-5-5", "quota", "fake-session",
             now, now, 1, "code_reviewer"))
        con.commit()
    finally:
        con.close()


def _load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def main(argv):
    script_path = os.environ["FAKE_REVIEW_SCRIPT"]
    script = _load(script_path, [])
    calls = script_path + ".calls"
    with open(calls, "a") as fh:
        fh.write(json.dumps(argv) + "\n")

    if "--probe-limit" in argv:
        probes = [a for a in script if a.get("action") == "probe"]
        if not probes:
            sys.stderr.write("fake reviewer: --probe-limit with no `probe` scripted\n")
            return 99
        return int(probes[0].get("code", 0))

    rounds = [a for a in script if a.get("action") != "probe"]
    state_path = script_path + ".state"
    state = _load(state_path, {"rounds": 0, "heads": []})
    if state["rounds"] >= len(rounds):
        sys.stderr.write(f"fake reviewer: round {state['rounds'] + 1} but only "
                         f"{len(rounds)} scripted\n")
        return 98
    step = rounds[state["rounds"]]
    state["rounds"] += 1

    positional = [a for a in argv if not a.startswith("--")]
    repo, pr = positional[0], positional[1]
    head = _head(repo, pr)
    earlier = [h for h in state["heads"] if h != head]
    state["heads"].append(head)
    with open(state_path, "w") as fh:
        json.dump(state, fh)

    action = step.get("action")
    findings = [tuple(f) for f in step.get("findings", [])]
    if action == "approve":
        _post_review(repo, pr, "APPROVED", "APPROVED. No blocking findings.", head)
    elif action == "comment":
        _post_review(repo, pr, "COMMENT", findings_body(findings), head)
    elif action == "request_changes":
        _post_review(repo, pr, "REQUEST_CHANGES", findings_body(findings), head)
    elif action == "unparseable":
        _post_review(repo, pr, "COMMENT",
                     "This looks mostly fine, though I have some reservations "
                     "about the approach that I would like to discuss.", head)
    elif action == "post_nothing":
        print("VERDICT: APPROVE")
    elif action == "stale":
        # An approval of the tree BEFORE the last push. Before any push existed
        # there is no earlier head, so it names one no commit has.
        _post_review(repo, pr, "APPROVED", "APPROVED (of an earlier tree).",
                     earlier[-1] if earlier else "0" * 40)
    elif action == "exit":
        sys.stderr.write(step.get("stderr", "") + "\n")
        return int(step.get("code", 1))
    elif action == "quota":
        _quota_row(repo, pr, head)
        sys.stderr.write("usage limit reached; try again later\n")
        return 75
    else:
        sys.stderr.write(f"fake reviewer: unknown action {action!r}\n")
        return 99
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
