"""Drive one mission through all three states and assert the redesigned
mission surfaces actually render (gate -> matrix -> brief + citations).
Also exercises the editable approval plan: rename, drop, add a query.
"""
import json, re, sqlite3, time, urllib.parse, urllib.request

BASE = "http://127.0.0.1:5000"; DB = "/app/data/research.db"
con = sqlite3.connect(DB)
ok = fail = 0

def check(cond, label, detail=""):
    global ok, fail
    if cond: ok += 1
    else: fail += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}", flush=True)

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

def post(path, data):
    req = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(data).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")

def poll(mid, terminal, timeout):
    end = time.time() + timeout
    while time.time() < end:
        with urllib.request.urlopen(f"{BASE}/api/mission/{mid}", timeout=30) as r:
            s = json.load(r)
        if s["status"] in terminal: return s
        time.sleep(2)
    return s

print("1) create agent + run mission", flush=True)
post("/agents/new", {"name": "QA-UI", "expertise": "general science",
                     "max_sources": "6", "max_passes": "1", "per_req_attempts": "1"})
aid = con.execute("SELECT id FROM agents WHERE name='QA-UI' ORDER BY created_at DESC LIMIT 1").fetchone()[0]
final, _ = post(f"/agents/{aid}/run", {"query": "How do solar panels convert light to electricity?",
                                       "max_sources": "6", "max_passes": "1", "per_req_attempts": "1"})
mid = final.rstrip("/").split("/")[-1]
s = poll(mid, {"awaiting_approval", "error"}, 120)
check(s["status"] == "awaiting_approval", "reached approval gate", s.get("error") or s["status"])

print("2) approval gate renders editable plan", flush=True)
html = get(f"/missions/{mid}")
check('class="gate"' in html, "gate rendered")
check(html.count("plan-row") >= 2, "plan rows present", f"{html.count('plan-row')} refs")
check("plan-title-in" in html, "titles are editable inputs")
check('data-qadd' in html, "add-query affordance present")
check('id="planJson"' in html, "plan serializes to a hidden field")
check("telemetry" in html and "tele-cell" in html, "telemetry strip rendered")
check("pill-approval" in html, "semantic status pill (approval)")
check("statebar" not in html, "design-preview scaffolding NOT ported")

print("3) approve an EDITED plan (rename first, drop last, add query)", flush=True)
reqs = con.execute("SELECT id, title FROM requirements WHERE mission_id=? ORDER BY rowid", (mid,)).fetchall()
before = len(reqs)
edited = []
for i, (rid, title) in enumerate(reqs):
    if i == 0:
        edited.append({"id": rid, "title": "RENAMED BY QA", "queries": ["solar photovoltaic effect", "QA added query"], "dropped": False})
    elif i == len(reqs) - 1:
        edited.append({"id": rid, "title": title, "queries": [], "dropped": True})
    else:
        edited.append({"id": rid, "title": title, "queries": [], "dropped": False})
post(f"/missions/{mid}/approve", {"plan_json": json.dumps(edited)})
after = con.execute("SELECT COUNT(*) FROM requirements WHERE mission_id=?", (mid,)).fetchone()[0]
check(after == before - 1, "dropped requirement deleted", f"{before} -> {after}")
renamed = con.execute("SELECT COUNT(*) FROM requirements WHERE mission_id=? AND title='RENAMED BY QA'", (mid,)).fetchone()[0]
check(renamed == 1, "renamed title persisted")
q = con.execute("SELECT next_queries_json FROM requirements WHERE mission_id=? AND title='RENAMED BY QA'", (mid,)).fetchone()[0]
check("QA added query" in (q or ""), "added query persisted", q)

print("4) collecting state", flush=True)
time.sleep(6)
html = get(f"/missions/{mid}")
check("matrix" in html, "requirements matrix rendered")
check("attempts" in html, "attempt dots rendered")
s = poll(mid, {"done", "error"}, 400)
check(s["status"] == "done", "mission completed", s.get("error") or s["status"])

print("5) complete state: matrix reasoning + brief + citations", flush=True)
html = get(f"/missions/{mid}")
check("pill-done" in html, "semantic status pill (done)")
check('class="brief"' in html, "brief rendered")
check("srail" in html, "source rail rendered")
n_cites = len(re.findall(r'class="cite"', html))
n_srcs = len(re.findall(r'class="src"', html))
check(n_srcs >= 1, "sources numbered in rail", f"{n_srcs} sources")
check(n_cites >= 1, "brief citations are controls", f"{n_cites} [n] controls")
# every cite number must exist in the rail
cite_ns = set(int(n) for n in re.findall(r'data-cite="(\d+)"', html))
src_ns = set(int(n) for n in re.findall(r'data-src="(\d+)"', html))
check(cite_ns <= src_ns if cite_ns else True, "every citation binds to a rail source",
      f"cites={sorted(cite_ns)} rail={sorted(src_ns)}")
# assessor reasoning persisted + surfaced
rows = con.execute("SELECT status, assessment_confidence, assessment_missing FROM requirements WHERE mission_id=?", (mid,)).fetchall()
check(any(r[1] for r in rows), "assessor confidence persisted", str([r[1] for r in rows]))
unmet = [r for r in rows if r[0] == "unmet"]
if unmet:
    check(any(r[2] for r in unmet), "gap reasoning persisted for unmet", str([r[2][:40] if r[2] else None for r in unmet]))
    check('class="gap"' in html, "gap callout rendered")
    check("gapnote" in html, "unmet stated in brief, not hidden")
else:
    check("satis" in html, "satisfied callout rendered")
check("Export" in html and "brief.md" in html, "export action present")

print("6) export + missions index grouping", flush=True)
md = get(f"/missions/{mid}/brief.md")
check(len(md) > 100, "brief.md export works", f"{len(md)} chars")
idx = get("/missions")
check("mrow" in idx, "missions index uses new rows")
check("coverage-compact" in idx or "mrow-cov" in idx, "compact coverage in index")
ag = get("/agents")
check("agent-av" in ag and "agent-budget" in ag, "agents cards redesigned")
check("Requirements met" in ag, "agent track record rendered")

post(f"/agents/{aid}/delete", {})
print(f"\nRESULT: {ok} passed, {fail} failed", flush=True)
print("QA_MISSION_UI_DONE", flush=True)
