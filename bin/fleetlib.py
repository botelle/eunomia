"""fleetlib — the ledger's shared protocol. Library, not a CLI.

Implements SPEC.md's Locking protocol and Lease record contracts for
fleet-claim / fleet-release / fleet-status. Three rules bind everything here:

  * Writers flock the PERMANENT per-lease lockfile in locks/ — never the data
    file, because rename would invalidate a held lock — then re-read, validate
    the expected state still holds, write a temp file in the same directory,
    and rename. Readers never lock: rename is atomic, so they see a complete
    old record or a complete new one, never a torn write.
  * Stored states are exactly assigned | active | released. Orphaned is
    DERIVED, never stored: an earlier SPEC draft stored it while forbidding
    anyone to write it, which made takeover unsatisfiable.
  * Pool rows (holder null) are queued work, never orphans.
"""
import collections
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def fleet_dir() -> Path:
    return Path(os.environ.get("EUNOMIA_FLEET_DIR", str(Path.home() / "dev" / ".fleet")))


def leases_dir() -> Path:
    return fleet_dir() / "leases"


def locks_dir() -> Path:
    return fleet_dir() / "locks"


def sessions_dir() -> Path:
    return fleet_dir() / "sessions"


STATES = ("assigned", "active", "released")
RESOURCE_TYPES = ("branch", "paths", "sim", "service", "budget")
# No `/` in the class: ids become file paths, and _SLUG_RE (fleet-claim) already
# strips slashes from generated slugs, so a legitimate id never contains one.
# With `/` gone a residual `..` is a single harmless filename component
# (`branch--..--001.json`), not a traversal (revbot 1522 H1).
# `\Z`, not `$`: `$` matches before a terminal newline, admitting "…--001\n"
# (same trap as fleet-heartbeat's _SID_RE). `\d{3,}`: leases are never deleted,
# so a hot slug's 1000th grant must widen the counter, not hard-fail (1522 r6).
_ID_RE = re.compile(r"^[a-z]+--[A-Za-z0-9._-]+--\d{3,}\Z")

# Session/holder ids also become path components (sessions/<sid>/hb), so the
# library must hold the same line fleet-heartbeat's _SID_RE does (1522 r9 L1).
_SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

ACTIVATE_TIMEOUT_MIN = int(os.environ.get("EUNOMIA_ACTIVATE_TIMEOUT", "10"))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def session_id() -> str:
    sid = os.environ.get("EUNOMIA_SESSION", "").strip()
    if not sid:
        sys.exit("fleet: no session identity — set EUNOMIA_SESSION")
    if not _SID_RE.match(sid):
        sys.exit("fleet: EUNOMIA_SESSION is not a valid session id "
                 "(alphanumeric lead, [A-Za-z0-9._-], max 128) — it becomes a path")
    return sid


def validate_lease_id(lease_id: str) -> str:
    """Lease ids become file paths; refuse anything that isn't id-shaped before
    it can traverse. (Closes review 1493's dead-regex nit by using the regex.)"""
    if not _ID_RE.match(lease_id or ""):
        sys.exit(f"fleet: {lease_id!r} is not a lease id (type--slug--NNN)")
    return lease_id


def lease_path(lease_id: str) -> Path:
    return leases_dir() / f"{lease_id}.json"


def marker_path(lease_id: str) -> Path:
    return leases_dir() / f"{lease_id}.orphaned"


def lock_path(lease_id: str) -> Path:
    return locks_dir() / f"{lease_id}.lock"


class LockUnavailable(Exception):
    """Raised by held_lock(blocking=False) when the lock is already held."""


class held_lock:
    """flock(EX) on a PERMANENT lockfile. The lockfile is never the data file
    and never deleted — rename of the data file must not invalidate a held lock.

    blocking=False takes LOCK_NB and raises LockUnavailable instead of waiting —
    for a lock-free-by-design reader (fleet-status) that wants to attempt an
    idempotent write without ever being wedged by a hung-alive holder."""

    def __init__(self, path: Path, blocking: bool = True):
        self.path = path
        self.blocking = blocking
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        flags = fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(self.fd, flags)
        except BlockingIOError:
            # ONLY non-blocking contention becomes LockUnavailable. A real lock
            # failure (ENOLCK on a network fs, EBADF) must propagate as the OSError
            # it is, not be mistaken for "someone else holds it" and silently
            # suppress the caller's write (1522 r3 L1).
            os.close(self.fd)
            self.fd = None
            raise LockUnavailable(self.path)
        except BaseException:
            # Any other failure (ENOLCK, or a KeyboardInterrupt landing while
            # blocked): don't leak the fd — __exit__ never runs when __enter__
            # raises (1522 r8 L2).
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
        return False


