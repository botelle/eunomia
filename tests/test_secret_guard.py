"""Tests for fleet-secret-guard (block) + fleet-leak-watch (detect). Plan 0006 §4."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


def _guard(payload, fleet_dir):
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet_dir))
    return subprocess.run([sys.executable, str(BIN / "fleet-secret-guard")],
                          input=json.dumps(payload) if isinstance(payload, dict) else payload,
                          env=env, capture_output=True, text=True)


def _bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


# --------------------------------------------------------------- guard: blocks

def test_three_historical_leaks_are_blocked(tmp_path):
    for cmd in (
        'ssh tunnelhost "systemctl cat cloudflared"',        # 2026-08-27
        'grep -B2 WEBHOOK_SECRET ~/minos/minos.env',     # 2026-08-18
        'sed -n 1,5p ~/sniff-cf.env',                    # 2026-08-17
    ):
        r = _guard(_bash(cmd), tmp_path)
        assert r.returncode == 2, cmd
        # the refusal must never echo the target's contents (there are none here,
        # but the message must also not parrot the secret path verbatim as data)
        assert "sanctioned" in r.stderr.lower()


def test_read_and_grep_of_credential_paths_blocked(tmp_path):
    for tool, key, val in (
        ("Read", "file_path", "/Users/operator/bao/.implbot.role_id"),
        ("Read", "file_path", "/etc/cloudflared/token.env"),
        ("Read", "file_path", "/Users/operator/agent/.forgejo-token-revbot"),
        ("Read", "file_path", "/Users/operator/.ssh/id_ed25519"),
        ("Grep", "path", "/etc/cloudflared/"),
    ):
        r = _guard({"tool_name": tool, "tool_input": {key: val}}, tmp_path)
        assert r.returncode == 2, (tool, val)


def test_dodges_are_blocked(tmp_path):
    for cmd in (
        "FOO=1 cat /etc/cloudflared/token.env",          # env-prefix
        "cd /etc/cloudflared && cat token.env",          # compound
        "cat < ~/oracle/oracle.env",                     # redirect
        "cat  ~/minos/minos.env  | grep TOKEN",          # pipe
    ):
        assert _guard(_bash(cmd), tmp_path).returncode == 2, cmd


def test_no_in_band_override(tmp_path):
    # nothing in the command can whitelist it
    for cmd in ("cat ~/minos/minos.env # ALLOW",
                "EUNOMIA_GUARD=off cat ~/minos/minos.env"):
        assert _guard(_bash(cmd), tmp_path).returncode == 2, cmd


# ------------------------------------------- guard: Keychain reads (plan 0074)

_FGP = "security find-generic-password"
_CLAUDE_TOK = "CLAUDE_CODE_OAUTH_TOKEN=\"$(security find-generic-password -w -s claude-code-oauth-token)\""
# the three shapes of the fleet's own review dispatch in circulation on 2026-09-23
_DISPATCH_SHAPES = (
    "cd ~/agent && python3 dispatch-review.py operator/eunomia 536 --post",
    f"{_CLAUDE_TOK} python3 ~/agent/dispatch-review.py --pr 999 --post",
    f"cd ~/agent && {_CLAUDE_TOK} python3 dispatch-review.py operator/eunomia 536 "
    "--post --out /tmp/r.md",
)


def test_every_keychain_display_form_is_refused(tmp_path):
    for cmd, rule in (
        (f"{_FGP} -w -s keyvault-root", "keychain-read"),
        (f"{_FGP} -g -s keyvault-root", "keychain-read"),
        (f"{_FGP} -ws keyvault-root", "keychain-read"),
        ("security find-internet-password -s forge.example -w", "keychain-read"),
        ("security -q find-generic-password -w -s keyvault-root", "keychain-read"),
        ("security -p 'PR> ' find-generic-password -w -s keyvault-root", "keychain-read"),
        (f"/usr/bin/{_FGP} -w -s keyvault-root", "keychain-read"),
        (f"FOO=1 {_FGP} -s keyvault-root -w", "keychain-read"),
        ("security dump-keychain -d", "keychain-dump"),
        ("security dump-keychain -ad login.keychain", "keychain-dump"),
    ):
        r = _guard(_bash(cmd), tmp_path)
        assert r.returncode == 2, cmd
        assert rule in r.stderr, cmd


def test_attribute_only_keychain_reads_and_the_write_form_pass(tmp_path):
    for cmd in (
        f"{_FGP} -s keyvault-root",                      # presence check, no value
        f"{_FGP} -G somevalue -s keyvault-root",         # -G matches, displays nothing
        f"{_FGP} -D kind -s keyvault-root",
        "security -q find-generic-password -s keyvault-root",
        "security add-generic-password -a x -s y -w",    # a write prompts for the value
        "security add-generic-password -s keyvault-unseal -w",
        "security dump-keychain",
        "security dump-keychain -a",
        "security dump-keychain -r",                     # raw encrypted data
        "security list-keychains",
        "security find-certificate -a -p",               # public
        "security find-key -t private -w",               # -w is a search predicate here
        f"{_FGP} -s keyvault-root; ls -w",               # separator bounds the rule
        "cat README.md",
    ):
        assert _guard(_bash(cmd), tmp_path).returncode == 0, cmd


def test_review_dispatch_has_no_keychain_carve_out(tmp_path):
    # The record of the round-2 decision (plan 0074 §2): a carve-out cannot be
    # expressed in a deny-only conf without a bypass. The dispatch is refused
    # until it takes its token from ~/bin/fetch-claude-oauth-token.sh.
    for cmd in _DISPATCH_SHAPES[1:]:
        r = _guard(_bash(cmd), tmp_path)
        assert r.returncode == 2, cmd
        assert "keychain-read" in r.stderr, cmd
    # the token-less documented shape reads no Keychain, so it is not this rule's
    assert _guard(_bash(_DISPATCH_SHAPES[0]), tmp_path).returncode == 0


def test_merger_and_second_reviewer_token_helpers_are_refused(tmp_path):
    # plan 0109: ADR-0019's mergebot and rev2bot identities are not a session's to hold
    for name, rule in (("mergebot", "mergebot-token-helper"), ("rev2bot", "rev2bot-token-helper")):
        h = f"fetch-forgejo-{name}-token.sh"
        for cmd in (
            h,
            f"$(~/bin/{h})",
            f"bash -c '{h}'",
            f"~/bin/{h}",
            f"/Users/operator/bin/{h} | wc -c",
        ):
            r = _guard(_bash(cmd), tmp_path)
            assert r.returncode == 2, cmd
            assert rule in r.stderr, cmd


def test_other_token_helpers_and_the_sanctioned_holder_still_pass(tmp_path):
    for cmd in (
        "~/bin/fetch-forgejo-token.sh",
        "~/bin/fetch-forgejo-revbot-token.sh",
        "python3 ~/agent/dispatch-review.py operator/x 1 --post",
    ):
        assert _guard(_bash(cmd), tmp_path).returncode == 0, cmd


def test_keychain_refusal_cannot_be_talked_out_of(tmp_path):
    tok = "claude-code-oauth-token"
    for cmd in (
        f"{_FGP} -w -s {tok} # dispatch-review.py",
        f"echo \"$({_FGP} -w -s {tok})\" dispatch-review.py",
        f"{_FGP} -w -s {tok}\necho dispatch-review.py",
    ):
        assert _guard(_bash(cmd), tmp_path).returncode == 2, cmd


# --------------------------------------------------------------- guard: allows

def test_benign_traffic_passes(tmp_path):
    for payload in (
        _bash("cat README.md && echo done"),
        _bash("grep TODO app.py"),
        _bash("ls -la ~/bao/"),                          # navigation, not a file read
        _bash("systemctl status cloudflared"),           # status != cat/show
        _bash("cat config/app.env.example"),             # .example is not .env
        {"tool_name": "Read", "tool_input": {"file_path": "plans/0006-secret-guard.md"}},
    ):
        assert _guard(payload, tmp_path).returncode == 0, payload


def test_pipe_helper_is_invisible_and_allowed(tmp_path):
    cmd = 'curl -H @<(printf "Authorization: token "; ~/bin/fetch-forgejo-token.sh) http://x'
    assert _guard(_bash(cmd), tmp_path).returncode == 0


def test_fail_open_on_malformed_payload(tmp_path):
    r = _guard("not json at all", tmp_path)
    assert r.returncode == 0 and r.stderr == ""
    assert (tmp_path / "secret-guard.log").exists()


def test_block_message_never_contains_target_contents(tmp_path):
    # even if a secret-looking string is in the command, the refusal must not
    # reflect the *value* — only the rule name and the sanctioned path
    r = _guard(_bash("cat ~/minos/minos.env"), tmp_path)
    assert r.returncode == 2
    assert "minos.env" not in r.stderr or "sanctioned" in r.stderr.lower()
    # the message is a fixed template; assert it carries no arbitrary reflected data
    assert "\n" in r.stderr and "protected credential" in r.stderr


# ----------------------------------------- guard: conf defects fail CLOSED
# (#11 r4 L2) fleetlib._content_rules fails loud on a mangled conf, but the
# guard's allow-all would have silently disabled PreToolUse blocking fleet-wide.
# The guard resolves its conf relative to its own file, so these tests copy the
# script into a tmp bin/ beside a doctored config/ — no env-var override exists
# (an override the harness could use is an override a session could use).

REAL_CONF = (BIN.parent / "config" / "secret-patterns.conf").read_text()


def _guard_with_conf(payload, tmp_path, conf_text):
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    guard = tmp_path / "bin" / "fleet-secret-guard"
    guard.write_text((BIN / "fleet-secret-guard").read_text())
    if conf_text is not None:
        (tmp_path / "config" / "secret-patterns.conf").write_text(conf_text)
    fleet = tmp_path / "fleet"
    env = dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet))
    return subprocess.run([sys.executable, str(guard)], input=json.dumps(payload),
                          env=env, capture_output=True, text=True), fleet


BENIGN = _bash("cat README.md")


def test_conf_harness_faithful_with_real_conf(tmp_path):
    """Sanity: the copied guard behaves exactly like the installed one."""
    r, _ = _guard_with_conf(BENIGN, tmp_path, REAL_CONF)
    assert r.returncode == 0, r.stderr
    r, _ = _guard_with_conf(_bash("cat ~/minos/minos.env"), tmp_path, REAL_CONF)
    assert r.returncode == 2


def test_conf_mangled_line_fails_closed(tmp_path):
    """One tabs->spaces accident on a single rule line blocks with the why —
    it must NOT silently drop the rule (or the guard)."""
    mangled = REAL_CONF.replace("path\tbao-tree", "path bao-tree", 1)
    assert mangled != REAL_CONF, "fixture rot: bao-tree rule not found"
    r, fleet = _guard_with_conf(BENIGN, tmp_path, mangled)
    assert r.returncode == 2
    assert "KIND<TAB>NAME<TAB>REGEX" in r.stderr
    assert "secret-patterns.conf" in r.stderr
    assert (fleet / "secret-guard.log").read_text().startswith("BLOCK conf defect")


def test_conf_deleted_rules_block_fails_closed(tmp_path):
    """path/cmd blocks deleted (only comments + content rows left): the parse
    'succeeds' with zero enforceable rules — that is a defect, not an empty list."""
    survivors = [l for l in REAL_CONF.splitlines()
                 if not l.strip() or l.startswith("#") or l.startswith("content\t")]
    r, _ = _guard_with_conf(BENIGN, tmp_path, "\n".join(survivors) + "\n")
    assert r.returncode == 2
    assert "zero path rules" in r.stderr


def test_conf_deleted_single_kind_fails_closed(tmp_path):
    """#13 r3 M1 (verified in review): the zero-rules check is AND across
    kinds. Deleting only the path block — one contiguous hunk in the real
    conf — previously left a 'healthy' parse with the entire Read/Grep guard
    silently gone."""
    no_path = "\n".join(l for l in REAL_CONF.splitlines()
                        if not l.startswith("path\t")) + "\n"
    r, _ = _guard_with_conf(BENIGN, tmp_path, no_path)
    assert r.returncode == 2
    assert "zero path rules" in r.stderr
    no_cmd = "\n".join(l for l in REAL_CONF.splitlines()
                       if not l.startswith("cmd\t")) + "\n"
    r, _ = _guard_with_conf(BENIGN, tmp_path, no_cmd)
    assert r.returncode == 2
    assert "zero cmd rules" in r.stderr
    # the review's replayed historical leaks must never again pass on a
    # path-block-deleted conf
    r, _ = _guard_with_conf(_bash("grep -B2 WEBHOOK_SECRET ~/minos/minos.env"),
                            tmp_path, no_path)
    assert r.returncode == 2


def test_conf_invalid_regex_fails_closed_without_echo(tmp_path):
    """#13 r1 M3: str(re.error) can carry pattern snippets and the rule NAME is
    an uncontrolled field of a possibly-mangled line — the message cites line
    number + defect class only."""
    bad = REAL_CONF + "path\tXNAMEMARKERX\t([unclosed XPATTERNMARKERX\n"
    r, fleet = _guard_with_conf(BENIGN, tmp_path, bad)
    assert r.returncode == 2
    assert "does not compile" in r.stderr
    leaked = r.stderr + r.stdout + (fleet / "secret-guard.log").read_text()
    assert "XNAMEMARKERX" not in leaked and "XPATTERNMARKERX" not in leaked


def test_conf_unknown_kind_fails_closed(tmp_path):
    """A KIND typo ('pth') would silently un-enforce that rule — same defect
    class as a mangled tab, same loud block."""
    bad = REAL_CONF.replace("path\tenv-file", "pth\tenv-file", 1)
    assert bad != REAL_CONF, "fixture rot: env-file rule not found"
    r, _ = _guard_with_conf(BENIGN, tmp_path, bad)
    assert r.returncode == 2
    assert "unknown KIND" in r.stderr


def test_conf_unknown_kind_never_echoes_the_field(tmp_path):
    """#13 r1 M3 (verified in review): a TSV-ish paste accident can put a
    credential in the KIND field; the refusal and the log must not repeat it."""
    bad = REAL_CONF + "AKIAXSECRETMARKERX\tfoo\tbar\n"
    r, fleet = _guard_with_conf(BENIGN, tmp_path, bad)
    assert r.returncode == 2
    leaked = r.stderr + r.stdout + (fleet / "secret-guard.log").read_text()
    assert "AKIAXSECRETMARKERX" not in leaked


def test_conf_invalid_utf8_fails_closed_and_logs(tmp_path):
    """#13 r1 H1 (verified in review): UnicodeDecodeError is a ValueError, not
    an OSError — byte-level mangling (bad merge, wrong-encoding save,
    truncated multibyte char) previously escaped as exit 1, unlogged."""
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    guard = tmp_path / "bin" / "fleet-secret-guard"
    guard.write_text((BIN / "fleet-secret-guard").read_text())
    (tmp_path / "config" / "secret-patterns.conf").write_bytes(
        b"path\tenv-file\t\x80\xff broken bytes\n")
    fleet = tmp_path / "fleet"
    r = subprocess.run([sys.executable, str(guard)], input=json.dumps(BENIGN),
                       env=dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet)),
                       capture_output=True, text=True)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "cannot read" in r.stderr and "UnicodeDecodeError" in r.stderr
    assert (fleet / "secret-guard.log").read_text().startswith("BLOCK conf defect")


def test_conf_defect_dominates_malformed_payload(tmp_path):
    """#13 r1 L4: the conf check runs first, so a defective conf blocks even
    when the payload is also unparseable."""
    mangled = REAL_CONF.replace("path\tbao-tree", "path bao-tree", 1)
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    guard = tmp_path / "bin" / "fleet-secret-guard"
    guard.write_text((BIN / "fleet-secret-guard").read_text())
    (tmp_path / "config" / "secret-patterns.conf").write_text(mangled)
    r = subprocess.run([sys.executable, str(guard)], input="not json at all",
                       env=dict(os.environ, EUNOMIA_FLEET_DIR=str(tmp_path / "fleet")),
                       capture_output=True, text=True)
    assert r.returncode == 2


def test_conf_missing_fails_closed_with_restore_hint(tmp_path):
    """Unreadable conf is conf-related, so it is NOT the fail-open path; the
    block names the recovery (unlike the 2026-08-27 blind hook failure) and
    points at the actual CONF path the guard read (#13 r2 L1), not a
    hardcoded checkout location."""
    r, _ = _guard_with_conf(BENIGN, tmp_path, None)
    assert r.returncode == 2
    assert "cannot read" in r.stderr and "known-good" in r.stderr
    assert str(tmp_path / "config" / "secret-patterns.conf") in r.stderr


def test_conf_indented_lines_parse_like_fleetlib(tmp_path):
    """#13 r2 M: fleetlib._content_rules strips lines; the guard must too — an
    indented comment or rule that fleet-cr accepts as healthy must not brick
    the fleet through the guard's stricter parse."""
    indented = REAL_CONF.replace("# --- env files",
                                 "  # indented comment\n# --- env files", 1)
    indented = indented.replace("path\tbao-tree", "  path\tbao-tree", 1)
    assert "  # indented" in indented and "  path\tbao-tree" in indented
    r, _ = _guard_with_conf(BENIGN, tmp_path, indented)
    assert r.returncode == 0, r.stderr
    # and the indented rule still ENFORCES
    r, _ = _guard_with_conf(_bash("cat ~/bao/.implbot.role_id"), tmp_path, indented)
    assert r.returncode == 2


def test_conf_defect_message_never_echoes_the_line(tmp_path):
    """A mangled line is uncontrolled content — the refusal cites line NUMBER
    and format, never the line body."""
    marker = "XSECRETMARKERX"
    mangled = REAL_CONF.replace("path\tbao-tree\t", f"path {marker} ", 1)
    r, _ = _guard_with_conf(BENIGN, tmp_path, mangled)
    assert r.returncode == 2
    assert marker not in r.stderr and marker not in r.stdout


def test_fail_open_still_scoped_to_non_conf_errors(tmp_path):
    """The availability valve survives, but only for errors UNRELATED to the
    conf: with a healthy conf, a malformed payload still exits 0 + logs."""
    r, fleet = _guard_with_conf(BENIGN, tmp_path, REAL_CONF)
    assert r.returncode == 0
    p = subprocess.run([sys.executable, str(tmp_path / "bin" / "fleet-secret-guard")],
                       input="not json at all",
                       env=dict(os.environ, EUNOMIA_FLEET_DIR=str(fleet)),
                       capture_output=True, text=True)
    assert p.returncode == 0 and p.stderr == ""
    assert "failing open" in (fleet / "secret-guard.log").read_text()


# --------------------------------------------------------------- watcher

def _load_watch():
    loader = importlib.machinery.SourceFileLoader("lw", str(BIN / "fleet-leak-watch"))
    spec = importlib.util.spec_from_loader("lw", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _transcript(projects, name, lines):
    d = projects / "-Users-x"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("\n".join(lines) + "\n")


def _result_line(text):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": text}]}})


def test_watcher_alerts_once_per_finding_and_never_leaks_the_value(tmp_path, monkeypatch):
    mod = _load_watch()
    projects = tmp_path / "projects"
    secret_jwt = "eyJ0eXAiOiJKV1QifQ.SECRETPART.zzz"
    _transcript(projects, "s1.jsonl", [
        _result_line(f"tunnel token is {secret_jwt}"),
        _result_line("nothing here"),
        _result_line("-----BEGIN OPENSSH PRIVATE KEY-----"),
    ])
    monkeypatch.setattr(mod, "PROJECTS", projects)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    alerts = []
    monkeypatch.setattr(mod, "_alert", lambda rel, ln, f: alerts.append((rel, ln, f)))

    mod.main([])
    kinds = sorted(a[2] for a in alerts)
    assert kinds == ["jwt-or-tunnel", "private-key"], kinds
    # never the value, anywhere it writes
    assert not any(secret_jwt in " ".join(map(str, a)) for a in alerts)
    state_bytes = (tmp_path / "leak-watch.state.json").read_bytes()
    assert secret_jwt.encode() not in state_bytes, "state must not store the matched text"

    # second run: no new alerts (dedupe)
    alerts.clear()
    mod.main([])
    assert alerts == []

    # state loss does not re-page (content-derived dedupe)
    (tmp_path / "leak-watch.state.json").unlink()
    mod.main([])
    # offsets reset -> it re-scans, but the alert keys are recomputed from content;
    # a fresh state has an empty alerted set, so it WILL re-page once. That is the
    # documented behaviour: losing state re-scans and re-pages at most once, never
    # a flood. Assert it is at most the two findings, not per-line noise.
    assert len(alerts) <= 2


def test_watcher_ignores_secrets_outside_tool_results(tmp_path, monkeypatch):
    """A secret SHAPE in assistant prose or a prompt is not a leaked tool output —
    the watcher scans tool_result content only, which is also what keeps it fast."""
    mod = _load_watch()
    projects = tmp_path / "projects"
    d = projects / "-Users-x"; d.mkdir(parents=True)
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "the token format is eyJabc.def.ghi for reference"}]}}) + "\n")
    monkeypatch.setattr(mod, "PROJECTS", projects)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    alerts = []
    monkeypatch.setattr(mod, "_alert", lambda *a: alerts.append(a))
    mod.main([])
    assert alerts == [], "shapes in prose/prompts are not leaked outputs"


