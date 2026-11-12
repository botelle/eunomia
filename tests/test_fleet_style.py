"""bin/fleet-style: the style review is declared per repository and routed by one
function (plan 0150, ADR-0025). Only an invented repository appears here."""
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
REPO = "acme/widgets"
PLAN_ID = "0007-reskin"
ED = "docs/screenshots"


def _load():
    loader = importlib.machinery.SourceFileLoader("fleet_style", str(BIN / "fleet-style"))
    spec = importlib.util.spec_from_loader("fleet_style", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fs = _load()

SHOT = {"platform": "ios-simulator", "command": "tools/shoot.sh",
        "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-26-5",
        "device": "iPhone 17", "scale": 3, "themes": ["classic", "bold"],
        "appearances": ["light", "dark"], "timeout_minutes": 10}
ENTRY = {"paths": ["app/Theme/*.swift", "app/Colors/**/*.json", "app/Sizes/?.swift"],
         "binary_paths": ["app/Art/*.png"], "screenshot": SHOT}


def _config(tmp_path, entry=None, repos=None):
    p = tmp_path / "style-review.json"
    p.write_text(json.dumps({"v": 1, "repos": repos if repos is not None
                             else {REPO: entry or ENTRY}}, indent=2))
    return p


def _plan(review="style", repo=REPO, pid=PLAN_ID):
    return f"---\nid: {pid}\nstatus: ready\nrepo: {repo}\nreview: {review}\n---\n\nbody\n"


# ------------------------------------------------------------------ matching


@pytest.mark.parametrize("pattern,path,want", [
    ("theme/**", "Theme/x.swift", False),          # case-sensitive
    ("a/*/c", "a/b/c", True),
    ("a/*/c", "a/b/x/c", False),                   # `*` never crosses `/`
    ("ios/Theme/*", "ios/Theme/Sub/Session.swift", False),
    ("ios/Theme/*", "ios/Theme/Session.swift", True),
    ("a/**/c", "a/c", True),                       # `**` is zero or more segments
    ("a/**/c", "a/x/y/c", True),
    ("a/**", "a/x/y/z", True),
    ("a/?.txt", "a/b.txt", True),
    ("a/?.txt", "a/bb.txt", False),
    ("a/[ab].txt", "a/a.txt", False),              # no character classes
    ("a/[ab].txt", "a/[ab].txt", True),
    ("a/*.swift", "a/.swift", True),
    ("a/b", "/a/b", False),                        # normalisation first
    ("a/b", "a//b", False),
    ("a/**", "a/../a/b", False),
    ("a/**", "a/./b", False),
    ("a/**", "a/b/", False),
    ("**", "a", True),
])
def test_matching(pattern, path, want):
    assert fs.match(pattern, path) is want


def test_a_dotdot_path_matches_nothing_even_a_permissive_pattern():
    assert not fs.match("a/**", "a/../../etc/passwd")


# --------------------------------------------------------------- declaration


def test_a_shipped_file_declares_nothing_and_is_valid():
    cfg = json.loads((BIN.parent / "config" / "style-review.json").read_text())
    assert cfg == {"v": 1, "repos": {}}
    assert fs.load_config() == {}


def test_declared_defaults(tmp_path):
    entry = fs.load_config(_config(tmp_path))[REPO]
    assert entry["evidence_dir"] == "docs/screenshots"
    assert entry["screenshot"]["max_image_kb"] == 300


def _refused(tmp_path, **changes):
    entry = json.loads(json.dumps(ENTRY))
    for k, v in changes.items():
        if k.startswith("shot_"):
            entry["screenshot"][k[5:]] = v
        else:
            entry[k] = v
    with pytest.raises(fs.Malformed) as e:
        fs.load_config(_config(tmp_path, entry))
    return str(e.value)


@pytest.mark.parametrize("pattern,why", [
    ("", "non-empty"), ("/abs/**", "absolute"), ("a/../b", "`..`"),
    ("**/x.swift", "starts with `**`"), ("*", "alone"), ("a/b**/c", "inside a segment"),
    ("tests/**", "tests"), ("Tests/**", "tests"), ("App/FooTests/*.swift", "tests"),
    ("app/*.strings", "strings"), ("app/*.stringsdict", "strings"),
    ("app/*.xcstrings", "strings"), ("plans/*", "plans/x.md"), ("docs/*.md", "docs/x.md"),
    (".forgejo/workflows/*", "workflows"), (".gitignore", "ignore"),
    ("project.yml", "project"), ("App.xcodeproj/*.pbxproj", "project"),
    ("tools/*.sh", "tools/shoot.sh"),                   # the screenshot command itself
    ("docs/screenshots/**", "docs/screenshots"),       # anything under evidence_dir
    ("docs/**", "docs/x.md"),
    ("app/*.png", "binary"), ("app/*.jpg", "binary"), ("app/*.pdf", "binary"),
    ("app/*.ttf", "binary"), ("app/*.otf", "binary"), ("app/*.car", "binary"),
    ("app/Assets.xcassets/**", "asset catalog"),
])
def test_a_forbidden_pattern_is_refused(tmp_path, pattern, why):
    msg = _refused(tmp_path, paths=[pattern])
    assert why in msg and "style-review.json" in msg


def test_a_contents_json_under_a_catalog_is_text_and_allowed(tmp_path):
    entry = dict(ENTRY, paths=["app/Assets.xcassets/Colors/*/Contents.json"])
    assert fs.load_config(_config(tmp_path, entry))[REPO]


def test_the_same_checks_apply_to_binary_paths(tmp_path):
    for bad in ("tests/*.png", "docs/screenshots/**", "tools/shoot.sh", "*"):
        assert "binary_paths" in _refused(tmp_path, binary_paths=[bad])


def test_a_binary_pattern_is_allowed_under_binary_paths_only(tmp_path):
    assert fs.load_config(_config(tmp_path))[REPO]["binary_paths"] == ["app/Art/*.png"]


@pytest.mark.parametrize("ed", ["/abs", "a/../b", "", "a//b"])
def test_a_bad_evidence_dir_is_refused(tmp_path, ed):
    assert "evidence_dir" in _refused(tmp_path, evidence_dir=ed)


def test_an_evidence_dir_under_a_pattern_is_refused(tmp_path):
    msg = _refused(tmp_path, evidence_dir="app/Shots/e",
                   paths=ENTRY["paths"] + ["app/Shots/**"])
    assert "app/Shots/e/x" in msg


@pytest.mark.parametrize("field,value", [
    ("platform", "android"), ("command", "/abs/shoot"), ("command", "a/../b"),
    ("scale", 0), ("scale", 4), ("scale", True), ("themes", []), ("themes", ["Bad Name"]),
    ("appearances", []), ("max_image_kb", 49), ("max_image_kb", 1001),
    ("timeout_minutes", 0), ("timeout_minutes", 61), ("runtime", ""), ("device", ""),
])
def test_a_bad_screenshot_field_is_refused(tmp_path, field, value):
    assert f"screenshot" in _refused(tmp_path, **{"shot_" + field: value})


def test_a_default_entry_is_refused(tmp_path):
    with pytest.raises(fs.Malformed, match="no `default` entry"):
        fs.load_config(_config(tmp_path, repos={"default": ENTRY}))


def test_malformed_json_names_the_line(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"v": 1,\n "repos": {,}}')
    with pytest.raises(fs.Malformed, match=r"c\.json:2:"):
        fs.load_config(p)


def test_a_semantic_error_names_the_entry_line(tmp_path):
    msg = _refused(tmp_path, paths=["tests/**"])
    assert "style-review.json:4:" in msg


def _cli(*argv, env=None, bin_dir=BIN):
    e = dict(os.environ, **(env or {}))
    return subprocess.run([sys.executable, str(bin_dir / "fleet-style"), *argv],
                          capture_output=True, text=True, env=e)


@pytest.fixture
def pinned(tmp_path):
    """A copy of bin/ whose sibling config/ is ours: fleet-style reads the config
    beside itself. The other tools are symlinks so they still find their siblings."""
    root = tmp_path / "pin"
    (root / "bin").mkdir(parents=True)
    (root / "config").mkdir()
    for f in BIN.iterdir():
        if f.name != "fleet-style":
            (root / "bin" / f.name).symlink_to(f)
    shutil.copy(BIN / "fleet-style", root / "bin" / "fleet-style")
    return root


def test_declared_cli_reports_an_entry_and_an_undeclared_repo(pinned, tmp_path):
    (pinned / "config" / "style-review.json").write_text(
        _config(tmp_path).read_text())
    r = _cli("declared", REPO, "--json", bin_dir=pinned / "bin")
    got = json.loads(r.stdout)
    assert r.returncode == 0 and got["declared"] is True
    assert got["paths"] == ENTRY["paths"] and got["evidence_dir"] == ED
    assert got["screenshot"]["max_image_kb"] == 300
    r = _cli("declared", "acme/other", "--json", bin_dir=pinned / "bin")
    assert r.returncode == 0
    assert json.loads(r.stdout) == {"repo": "acme/other", "declared": False, "paths": None,
                                    "evidence_dir": None, "screenshot": None}


def test_a_malformed_declaration_exits_2_from_declared_and_routes_full(pinned, tmp_path):
    bad = json.loads(json.dumps(ENTRY))
    bad["paths"] = ["tests/**"]
    (pinned / "config" / "style-review.json").write_text(_config(tmp_path, bad).read_text())
    r = _cli("declared", REPO, "--json", bin_dir=pinned / "bin")
    assert r.returncode == 2 and "style-review.json" in r.stderr
    g = Git(tmp_path / "g")
    base = g.commit({"app/Theme/a.swift": "1"})
    head = g.commit({"app/Theme/a.swift": "2"})
    out = _route_cli(pinned, g, base, head, plan=None, tmp=tmp_path)
    assert out["position"] == "code_reviewer"
    assert any("malformed" in r for r in out["reasons"])


# ----------------------------------------------------------------------- git


class Git:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True)
        self.run("init", "-q", "-b", "main")
        self.run("config", "user.email", "t@example.org")
        self.run("config", "user.name", "t")

    @property
    def git_dir(self):
        return self.path / ".git"

    def run(self, *args):
        r = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.decode().strip()

    def commit(self, files, delete=(), move=()):
        for old, new in move:
            (self.path / new).parent.mkdir(parents=True, exist_ok=True)
            self.run("mv", old, new)
        for name, content in files.items():
            p = self.path / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content if isinstance(content, bytes) else content.encode())
        for name in delete:
            self.run("rm", "-q", name)
        self.run("add", "-A")
        self.run("commit", "-q", "--allow-empty", "-m", "c")
        return self.run("rev-parse", "HEAD")

    def changes(self, base, head):
        out = self.run("diff", "--name-status", "-M", base, head).splitlines()
        names = {"A": "added", "M": "modified", "D": "removed"}
        res = []
        for line in out:
            parts = line.split("\t")
            if parts[0].startswith("R"):
                res.append({"filename": parts[2], "status": "renamed",
                            "previous_filename": parts[1]})
            else:
                res.append({"filename": parts[1], "status": names[parts[0][0]]})
        return res


