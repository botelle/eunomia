#!/usr/bin/env python3
"""Plan dispatch — processes and data, v1.0.

The companion to plan-dispatch-flow.svg. That figure shows WHO decides; this one
shows WHAT RUNS and WHAT IT WRITES at each step, so a reader can go from a step
to the file, table or forge object it leaves behind. Every process named here is
a program in bin/ or ~/agent, every store is a path or a table that exists on
opshost on the date in the title block, and the "writes" lines are what that step
writes — not what the design says it should.

Publishable by construction: mechanism only, no repo names, no client names.
Regenerate with `python3 docs/diagrams/gen-dispatch-dataflow.py`; do not edit the SVG.
"""
W, H = 1900, 2840
out = []
def e(s): out.append(s)

def x(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def box_h(lines=(), writes=()):
    return 24 + 21 + 17 * len(lines) + (4 + 16 * len(writes) if writes else 0) + 6

def box(bx, y, w, h, kind, title, lines=(), writes=(), act=None, num=None):
    if h is None:
        h = box_h(lines, writes)
    e(f'<rect class="{kind}" x="{bx}" y="{y}" width="{w}" height="{h}" rx="8"/>')
    ty = y + 24
    if act:
        cls = {"agent": "actA", "mech": "actM", "human": "actH"}[act]
        txt = {"agent": "AGENT", "mech": "MECHANICAL", "human": "HUMAN"}[act]
        e(f'<text class="{cls}" x="{bx+w-16}" y="{ty}" text-anchor="end">{txt}</text>')
    t = f"{num} {title}" if num else title
    e(f'<text class="h" x="{bx+16}" y="{ty}">{x(t)}</text>'); ty += 21
    for ln in lines:
        e(f'<text class="b" x="{bx+16}" y="{ty}">{x(ln)}</text>'); ty += 17
    if writes:
        ty += 4
        e(f'<text class="w" x="{bx+16}" y="{ty}">writes</text>')
        for ln in writes:
            e(f'<text class="wv" x="{bx+70}" y="{ty}">{x(ln)}</text>'); ty += 16

def poly(pts, cls="fl"):
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        assert abs(x0-x1) < .01 or abs(y0-y1) < .01, f"not orthogonal: {(x0,y0)}->{(x1,y1)}"
    d = f"M{pts[0][0]},{pts[0][1]}" + "".join(f" L{x},{y}" for x, y in pts[1:])
    e(f'<path class="{cls}" d="{d}"/>')

def label(lx, y, s, cls="n", anchor="start"):
    e(f'<text class="{cls}" x="{lx}" y="{y}" text-anchor="{anchor}">{x(s)}</text>')

def band(y, h, name, tint):
    e(f'<rect class="band" x="40" y="{y}" width="{W-80}" height="{h}" rx="10" fill="{tint}"/>')
    cy = y + h / 2
    e(f'<text class="bandl" transform="translate(66,{cy}) rotate(-90)" text-anchor="middle">{x(name)}</text>')

e(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
  'font-family="-apple-system, BlinkMacSystemFont, \'Helvetica Neue\', Arial, sans-serif">')
e('''<style>
 .t{font-size:27px;font-weight:700;fill:#1c1c1c}
 .sub{font-size:14px;fill:#5c6670}
 .h{font-size:15px;font-weight:700;fill:#1c1c1c}
 .b{font-size:13px;fill:#222}
 .n{font-size:11.5px;fill:#5c6670;font-style:italic}
 .w{font-size:10px;font-weight:700;letter-spacing:.08em;fill:#0e6b70}
 .wv{font-size:12px;fill:#0e6b70;font-family:ui-monospace,Menlo,monospace}
 .bandl{font-size:13px;font-weight:700;letter-spacing:.12em;fill:#5c6670}
 .band{stroke:#ddd8cf;stroke-width:1}
 .proc{fill:#ffffff;stroke:#444;stroke-width:1.8}
 .agent{fill:#f5f0fb;stroke:#7c3aed;stroke-width:1.8}
 .gate{fill:#f5ecfd;stroke:#6b21a8;stroke-width:2}
 .store{fill:#e6f3f3;stroke:#0e6b70;stroke-width:1.6}
 .panel{fill:#f7f7f9;stroke:#cccccc;stroke-width:1.5}
 .fl{fill:none;stroke:#444;stroke-width:2.2;marker-end:url(#a)}
 .ret{fill:none;stroke:#1d4ed8;stroke-width:2;stroke-dasharray:7 5;marker-end:url(#ab)}
 .bus{fill:none;stroke:#444;stroke-width:2.2}
 .fail{fill:none;stroke:#b91c1c;stroke-width:2;stroke-dasharray:7 5;marker-end:url(#af)}
 .actA{font-size:9.5px;letter-spacing:.09em;font-weight:700;fill:#7c3aed}
 .actM{font-size:9.5px;letter-spacing:.09em;font-weight:700;fill:#6b7280}
 .actH{font-size:9.5px;letter-spacing:.09em;font-weight:700;fill:#a21caf}
</style>
<defs>
 <marker id="a" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
  <path d="M0,0 L10,4 L0,8 z" fill="#444"/></marker>
 <marker id="ab" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
  <path d="M0,0 L10,4 L0,8 z" fill="#1d4ed8"/></marker>
 <marker id="af" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
  <path d="M0,0 L10,4 L0,8 z" fill="#b91c1c"/></marker>
</defs>''')
e(f'<rect class="bg" x="0" y="0" width="{W}" height="{H}" fill="#ffffff"/>')

label(950, 52, "Plan dispatch — the processes that run, and the data each one writes", "t", "middle")
label(950, 76, "Version 1.0 — 2026-09-22 by Fleet Operator", "sub", "middle")
label(60, 46, "One plan, from a filed PR to a merged PR, as it actually runs on opshost.", "n")
label(60, 62, "Bands are phases. Every box names the program; `writes` names what it leaves behind.", "n")
label(60, 78, "The right rail lists every store, its writer and its readers.", "n")

SX, SW = 420, 560          # spine column
CX = SX + SW // 2          # 700

# ---- layout is computed: every box is placed by a cursor, so heights come
# from content and the arrows between boxes are always 20px.
GAP = 20
spine = []      # (y, h, args) in order; bands are ranges over these
def place(cur, kind, title, lines, writes, act, num, col=(SX, SW), gap=GAP):
    h = box_h(lines, writes)
    spine.append((cur, h, (col[0], col[1], kind, title, lines, writes, act, num)))
    return cur + h + gap

bands = []   # (name, tint, first_index, last_index, note)
def start_band(name, tint, note=None):
    bands.append([name, tint, len(spine), None, note])
def end_band():
    bands[-1][3] = len(spine) - 1

y = 116
start_band("1 · FILE", "#f7f5f0")
y = place(y, "proc", "Plan PR opened", ["file-a-plan → forgejo-open-pr.sh, as implbot.", "Front matter: id, status: ready, paths (the lease),", "depends_on. No Plan: marker on a plan PR."],
          ["forge: pull request, branch plans/NNNN-slug"], "mech", "①")
y = place(y, "agent", "Plan review", ["dispatch-review.py → a Fable session reads the", "plan against the checkout; HIGH blocks, else deferred."],
          ["forge: review (APPROVE/COMMENT), review-ledger", "comment, follow-ups issue"], "agent", "②")
y = place(y, "gate", "Merge the plan PR", ["Two approvals; the operator merges. Merge is ignition."],
          ["main: plans/NNNN-slug.md at status: ready"], "human", "③")
end_band(); y += 30
start_band("2 · IGNITE", "#f1f4f7", "fleet-watch --resident on opshost, one cycle every 120 s")
y += 18
y = place(y, "proc", "Verdict per enrolled repo", ["FLEET_WATCH_REPOS is the allowlist. The branch-", "protection verdict comes from keyvault's forgejo-broker", "over FLEET_BROKER_SOCKET; undetermined = scan nothing."],
          ["pages.jsonl (once, on refusal)", "leases/*.watch-notified"], "mech", "④")
y = place(y, "proc", "Scan main, dedupe, gate", ["plans/ read via the contents API at ref=main, never a", "working tree. A PR carrying `Plan: <id>` in ANY state", "= already dispatched. depends_on unmet = blocked.", "Live leases ≥ FLEET_WATCH_CAP (default 1) = deferred."],
          ["events.jsonl: plan-blocked (on transition only)", "watch.log: deferred — at cap N"], "mech", "⑤")
y = place(y, "proc", "Spawn one orchestrator", ["fleet-claim --assign → a branch lease for the plan.", "fleetjob.start → a one-shot launchd job", "org.eunomia.fleet-orch.<lease>, env EUNOMIA_SESSION,", "EUNOMIA_PLAN_ID / ZONE / TIER."],
          ["leases/<lease>.json (state: assigned), locks/", "events.jsonl: lease-assigned, plan-dispatched", "launchd: the job label"], "mech", "⑥")
end_band(); y += 30
start_band("3 · IMPLEMENT", "#f7f5f0", "bin/orchestrator — one process per plan, phases stamped to its run log")
y += 18
y = place(y, "proc", "Start: lease, workspace, branch", ["Activates the lease; heartbeats it. add_worktree from", "the private .git-store clone at origin/main; core.hooksPath", "pinned. create_branch feat/<plan-slug>. Runner resolved", "from fleet-models (repo row, else default)."],
          ["leases/<lease>.json (state: active); events: lease-activated", "work/<repo>/<lease>/ (worktree)", "work/logs/<lease>.log; sessions/<sid>/"], "mech", "⑦")
y = place(y, "agent", "Implementer session", ["claude -p as the resolved runner, stdin prompt = preamble", "+ plan + bounds. Commits on the branch, pushes, opens", "the work PR itself with `Plan: <id>` in the body.", "Timeout per repo (impl-bounds.conf)."],
          ["forge: branch, pull request (marker)", ".orchestrator-transcripts/<lease>.log (redacted)", "run log: implementer-start / implementer-exit"], "agent", "⑧")
y = place(y, "proc", "Verify, then wait for CI", ["Finds the PR by marker; refuses if the plan file still says", "ready (unverified). Polls the combined commit status", "until every check has registered and resolved."],
          ["forge: PR body edited (marker stamped)", "run log: verified / unverified, ci-waiting, ci-resolved"], "mech", "⑨")
end_band(); y += 30
start_band("4 · REVIEW", "#f1f4f7", "bounded: per-repo passes (review-bounds.conf), each round a fresh session")
y += 18
y = place(y, "agent", "Review round", ["CI red → a CI-fix session (check names only today; 0069", "adds the stored log). CI green → dispatch-review.py: a", "Fable session posts the verdict; MEDIUM/LOW → issue."],
          ["forge: review, review-ledger comment, follow-ups issue", "fleet.db review (via fleet-reviews)"], "agent", "⑩")
y = place(y, "agent", "Fix round, or stop", ["HIGH → a fix session on the same branch, push, back", "to ⑩. Bound exhausted, or nothing pushed → stop."],
          ["forge: commits; run log: review-round, review-stopped", "events.jsonl: plan-failed (on stop)"], "agent", "⑪")
y = place(y, "proc", "Approved: freeze and steward", ["Pushes refused while an approval stands. Polls the PR", "every 60 s for a merge; top-level comments are the", "command channel (unfreeze, instruction, followup)."],
          ["run log: review-approved, steward-start, steward-*", "forge: ack comments"], "mech", "⑫")
end_band(); y += 30
start_band("5 · MERGE", "#f7f5f0")
y = place(y, "gate", "Merge the work PR", ["Two approvals; the operator merges in Minos. The steward sees", "merged and releases."], ["main: the feature"], "human", "⑬")
y = place(y, "proc", "Release", ["release_lease; the worktree is kept for the record; the job", "exits and fleet-watch reaps its launchd label next cycle."],
          ["leases (released); events: lease-released", "run log: steward-end"], "mech", "⑭")
end_band()
spine_bottom = y - GAP

# bands first (z-order), then boxes, then arrows
for name, tint, i0, i1, note in bands:
    top = spine[i0][0] - 16 - (18 if note else 0)
    bot = spine[i1][0] + spine[i1][1] + 16
    band(top, bot - top, name, tint)
    if note:
        label(CX + 14, top + 14, note, "n")
# legend
e('<rect class="panel" x="1560" y="96" width="300" height="236" rx="8"/>')
label(1578, 124, "Legend", "h")
ly = 144
for cls, txt in (("proc", "a program (bin/ or ~/agent)"), ("agent", "a model session"),
                 ("gate", "a human, in Minos"), ("store", "a data store")):
    e(f'<rect class="{cls}" x="1578" y="{ly}" width="26" height="15" rx="3"/>')
    label(1614, ly + 12, txt, "b"); ly += 24
poly([(1578, ly + 8), (1604, ly + 8)], "fl"); label(1614, ly + 12, "flow", "b"); ly += 24
poly([(1578, ly + 8), (1604, ly + 8)], "ret"); label(1614, ly + 12, "return path", "b"); ly += 24
poly([(1578, ly + 8), (1604, ly + 8)], "fail"); label(1614, ly + 12, "a run that failed", "b"); ly += 24
label(1578, ly + 12, "writes", "w"); label(1640, ly + 12, "what the step leaves behind", "b")

for yy, h, (bx, w, kind, title, lines, writes, act, num) in spine:
    box(bx, yy, w, h, kind, title, lines, writes, act=act, num=num)
for (y0, h0, _), (y1, h1, _) in zip(spine, spine[1:]):
    poly([(CX, y0 + h0), (CX, y1)])
# the fix loop: ⑪ back to ⑩
i10 = next(i for i, s in enumerate(spine) if s[2][7] == "⑩"); i11 = i10 + 1
y10, h10, _ = spine[i10]; y11, h11, _ = spine[i11]
poly([(SX, y11 + h11 // 2), (SX - 40, y11 + h11 // 2), (SX - 40, y10 + h10 // 2), (SX - 4, y10 + h10 // 2)], "ret")
label(SX - 48, y10 + h10 // 2 - 8, "fix → re-review", "n", "end")

# ---------------- band 6: FAIL (right of the spine; flows UPWARD so the
# failure enters at the bottom and the respawn leaves at the top)
FX, FW = 1040, 460
i6 = next(i for i, s in enumerate(spine) if s[2][7] == "⑥"); y6, h6, _ = spine[i6]
i7 = next(i for i, s in enumerate(spine) if s[2][7] == "⑦"); ytop = spine[i7][0]
f17 = (["`respawn` as a top-level comment on the PR (bound to", "FLEET_OPERATOR_UID); or a fix by hand; or nothing."], ["forge: comment"])
f16 = (["Reads leases and fleet.db, classifies the stop reason", "(CLASS_RULES) and composes a brief with a shortlist", "that fits THIS failure and a Minos deep link."], ["pages.jsonl + angelia push: the brief", "mopsus.state.json (what was briefed)"])
f15 = (["A lease whose heartbeat lapsed or whose process is gone", "is orphaned. Paged ONCE per lease; never respawned", "without a human's `respawn` comment newer than the lease."], ["pages.jsonl + angelia push: 'Orchestrator dead'", "leases/<lease>.watch-notified", "events.jsonl: lease-orphaned"])
yf = ytop
box(FX, yf, FW, None, "gate", "A person answers", *f17, act="human", num="⑰"); h17 = box_h(*f17)
yf2 = yf + h17 + GAP
box(FX, yf2, FW, None, "proc", "mopsus (every cycle)", *f16, act="mech", num="⑯"); h16 = box_h(*f16)
yf3 = yf2 + h16 + GAP
box(FX, yf3, FW, None, "proc", "Orphan sweep (fleet-watch)", *f15, act="mech", num="⑮"); h15 = box_h(*f15)
FC = FX + FW // 2
poly([(FC, yf3), (FC, yf2 + h16)])
poly([(FC, yf2), (FC, yf + h17)])
# failure path: from ⑪'s right edge, across, and up into ⑮ from below
poly([(SX + SW, y11 + h11 // 2), (FC, y11 + h11 // 2), (FC, yf3 + h15)], "fail")
label(SX + SW + 12, y11 + h11 // 2 - 8, "stopped / died", "n")
# respawn return: from ⑰'s right edge, up, and back into ⑥ (the cap applies)
yr = y6 + h6 // 2
poly([(FX + FW, yf + h17 // 2), (FX + FW + 30, yf + h17 // 2), (FX + FW + 30, yr), (SX + SW + 4, yr)], "ret")
label(SX + SW + 12, yr - 8, "respawn → a fresh dispatch of the same plan, at the cap", "n")

# ---------------- band 7: OBSERVE
yo = spine_bottom + 60
band(yo, 240, "7 · OBSERVE", "#f1f4f7")
label(SX, yo + 22, "no arrows: these read the stores above on their own schedules", "n")
ob = [("fleet-collect (120 s)", ["transcripts → session, turn, tool_use", "events.jsonl → event", "run logs → dispatch_phase (+ dispatch view)", "forge tasks + cihost logs → ci_task, ci_log"], ["fleet.db (the one writer of its schema)"]),
      ("fleet-reviews / fleet-models", ["fleet-reviews --backfill: forge reviews →", "review, model_price. fleet-models:", "repo_model + repo_model_change + event;", "the one writer for per-repo runners."], ["fleet.db: review, repo_model*", "events.jsonl: repo-model-changed"]),
      ("Pagers and keepers", ["fleet-stalled (daily): PRs waiting on nobody.", "fleet-orphans (07:00): plans only on branches.", "fleet-pin-advance / pin-watch: the pins.", "fleet-snapshot (04:30): VACUUM copy of fleet.db."], ["pages.jsonl; ~/dev/.fleet/snapshots/"]),
      ("Readers", ["lynceus API (opshost:8795) → the phone:", "live sessions, dispatches, failures, reviews.", "Minos → the PR, the merge, the comments.", "atlas → history views."], ["nothing in the fleet tree"])]
for i, (t, ls, ws) in enumerate(ob):
    box(420 + i * 360, yo + 36, 340, None, "proc", t, ls, ws, act="mech")
H_needed = yo + 240 + 40

# ---------------- data rail
RX, RW = 1560, 300
e(f'<rect class="panel" x="{RX}" y="350" width="{RW}" height="1700" rx="8"/>')
label(RX + 18, 378, "Data stores — writer → readers", "h")
ry = 400
stores = [
 ("forge (Forgejo)", ["PR, reviews, comments, status;", "written by implbot, revbot,", "the orchestrator, the operator. Read by", "everything."]),
 ("main: plans/*.md", ["written only by a merge (③).", "read by fleet-watch via the API,", "never from a working tree."]),
 ("~/dev/.fleet/leases/*.json", ["fleet-claim; states assigned →", "active → released/orphaned.", "*.watch-notified = paged once."]),
 ("~/dev/.fleet/locks/", ["fleet-claim's write locks."]),
 ("~/dev/.fleet/events.jsonl", ["fleet-emit, append-only, closed", "EVENT_TYPES; read by fleet-events", "and fleet-collect → event."]),
 ("~/dev/.fleet/work/<repo>/<lease>/", ["the orchestrator's worktree off", "the private .git-store clone."]),
 ("~/dev/.fleet/work/logs/<lease>.log", ["the orchestrator's phase stream;", "read by mopsus, fleet-collect,", "and (0067) the cap count."]),
 ("~/dev/.orchestrator-transcripts/", ["implementer/fix session output,", "redacted at write; outside the", "tree atlas renders."]),
 ("~/dev/.fleet/pages.jsonl", ["every page any unit sent (0058):", "ts, unit, title, delivered."]),
 ("~/dev/.fleet/mopsus.state.json", ["which failures were briefed."]),
 ("~/dev/.fleet/sessions/<sid>/", ["fleet-bind: sid → pid, host."]),
 ("launchd (gui/501)", ["org.eunomia.fleet-orch.<lease>", "one-shot jobs; reaped by ⑤."]),
 ("~/dev/.fleet/fleet.db", ["session turn tool_use event", "dispatch_phase (dispatch view)", "ci_task ci_log ci_sync ingest", "review model_price", "repo_model repo_model_change", "fleet_setting (0068, to come).", "Snapshots: ~/dev/.fleet/snapshots/"]),
]
for name, lines in stores:
    e(f'<rect class="store" x="{RX + 16}" y="{ry}" width="{RW - 32}" height="{20 + 15 * len(lines) + 8}" rx="5"/>')
    label(RX + 26, ry + 16, name, "wv"); yy = ry + 32
    for ln in lines:
        label(RX + 26, yy, ln, "b"); yy += 15
    ry += 20 + 15 * len(lines) + 8 + 10

e("</svg>")
open(__file__.replace("gen-dispatch-dataflow.py", "dispatch-dataflow.svg"), "w").write("\n".join(out))
print("written")