def test_dry_run_writes_no_state_and_no_alerts(tmp_path, monkeypatch):
    mod = _load_watch()
    projects = tmp_path / "projects"
    _transcript(projects, "s1.jsonl", [_result_line("eyJ0eXAiOiJKV1QifQ.X.Y")])
    monkeypatch.setattr(mod, "PROJECTS", projects)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    alerts = []
    monkeypatch.setattr(mod, "_alert", lambda *a: alerts.append(a))
    mod.main(["--dry-run"])
    assert alerts == []
    assert not (tmp_path / "leak-watch.state.json").exists()


def test_review_1509_backtick_substitution_blocked(tmp_path):
    """Review 1509 M1: `echo `cat ~/x.env`` slipped — backtick wasn't a split char."""
    assert _guard(_bash("echo `cat ~/oracle/oracle.env`"), tmp_path).returncode == 2
    assert _guard(_bash("X=`cat ~/minos/minos.env` echo $X"), tmp_path).returncode == 2


def test_review_1509_proc_environ_all_forms_blocked(tmp_path):
    """M2: /proc/<pid>/environ dumps a process's secret env; every form must block."""
    for cmd in ("cat /proc/self/environ", "grep TOKEN /proc/1234/environ",
                "od -c /proc/$$/environ", "cat /proc/$BASHPID/environ",
                "head /proc/self/environ"):
        assert _guard(_bash(cmd), tmp_path).returncode == 2, cmd
    # Read/Grep of the environ (incl. the /task/<tid>/ alias — review 1511) block
    for tgt in ("/proc/1234/environ", "/proc/1234/task/1234/environ",
                "/proc/self/task/99/environ"):
        assert _guard({"tool_name": "Read", "tool_input": {"file_path": tgt}},
                      tmp_path).returncode == 2, tgt
        assert _guard({"tool_name": "Grep", "tool_input": {"path": tgt}},
                      tmp_path).returncode == 2, tgt
    # benign /proc reads still pass, via Bash and Read
    assert _guard(_bash("cat /proc/cpuinfo"), tmp_path).returncode == 0
    assert _guard({"tool_name": "Read", "tool_input": {"file_path": "/proc/cpuinfo"}},
                  tmp_path).returncode == 0