def _route(tmp_path, g, base, head, plan=_plan(), override="none", ordinals=None,
           changes=None, pr=5, entry=None):
    return fs.route(REPO, pr, plan, changes if changes is not None else g.changes(base, head),
                    fs.Git(g.git_dir), base, head, override,
                    config=_config(tmp_path, entry),
                    ordinals=ordinals or (lambda r, p: 2))


@pytest.fixture
def g(tmp_path):
    return Git(tmp_path / "repo")


def _base(g):
    return g.commit({"app/Theme/a.swift": "a", "app/Colors/x/c.json": "{}",
                     "app/Other/o.swift": "o", "app/Art/logo.png": b"\x89PNG-old\x00\x01",
                     "plans/0007-reskin.md": "p"})


def test_a_change_inside_the_patterns_routes_style(g, tmp_path):
    base = _base(g)
    head = g.commit({"app/Theme/a.swift": "b", "app/Colors/x/c.json": '{"a":1}'})
    out = _route(tmp_path, g, base, head)
    assert out["position"] == "style_reviewer" and out["review"] == "style"
    assert out["reasons"] == [] and out["screenshot"]["device"] == "iPhone 17"
    assert out["evidence_folder"] is None and out["evidence_id"] == PLAN_ID


def test_a_path_outside_the_patterns_routes_full_naming_the_path(g, tmp_path):
    base = _base(g)
    head = g.commit({"app/Theme/a.swift": "b", "app/Other/o.swift": "x"})
    out = _route(tmp_path, g, base, head)
    assert out["position"] == "code_reviewer" and out["review"] == "full"
    assert any("app/Other/o.swift" in r for r in out["reasons"])
    assert out["screenshot"] is None


