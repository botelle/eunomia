"""Tests for `bin/fleet-depsd serve` (plan 0181): the webhook, the recompute,
and the read/attest API, with a stubbed forge, a temporary `fleet.db` and a
server on `127.0.0.1:0`."""
import ast
import hashlib
import hmac
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load_depsd():
    loader = importlib.machinery.SourceFileLoader("fleet_depsd_serve", str(BIN / "fleet-depsd"))
    spec = importlib.util.spec_from_loader("fleet_depsd_serve", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


DEPSD = _load_depsd()


@pytest.fixture(autouse=True)
def _isolated_fleet_dir(tmp_path, monkeypatch):
    """Every path `fleetlib.fleet_dir()` resolves (on EITHER loaded copy —
    `DEPSD.lib` and `DEPSD.watch.lib` are separate SourceFileLoader instances
    of the same module) must stay inside this test's own `tmp_path`, never
    the real host's `~/dev/.fleet` — `_attestation_check` and the wake spool
    both read through it."""
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / "fleet-dir"))
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))


@pytest.fixture(autouse=True)
def _reload_depsd(_isolated_fleet_dir):
    """`DEPSD`'s module-level load above runs once, at collection — before
    `_isolated_fleet_dir` has set this test's own `EUNOMIA_FLEET_DIR`/`FLEET_DB`,
    and before any other test's monkeypatching has been torn down. Its two
    bare `os.environ.get(...)` constants (`WEBHOOK_SECRET_CMD`,
    `ATTEST_TOKEN_CMD`) and its `DB` default would otherwise be baked in from
    whatever the process environment happened to be at collection time —
    exactly the "read at import, not cleared between loads" class
    `tests/conftest.py` warns about. Reloading fresh per test, after the
    environment fixture above, matches the pattern `tests/test_fleet_watch.py`
    and `tests/test_stalled.py` already use for the same reason."""
    global DEPSD
    DEPSD = _load_depsd()


def _db(tmp_path):
    path = Path(os.environ.get("FLEET_DB") or (tmp_path / "fleet.db"))
    con = sqlite3.connect(path)
    con.executescript(DEPSD.SCHEMA)
    con.commit()
    con.close()
    return path


def _plan(id_, status="ready", depends_on=None, repo="operator/sniff"):
    return {"id": id_, "status": status, "repo": repo, "depends_on": depends_on or []}


def _plan_files(by_repo):
    """A `watch.plan_files(repo, token)` stand-in: {repo: [(path, fm), ...]}."""
    def _pf(repo, token):
        return by_repo.get(repo)
    return _pf


# ------------------------------------------------------------------ recompute