def test_review_1509_bao_whole_tree_blocked_dir_nav_allowed(tmp_path):
    """M3: the plan promised 'the keyvault tree' but only dotfiles were caught."""
    for f in ("recovery-keys.json", "unseal.key", "roottoken", ".implbot.role_id",
              "tls/ca.crt"):
        r = _guard({"tool_name": "Read", "tool_input":
                    {"file_path": f"/Users/operator/bao/{f}"}}, tmp_path)
        assert r.returncode == 2, f
    # directory navigation is not a file read
    assert _guard(_bash("ls -la ~/bao/"), tmp_path).returncode == 0
    assert _guard({"tool_name": "Read", "tool_input":
                   {"file_path": "/Users/operator/bao"}}, tmp_path).returncode == 0


def test_public_cert_pem_not_overblocked(tmp_path):
    """A public cert is fine to read; only private key material blocks."""
    for pub in ("/etc/ssl/fullchain.pem", "cert.pem", "chain.pem"):
        assert _guard({"tool_name": "Read", "tool_input": {"file_path": pub}},
                      tmp_path).returncode == 0, pub
    for priv in ("/etc/ssl/privkey.pem", "key.p8", "/home/x/id_ed25519"):
        assert _guard({"tool_name": "Read", "tool_input": {"file_path": priv}},
                      tmp_path).returncode == 2, priv