def test_a_rename_whose_old_path_is_outside_routes_full(g, tmp_path):
    base = _base(g)
    head = g.commit({}, move=[("app/Other/o.swift", "app/Theme/o.swift")])
    ch = g.changes(base, head)
    assert ch[0]["previous_filename"] == "app/Other/o.swift"
    out = _route(tmp_path, g, base, head, changes=ch)
    assert out["position"] == "code_reviewer"
    assert any("app/Other/o.swift" in r for r in out["reasons"])


def test_a_deletion_must_be_inside_too(g, tmp_path):
    base = _base(g)
    head = g.commit({}, delete=["app/Other/o.swift"])
    assert _route(tmp_path, g, base, head)["position"] == "code_reviewer"


def test_fewer_style_ordinals_than_code_ordinals_routes_full(g, tmp_path):
    base = _base(g)
    head = g.commit({"app/Theme/a.swift": "b"})
    out = _route(tmp_path, g, base, head,
                 ordinals=lambda r, p: 1 if p == "style_reviewer" else 2)
    assert out["position"] == "code_reviewer"
    assert any("ordinal" in r for r in out["reasons"])


def test_an_unresolvable_style_reviewer_routes_full(g, tmp_path):
    base = _base(g)
    head = g.commit({"app/Theme/a.swift": "b"})

    def boom(repo, position):
        raise fs.Undecided("fleet-models: exit 3")

    assert _route(tmp_path, g, base, head, ordinals=boom)["position"] == "code_reviewer"