def test_an_external_hold_appears_in_the_recompute(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(DEPSD.watch, "repos", lambda: ["operator/sniff"])
    monkeypatch.setattr(DEPSD.watch, "plan_files", _plan_files(
        {"operator/sniff": [("plans/0009-a.md",
                            _plan("0009-a", depends_on=["external:cihost-caddy"]))]}))
    result = DEPSD.recompute(str(db), token_fn=lambda: ("tok", ""))
    assert result["holds"] == [
        ("operator/sniff", "0009-a", "external:cihost-caddy", "external",
         "unattested", "owner attests cihost-caddy from lynceus")]
    holds = DEPSD.api_holds(str(db))["holds"]
    assert len(holds) == 1 and holds[0]["dep"] == "external:cihost-caddy"


def test_an_attested_slug_clears_the_hold_and_writes_a_wake(tmp_path, monkeypatch):
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    now = time.time()
    con.execute("INSERT INTO dependency_hold (repo, plan, dep, form, reason, "
                "detail, created_at, updated_at) VALUES "
                "('operator/sniff','0009-a','external:cihost-caddy','external',"
                "'unattested','x',?,?)", (now, now))
    con.commit()
    con.close()
    monkeypatch.setattr(DEPSD, "wake_dir", lambda: tmp_path / "wake")
    monkeypatch.setattr(DEPSD.watch, "repos", lambda: ["operator/sniff"])
    monkeypatch.setattr(DEPSD.watch, "plan_files", _plan_files(
        {"operator/sniff": [("plans/0009-a.md", _plan("0009-a"))]}))
    result = DEPSD.recompute(str(db), token_fn=lambda: ("tok", ""))
    assert result["holds"] == []
    assert result["wakes"] == [("operator/sniff", "0009-a")]
    names = list((tmp_path / "wake").glob("*.json"))
    assert len(names) == 1
    rec = json.loads(names[0].read_text())
    assert rec["repo"] == "operator/sniff" and rec["plan"] == "0009-a"


def test_a_plan_with_two_holds_where_one_clears_writes_no_wake(tmp_path, monkeypatch):
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    now = time.time()
    for dep in ("external:a", "external:b"):
        con.execute("INSERT INTO dependency_hold (repo, plan, dep, form, "
                    "reason, detail, created_at, updated_at) VALUES "
                    "('operator/sniff','0009-a',?, 'external','unattested','x',?,?)",
                    (dep, now, now))
    con.commit()
    con.close()
    monkeypatch.setattr(DEPSD, "wake_dir", lambda: tmp_path / "wake")
    monkeypatch.setattr(DEPSD.watch, "repos", lambda: ["operator/sniff"])
    monkeypatch.setattr(DEPSD.watch, "plan_files", _plan_files(
        {"operator/sniff": [("plans/0009-a.md",
                            _plan("0009-a", depends_on=["external:a", "external:b"]))]}))
    # attest only "a"
    con = sqlite3.connect(db)
    DEPSD.record_attestation(con, "a", None, "cli:test", "test", "attest")
    con.close()
    result = DEPSD.recompute(str(db), token_fn=lambda: ("tok", ""))
    assert result["wakes"] == []
    assert len(result["holds"]) == 1 and result["holds"][0][2] == "external:b"


def test_unreadable_plans_keeps_old_rows_and_lists_unreadable(tmp_path, monkeypatch):
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    now = time.time()
    con.execute("INSERT INTO dependency_hold (repo, plan, dep, form, reason, "
                "detail, created_at, updated_at) VALUES "
                "('operator/sniff','0009-a','external:cihost-caddy','external',"
                "'unattested','x',?,?)", (now, now))
    con.commit()
    con.close()
    monkeypatch.setattr(DEPSD.watch, "repos", lambda: ["operator/sniff"])
    monkeypatch.setattr(DEPSD.watch, "plan_files", lambda repo, token: None)
    result = DEPSD.recompute(str(db), token_fn=lambda: ("tok", ""))
    assert result["unreadable"] == ["operator/sniff"]
    holds = DEPSD.api_holds(str(db))
    assert holds["unreadable"] == ["operator/sniff"]
    assert len(holds["holds"]) == 1   # the old row survived


def test_a_crash_between_the_wake_write_and_the_hold_commit_leaves_the_hold(
        tmp_path, monkeypatch):
    """The wake is written before the transaction that deletes the hold
    commits (Boundaries) — simulated here by making the commit after the
    wake write raise. The hold row must still be there (never half-applied),
    so the next pass sees the same transition and rewrites the same wake."""
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    now = time.time()
    con.execute("INSERT INTO dependency_hold (repo, plan, dep, form, reason, "
                "detail, created_at, updated_at) VALUES "
                "('operator/sniff','0009-a','external:cihost-caddy','external',"
                "'unattested','x',?,?)", (now, now))
    con.commit()
    con.close()
    monkeypatch.setattr(DEPSD, "wake_dir", lambda: tmp_path / "wake")
    monkeypatch.setattr(DEPSD.watch, "repos", lambda: ["operator/sniff"])
    monkeypatch.setattr(DEPSD.watch, "plan_files", _plan_files(
        {"operator/sniff": [("plans/0009-a.md", _plan("0009-a"))]}))

    real_connect_rw = DEPSD._connect_rw

    class _CrashingConnection:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def commit(self):
            raise RuntimeError("simulated crash before commit")

    def _crashing_connect(path, set_wal=True):
        return _CrashingConnection(real_connect_rw(path))

    monkeypatch.setattr(DEPSD, "_connect_rw", _crashing_connect)
    with pytest.raises(RuntimeError):
        DEPSD.recompute(str(db), token_fn=lambda: ("tok", ""))
    # the wake file was written before the crash
    assert list((tmp_path / "wake").glob("*.json"))
    # ...and the hold row the "crash" happened before deleting is untouched
    monkeypatch.setattr(DEPSD, "_connect_rw", real_connect_rw)
    holds = DEPSD.api_holds(str(db))["holds"]
    assert len(holds) == 1 and holds[0]["dep"] == "external:cihost-caddy"
    # D8: "the next recompute rewrites the same wake" — simulate a watcher
    # having already consumed the first (now-crashed-away) wake file, then
    # run a real pass with no crash. It must see the SAME "held, now clear"
    # transition and write the SAME file name again, since the hold row
    # never actually cleared the first time.
    first_names = {p.name for p in (tmp_path / "wake").glob("*.json")}
    for p in (tmp_path / "wake").glob("*.json"):
        p.unlink()
    DEPSD.recompute(str(db), token_fn=lambda: ("tok", ""))
    second_names = {p.name for p in (tmp_path / "wake").glob("*.json")}
    assert second_names == first_names
    assert DEPSD.api_holds(str(db))["holds"] == []


def test_write_wake_is_a_tmp_file_then_an_os_replace(tmp_path, monkeypatch):
    """D1/D4 (Boundaries): the write goes through a `.tmp-*` file in the same
    directory, then `os.replace` — never a direct write to the final name —
    so a reader (`fleet-watch`'s `_take_wakes`) can never observe a half
    file. Proven by recording every path `os.replace` is called with and
    checking its SOURCE matches the `.tmp-*` pattern and lands at the exact
    name `write_wake` promises, with no file EVER visible at the final name
    under any other name first."""
    calls = []
    real_replace = os.replace

    def _spy_replace(src, dst):
        calls.append((str(src), str(dst)))
        # the final name must not exist until THIS replace — proof nothing
        # wrote it directly
        assert not Path(dst).exists()
        return real_replace(src, dst)

    monkeypatch.setattr(DEPSD.os, "replace", _spy_replace)
    monkeypatch.setattr(DEPSD, "wake_dir", lambda: tmp_path / "wake")
    DEPSD.write_wake("operator/sniff", "0009-a", "dependency-cleared")
    assert len(calls) == 1
    src, dst = calls[0]
    assert Path(src).name.startswith(".tmp-")
    assert Path(dst).name == DEPSD._wake_name("operator/sniff", "0009-a")
    assert Path(dst).exists()
    assert not Path(src).exists()


def test_since_survives_a_recompute(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(DEPSD.watch, "repos", lambda: ["operator/sniff"])
    monkeypatch.setattr(DEPSD.watch, "plan_files", _plan_files(
        {"operator/sniff": [("plans/0009-a.md",
                            _plan("0009-a", depends_on=["external:cihost-caddy"]))]}))
    DEPSD.recompute(str(db), token_fn=lambda: ("tok", ""))
    since1 = DEPSD.api_holds(str(db))["holds"][0]["since"]
    time.sleep(0.01)
    DEPSD.recompute(str(db), token_fn=lambda: ("tok", ""))
    since2 = DEPSD.api_holds(str(db))["holds"][0]["since"]
    assert since1 == since2


def test_recompute_deletes_holds_for_a_repo_dropped_from_dispatch(tmp_path, monkeypatch):
    """A repo removed from `repos.conf` never comes up in `recompute()`'s own
    loop (it only walks `watch.repos()`), so without this cleanup its rows
    would survive every future pass — still listed in `/api/v1/holds`, and
    still able to satisfy the POST handler's "cited by a held plan" check for
    a slug nobody watches anymore."""
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    now = time.time()
    con.execute(
        "INSERT INTO dependency_hold (repo, plan, dep, form, reason, detail, "
        "created_at, updated_at) VALUES ('operator/gone','0001-x',"
        "'external:x','external','unattested','x',?,?)", (now, now))
    con.execute(
        "INSERT INTO dependency_scan (repo, scanned_at, ok, detail, "
        "created_at, updated_at) VALUES ('operator/gone', ?, 1, '', ?, ?)",
        (now, now, now))
    con.commit()
    con.close()
    monkeypatch.setattr(DEPSD.watch, "repos", lambda: ["operator/sniff"])
    monkeypatch.setattr(DEPSD.watch, "plan_files", _plan_files({"operator/sniff": []}))
    DEPSD.recompute(str(db), token_fn=lambda: ("tok", ""))
    holds = DEPSD.api_holds(str(db))
    assert holds["holds"] == []
    assert "operator/gone" not in holds["unreadable"]


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    db = _db(tmp_path)
    before = db.stat().st_mtime_ns
    monkeypatch.setattr(DEPSD, "wake_dir", lambda: tmp_path / "wake")
    monkeypatch.setattr(DEPSD.watch, "repos", lambda: ["operator/sniff"])
    monkeypatch.setattr(DEPSD.watch, "plan_files", _plan_files(
        {"operator/sniff": [("plans/0009-a.md",
                            _plan("0009-a", depends_on=["external:cihost-caddy"]))]}))
    result = DEPSD.recompute(str(db), token_fn=lambda: ("tok", ""), dry_run=True)
    assert len(result["holds"]) == 1
    assert db.stat().st_mtime_ns == before
    assert not (tmp_path / "wake").exists()
    assert DEPSD.api_holds(str(db))["holds"] == []


# -------------------------------------------------------------------- server

def _server(tmp_path, db=None, secret=None, attest_token=None, bind_host="127.0.0.1"):
    db = db or _db(tmp_path)
    ctx = DEPSD._Ctx(str(db), bind_host)
    ctx.secret = secret
    ctx.attest_token = attest_token
    ctx.lease_id = "lease-test"
    from http.server import ThreadingHTTPServer
    import threading
    srv = ThreadingHTTPServer((bind_host, 0), DEPSD.make_handler_class(ctx))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address
    base = f"http://{host}:{port}"
    return srv, ctx, base, db


def _req(base, path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            parsed = json.loads(e.read().decode() or "{}")
        except Exception:
            parsed = {}
        return e.code, parsed


@pytest.fixture
def server(tmp_path):
    srv, ctx, base, db = _server(tmp_path)
    yield srv, ctx, base, db
    srv.shutdown()
    srv.server_close()


def test_healthz_reports_booleans_never_values(server):
    _srv, ctx, base, _db = server
    ctx.secret = "shh"
    ctx.attest_token = "tok"
    status, body = _req(base, "/healthz")
    assert status == 200
    assert body["webhook_secret"] is True and body["attest_token"] is True
    assert body["lease"] == "lease-test"


def test_holds_and_attestations_are_unauthenticated(server):
    _srv, _ctx, base, _db = server
    assert _req(base, "/api/v1/holds")[0] == 200
    assert _req(base, "/api/v1/attestations")[0] == 200


def test_get_attestations_uses_the_documented_field_names(tmp_path):
    """D3/`docs/depsd.md`: the lynceus sibling is promised
    slug/note/attested_at/attested_by — not this file's own internal
    created_at/updated_at/actor shape (pinned elsewhere by
    tests/test_dependency_attestation.py, outside this plan's lane)."""
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    DEPSD.record_attestation(con, "cihost-caddy", "note", "cli:t", "test", "attest")
    con.close()
    srv, ctx, base, db = _server(tmp_path, db=db)
    try:
        status, body = _req(base, "/api/v1/attestations")
        assert status == 200
        row = body["attestations"][0]
        assert set(row) == {"slug", "note", "attested_at", "attested_by"}
        assert row["slug"] == "cihost-caddy" and row["attested_by"] == "cli:t"
    finally:
        srv.shutdown()
        srv.server_close()


def test_unknown_path_is_404_and_wrong_method_is_405(server):
    _srv, _ctx, base, _db = server
    assert _req(base, "/nope")[0] == 404
    assert _req(base, "/healthz", method="POST")[0] == 405
    assert _req(base, "/api/v1/holds", method="DELETE")[0] == 405


# ------------------------------------------------------------------- webhook

def _sign(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_a_correctly_signed_push_marks_dirty_and_returns_202(tmp_path, monkeypatch):
    srv, ctx, base, db = _server(tmp_path, secret="s3cret")
    monkeypatch.setattr(DEPSD.watch, "repos", lambda: ["operator/sniff"])
    body = json.dumps({"ref": "refs/heads/main",
                       "repository": {"full_name": "operator/sniff"}}).encode()
    try:
        req = urllib.request.Request(
            base + "/webhook/forgejo", data=body, method="POST",
            headers={"X-Forgejo-Signature": _sign("s3cret", body),
                     "X-Forgejo-Event": "push"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 202
        assert ctx.dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("sig_fn", [
    lambda secret, body: "0" * 64,
    lambda secret, body: "",
])
def test_a_bad_or_missing_signature_is_401_and_parses_nothing(tmp_path, sig_fn):
    srv, ctx, base, db = _server(tmp_path, secret="s3cret")
    try:
        body = json.dumps({"ref": "refs/heads/main"}).encode()
        headers = {"X-Forgejo-Event": "push"}
        sig = sig_fn("s3cret", body)
        if sig:
            headers["X-Forgejo-Signature"] = sig
        req = urllib.request.Request(base + "/webhook/forgejo", data=body,
                                     method="POST", headers=headers)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 401
        assert not ctx.dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_signature_over_a_different_body_is_401(tmp_path):
    """The signature must verify against the body AS RECEIVED, not merely be
    a well-formed HMAC the server happens to accept — a signature computed
    over some OTHER body (a prior delivery, a tampered one) must not pass
    just because it is 64 hex characters."""
    srv, ctx, base, db = _server(tmp_path, secret="s3cret")
    try:
        sent_body = json.dumps({"ref": "refs/heads/main",
                                "repository": {"full_name": "operator/sniff"}}).encode()
        other_body = json.dumps({"ref": "refs/heads/other"}).encode()
        req = urllib.request.Request(
            base + "/webhook/forgejo", data=sent_body, method="POST",
            headers={"X-Forgejo-Signature": _sign("s3cret", other_body),
                     "X-Forgejo-Event": "push"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 401
        assert not ctx.dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()


def test_no_secret_loaded_is_503(tmp_path):
    srv, ctx, base, db = _server(tmp_path, secret=None)
    try:
        status, _ = _req(base, "/webhook/forgejo", method="POST", body={"ref": "x"},
                         headers={"X-Forgejo-Signature": "a" * 64})
        assert status == 503
    finally:
        srv.shutdown()
        srv.server_close()


def test_over_one_mib_is_413(tmp_path):
    srv, ctx, base, db = _server(tmp_path, secret="s3cret")
    try:
        big = b"x" * (DEPSD._MAX_WEBHOOK_BODY + 10)
        req = urllib.request.Request(
            base + "/webhook/forgejo", data=big, method="POST",
            headers={"X-Forgejo-Signature": "a" * 64})
        # The server replies 413 and closes the connection as soon as it sees
        # the declared Content-Length, before the client finishes uploading
        # the (deliberately oversized) body — so the client may see either
        # the 413 response or a connection error from the early close.
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected a refusal"
        except urllib.error.HTTPError as e:
            assert e.code == 413
        except urllib.error.URLError:
            pass
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("payload,headers_extra", [
    ({"ref": "refs/heads/other", "repository": {"full_name": "operator/sniff"}},
     {"X-Forgejo-Event": "push"}),
    ({"ref": "refs/heads/main", "repository": {"full_name": "operator/other"}},
     {"X-Forgejo-Event": "push"}),
    ({"ref": "refs/heads/main", "repository": {"full_name": "operator/sniff"}},
     {"X-Forgejo-Event": "issue"}),
])
def test_non_matching_deliveries_are_204_and_change_nothing(tmp_path, monkeypatch,
                                                            payload, headers_extra):
    monkeypatch.setattr(DEPSD.watch, "repos", lambda: ["operator/sniff"])
    srv, ctx, base, db = _server(tmp_path, secret="s3cret")
    try:
        body = json.dumps(payload).encode()
        headers = dict(headers_extra)
        headers["X-Forgejo-Signature"] = _sign("s3cret", body)
        req = urllib.request.Request(base + "/webhook/forgejo", data=body,
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 204
        assert not ctx.dirty.is_set()
    finally:
        srv.shutdown()
        srv.server_close()


def test_webhook_log_line_never_carries_signature_or_body(tmp_path, capsys):
    srv, ctx, base, db = _server(tmp_path, secret="s3cret")
    try:
        body = json.dumps({"secret-looking-field": "AKIASOMETHINGSENSITIVE"}).encode()
        sig = _sign("s3cret", body)
        req = urllib.request.Request(
            base + "/webhook/forgejo", data=body, method="POST",
            headers={"X-Forgejo-Signature": sig, "X-Forgejo-Delivery": "d-1"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    finally:
        srv.shutdown()
        srv.server_close()
    out = capsys.readouterr().out
    assert sig not in out
    assert "AKIASOMETHINGSENSITIVE" not in out
    assert "d-1" in out


def test_negative_content_length_does_not_request_an_unbounded_read():
    """A declared length of `-1` parses as a valid int below
    `_MAX_WEBHOOK_BODY`, so it is never caught by the "too large" check — but
    `rfile.read(n)` with a negative `n` means "read to EOF", unbounded,
    exactly what the length cap exists to prevent. The fix folds a negative
    declared length into the same "no usable length" case already used when
    the header is missing, which reads a bounded amount instead."""
    ctx = DEPSD._Ctx("db", "127.0.0.1")
    ctx.secret = "s3cret"
    body = json.dumps({"ref": "refs/heads/other"}).encode()
    sig = _sign("s3cret", body)

    class _SpyRFile(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.read_calls = []

        def read(self, n=-1):
            self.read_calls.append(n)
            return super().read(n)

    handler = _FakeHandler(
        "127.0.0.1",
        {"Content-Length": "-1", "X-Forgejo-Signature": sig},
        body)
    handler.rfile = _SpyRFile(body)
    DEPSD._handle_webhook(handler, ctx)
    assert handler.rfile.read_calls
    assert all(0 <= n <= DEPSD._MAX_WEBHOOK_BODY + 1
              for n in handler.rfile.read_calls)


def test_204_response_carries_no_body_or_content_length():
    """RFC 7230 §3.3.2: a 204 must have neither. On a keep-alive connection,
    sending one anyway leaves bytes a reusing client parses as the start of
    the NEXT response."""
    handler = _FakeHandler("127.0.0.1", {})
    DEPSD._send_json(handler, 204, {"ignored": True})
    assert handler.status == 204
    assert "Content-Length" not in handler.response_headers
    assert handler.wfile.getvalue() == b""


def test_depsd_host_is_the_machine_hostname_not_the_bind_address(monkeypatch):
    """The service lease must be keyed on this host's name (`opshost`, matching
    `config/services.conf`'s `opshost:fleet-depsd` enrolment and
    `fleet-watch`'s own `_service_resource`) — never `FLEET_DEPSD_BIND`'s
    address, a LAN reachability choice with no bearing on which host is
    running the process."""
    monkeypatch.setenv("FLEET_DEPSD_BIND", "192.0.2.5:8797")
    assert DEPSD._depsd_host() == DEPSD.socket.gethostname().split(".")[0]
    assert DEPSD._depsd_host() != DEPSD._depsd_bind()[0]


def test_cmd_serve_acquires_the_lease_on_the_hostname_not_the_bind_host(
        tmp_path, monkeypatch):
    seen = {}

    def _fake_acquire(host, sid):
        seen["host"] = host
        return None            # refuse to start — this test only checks the arg

    monkeypatch.setenv("FLEET_DEPSD_BIND", "192.0.2.5:8797")
    monkeypatch.setattr(DEPSD, "_single_instance", lambda: 3)
    monkeypatch.setattr(DEPSD, "acquire_service_lease", _fake_acquire)
    monkeypatch.setattr(DEPSD, "_depsd_host", lambda: "opshost")
    monkeypatch.setattr(DEPSD.os, "close", lambda fd: None)
    args = type("Args", (), {"once": False, "db": str(tmp_path / "fleet.db")})()
    DEPSD.cmd_serve(args)
    assert seen["host"] == "opshost"


# ------------------------------------------------------------- attestations

def test_post_from_a_non_local_peer_is_403_and_never_compares_the_token(
        tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hmac, "compare_digest",
                        lambda a, b: calls.append((a, b)) or (a == b))
    monkeypatch.setattr(DEPSD, "_peer_allowed", lambda ctx, addr: False)
    srv, ctx, base, db = _server(tmp_path, attest_token="secrettoken")
    try:
        status, body = _req(base, "/api/v1/attestations", method="POST",
                            body={"slug": "x"},
                            headers={"Authorization": "Bearer secrettoken"})
        assert status == 403
    finally:
        srv.shutdown()
        srv.server_close()
    # the token compare is never reached — only hmac calls for OTHER purposes
    # (there are none here) would appear
    assert calls == []


def test_post_from_the_loopback_peer_with_the_right_token_attests(tmp_path, monkeypatch):
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    now = time.time()
    con.execute("INSERT INTO dependency_hold (repo, plan, dep, form, reason, "
                "detail, created_at, updated_at) VALUES "
                "('operator/sniff','0009-a','external:cihost-caddy','external',"
                "'unattested','x',?,?)", (now, now))
    con.commit()
    con.close()
    srv, ctx, base, db = _server(tmp_path, db=db, attest_token="secrettoken")
    try:
        status, body = _req(base, "/api/v1/attestations", method="POST",
                            body={"slug": "cihost-caddy", "note": "piloted"},
                            headers={"Authorization": "Bearer secrettoken"})
        assert status == 201
        assert body["slug"] == "cihost-caddy" and body["note"] == "piloted"
        assert ctx.dirty.is_set()
        rows = DEPSD.attestations(str(db))
        assert rows[0]["slug"] == "cihost-caddy" and rows[0]["actor"] == "lynceus"
    finally:
        srv.shutdown()
        srv.server_close()


class _FakeHandler:
    """A duck-typed stand-in for the one `handler` argument
    `_handle_attest_write` touches — `client_address`, `headers.get`,
    `rfile.read`, and the handful of response methods `_send_json` calls.
    A real HTTP client's peer is always loopback in this suite (the server
    only ever binds `127.0.0.1`), so there is no way to make an ACTUAL
    connection arrive claiming the configured bind address — this stubs the
    one attribute `_peer_allowed` reads instead of standing up a routable
    second interface for a test."""

    def __init__(self, peer, headers, body=b""):
        self.client_address = (peer, 0)
        self.headers = headers
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


def test_post_from_the_bind_address_peer_with_the_right_token_attests(tmp_path):
    """D8: the host's own configured bind address, not only `127.0.0.1`,
    takes the normal auth path — `_peer_allowed`'s OTHER branch."""
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    now = time.time()
    con.execute("INSERT INTO dependency_hold (repo, plan, dep, form, reason, "
                "detail, created_at, updated_at) VALUES "
                "('operator/sniff','0009-a','external:cihost-caddy','external',"
                "'unattested','x',?,?)", (now, now))
    con.commit()
    con.close()
    ctx = DEPSD._Ctx(str(db), "192.0.2.5")
    ctx.attest_token = "secrettoken"
    body = json.dumps({"slug": "cihost-caddy", "note": "piloted"}).encode()
    handler = _FakeHandler(
        "192.0.2.5",
        {"Content-Length": str(len(body)), "Authorization": "Bearer secrettoken"},
        body)
    DEPSD._handle_attest_write(handler, ctx, "attest", None)
    assert handler.status == 201
    assert ctx.dirty.is_set()
    rows = DEPSD.attestations(str(db))
    assert rows[0]["slug"] == "cihost-caddy"


def test_post_with_wrong_token_is_401(tmp_path):
    srv, ctx, base, db = _server(tmp_path, attest_token="right")
    try:
        status, _ = _req(base, "/api/v1/attestations", method="POST",
                         body={"slug": "x"}, headers={"Authorization": "Bearer wrong"})
        assert status == 401
    finally:
        srv.shutdown()
        srv.server_close()


def test_post_with_no_token_loaded_is_503(tmp_path):
    srv, ctx, base, db = _server(tmp_path, attest_token=None)
    try:
        status, _ = _req(base, "/api/v1/attestations", method="POST",
                         body={"slug": "x"}, headers={"Authorization": "Bearer x"})
        assert status == 503
    finally:
        srv.shutdown()
        srv.server_close()


def test_an_uncited_slug_is_422(tmp_path):
    srv, ctx, base, db = _server(tmp_path, attest_token="tok")
    try:
        status, body = _req(base, "/api/v1/attestations", method="POST",
                            body={"slug": "never-held"},
                            headers={"Authorization": "Bearer tok"})
        assert status == 422
        assert "never-held" in body["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_malformed_slug_is_422(tmp_path):
    srv, ctx, base, db = _server(tmp_path, attest_token="tok")
    try:
        status, _ = _req(base, "/api/v1/attestations", method="POST",
                         body={"slug": "Not A Slug"},
                         headers={"Authorization": "Bearer tok"})
        assert status == 422
    finally:
        srv.shutdown()
        srv.server_close()


def test_delete_revokes_and_an_absent_slug_is_404_with_no_write(tmp_path):
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    DEPSD.record_attestation(con, "cihost-caddy", None, "cli:t", "test", "attest")
    con.close()
    srv, ctx, base, db = _server(tmp_path, db=db, attest_token="tok")
    try:
        status, body = _req(base, "/api/v1/attestations/cihost-caddy",
                            method="DELETE", headers={"Authorization": "Bearer tok"})
        assert status == 200
        assert DEPSD.attestations(str(db)) == []
        status2, _ = _req(base, "/api/v1/attestations/cihost-caddy",
                          method="DELETE", headers={"Authorization": "Bearer tok"})
        assert status2 == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_delete_from_a_non_local_peer_is_403(tmp_path, monkeypatch):
    monkeypatch.setattr(DEPSD, "_peer_allowed", lambda ctx, addr: False)
    srv, ctx, base, db = _server(tmp_path, attest_token="tok")
    try:
        status, _ = _req(base, "/api/v1/attestations/cihost-caddy", method="DELETE",
                         headers={"Authorization": "Bearer tok"})
        assert status == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_delete_with_wrong_token_is_401(tmp_path):
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    DEPSD.record_attestation(con, "cihost-caddy", None, "cli:t", "test", "attest")
    con.close()
    srv, ctx, base, db = _server(tmp_path, db=db, attest_token="right")
    try:
        status, _ = _req(base, "/api/v1/attestations/cihost-caddy", method="DELETE",
                         headers={"Authorization": "Bearer wrong"})
        assert status == 401
        assert len(DEPSD.attestations(str(db))) == 1
    finally:
        srv.shutdown()
        srv.server_close()


# -------------------------------------------------------------- startup refusals

def test_wildcard_bind_exits_2(monkeypatch):
    monkeypatch.setenv("FLEET_DEPSD_BIND", "0.0.0.0:8797")
    with pytest.raises(SystemExit) as e:
        DEPSD._depsd_bind()
    assert e.value.code == 2


def test_poll_out_of_range_exits_2(monkeypatch):
    monkeypatch.setenv("FLEET_DEPSD_POLL", "30")
    with pytest.raises(SystemExit) as e:
        DEPSD._depsd_poll()
    assert e.value.code == 2


def test_a_held_lease_refuses_to_start(tmp_path, monkeypatch):
    monkeypatch.setattr(DEPSD, "_foreign_service_holder",
                        lambda host, sid: {"holder": "someone-else", "id": "x"})
    assert DEPSD.acquire_service_lease("testhost", "me") is None


def test_rejects_attest_and_revoke_subcommands():
    for argv in (["attest", "x"], ["revoke", "x"]):
        with pytest.raises(SystemExit) as e:
            DEPSD.main(argv)
        assert e.value.code == 2


# --------------------------------------------------------------------- lint

def test_only_the_write_path_writes_dependency_attestation():
    """A file other than bin/fleet-depsd must never write to
    `dependency_attestation`/`_change`; inside bin/fleet-depsd,
    `record_attestation` must be called from nowhere but the shared
    POST/DELETE handler."""
    bin_dir = BIN
    write_re = re.compile(
        r"\b(INSERT|UPDATE|DELETE|REPLACE)\b[^\n]*\bdependency_attestation\b",
        re.I)
    for f in bin_dir.iterdir():
        if not f.is_file() or f.name == "fleet-depsd":
            continue
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        assert not write_re.search(text), f"{f} writes dependency_attestation"

    tree = ast.parse((bin_dir / "fleet-depsd").read_text())
    callers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "record_attestation"):
                    callers.add(node.name)
    assert callers == {"_handle_attest_write"}