# ------------------------------------------------- watcher: synthetic suppression

def test_the_suites_own_fixture_shape_does_not_page(tmp_path, monkeypatch, capsys):
    """Plan 0027. tests/test_orchestrator.py carries `ghp_` + 36 identical
    characters to prove redact() strips a credential shape, so every session that
    runs the suite echoes it into a transcript and this watcher paged for it."""
    mod = _load_watch()
    projects = tmp_path / "projects"
    fixture = "ghp_" + "A" * 36
    _transcript(projects, "s1.jsonl", [_result_line(f"use {fixture} to fetch")])
    monkeypatch.setattr(mod, "PROJECTS", projects)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    alerts = []
    monkeypatch.setattr(mod, "_alert", lambda rel, ln, f: alerts.append(f))

    mod.main([])
    assert alerts == [], "a placeholder must not page"
    # suppressed, and visibly so: an invisible suppression is a second
    # un-validated check hiding behind the first.
    assert "1 synthetic suppressed" in capsys.readouterr().out


def test_a_real_shaped_token_of_the_same_length_still_pages(tmp_path, monkeypatch):
    mod = _load_watch()
    projects = tmp_path / "projects"
    real_shape = "ghp_" + "aB3xQ9zK7mR2wL5tY8nP4vC6hJ0sD1fG7uE4"
    _transcript(projects, "s1.jsonl", [_result_line(f"use {real_shape} to fetch")])
    monkeypatch.setattr(mod, "PROJECTS", projects)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    alerts = []
    monkeypatch.setattr(mod, "_alert", lambda rel, ln, f: alerts.append(f))

    mod.main([])
    assert alerts == ["github-pat"], alerts