@pytest.mark.parametrize("plan", [_plan(review="full"), "---\nid: 0007-reskin\nrepo: acme/widgets\n---\n",
                                  _plan(repo="acme/other"), _plan(review="styel")])
def test_a_plan_not_opting_in_routes_full(g, tmp_path, plan):
    base = _base(g)
    head = g.commit({"app/Theme/a.swift": "b"})
    assert _route(tmp_path, g, base, head, plan=plan)["position"] == "code_reviewer"


def test_a_planless_change_routes_by_its_diff_alone_with_a_pr_id(g, tmp_path):
    base = _base(g)
    head = g.commit({"app/Theme/a.swift": "b"})
    out = _route(tmp_path, g, base, head, plan=None, pr=12)
    assert out["position"] == "style_reviewer" and out["evidence_id"] == "pr12"
    out = _route(tmp_path, g, base, head, plan=None, override="full")
    assert out["position"] == "code_reviewer"
    assert any("Review: full" in r for r in out["reasons"])


def test_the_plans_own_file_is_dropped(g, tmp_path):
    base = _base(g)
    head = g.commit({"app/Theme/a.swift": "b", "plans/0007-reskin.md": "done"})
    assert _route(tmp_path, g, base, head)["position"] == "style_reviewer"


def test_a_plan_only_change_is_not_a_style_change(g, tmp_path):
    base = _base(g)
    head = g.commit({"plans/0007-reskin.md": "done"})
    assert _route(tmp_path, g, base, head)["position"] == "code_reviewer"


def test_an_undeclared_repo_routes_full(g, tmp_path):
    base = _base(g)
    head = g.commit({"app/Theme/a.swift": "b"})
    out = fs.route("acme/none", 5, None, g.changes(base, head), fs.Git(g.git_dir), base,
                   head, "none", config=_config(tmp_path), ordinals=lambda r, p: 2)
    assert out["position"] == "code_reviewer" and "declares no style" in out["reasons"][0]