def read_lease(lease_id: str):
    """Lock-free read. Rename atomicity guarantees a whole record or none."""
    try:
        return json.loads(lease_path(lease_id).read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        # A torn lease file cannot happen through this library (temp+rename);
        # seeing one means something else wrote the tree. Loud, not silent.
        sys.exit(f"fleet: {lease_path(lease_id)} is not valid JSON — "
                 "the ledger was written by something other than fleetlib")


def write_lease(record: dict):
    """Temp-in-same-dir + rename. Caller must hold the lease's lock.

    The destination is keyed on the record's OWN id. Shape validation here closes
    the TRAVERSAL half of the ledger-content H1 for every writer (1522 r5 M1); it
    cannot close ALIASING (a body id that is a different, valid id) — that is an
    identity question the mutating commands answer at read time via
    read_lease_checked (1522 r8 M1)."""
    lease_id = record.get("id")
    if not (isinstance(lease_id, str) and _ID_RE.match(lease_id)):
        raise ValueError(f"refusing to write lease with non-id body {lease_id!r}")
    dest = lease_path(lease_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.rename(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def all_leases():
    out = []
    d = leases_dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            rec = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            # a reader sweep must not die on one bad file; report on stderr
            print(f"fleet: WARN unreadable lease file {p}", file=sys.stderr)
            continue
        if not isinstance(rec, dict):
            # `[1]` is valid JSON; rec.get would AttributeError downstream and
            # kill the sweep (1522 r8 M2) — degrade the row, never the sweep.
            print(f"fleet: WARN non-record lease file {p} — skipped", file=sys.stderr)
            continue
        out.append(rec)
    return out


def resource_of(record: dict) -> dict:
    """record["resource"] if it is a dict, else {}. `.get("resource", {})` only
    defaults when the KEY is absent — a hand-edited non-dict value (a bare
    string) reaches `.get` and AttributeErrors the whole sweep (1522 r8 M2)."""
    res = record.get("resource")
    return res if isinstance(res, dict) else {}


def read_lease_checked(lease_id: str):
    """read_lease + identity check. Shape-validation is not identity-validation:
    a body whose id is a DIFFERENT valid id (an operator copied a lease file as a
    template and forgot to fix the body id) would make write_lease silently
    create/clobber the alias target under the ARGUMENT's lock while the argument
    lease never updates (1522 r8 M1). Refuse loudly instead."""
    rec = read_lease(lease_id)
    if rec is not None and rec.get("id") != lease_id:
        sys.exit(f"fleet: {lease_id} body id {rec.get('id')!r} does not match its "
                 "filename — hand-edited alias; refusing to act on it")
    return rec


def liveness(record: dict):
    """The instant the holder was last known alive: hb mtime, falling back to
    `activated` while no hb file exists (the heartbeat hook is row 4 — without
    the fallback, every active lease would be instantly claimable pre-row-4)."""
    holder = record.get("holder")
    if not holder:
        return None
    if not (isinstance(holder, str) and _SID_RE.match(holder)):
        # a hand-edited holder (123, a dict, NUL in the string) must degrade to
        # the activated fallback, not TypeError the whole sweep — and must never
        # be composed into a path (1522 r9 M1/L1).
        return parse_ts(record.get("activated"))
    hb = sessions_dir() / holder / "hb"
    try:
        return datetime.fromtimestamp(hb.stat().st_mtime, tz=timezone.utc)
    except FileNotFoundError:
        return parse_ts(record.get("activated"))


def is_orphaned(record: dict, at: datetime = None) -> bool:
    """DERIVED, never stored. Pool rows (holder null) are queued work, never
    orphans — without the holder qualifier every aging pool row would
    spuriously orphan (SPEC review 1314)."""
    if not record.get("holder"):
        return False
    at = at or now_utc()
    state = record.get("state")
    if state == "assigned":
        created = parse_ts(record.get("created"))
        return bool(created) and at - created > timedelta(minutes=ACTIVATE_TIMEOUT_MIN)
    if state == "active":
        alive = liveness(record)
        try:
            ttl = int(record.get("ttl_minutes") or 0)
        except (TypeError, ValueError):
            # A hand-edited lease must degrade THIS row, never blind the sweep
            # to every other lease. Unparseable ttl = conservatively live.
            print(f"fleet: WARN non-integer ttl_minutes on {record.get('id')}",
                  file=sys.stderr)
            return False
        if alive is None or ttl <= 0:
            return False
        return at - alive > timedelta(minutes=ttl)
    return False


def emit(event_type, lease_id, actor, detail=None):
    """Emit through row 2's fleet-emit, imported as a sibling module so the
    append goes through the same lock/rotation/repair path as every producer.
    The ledger file is the authority and the log is awareness (Principle 3), so
    the transition stands even if the emit fails — but a broken log must never
    be silent: the failure is printed and the caller exits non-zero."""
    import importlib.machinery
    import importlib.util

    src = Path(__file__).resolve().parent / "fleet-emit"
    loader = importlib.machinery.SourceFileLoader("_fleet_emit", str(src))
    spec = importlib.util.spec_from_loader("_fleet_emit", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    # fleet-emit resolves its tree at import; align it to ours.
    mod.FLEET_DIR = fleet_dir()
    mod.EVENTS = fleet_dir() / "events.jsonl"
    mod.LOCK = fleet_dir() / "locks" / "events.lock"
    mod.emit(event_type, actor, lease=lease_id, detail=detail)


def touch_heartbeat(sid: str):
    if not _SID_RE.match(sid or ""):
        return                  # never compose junk into a path; hb is best-effort
    hb = sessions_dir() / sid / "hb"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.touch()


# ---------------------------------------------------------------- process identity (0012)
# Shared by fleet-bind (writer) and, from plan 0014, fleet-pkill (reader) — a
# SessionStart hook importing a kill CLI would run the kill CLI's side effects
# wherever a session starts, so the walk lives here instead (plan 0012 §3).

def _lstart(pid: int):
    """Process start time — the robust identity that survives setproctitle
    retitling (gunicorn, postgres, nginx rewrite argv) and still catches pid
    reuse. Copied verbatim from fleet-pkill's own `_lstart` (1522 #12 r4/r5
    M2); fleet-pkill's copy is retired under plan 0014's lease, not this
    one's. LC_ALL=C: lstart is locale-formatted, and a value recorded from one
    locale must still compare equal when read back from another."""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True,
                           errors="replace", timeout=10,
                           env={**os.environ, "LC_ALL": "C"})
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def proc_table():
    """pid -> (ppid, basename) for every process, from `ps -axo pid=,ppid=,comm=`.
    `comm=` is the executable PATH with no argv — a raw command line can carry
    secrets (--password=, Bearer tokens) and a shell wrapper's argv doesn't
    identify the program either; the basename of the executable is what
    ancestry matching needs and nothing more."""
    try:
        r = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="],
                           capture_output=True, text=True,
                           errors="replace", timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if r.returncode != 0:
        return {}
    table = {}
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        table[pid] = (ppid, os.path.basename(parts[2]))
    return table


def nearest_ancestor_pid(start_pid: int, basename: str, table=None, max_hops: int = 8):
    """Walk up from `start_pid` via ppid links, matching the ancestor's
    executable basename EXACTLY (case-sensitive — `claude` the CLI engine and
    `Claude` the desktop app shell are different processes at different
    depths, and taking the outer one binds the wrong pid). Returns the
    NEAREST match and stops there: a `claude -p` reviewer or orchestrator-
    spawned implementer routinely nests a second match further up, and
    binding that one would name the outer process's pid with the outer
    process's genuine start time — a record that passes every liveness check
    while naming someone else's session (plan 0012 §2).

    Bounded to `max_hops` candidates and stops at pid 1 or a repeated pid
    (cycle-shaped ancestry): both return None, i.e. "record nothing", never a
    guess."""
    if table is None:
        table = proc_table()
    pid = start_pid
    seen = set()
    hops = 0
    while pid > 1 and pid not in seen and hops < max_hops:
        seen.add(pid)
        entry = table.get(pid)
        if entry is None:
            return None
        ppid, name = entry
        if name == basename:
            return pid
        pid = ppid
        hops += 1
    return None


# ---------------------------------------------------------------- change-requests
# (row 5, plan 0007). Every lesson from PR #9 is load-bearing here: validate
# BEFORE pathing (H1), identity not just shape (r8 M1), degrade the row never
# the sweep (r4 M2), and nothing secret-shaped in a tree atlas renders (P6).

CR_STATES = ("filed", "applied", "rejected")
_CR_REQUIRED = ("id", "repo", "lane", "target", "state", "filed")
_CR_OPTIONAL = ("intent", "patch", "for_pr", "applied_commit", "applied_at")
# repo components lead with an alphanumeric, so "." / ".." can never be a
# component and cr/<owner>/<name>/ cannot climb. Lowercase only (1522 #11 r6):
# Forgejo repo identity is case-insensitive but every gate here is exact-string,
# so a case-typo'd grant would mint a parallel integrator slot / CR tree for
# the "same" repo (and collide confusingly on case-insensitive APFS).
_REPO_COMP_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")
_LANE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}\Z")
_CR_ID_RE = re.compile(r"^CR-[a-z0-9][a-z0-9-]{0,31}-\d{3,}\Z")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}\Z")


def cr_dir() -> Path:
    return fleet_dir() / "cr"


def validate_repo(repo) -> str:
    parts = repo.split("/") if isinstance(repo, str) else []
    if len(parts) != 2 or not all(_REPO_COMP_RE.match(c) for c in parts):
        sys.exit(f"fleet: {repo!r} is not an owner/name repo")
    return repo


def validate_lane(lane) -> str:
    if not (isinstance(lane, str) and _LANE_RE.match(lane)):
        sys.exit(f"fleet: {lane!r} is not a lane (lowercase alnum + dashes)")
    return lane


def validate_cr_id(cr_id) -> str:
    if not (isinstance(cr_id, str) and _CR_ID_RE.match(cr_id)):
        sys.exit(f"fleet: {cr_id!r} is not a CR id (CR-<lane>-<nnn>)")
    return cr_id


def validate_sha(sha) -> str:
    if not (isinstance(sha, str) and _SHA_RE.match(sha)):
        sys.exit(f"fleet: {sha!r} is not a commit sha")
    return sha


def cr_path(repo: str, cr_id: str) -> Path:
    """cr/<owner>/<name>/<lane>-<nnn>.json — id CR-w1-007 <-> file w1-007.json.
    Both inputs must be pre-validated (they become path components)."""
    return cr_dir() / repo / (cr_id[3:] + ".json")


def _repo_slug(repo: str) -> str:
    """Injective repo -> filename encoding (escape the escape char first): a
    naive '/'->'--' collides when components themselves contain '--'
    ('a--b/c' vs 'a/b--c', 1522 #11 r3 low). Over-serialization from that was
    benign, but the scheme is now shared with the role lock — keep it exact."""
    return repo.replace("_", "__").replace("/", "_s_")


def cr_lock_path(repo: str) -> Path:
    """One lock per repo queue: allocation AND state transitions serialize on
    it. Coarser than per-CR, but a repo's CR traffic is small and one lock
    removes the alloc-vs-transition race class entirely."""
    return locks_dir() / ("cr-" + _repo_slug(repo) + ".lock")


def role_lock_path(repo: str) -> Path:
    """Serializes integrator-role grant scans (1522 #11 r3 MEDIUM): the
    one-per-repo refusal is scan-then-write, and two concurrent --assigns with
    different branch names hold different LEASE locks — the same class
    pool-<type>.lock already closes for --next."""
    return locks_dir() / ("role-" + _repo_slug(repo) + ".lock")


def resource_lock_path(resource: dict) -> Path:
    """Serializes grant scans for ONE resource, as role_lock_path does for the
    integrator role.

    Two concurrent --assigns naming the same branch hold DIFFERENT lease locks
    (the ids differ), so without a lock keyed on the resource itself both scan a
    conflict-free ledger and both write — 1522 #11 r3 MEDIUM, one resource kind
    over. Lock order resource-lock -> lease-lock; nothing takes them reversed.

    The key is built from the identifying fields in a fixed order, each through
    the injective encoder _repo_slug uses, so 'a--b/c' and 'a/b--c' cannot land
    on one lock file."""
    parts = [_repo_slug(str(resource.get("type", "")))]
    for k in ("repo", "branch", "path", "role", "service", "udid", "name"):
        v = resource.get(k)
        if v:
            parts.append(_repo_slug(f"{k}={v}"))
    return locks_dir() / ("res-" + "-".join(parts) + ".lock")


def _content_rules():
    """kind=content rows of config/secret-patterns.conf — the same versioned
    file fleet-secret-guard reads (it ignores this kind), so "what a secret
    looks like" has one source of truth. Extending the list is a PR."""
    conf = Path(__file__).resolve().parent.parent / "config" / "secret-patterns.conf"
    rules = []
    try:
        text = conf.read_text()
    except OSError as e:
        # FAIL LOUD (1522 #11 M1): a missing/unreadable conf must never silently
        # disable the one gate keeping secrets out of an atlas-rendered tree.
        sys.exit(f"fleet: cannot read {conf} ({e.__class__.__name__}) — "
                 "refusing to validate CRs blind")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            kind, name, rx = line.split("\t", 2)
        except ValueError:
            # The conf is versioned and fixed-format: a non-comment line that
            # doesn't parse is a DEFECT (a tabs->spaces accident on one line
            # would silently un-enforce one rule while the rest keep the parse
            # non-empty — 1522 #11 r3 low). Loud, like the invalid-regex branch.
            sys.exit(f"fleet: unparseable rule line in {conf}: a non-comment "
                     "line must be KIND<TAB>NAME<TAB>REGEX")
        if kind == "content":
            try:
                # re.I: the conf header promises case-insensitive rules; this
                # reader honors the same contract the guard does (#11 L1).
                rules.append((name, re.compile(rx, re.I)))
            except re.error as e:
                sys.exit(f"fleet: secret rule '{name}' is not a valid regex "
                         f"({e}) — fix config/secret-patterns.conf")
    if not rules:
        # The merged conf always carries content rows, so an empty parse means
        # the file degraded (tabs->spaces, block deleted) — the same
        # scan-against-zero-rules failure the missing-conf exit guards, through
        # a different door (1522 #11 r2 M2).
        sys.exit(f"fleet: {conf} parsed to zero content rules — refusing to "
                 "validate CRs blind")
    return rules


def validate_cr(record) -> dict:
    """The full CR contract: closed key set, valid ids, intent-or-patch, and
    NOTHING secret-shaped in the text fields (Principle 6 — atlas renders this
    tree). `file` runs it before the first write; `apply` re-runs it so nothing
    can be laundered in by hand-editing between filing and applying."""
    if not isinstance(record, dict):
        sys.exit("fleet: CR record must be a JSON object")
    missing = [k for k in _CR_REQUIRED if k not in record]
    extra = [k for k in record if k not in _CR_REQUIRED + _CR_OPTIONAL]
    if missing or extra:
        sys.exit(f"fleet: CR record malformed (missing={missing} extra={extra})")
    validate_cr_id(record["id"])
    validate_repo(record["repo"])
    validate_lane(record["lane"])
    if record["state"] not in CR_STATES:
        sys.exit(f"fleet: CR state {record['state']!r} not in {CR_STATES}")
    # full-equality reconstruction, not a prefix check: lane w1 must not
    # accept id CR-w1-2-003 (lane w1-2's) (#11 L2)
    if not re.match("^CR-" + re.escape(record["lane"]) + r"-\d{3,}\Z", record["id"]):
        sys.exit(f"fleet: CR id {record['id']!r} does not match lane {record['lane']!r}")
    if not (isinstance(record.get("target"), str) and record["target"].strip()):
        sys.exit("fleet: CR target must be a non-empty string")
    if not (record.get("intent") or record.get("patch")):
        sys.exit("fleet: a CR needs intent prose and/or a patch (SPEC \u00a7CR)")
    # Structural typing for the fields the secret scan does NOT read (#11 r2
    # M1): a timestamp that must parse, a sha that must be a sha, and an int
    # cannot carry a token — without this, a hand-edited "for_pr": "ghp_..."
    # laundered straight through apply into the rendered tree.
    if parse_ts(record["filed"]) is None:
        sys.exit("fleet: CR filed must be a YYYY-MM-DDTHH:MM:SSZ timestamp")
    if "applied_at" in record and parse_ts(record["applied_at"]) is None:
        sys.exit("fleet: CR applied_at must be a YYYY-MM-DDTHH:MM:SSZ timestamp")
    if "applied_commit" in record:
        validate_sha(record["applied_commit"])
    if "for_pr" in record and (isinstance(record["for_pr"], bool)
                               or not isinstance(record["for_pr"], int)):
        sys.exit("fleet: CR for_pr must be an integer")
    # Size caps (#11 r2 L4): one fat CR must not bloat the rendered tree or an
    # events.jsonl line. Generous — a real patch fits; a dumped corpus doesn't.
    for field, cap in (("target", 512), ("intent", 16384), ("patch", 524288)):
        v = record.get(field)
        if isinstance(v, str) and len(v.encode("utf-8")) > cap:
            sys.exit(f"fleet: CR {field} exceeds {cap} bytes — trim it or "
                     "reference the material instead of inlining it")
    # target included (#11 M2): it rides the rendered cr/ tree, cr-filed
    # detail in events.jsonl, and list output — it is a text field.
    rules = _content_rules()      # hoisted: one read per validation (#11 r2 L3)
    for field in ("intent", "patch", "target"):
        text = record.get(field)
        if text is None:
            continue
        if not isinstance(text, str):
            sys.exit(f"fleet: CR {field} must be a string")
        for name, rx in rules:
            if rx.search(text):
                # Name the rule, NEVER the content.
                sys.exit(f"fleet: CR {field} matches secret rule '{name}' — "
                         "secrets never ride the CR queue (Principle 6); "
                         "reference the keyvault kv path instead")
    return record


def read_cr_checked(repo: str, cr_id: str):
    """Read + identity check (the r8 M1 lesson, applied to CRs): a body whose id
    differs from its filename is a template-copy alias — refuse loudly."""
    try:
        rec = json.loads(cr_path(repo, cr_id).read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        sys.exit(f"fleet: {cr_path(repo, cr_id)} is not valid JSON — "
                 "the CR queue was written by something other than fleet-cr")
    if not isinstance(rec, dict) or rec.get("id") != cr_id or rec.get("repo") != repo:
        sys.exit(f"fleet: {cr_id} body identity does not match its path — "
                 "hand-edited alias; refusing to act on it")
    return rec


def write_cr(record: dict):
    """Temp-in-same-dir + rename, destination from the VALIDATED record.
    Caller must hold the repo's CR lock."""
    validate_cr(record)
    dest = cr_path(record["repo"], record["id"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.rename(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------- workspaces (ADR-0001)
# One git worktree per lease, attached to a shared per-repo clone. The decision
# and its alternatives are in docs/adr/0001-orchestrator-workspace.md; what
# matters here is WHY the key is a lease id: fleet-watch's dispatch lease is on
# the BRANCH, so two plans in one repo dispatch concurrently, and a workspace
# keyed on anything that can collide reintroduces that bug one layer down.
#
# Env is read at CALL time, not import: these helpers are loaded once per test
# session but configured per test, and an import-time read makes the suite
# order-dependent (the class ci.yml runs pytest-randomly to catch).

def heartbeat_secs(default: int = 60) -> int:
    """FLEET_HEARTBEAT_SECS, parsed defensively.

    A bare int() on this raised ValueError inside the orchestrator AFTER it had
    activated its lease — leaving a live lease with no heartbeat behind it,
    which is the exact state the heartbeat exists to make detectable. A
    malformed knob must degrade to the default, never strand a lease."""
    try:
        v = int(os.environ.get("FLEET_HEARTBEAT_SECS", ""))
    except (TypeError, ValueError):
        return default
    return v if 5 <= v <= 3600 else default


def lease_ttl_minutes() -> int:
    """The ttl a dispatch lease should carry, sized against the heartbeat.

    fleet-claim's --ttl defaults to 240 minutes, and is_orphaned takes a
    DIFFERENT branch once a lease is activated: `assigned` orphans on a 10
    minute activate timeout, `active` orphans only after ttl_minutes of
    heartbeat silence. So the first code to call --activate silently moved
    dead-orchestrator detection from 10 minutes to 4 hours. Five missed beats,
    floor of two minutes."""
    return max(2, -(-heartbeat_secs() * 5 // 60))


_WT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def work_root() -> Path:
    """Inside the fleet tree, not beside it (ADR-0001 §1).

    A default of ~/.fleet/work would put worktrees in a SECOND state root while
    the leases they are keyed on live in ~/dev/.fleet — so clearing the
    documented runtime tree would recycle lease ids that surviving worktrees
    still hold. One root means they are cleared together or not at all."""
    env = os.environ.get("FLEET_WORK_ROOT")
    return Path(env).expanduser() if env else fleet_dir() / "work"


def git_ssh_base() -> str:
    return os.environ.get("FLEET_GIT_SSH_BASE",
                          "ssh://implbot@forge.example:2222").rstrip("/")


# ------------------------------------------------------------- agent roles (0056)
# Author, reviewer, merger are the design (coder != reviewer != merger, two
# approvals, never self-merge) — this fleet just happens to run them as
# implbot, revbot and operator. A deployment under different forge
# accounts configures FLEET_AUTHOR_ACCOUNT / FLEET_REVIEWER_ACCOUNT /
# FLEET_MERGER_ACCOUNT; the three POSITIONS are not configurable, only the
# names filling them. docs/operating.md carries this fleet's own mapping.
#
# Env is read at CALL time, not import (same reason as the workspace helpers
# above): these are loaded once per test session but configured per test.
Role = collections.namedtuple("Role", "account logins commit_identity")


class RoleError(ValueError):
    """A role built whose own account does not count as itself.

    This is r7 M2's failure (fleet-watch), generalised: dedupe was bound to
    uids alone, and the caller that unioned in the configured extra logins
    forgot to also union in the literal `implbot` — so a rename made every
    historical marked PR invisible and re-dispatched every merged plan,
    silently. Refusing here makes that omission impossible to reintroduce at
    a call site, rather than trusting each call site to remember it."""


def _role(name, account, logins, commit_identity=None):
    logins = frozenset(l.strip().lower() for l in logins if l.strip())
    if account.strip().lower() not in logins:
        raise RoleError(
            f"role {name!r}: login set {sorted(logins)!r} omits its own "
            f"account {account!r}")
    return Role(account=account, logins=logins, commit_identity=commit_identity)


def _extra_names(env_key):
    """Lowercased extra names from a comma-separated env var."""
    return {x.strip().lower() for x in os.environ.get(env_key, "").split(",")
            if x.strip()}


# This fleet's own accounts. Each has its own override key so a deployment can
# rename one position without touching the others.
_ROLE_ACCOUNT_DEFAULTS = {
    "author": ("FLEET_AUTHOR_ACCOUNT", "implbot"),
    "reviewer": ("FLEET_REVIEWER_ACCOUNT", "revbot"),
    "merger": ("FLEET_MERGER_ACCOUNT", "operator"),
}


def agent_roles():
    """The one role table (plan 0056): author, reviewer, merger -> Role.

    Extends plan 0055's per-role configuration (`_TOKEN_ROLES`: an env key
    plus a documented default, keyed by role) from credentials to identity.
    `FLEET_AGENT_ACCOUNTS`, `FLEET_IMPLEMENTER_LOGINS` and
    `FLEET_COMMIT_IDENTITY` remain each role's own per-key override — read
    HERE, once, so fleet-watch and the orchestrator resolve one answer
    instead of each keeping its own copy of this logic."""
    account = {role: os.environ.get(env_key, default)
              for role, (env_key, default) in _ROLE_ACCOUNT_DEFAULTS.items()}
    author_logins = {account["author"]} | _extra_names("FLEET_IMPLEMENTER_LOGINS")
    commit_identity = os.environ.get(
        "FLEET_COMMIT_IDENTITY",
        f"{account['author']} <{account['author']}@example.org>")
    return {
        "author": _role("author", account["author"], author_logins,
                        commit_identity),
        "reviewer": _role("reviewer", account["reviewer"], {account["reviewer"]}),
        "merger": _role("merger", account["merger"], {account["merger"]}),
    }


def agent_accounts(roles=None):
    """Accounts that must never be able to push or merge main unsupervised
    (fleet-watch's AGENT_ACCOUNTS): author + reviewer + FLEET_AGENT_ACCOUNTS'
    extras. Never merger — merger is this fleet's one human position, the
    account the merge whitelist exists FOR."""
    roles = roles if roles is not None else agent_roles()
    return ({roles["author"].account, roles["reviewer"].account} |
            _extra_names("FLEET_AGENT_ACCOUNTS"))


def repo_slug(repo: str) -> str:
    """owner/repo -> a directory name, refusing anything not two plain
    components. Validate BEFORE pathing: this value becomes a directory.

    Delegates to _repo_slug rather than joining on '__'. A naive join is NOT
    injective, because _REPO_COMP_RE admits '_': 'foo__bar/baz' and
    'foo/bar__baz' both become 'foo__bar__baz', so two repos would share one
    clone, one lock and one work root. _repo_slug escapes the escape character
    first and exists forty lines above for exactly this reason (1522 #11 r3)."""
    parts = (repo or "").split("/")
    if len(parts) != 2 or not all(_REPO_COMP_RE.match(p) for p in parts):
        raise ValueError(f"{repo!r} is not owner/repo")
    return _repo_slug(repo)


def store_path(repo: str) -> Path:
    return work_root() / repo_slug(repo) / ".git-store"


def worktree_path(repo: str, key: str) -> Path:
    if not _WT_KEY_RE.match(key or ""):
        raise ValueError(f"{key!r} is not a workspace key")
    return work_root() / repo_slug(repo) / key


@contextlib.contextmanager
def store_lock(repo: str):
    """Serialise fetches into a shared object store. Concurrent fetches collide
    on refs/ lock files and the loser's failure reads as a network fault."""
    d = work_root() / repo_slug(repo)
    d.mkdir(parents=True, exist_ok=True)
    # the canonical name, recorded rather than decoded. An encoding needs an
    # inverse to be reversed correctly, and a sweep that guesses one reports
    # work against a repo that does not own it.
    marker = d / ".repo"
    if not marker.exists():
        marker.write_text(repo + "\n")
    with open(d / ".fetch.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _git(args, cwd, check=True, timeout=900):
    """Run git. `push` is refused here for every workspace caller: the fleet's
    write path to a forge is a reviewed merge, and a helper that can push is a
    second one (PROVENANCE §2.2)."""
    if "push" in args:
        raise AssertionError("fleetlib workspaces never push (ADR-0002); refusing")
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:200]}")
    return r


def ensure_store(repo: str, ssh_base: str = None) -> Path:
    """The per-repo backing clone. Worktrees share its object store, so the Nth
    concurrent feature costs a working tree rather than another full history."""
    store = store_path(repo)
    base = (ssh_base or git_ssh_base()).rstrip("/")
    with store_lock(repo):
        if not (store / "HEAD").exists() and not (store / ".git").exists():
            store.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["git", "clone", "--quiet", f"{base}/{repo}.git",
                                str(store)], capture_output=True, text=True, timeout=900)
            if r.returncode != 0:
                raise RuntimeError(f"clone failed: {r.stderr.strip()[:200]}")
        else:
            _git(["fetch", "--quiet", "--prune", "origin"], cwd=store)
    return store


def add_worktree(repo: str, key: str, ref: str, ssh_base: str = None,
                 refspecs=()) -> Path:
    """Attach a worktree for `key` at `ref`. Replaces an existing one for the
    same key: a stale tree from a dead run must never be silently reused as if
    it were clean."""
    store = ensure_store(repo, ssh_base)
    if refspecs:
        with store_lock(repo):
            _git(["fetch", "--quiet", "origin", *refspecs], cwd=store)
    wt = worktree_path(repo, key)
    if wt.exists():
        remove_worktree(repo, key)
    _git(["worktree", "add", "--quiet", "--detach", str(wt), ref], cwd=store)
    return wt


def remove_worktree(repo: str, key: str) -> None:
    store = store_path(repo)
    wt = worktree_path(repo, key)
    if store.exists():
        _git(["worktree", "remove", "--force", str(wt)], cwd=store, check=False)
        _git(["worktree", "prune"], cwd=store, check=False)
    shutil.rmtree(wt, ignore_errors=True)


def orphan_worktrees():
    """Worktrees keyed on a lease that is gone or released — reported, never
    removed on sight. A tree left by a crashed run is evidence, and the lease it
    is keyed to is already the fleet's signal that the run died; deleting it
    here would destroy the diagnosis to tidy the directory.

    Keys that are not lease-shaped belong to short-lived callers that clean up
    their own (fleet-candidate), and are not reported."""
    out = []
    root = work_root()
    if not root.is_dir():
        return out
    for repo_dir in sorted(root.iterdir()):
        if not repo_dir.is_dir():
            continue
        for wt in sorted(repo_dir.iterdir()):
            if not wt.is_dir() or wt.name.startswith("."):
                continue
            if not _ID_RE.match(wt.name):
                continue
            rec = read_lease(wt.name)
            if rec is None or rec.get("state") == "released":
                marker = repo_dir / ".repo"
                repo = (marker.read_text().strip()
                        if marker.exists() else f"?{repo_dir.name}")
                out.append((repo, wt.name, wt))
    return out


# ---------------------------------------------------------------- pins (0059)
# Shared between bin/fleet-watch (report, and now delayed paging) and
# bin/fleet-pin-advance (the mover) so "would the advancer heal this stale
# pin unaided" has ONE definition rather than two that can drift -- the
# extraction plan 0059 asks for by name. Neither of those files is imported
# here, and this never calls into either of them (both are `SourceFileLoader`
# from directories that do not carry the other): fleetlib is the shared
# middle, not a bridge between the two.

_PIN_REPO_RE = re.compile(r"[/:]([^/:]+/[^/]+?)(\.git)?/?$")

PIN_ADVANCE_HEALS = "heals"          # the advancer will move this unaided
PIN_ADVANCE_STANDING = "standing"    # nothing but a person clears this


def _pin_git(path, *args):
    """(ok, stdout). Never raises -- the same contract fleet-watch's and
    fleet-pin-advance's own `_git` helpers make, kept as an independent copy:
    both of those live in files this library must not import (see above)."""
    try:
        r = subprocess.run(("git", "-C", str(path)) + args,
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0, r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False, ""


def pin_origin_repo(path):
    """The Forgejo `owner/repo` a pin's `origin` remote names, or None.

    Moved here from fleet-pin-advance's own `_origin_repo` (plan 0059) so
    fleet-watch's delayed-paging check can resolve the same repo without a
    second copy of this regex to keep in sync. fleet-pin-advance's
    `_origin_repo` now delegates here; it keeps its own name because a test
    monkeypatches that exact module attribute."""
    ok, url = _pin_git(path, "remote", "get-url", "origin")
    if not ok or not url:
        return None
    m = _PIN_REPO_RE.search(url.strip())
    if not m:
        return None
    owner_repo = m.group(1)
    return owner_repo if owner_repo.count("/") == 1 else None


def pin_advance_local_refusal(path, head, target):
    """None, or a reason string -- the two checks `fleet-pin-advance` applies
    to a STALE pin before it ever asks the broker: non-fast-forward, then a
    dirty tree. Both are STANDING (plan 0059): a diverged head or a dirty
    tree does not resolve itself, so delaying a page for either buys nothing
    -- only a person clears them."""
    ok, _ = _pin_git(path, "merge-base", "--is-ancestor", head or "", target or "")
    if not ok:
        return (f"{path}: {(head or '?')[:7]} is not an ancestor of "
                f"{(target or '?')[:7]} -- refusing a non-fast-forward move "
                "(diverged, or the target is behind HEAD)")
    ok_st, porcelain = _pin_git(path, "status", "--porcelain")
    if ok_st and porcelain:
        return (f"{path}: has {len(porcelain.splitlines())} uncommitted "
                "change(s) -- refusing to check out over them")
    return None


def pin_advance_category(path, head, target, resolve_repo, broker_verdict):
    """(category, reason) -- would `fleet-pin-advance` heal this STALE pin
    unaided (PIN_ADVANCE_HEALS) or does it refuse for a reason only a person
    clears (PIN_ADVANCE_STANDING)? Mirrors `fleet-pin-advance.plan_pin`'s own
    order exactly: non-fast-forward, a dirty tree, an unparseable origin,
    then the broker's verdict.

    `resolve_repo(path)` and `broker_verdict(repo) -> (result, detail)`
    (`result` one of "ok" / "refused" / "unknown") are the CALLER's own:
    reaching the origin remote and reaching the broker are each already a
    job one specific module owns (fleet-pin-advance's `_origin_repo`,
    fleet-watch's `broker_verdict`), and this predicate does not reopen
    either door itself -- it is called with the answer, never the
    credential. Neither is invoked unless the checks before it already pass,
    so a pin already refused locally never costs a broker round trip.

    A broker that could not be reached ("unknown") is TRANSIENT, same as a
    vouched-for ("ok") pin the advancer will actually move -- both heal.
    Only a broker that answers and REFUSES is standing: nothing but a person
    protecting the branch clears that."""
    local = pin_advance_local_refusal(path, head, target)
    if local:
        return PIN_ADVANCE_STANDING, local
    repo = resolve_repo(path)
    if repo is None:
        return PIN_ADVANCE_STANDING, (
            f"{path}: could not determine the Forgejo owner/repo from its "
            "`origin` remote -- cannot ask the broker whether main is "
            "protected, and unreadable is never advance-anyway")
    result, detail = broker_verdict(repo)
    if result == "refused":
        return PIN_ADVANCE_STANDING, (
            f"{path}: {repo}'s main is not protected -- {detail}")
    return PIN_ADVANCE_HEALS, ""