def test_short_matches_are_never_suppressed():
    """The floor judges repetition, and a short string cannot be judged. Staying
    loud on those is the safe direction for a detector."""
    mod = _load_watch()
    assert mod._synthetic("ghp_" + "A" * 36) is True
    assert mod._synthetic("sk-" + "0" * 40) is True
    assert mod._synthetic("ghp_aB3xQ9zK7mR2wL5t") is False
    assert mod._synthetic("AKIAIOSFODNN7EXAMPLE") is False
    for short in ("ghp_short", "sk-abc", "xoxb-1"):
        assert mod._synthetic(short) is False, short

# ------------------------------------------- guard: the broker's carve-out
# infra ADR-0003. The forgejo-broker's socket, its two logs and its deployed
# source all live under the credential tree and hold no secret, so the
# blanket rule blocked every session from smoke-testing the broker or reading
# its audit trail. The tests below are the fence around that narrowing: they
# prove the carve-out works, that it did not widen, that it cannot be
# traversed through, and that navigation under the tree still passes.

def test_the_broker_s_own_files_are_not_treated_as_credentials(tmp_path):
    """The whole point of the carve-out: a session must be able to operate and
    verify the broker. Over-blocking is what this file's own header warns
    teaches sessions to route around the guard."""
    for payload in (
        _bash("curl -s --unix-socket ~/bao/run/forgejo-broker.sock http://localhost/health"),
        _bash("~/bao/broker/forgejo-broker-query.sh operator/keyvault"),
        _bash("tail -5 ~/bao/logs/forgejo-broker.jsonl"),
        _bash("tail ~/bao/logs/forgejo-broker.log"),
        {"tool_name": "Read",
         "tool_input": {"file_path": "/Users/operator/bao/broker/forgejo_broker.py"}},
    ):
        assert _guard(payload, tmp_path).returncode == 0, payload


def test_the_carve_out_did_not_open_the_credential_tree(tmp_path):
    """The half that matters if someone widens the pattern later.

    logs/audit.log is the VAULT's own audit device, not the broker's, and stays
    protected — the carve-out is five EXACT filenames, not a directory, so
    there is nothing to traverse through."""
    for path in (
        "~/bao/.admin.role_id",
        "~/bao/.admin.secret_id",
        "~/bao/.implbot.field",
        "~/bao/logs/audit.log",
        "~/bao/tls/server.key",
        "~/bao/data/raft/raft.db",
        "~/bao/seed-secret.sh",
    ):
        r = _guard(_bash("cat " + path), tmp_path)
        assert r.returncode == 2, "no longer protected: " + path