def test_a_binary_under_paths_routes_full_and_under_binary_paths_is_allowed(g, tmp_path):
    base = _base(g)
    # a binary file at a `paths` match: Theme/*.swift is text-only
    head = g.commit({"app/Theme/blob.swift": b"\x00\x01\x02binary\x00"})
    out = _route(tmp_path, g, base, head)
    assert out["position"] == "code_reviewer"
    assert any("binary" in r and "app/Theme/blob.swift" in r for r in out["reasons"])
    # a binary at a binary_paths match
    base = head
    head = g.commit({"app/Art/logo.png": b"\x89PNG-new\x00\x02"})
    assert _route(tmp_path, g, base, head)["position"] == "style_reviewer"
    # a text file that only matches binary_paths is not allowed
    base = head
    head = g.commit({"app/Art/readme.png": "text, not binary"})
    assert _route(tmp_path, g, base, head)["position"] == "code_reviewer"


# ---------------------------------------------------------------- png / evidence


def png(seed=0, w=8, h=8, extra=(), noise=False):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + (os.urandom(w * 4) if noise else bytes((seed + x) % 256 for x in range(w * 4)))
                    for _ in range(h))
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    for t, d in extra:
        out += chunk(t, d)
    return out + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def test_the_png_decoder_accepts_a_good_image_and_names_each_defect():
    good = png(1)
    assert fs.png_problems(good, 10**6) == []
    assert fs.png_problems(png(1, extra=[(b"tEXt", b"k\x00v")]), 10**6)
    assert fs.png_problems(b"not a png", 10**6)
    assert fs.png_problems(good[:-5], 10**6)
    assert fs.png_problems(good, 10)  # over the cap
    flipped = bytearray(good)
    flipped[40] ^= 0xFF
    assert fs.png_problems(bytes(flipped), 10**6)
    assert fs.png_problems(good + b"x", 10**6)       # trailing data
    for chunk_type in (b"sRGB", b"gAMA", b"cHRM", b"tRNS"):
        assert fs.png_problems(png(1, extra=[(chunk_type, b"\x00" * 4)]), 10**6) == []


def test_a_png_whose_idat_has_the_wrong_size_is_refused():
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    short = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 6, 0, 0, 0))
             + chunk(b"IDAT", zlib.compress(b"\x00" * 10)) + chunk(b"IEND", b""))
    assert any("implies" in p for p in fs.png_problems(short, 10**6))


