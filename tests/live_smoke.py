"""Live end-to-end smoke test, run INSIDE the container against the running app.
Drives: create agent -> run mission -> wait for plan -> approve -> wait for done.
Prints status at each step. Exercises the real LLM + crawl path.

    docker compose exec web-researcher python /app/tests/live_smoke.py
"""
import json
import re
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:5000"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def post(path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def poll(mid, want_terminal, timeout):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _, body = get(f"/api/mission/{mid}")
        s = json.loads(body)
        if s["status"] != last:
            print(f"  status -> {s['status']}")
            last = s["status"]
        if s["status"] in want_terminal:
            return s
        time.sleep(2)
    return s


print("1) creating agent")
post("/agents/new", {
    "name": "Smoke Analyst", "expertise": "general current affairs",
    "max_sources": "15", "max_passes": "2", "per_req_attempts": "2",
})
_, agents_html = get("/agents")
m = re.search(r"/agents/([0-9a-f\-]+)/run", agents_html)
agent_id = m.group(1)
print("   agent_id =", agent_id)

print("2) running mission")
final_url, _ = post(f"/agents/{agent_id}/run",
                    {"question": "What is the James Webb Space Telescope?"})
mid = final_url.rstrip("/").split("/")[-1]
print("   mission_id =", mid)

print("3) waiting for plan (planning -> awaiting_approval)")
s = poll(mid, {"awaiting_approval", "error", "done"}, timeout=90)
if s["status"] != "awaiting_approval":
    print("   PLAN FAILED:", s.get("error"))
    raise SystemExit(1)
print("   requirements:", [r["title"] for r in s["requirements"]])

print("4) approving -> collecting")
post(f"/missions/{mid}/approve", {})

print("5) waiting for completion")
s = poll(mid, {"done", "error"}, timeout=300)
print("   final status:", s["status"], "error:", s.get("error"))
for r in s["requirements"]:
    print(f"     [{r['status']}] {r['title']} ({r['attempts']} tries)")
print("   has_brief:", s.get("has_brief"))
print("DONE" if s["status"] == "done" else "ENDED_WITH_" + s["status"].upper())