def test_the_carve_out_cannot_be_traversed_through(tmp_path):
    """#108 review 2101 H1, and the reason the carve-out names FILES.

    The first cut exempted `broker/` as a subtree, so
    `~/bao/broker/../.admin.role_id` matched the exemption and read a
    credential — a one-token bypass of the highest-value rule in the conf,
    against a guard whose documented model is that nothing a session puts in
    the command bypasses the match.

    Two fixes, and they are NOT redundant — measured, not assumed. The rule
    naming exact filenames stops every case here. normpath alone does not: a
    Grep of `<tree>/broker/..` normalises to the bare directory, which this
    rule never matched anyway (it needs a trailing non-slash component). So the
    bound is what closes THIS hole, and normpath is what stops `..` reaching
    every OTHER rule in the conf."""
    for path in (
        "~/bao/broker/../.admin.role_id",
        "~/bao/broker/../.admin.secret_id",
        "~/bao/broker/../logs/audit.log",
        "~/bao/broker/../tls/server.key",
        "~/bao/broker/./../.admin.role_id",
        "~/bao/run/../.admin.role_id",
        "~/bao/logs/../.admin.role_id",
        "~/bao/BROKER/../.admin.role_id",
    ):
        assert _guard(_bash("cat " + path), tmp_path).returncode == 2, \
            "traversable: " + path
    for payload in (
        {"tool_name": "Read",
         "tool_input": {"file_path": "/Users/operator/bao/broker/../.admin.role_id"}},
        {"tool_name": "Grep",
         "tool_input": {"path": "/Users/operator/bao/broker/.."}},
    ):
        assert _guard(payload, tmp_path).returncode == 2, payload


def test_broker_is_not_an_open_subtree(tmp_path):
    """#108 review 2101 M1. `broker/` is not exempt as a directory, so a state
    file or a cached token dropped there later is still protected — which is
    what the conf comment claims."""
    for path in ("~/bao/broker/token", "~/bao/broker/sub/x",
                 "~/bao/broker/eunomia-bin/fleet-watch",
                 "~/bao/logs/forgejo-broker.log.1"):
        assert _guard(_bash("cat " + path), tmp_path).returncode == 2, \
            "unexpectedly exempt: " + path


def test_navigation_under_the_tree_still_passes(tmp_path):
    """#108 review 2115 M1. `normpath` strips a trailing slash, so normalising
    turned `ls <tree>/broker/` — which the raw rules deliberately allowed,
    since they end in `[^/]$` to match FILES and not their directories — into a
    refusal. That is over-blocking, the exact thing this change exists to
    remove, introduced by the fix for the traversal hole.

    `_norm` now preserves a trailing separator. This is the fence: navigation
    passes, and the traversal cases in the test above still do not."""
    for cmd in ("ls -la ~/bao/",
                "ls -la ~/bao/broker/",
                "ls ~/bao/logs/",
                "ls ~/bao/run/"):
        assert _guard(_bash(cmd), tmp_path).returncode == 0, \
            "navigation blocked: " + cmd


# ------------------------------------------------- guard: it protects itself

# #222. The hook runs ~/.local/share/pins/<repo>/bin/fleet-secret-guard for
# EVERY session on this machine. Measured 2026-09-08, before this change:
#
#   exit 2  Bash: cat <a path matching the forgejo-bot-token rule>  (control)
#   exit 0  Bash: echo ... > .../bin/fleet-secret-guard
#   exit 0  Bash: git checkout <rev> -- bin/fleet-secret-guard
#   exit 0  Bash: echo bogus > .../config/secret-patterns.conf
#
# The control blocked, so the probe discriminated: three ways to disable the
# fleet's only code-enforced credential control, for every session, silently.

PIN_EXE = "~/.local/share/pins/eunomia/bin/fleet-secret-guard"
PIN_CONF = "~/.local/share/pins/eunomia/config/secret-patterns.conf"


def test_a_bash_redirect_cannot_overwrite_the_installed_guard(tmp_path):
    r = _guard(_bash(f'echo "import sys;sys.exit(0)" > {PIN_EXE}'), tmp_path)
    assert r.returncode == 2, r.stderr
    assert "guard-pin-exe" in r.stderr


def test_a_bash_redirect_cannot_overwrite_the_installed_conf(tmp_path):
    r = _guard(_bash(f"echo bogus > {PIN_CONF}"), tmp_path)
    assert r.returncode == 2, r.stderr
    assert "guard-pin-conf" in r.stderr


def test_the_write_tool_cannot_replace_the_installed_guard(tmp_path):
    """The tool most obviously suited to replacing a file was the one the hook
    never saw: the matcher was Bash|Read|Grep."""
    r = _guard({"tool_name": "Write",
                "tool_input": {"file_path": PIN_EXE, "content": "pass"}}, tmp_path)
    assert r.returncode == 2, r.stderr


def test_the_edit_tool_cannot_replace_the_installed_conf(tmp_path):
    r = _guard({"tool_name": "Edit",
                "tool_input": {"file_path": PIN_CONF,
                               "old_string": "a", "new_string": "b"}}, tmp_path)
    assert r.returncode == 2, r.stderr


def test_git_checkout_cannot_restore_an_older_guard_into_the_pin(tmp_path):
    """The path rules match a TOKEN. `git -C <pin> checkout -- bin/x` defeats
    them: one token is a directory, the other is relative. A pin's only
    sanctioned writer is bin/fleet-pin-watch, which runs from launchd and never
    through a PreToolUse hook, so blocking session-side mutation costs nothing
    operationally."""
    r = _guard(_bash("git -C ~/.local/share/pins/eunomia checkout 81db242~5 "
                     "-- bin/fleet-secret-guard"), tmp_path)
    assert r.returncode == 2, r.stderr
    assert "pin-worktree-mutation" in r.stderr