def _fleet_png(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class Evidence:
    """A repository with a base, a code commit and an evidence commit, plus the
    README block and the producer's record, each tamperable."""

    SCREENS = ("home", "settings")

    def __init__(self, g, tmp_path, entry=None, date="2026-10-22", ident=PLAN_ID, pr=5):
        self.g, self.tmp, self.pr, self.ident = g, tmp_path, pr, ident
        self.entry = entry
        self.date = date
        self.folder = f"{ED}/{date}-{ident}"
        self.base = g.commit({"app/Theme/a.swift": "a", "tools/shoot.sh": "#!/bin/sh\n"})
        self.code = g.commit({"app/Theme/a.swift": "b"})
        self.images = {}
        for t in SHOT["themes"]:
            for a in SHOT["appearances"]:
                for s in self.SCREENS:
                    for kind in ("before", "after"):
                        self.images[f"{self.folder}/{t}-{a}/{s}-{kind}.png"] = png(
                            len(self.images) + 1)

    def block(self, **over):
        imgs = [{"path": p, "bytes": len(d), "sha256": hashlib.sha256(d).hexdigest()}
                for p, d in sorted(self.images.items())]
        b = {"v": 1, "id": self.ident, "pr": self.pr, "taken": f"{self.date}T10:00:00Z",
             "base": self.base, "code_head": self.code, "platform": SHOT["platform"],
             "runtime": SHOT["runtime"], "device": SHOT["device"], "command": SHOT["command"],
             "command_blob": self.g.run("rev-parse", f"{self.base}:tools/shoot.sh"),
             "images": imgs}
        b.update(over)
        return b

    def readme(self, **over):
        return "# Evidence\n\n| before | after |\n|---|---|\n\n```json\n" + json.dumps(
            self.block(**over), indent=1) + "\n```\n"

    def commit(self, readme=None, extra=None, drop=(), mutate=None, record_drop=(),
               record=True):
        imgs = {p: d for p, d in self.images.items() if p not in drop}
        if mutate:
            imgs = mutate(imgs)
        files = dict(imgs)
        files[f"{self.folder}/README.md"] = readme if readme is not None else self.readme()
        files.update(extra or {})
        head = self.g.commit(files)
        rec = self.tmp / "record.json"
        if record:
            rec.write_text(json.dumps({
                "repo": REPO, "pr": self.pr, "evidence_commit": head,
                "images": [{"path": p, "bytes": len(d), "sha256": hashlib.sha256(d).hexdigest()}
                           for p, d in sorted(self.images.items()) if p not in record_drop]}))
        self.head, self.rec = head, rec
        return head

    def verify(self, **kw):
        return fs.verify(REPO, self.pr, self.ident, fs.Git(self.g.git_dir), self.base,
                         self.head, config=_config(self.tmp, self.entry), record=self.rec, **kw)


@pytest.fixture
def ev(g, tmp_path):
    return Evidence(g, tmp_path)


def test_good_evidence_verifies(ev):
    ev.commit()
    got = ev.verify()
    assert got["ok"], got["problems"]
    assert got["folder"] == ev.folder and got["code_head"] == ev.code
    assert len(got["images"]) == 16 and got["total_bytes"] == sum(map(len, ev.images.values()))


def test_a_planless_pr_uses_the_pr_id(g, tmp_path):
    e = Evidence(g, tmp_path, ident="pr5")
    e.commit()
    got = fs.verify(REPO, 5, "none", fs.Git(g.git_dir), e.base, e.head,
                    config=_config(tmp_path), record=e.rec)
    assert got["ok"], got["problems"]


def _bad(ev, needle, **kw):
    ev.commit(**kw)
    got = ev.verify()
    assert not got["ok"]
    assert any(needle in p for p in got["problems"]), got["problems"]


def test_a_head_that_also_changes_a_style_file_is_not_ok(ev):
    _bad(ev, "outside the evidence folder", extra={"app/Theme/a.swift": "c"})


def test_a_readme_naming_the_wrong_code_head_is_not_ok(ev):
    _bad(ev, "code_head", readme=ev.readme(code_head=ev.base))


def test_a_readme_naming_the_wrong_runtime_or_device_is_not_ok(ev):
    _bad(ev, "runtime", readme=ev.readme(runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-0"))
    _bad(ev, "device", readme=ev.readme(device="iPhone SE"))


def test_a_readme_dated_on_another_day_is_not_ok(ev):
    _bad(ev, "date", readme=ev.readme(taken="2026-10-21T10:00:00Z"))


def test_a_missing_after_image_is_not_ok(ev):
    gone = f"{ev.folder}/bold-dark/home-after.png"
    _bad(ev, "bold-dark after", drop=[gone])


def test_a_flipped_byte_is_not_ok(ev):
    def flip(imgs):
        k = sorted(imgs)[0]
        b = bytearray(imgs[k])
        b[len(b) // 2] ^= 0x01
        imgs[k] = bytes(b)
        return imgs
    _bad(ev, "CRC", mutate=flip)


def test_a_text_chunk_is_not_ok(ev):
    def tag(imgs):
        imgs[sorted(imgs)[0]] = png(99, extra=[(b"tEXt", b"k\x00v")])
        return imgs
    ev.images[sorted(ev.images)[0]] = png(99, extra=[(b"tEXt", b"k\x00v")])
    _bad(ev, "tEXt")


def test_an_image_over_the_cap_is_not_ok(g, tmp_path):
    entry = json.loads(json.dumps(ENTRY))
    entry["screenshot"]["max_image_kb"] = 50
    e = Evidence(g, tmp_path, entry=entry)
    e.images[sorted(e.images)[0]] = png(0, w=200, h=200, noise=True)
    e.commit()
    got = e.verify()
    assert not got["ok"] and any("exceeds the cap" in p for p in got["problems"])


def test_a_hash_missing_from_the_producers_record_is_not_ok(ev):
    _bad(ev, "record", record_drop=[sorted(ev.images)[0]])


def test_a_missing_producers_record_is_not_ok(ev):
    _bad(ev, "record is missing", record=False)


def test_a_hash_missing_from_the_readme_block_is_not_ok(ev):
    blk = ev.block()
    blk["images"] = blk["images"][1:]
    _bad(ev, "README evidence block", readme="```json\n" + json.dumps(blk) + "\n```\n")


def test_a_folder_file_already_present_at_the_heads_parent_is_not_ok(g, tmp_path):
    """The file is identical at the parent, so the head commit does not touch it: only
    the parent check sees that it did not come from the evidence commit."""
    e = Evidence(g, tmp_path)
    first = sorted(e.images)[0]
    e.code = g.commit({"app/Theme/a.swift": "b", first: e.images[first]})
    e.commit()
    got = e.verify()
    assert not got["ok"]
    assert any("already exist" in p for p in got["problems"]), got["problems"]


def test_an_extra_file_in_the_folder_is_not_ok(ev):
    _bad(ev, "notes.txt", extra={f"{ev.folder}/notes.txt": "hi"})


def test_uneven_screens_across_combinations_are_not_ok(ev):
    extra = {f"{ev.folder}/classic-light/about-before.png": png(77),
             f"{ev.folder}/classic-light/about-after.png": png(78)}
    for p, d in extra.items():
        ev.images[p] = d
    _bad(ev, "screens differ")


def test_a_malformed_png_block_is_reported_not_raised(ev):
    _bad(ev, "evidence block", readme="no block here")


# ------------------------------------------------------- evidence in the route


def _routed_with_evidence(g, tmp_path, files, **kw):
    base = _base(g)
    head = g.commit(dict({"app/Theme/a.swift": "b"}, **files))
    return _route(tmp_path, g, base, head, **kw), base, head


def _folder(date="2026-10-22", ident=PLAN_ID):
    return f"{ED}/{date}-{ident}"


def test_one_new_evidence_folder_of_declared_names_is_style(g, tmp_path):
    f = _folder()
    out, *_ = _routed_with_evidence(g, tmp_path, {
        f"{f}/README.md": "r", f"{f}/classic-light/home-before.png": png(1),
        f"{f}/bold-dark/home-after.png": png(2)})
    assert out["position"] == "style_reviewer", out["reasons"]
    assert out["evidence_folder"] == f


@pytest.mark.parametrize("files,needle", [
    ({f"{ED}/2026-10-22-{PLAN_ID}/classic-light/home-before.png": png(1),
      f"{ED}/2026-10-23-{PLAN_ID}/README.md": "r"}, "more than one evidence folder"),
    ({f"{ED}/2026-10-22-{PLAN_ID}/classic-light/home.jpg": b"x"}, "not README.md"),
    ({f"{ED}/2026-10-22-{PLAN_ID}/notes.txt": "x"}, "not README.md"),
    ({f"{ED}/2026-10-22-{PLAN_ID}/classic-light/home-before.jpg": b"x"}, "not README.md"),
    ({f"{ED}/2026-10-22-{PLAN_ID}/other-light/home-before.png": png(1)}, "not README.md"),
    ({f"{ED}/2026-10-22-{PLAN_ID}/classic-light/Home-before.png": png(1)}, "not README.md"),
    ({f"{ED}/2026-10-22-0099-other/README.md": "r"}, "not named for"),
    ({f"{ED}/2026-02-30-{PLAN_ID}/README.md": "r"}, "real YYYY-MM-DD"),
    ({f"{ED}/{PLAN_ID}/README.md": "r"}, "real YYYY-MM-DD"),
    ({f"{ED}/README.md": "r"}, ""),
])
def test_a_bad_evidence_folder_routes_full(g, tmp_path, files, needle):
    out, *_ = _routed_with_evidence(g, tmp_path, files)
    assert out["position"] == "code_reviewer"
    assert needle in " ".join(out["reasons"])


def test_a_modified_evidence_file_routes_full(g, tmp_path):
    old = _folder("2026-09-01", "0001-old")
    base = g.commit({"app/Theme/a.swift": "a", f"{old}/README.md": "r"})
    head = g.commit({"app/Theme/a.swift": "b", f"{old}/README.md": "changed"})
    out = _route(tmp_path, g, base, head)
    assert out["position"] == "code_reviewer"
    assert any("status 'modified'" in r for r in out["reasons"])


def test_a_deleted_evidence_file_routes_full(g, tmp_path):
    old = _folder("2026-09-01", "0001-old")
    base = g.commit({"app/Theme/a.swift": "a", f"{old}/README.md": "r"})
    head = g.commit({"app/Theme/a.swift": "b"}, delete=[f"{old}/README.md"])
    assert _route(tmp_path, g, base, head)["position"] == "code_reviewer"


def test_an_evidence_folder_that_exists_at_base_routes_full(g, tmp_path):
    f = _folder()
    base = g.commit({"app/Theme/a.swift": "a", f"{f}/README.md": "r"})
    head = g.commit({"app/Theme/a.swift": "b", f"{f}/bold-dark/home-after.png": png(1)})
    # the forge reports the new file as added even though the folder pre-exists
    out = _route(tmp_path, g, base, head)
    assert out["position"] == "code_reviewer"
    assert any("already exists at the base" in r for r in out["reasons"])


def test_a_rename_into_the_evidence_dir_routes_full(g, tmp_path):
    base = _base(g)
    head = g.commit({}, move=[("app/Theme/a.swift", f"{_folder()}/README.md")])
    assert _route(tmp_path, g, base, head)["position"] == "code_reviewer"


# ------------------------------------------------------------------- the CLI


def _route_cli(pinned, g, base, head, plan, tmp, override="none", pr=5):
    changes = tmp / "changes.json"
    changes.write_text(json.dumps(g.changes(base, head)))
    pf = "none"
    if plan:
        (tmp / "plan.md").write_text(plan)
        pf = str(tmp / "plan.md")
    r = _cli("route", REPO, "--pr", str(pr), "--plan-file", pf, "--changes", str(changes),
             "--git-dir", str(g.git_dir), "--base", base, "--head", head,
             "--override", override, "--json", bin_dir=pinned / "bin",
             env={"FLEET_DB": str(tmp / "fleet.db"), "EUNOMIA_FLEET_DIR": str(tmp)})
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_cli_asks_fleet_models_for_the_ordinals(pinned, g, tmp_path):
    (pinned / "config" / "style-review.json").write_text(_config(tmp_path).read_text())
    base = _base(g)
    head = g.commit({"app/Theme/a.swift": "b"})
    # no rows at all: fleet-models exits 3, so the answer is the full review
    out = _route_cli(pinned, g, base, head, _plan(), tmp_path)
    assert out["position"] == "code_reviewer"
    assert any("ordinals could not be resolved" in r for r in out["reasons"])
    db = {"FLEET_DB": str(tmp_path / "fleet.db"), "EUNOMIA_FLEET_DIR": str(tmp_path)}
    for pos in ("code_reviewer", "style_reviewer"):
        for n in (1, 2):
            r = subprocess.run([sys.executable, str(BIN / "fleet-models"), "set", "default",
                                pos, str(n), "sub-opus-5-5"], env=dict(os.environ, **db),
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
    out = _route_cli(pinned, g, base, head, _plan(), tmp_path)
    assert out["position"] == "style_reviewer", out["reasons"]
    out = _route_cli(pinned, g, base, head, _plan(), tmp_path, override="full")
    assert out["position"] == "code_reviewer"


def test_an_unreadable_input_is_a_non_zero_exit(pinned, g, tmp_path):
    (pinned / "config" / "style-review.json").write_text(_config(tmp_path).read_text())
    base = _base(g)
    r = _cli("route", REPO, "--pr", "1", "--plan-file", "none", "--changes",
             str(tmp_path / "missing.json"), "--git-dir", str(g.git_dir), "--base", base,
             "--head", base, "--override", "none", "--json", bin_dir=pinned / "bin")
    assert r.returncode != 0
    r = _cli("route", REPO, "--pr", "1", "--plan-file", "none", "--changes", "x",
             "--git-dir", str(g.git_dir), "--base", base, "--head", base,
             "--override", "partial", "--json", bin_dir=pinned / "bin")
    assert r.returncode != 0


def test_a_hash_that_is_not_a_hash_is_not_decided(g, tmp_path):
    with pytest.raises(fs.Undecided):
        fs.Git(g.git_dir).resolve("--output=/tmp/x")


def test_the_module_names_no_model():
    for f in (BIN / "fleet-style", BIN.parent / "config" / "style-review.json",
              BIN.parent / "docs" / "style-review.md"):
        text = f.read_text().lower()
        assert not any(w in text for w in ("fable", "opus", "sonnet")), f
