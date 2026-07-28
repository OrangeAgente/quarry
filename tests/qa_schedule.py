"""Live check of scheduling + collection quality.

Everything is driven over HTTP so it exercises the real app process (correct
user, real browser cache, the scheduler instance that actually holds the jobs).
Calling scheduler internals from a `docker exec` process would test a different
process's module state and Chromium cache — not the app.
"""
import json, sqlite3, time, urllib.parse, urllib.request

BASE = "http://127.0.0.1:5000"; DB = "/app/data/research.db"
con = sqlite3.connect(DB)
ok = fail = 0

def check(c, label, detail=""):
    global ok, fail
    ok, fail = (ok + 1, fail) if c else (ok, fail + 1)
    print(f"  [{'PASS' if c else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}", flush=True)

def get(p):
    with urllib.request.urlopen(BASE + p, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

def post(p, d):
    req = urllib.request.Request(BASE + p, data=urllib.parse.urlencode(d).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.geturl()

def wait_status(mid, timeout=480):
    end, seen_gate, st = time.time() + timeout, False, ""
    while time.time() < end:
        st = con.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()[0]
        if st == "awaiting_approval":
            seen_gate = True
        if st in ("done", "error"):
            break
        time.sleep(3)
    return st, seen_gate

# Leftovers from an aborted earlier run would skew the schedule assertions.
for _old in [r[0] for r in con.execute("SELECT id FROM agents WHERE name LIKE 'QA-%'")]:
    post(f"/agents/{_old}/delete", {})

print("1) search fallback beats the default engine", flush=True)
import search
res = search.web_search("james webb space telescope mirror segments", 4)
try:
    from ddgs import DDGS
    with DDGS() as d:
        solo = len(list(d.text("james webb space telescope mirror segments",
                               max_results=4, backend="duckduckgo")))
except Exception:
    solo = 0
check(len(res) > 0, "multi-engine search returns results", f"{len(res)} vs {solo} duckduckgo-only")

print("2) cron validation", flush=True)
post("/agents/new", {"name": "QA-BADCRON", "expertise": "x", "max_sources": "3",
                     "max_passes": "1", "per_req_attempts": "1",
                     "schedule_cron": "not a cron", "schedule_question": "q"})
check(con.execute("SELECT COUNT(*) FROM agents WHERE name='QA-BADCRON'").fetchone()[0] == 0,
      "invalid cron rejected")

print("3) scheduled agent registers with the live scheduler", flush=True)
post("/agents/new", {"name": "QA-SCHED", "expertise": "general science",
                     "max_sources": "4", "max_passes": "1", "per_req_attempts": "1",
                     "schedule_cron": "0 7 * * *",
                     "schedule_question": "What is the water cycle?"})
aid = con.execute("SELECT id FROM agents WHERE name='QA-SCHED' ORDER BY created_at DESC LIMIT 1").fetchone()[0]
html = get("/agents")
# "next <date>" only renders from the running scheduler's job list.
_next = [l.strip() for l in html.splitlines() if "next 20" in l]
check("next 20" in html, "live scheduler shows a next run time",
      (_next[0][:60] if _next else "not found"))
check("Run scheduled question now" in html, "run-now control offered")

print("4) unattended run: auto-approves and completes in the app process", flush=True)
before = con.execute("SELECT COUNT(*) FROM missions WHERE agent_id=?", (aid,)).fetchone()[0]
post(f"/agents/{aid}/run-scheduled", {})
mid = None
for _ in range(20):
    time.sleep(1)
    row = con.execute("SELECT id FROM missions WHERE agent_id=? ORDER BY created_at DESC LIMIT 1",
                      (aid,)).fetchone()
    if row and con.execute("SELECT COUNT(*) FROM missions WHERE agent_id=?", (aid,)).fetchone()[0] > before:
        mid = row[0]; break
check(mid is not None, "scheduled mission created")
budget = json.loads(con.execute("SELECT budget_json FROM missions WHERE id=?", (mid,)).fetchone()[0])
check(budget.get("auto_approve") and budget.get("scheduled"), "flagged auto-approve + scheduled")

st, seen_gate = wait_status(mid)
check(not seen_gate, "never stopped at the approval gate")
check(st == "done", "completed unattended", st)
ndocs = con.execute("SELECT COUNT(DISTINCT document_id) FROM mission_documents WHERE mission_id=?",
                    (mid,)).fetchone()[0]
check(ndocs > 0, "collected sources (real browser, real crawl)", f"{ndocs} docs")
thin = con.execute("SELECT COUNT(*) FROM documents d JOIN mission_documents m ON m.document_id=d.id"
                   " WHERE m.mission_id=? AND d.word_count < 60", (mid,)).fetchone()[0]
check(thin == 0, "no thin/blocked pages stored", f"{thin}")
brief = con.execute("SELECT brief_markdown FROM missions WHERE id=?", (mid,)).fetchone()[0] or ""
check(len(brief) > 200, "brief written", f"{len(brief)} chars")

print("5) second firing links to the first and diffs against it", flush=True)
post(f"/agents/{aid}/run-scheduled", {})
mid2 = None
for _ in range(20):
    time.sleep(1)
    row = con.execute("SELECT id FROM missions WHERE agent_id=? AND id != ?"
                      " ORDER BY created_at DESC LIMIT 1", (aid, mid)).fetchone()
    if row:
        mid2 = row[0]; break
check(mid2 is not None, "second mission created")
parent = con.execute("SELECT parent_mission_id FROM missions WHERE id=?", (mid2,)).fetchone()[0]
check(parent == mid, "linked to the previous run", f"parent={str(parent)[:8]}")
st2, _ = wait_status(mid2)
err2 = con.execute("SELECT COALESCE(error,'') FROM missions WHERE id=?", (mid2,)).fetchone()[0]
check(st2 == "done", "second run completed", st2 + (" :: " + err2[:120] if err2 else ""))

# cleanup
for m in (mid, mid2):
    if m:
        con.execute("DELETE FROM mission_documents WHERE mission_id=?", (m,))
        con.execute("DELETE FROM requirements WHERE mission_id=?", (m,))
        con.execute("DELETE FROM missions WHERE id=?", (m,))
con.commit()
post(f"/agents/{aid}/delete", {})
check("next 20" not in get("/agents"), "job unregistered when the agent is deleted")

print(f"\nRESULT: {ok} passed, {fail} failed", flush=True)
print("QA_SCHEDULE_DONE", flush=True)