def test_the_repo_copy_stays_editable(tmp_path):
    """Scoped to the pin on purpose. The pin is the ENFORCEMENT copy; the repo
    copy is where the guard is developed, and blocking it would mean this very
    change could not be written without an override — the over-blocking the
    conf's own header warns teaches sessions to route around the guard."""
    for payload in (
            _bash("sed -n 1,40p bin/fleet-secret-guard"),
            _bash("vim ~/dev/eunomia/config/secret-patterns.conf"),
            {"tool_name": "Edit", "tool_input": {
                "file_path": "/Users/operator/dev/eunomia/bin/fleet-secret-guard",
                "old_string": "a", "new_string": "b"}},
            {"tool_name": "Read", "tool_input": {
                "file_path": "/Users/operator/dev/wt/x/config/secret-patterns.conf"}}):
        r = _guard(payload, tmp_path)
        assert r.returncode == 0, f"{payload} was blocked: {r.stderr}"


def test_a_second_pinned_repo_is_covered_too(tmp_path):
    """techne is pinned the same way; `[^/]+` rather than a literal `eunomia`
    so a second pinned guard is not unprotected by omission."""
    r = _guard(_bash("echo x > ~/.local/share/pins/techne/bin/fleet-secret-guard"),
               tmp_path)
    assert r.returncode == 2, r.stderr


def test_pin_rules_cannot_be_stepped_through(tmp_path):
    """The traversal lesson from #108 review 2101, applied to these rules."""
    r = _guard(_bash("cat ~/.local/share/pins/eunomia/bin/../bin/fleet-secret-guard"),
               tmp_path)
    assert r.returncode == 2, r.stderr


def test_documented_matcher_covers_write():
    """The Write/Edit branch is dead code unless settings.json matches those
    tools. The install snippet in the docstring is the thing people copy, so it
    is what gets pinned — a mismatch here ships a control that never runs."""
    src = (BIN / "fleet-secret-guard").read_text()
    assert '"matcher": "Bash|Read|Grep|Write|Edit"' in src, (
        "the documented matcher does not list Write|Edit, so the branch that "
        "protects the guard from being overwritten never executes")


# ------------------------------------------------- plan 0071: one list, new families

import random  # noqa: E402
import re  # noqa: E402
import string  # noqa: E402

import pytest  # noqa: E402

_ALNUM = string.ascii_letters + string.digits
_URLSAFE = _ALNUM + "-_"
_HEX = "0123456789abcdef"


def _rand(alphabet, n, seed):
    """A mixed-alphabet run. Seeded so a failure reproduces, and generated rather
    than written out so this file carries no literal token for a transcript
    that echoes it to page on."""
    rng = random.Random(seed)
    while True:
        s = "".join(rng.choice(alphabet) for _ in range(n))
        # bao-legacy needs a digit; make every fixture carry one of each class
        if any(c.isdigit() for c in s) and any(c.isalpha() for c in s):
            return s


# family -> (text carrying the fixture, paranoid tier?)
def _families():
    import base64
    tunnel = base64.b64encode(json.dumps(
        {"a": _rand(_HEX, 32, 1), "t": _rand(_ALNUM, 44, 2), "s": _rand(_ALNUM, 24, 3)}
    ).encode()).decode().rstrip("=")
    return {
        "bao-token": ("hvs." + _rand(_ALNUM, 30, 4), False),
        "bao-legacy": ("s." + _rand(_ALNUM, 24, 5), False),
        "anthropic-key": ("sk-ant-api03-" + _rand(_URLSAFE, 40, 6), False),
        "cf-tunnel": (tunnel, False),
        "forgejo-pat": ("Authorization: token " + _rand(_HEX, 40, 7), False),
        "forgejo-pat-context": ("forgejo pat is " + _rand(_HEX, 40, 8), True),
        "angelia-token": ('{"token": "' + _rand(_URLSAFE, 43, 9) + '"}', True),
    }


@pytest.mark.parametrize("family", sorted(_families()))
def test_each_new_family_pages_once_and_not_twice(family, tmp_path, monkeypatch):
    mod = _load_watch()
    text, paranoid = _families()[family]
    rules = dict(mod.PARANOID_FINDINGS if paranoid else mod.FINDINGS)
    assert family in rules, f"{family} not in the {'paranoid' if paranoid else 'always'} tier"
    m = rules[family].search(text)
    assert m, "fixture is not shaped like the family"
    # a future tightening of _synthetic must fail here, not silently mute a family
    assert mod._synthetic(m.group(0)) is False

    projects = tmp_path / "projects"
    _transcript(projects, "s1.jsonl", [_result_line(f"output: {text}")])
    monkeypatch.setattr(mod, "PROJECTS", projects)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    alerts = []
    monkeypatch.setattr(mod, "_alert", lambda rel, ln, f: alerts.append(f))
    argv = ["--paranoid"] if paranoid else []

    mod.main(argv)
    assert alerts.count(family) == 1, alerts
    alerts.clear()
    mod.main(argv)
    assert alerts == [], "an unchanged file must not page twice"
    assert text.encode() not in (tmp_path / "leak-watch.state.json").read_bytes()


def test_the_always_tier_is_not_the_noisy_tier():
    """long-hex is every git SHA and redact() runs over PR comments, where a head
    SHA is what an approval binds to (SPEC 4.6). The context-cued forgejo row and
    the angelia row are paranoid for the same reason: a commit URL on
    forge.example sits inside the 40-character cue."""
    mod = _load_watch()
    always = {n for n, _ in mod.FINDINGS}
    paranoid = {n for n, _ in mod.PARANOID_FINDINGS}
    assert {"long-hex", "assignment", "angelia-token", "forgejo-pat-context"} <= paranoid
    assert not (always & paranoid)
    sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    for text in (f"head {sha}", f"https://forge.example/operator/eunomia/commit/{sha}"):
        assert not any(rx.search(text) for _, rx in mod.FINDINGS), text


# the watcher's shapes as they stand on main at plan 0071's merge, verbatim
_OLD_WATCHER = [
    ("github-pat", r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}"),
    ("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{20,}"),
    ("openai-key", r"\bsk-[A-Za-z0-9_-]{20,}"),
    ("jwt-or-tunnel", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}"),
    ("private-key", r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    ("aws-key", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
]
_OLD_PARANOID = [
    ("assignment", r"(?i)\b(?:bearer|api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*\S{8,}"),
    ("long-hex", r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{40,}(?![A-Fa-f0-9])"),
]
_OLD_SAMPLES = {
    "github-pat": ["ghp_" + _rand(_ALNUM, 36, 11), "gho_" + _rand(_ALNUM, 36, 12),
                   "ghu_" + _rand(_ALNUM, 36, 13), "ghs_" + _rand(_ALNUM, 36, 14),
                   "ghr_" + _rand(_ALNUM, 36, 15), "github_pat_" + _rand(_URLSAFE, 60, 16)],
    "slack-token": ["xoxb-" + _rand(_ALNUM + "-", 30, 17), "xoxa-" + _rand(_ALNUM, 25, 18)],
    "openai-key": ["sk-" + _rand(_ALNUM, 40, 19), "sk-proj-" + _rand(_URLSAFE, 40, 20),
                   "sk-ant-api03-" + _rand(_URLSAFE, 40, 21),
                   "sk-ant-oat01-" + _rand(_URLSAFE, 40, 22)],
    "jwt-or-tunnel": ["eyJ" + _rand(_URLSAFE, 20, 23) + "." + _rand(_URLSAFE, 12, 24)],
    "private-key": ["-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----",
                    "-----BEGIN EC PRIVATE KEY-----", "-----BEGIN OPENSSH PRIVATE KEY-----"],
    "aws-key": ["AKIA" + "".join(random.Random(25).choices(string.ascii_uppercase, k=16)),
                "ASIA" + "".join(random.Random(26).choices(string.ascii_uppercase, k=16))],
    "assignment": ["password = " + _rand(_ALNUM, 20, 27), "Bearer: " + _rand(_ALNUM, 20, 28),
                   "api_key=" + _rand(_ALNUM, 20, 29)],
    "long-hex": [_rand(_HEX, 40, 30), _rand(_HEX, 64, 31)],
}


def test_union_nothing_the_watcher_matched_before_is_lost():
    mod = _load_watch()
    now = mod.FINDINGS + mod.PARANOID_FINDINGS
    for name, rx in _OLD_WATCHER + _OLD_PARANOID:
        old = re.compile(rx)
        for s in _OLD_SAMPLES[name]:
            assert old.search(s), f"sample for {name} is not shaped like {name}"
            assert any(r.search(s) for _, r in now), f"{name}: a shape the watcher caught is lost"
        # ... and the named row still exists, in the tier it was in
        tier = mod.FINDINGS if (name, rx) in _OLD_WATCHER else mod.PARANOID_FINDINGS
        assert name in {n for n, _ in tier}, name


def test_conf_rows_are_widened_to_the_union_for_the_cr_gate():
    """The conf's old openai-key missed sk-ant- and its github-pat missed gho_;
    validate_cr reads these rows, so the widening is asserted on that path."""
    sys.path.insert(0, str(BIN))
    import fleetlib
    gate = [(n, rx) for n, rx, tier in fleetlib._content_rules() if tier == "always"]
    for s in _OLD_SAMPLES["github-pat"] + _OLD_SAMPLES["openai-key"] + _OLD_SAMPLES["aws-key"]:
        assert any(rx.search(s) for _, rx in gate), "CR gate lost a shape"
    # the paranoid tier never reaches the CR gate: a patch may carry a git SHA
    assert not any(rx.search("bump to " + _rand(_HEX, 40, 32)) for _, rx in gate)


def test_a_content_row_without_the_tier_column_reads_as_always(tmp_path):
    root = tmp_path / "r"
    (root / "bin").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "secret-patterns.conf").write_text("content\told-row\tzzz[0-9]+\n")
    lib = root / "bin" / "fleetlib.py"
    lib.write_text((BIN / "fleetlib.py").read_text())
    spec = importlib.util.spec_from_file_location("fleetlib_scratch", lib)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert [(n, t) for n, _, t in m._content_rules()] == [("old-row", "always")]


def test_watcher_import_survives_an_unreadable_conf(tmp_path, capsys):
    """orchestrator._redaction_patterns has no error path: the module must fall
    back to the built-in shapes, loudly, rather than raise at import."""
    import shutil
    (tmp_path / "bin").mkdir()
    shutil.copy(BIN / "fleetlib.py", tmp_path / "bin" / "fleetlib.py")
    shutil.copy(BIN / "fleet-leak-watch", tmp_path / "bin" / "fleet-leak-watch")
    loader = importlib.machinery.SourceFileLoader("lw_nc", str(tmp_path / "bin" / "fleet-leak-watch"))
    spec = importlib.util.spec_from_loader("lw_nc", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)          # no config/ dir at all
    assert {n for n, _ in mod.FINDINGS} == {n for n, _ in _OLD_WATCHER}
    assert {n for n, _ in mod.PARANOID_FINDINGS} == {n for n, _ in _OLD_PARANOID}
    assert "fallback" in capsys.readouterr().err


def test_guard_parses_the_extended_conf_and_still_blocks_the_historical_leaks(tmp_path):
    assert re.search(r"^content\t[^\t]+\t[^\t]+\t(always|paranoid)$", REAL_CONF, re.M)
    for cmd in ('ssh tunnelhost "systemctl cat cloudflared"',
                'grep -B2 WEBHOOK_SECRET ~/minos/minos.env',
                'sed -n 1,5p ~/sniff-cf.env'):
        r, _ = _guard_with_conf(_bash(cmd), tmp_path, REAL_CONF)
        assert r.returncode == 2, (cmd, r.stderr)
    r, _ = _guard_with_conf(BENIGN, tmp_path, REAL_CONF)
    assert r.returncode == 0, r.stderr
