import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from flask import (Flask, Response, render_template, request, redirect,
                   url_for, flash, session, stream_with_context)

import auth
from config import (settings, save_overrides, known_models,
                    persistent_secret_key, active_api_key)
from search import web_search
from crawler import crawl_urls
from storage import (
    init_db, insert_document, insert_search, insert_extraction,
    get_document, get_documents_by_search, get_all_documents,
    get_extractions_for_document, get_search_history,
    count_documents, count_searches, count_extractions, count_domains,
    get_doc_ids_with_extractions, get_search_history_enriched,
    get_related_documents, search_documents_fts,
    insert_agent, get_agent, list_agents, update_agent, delete_agent,
    insert_mission, update_mission, get_mission, list_missions,
    get_requirements_for_mission, get_mission_documents,
    get_missions_enriched, get_distinct_search_queries,
    insert_requirement, update_requirement, delete_requirement,
    get_requirement_documents, get_agent_track_records,
    reconcile_interrupted_missions, delete_mission,
)
from jobs import (
    create_job, get_job, job_state, run_job_in_background,
    get_sidebar_jobs, get_in_memory_job_ids, create_mission_job,
    request_cancel, JobLimitReached,
)
from agent_runner import start_planning, start_collection
from brief import ordered_sources, linkify_citations
from scheduler import (start_scheduler, sync_agent_jobs, validate_cron,
                       describe_next_run, scheduled_jobs,
                       run_scheduled_agent_now)
from markdown_render import render_markdown, to_plain_text
from models import SearchRecord, Agent, Mission, Requirement
from prompt_templates import build_persona

app = Flask(__name__)
# Stable across restarts (generated once into the data volume) so login
# sessions and flash messages survive a redeploy.
app.secret_key = persistent_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
if settings.quarry_behind_proxy:
    # TLS-proxy deployment mode: trust one proxy hop so the login rate limiter
    # sees real client IPs (not the proxy collapsing everyone into one bucket),
    # and mark the session cookie Secure since TLS terminates upstream.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config["SESSION_COOKIE_SECURE"] = True


def insecure_exposure() -> bool:
    """True when the compose publish interface is non-loopback but no password
    is set — the one-line footgun the security audit flagged."""
    bind = (settings.quarry_bind or "127.0.0.1").strip()
    return bind not in ("127.0.0.1", "localhost", "::1", "") and not auth.enabled()


if insecure_exposure():
    print("=" * 70 + f"\n[SECURITY] QUARRY_BIND={settings.quarry_bind} exposes this app "
          "beyond localhost WITHOUT a password.\n[SECURITY] Set QUARRY_PASSWORD in .env "
          "or revert QUARRY_BIND to 127.0.0.1.\n" + "=" * 70,
          file=sys.stderr, flush=True)


@app.context_processor
def inject_globals():
    try:
        doc_ct = run_async(count_documents())
        search_ct = run_async(count_searches())
        extract_ct = run_async(count_extractions())
        llm_provider = settings.llm_provider
        llm_model = llm_provider.split("/")[-1] if "/" in llm_provider else llm_provider
        llm_vendor = llm_provider.split("/")[0] if "/" in llm_provider else llm_provider
        fast = settings.llm_provider_fast
        llm_fast_model = (fast.split("/")[-1] if "/" in fast else fast) if fast else "—"
        sidebar = get_sidebar_jobs()
        return dict(
            doc_count=doc_ct,
            search_count=search_ct,
            extraction_count=extract_ct,
            llm_vendor=llm_vendor.title(),
            llm_model=llm_model,
            llm_fast_model=llm_fast_model,
            default_results=settings.search_max_results,
            live_job=sidebar["live"],
            previous_jobs=sidebar["previous"],
            auth_enabled=auth.enabled(),
            insecure_exposure=insecure_exposure(),
        )
    except Exception:
        return dict(
            doc_count=0, search_count=0, extraction_count=0,
            llm_vendor="Cohere", llm_model="command-a-03-2025",
            llm_fast_model="—", default_results=5,
            live_job=None, previous_jobs=[],
            auth_enabled=auth.enabled(),
            insecure_exposure=insecure_exposure(),
        )


def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


@app.url_defaults
def _static_cache_bust(endpoint, values):
    """Stamp static URLs with the file's mtime so a deploy can't leave users on
    a cached stylesheet (CSS changes otherwise need a manual hard refresh)."""
    if endpoint == "static" and "filename" in values:
        try:
            values["v"] = int(os.stat(
                os.path.join(app.static_folder, values["filename"])).st_mtime)
        except OSError:
            pass


@app.before_request
def block_cross_site_posts():
    """CSRF defense via origin checking (no tokens needed): browsers label
    cross-site requests with Sec-Fetch-Site / a mismatching Origin, so a
    malicious page can't blind-POST to this (unauthenticated, localhost-bound)
    app. Same-origin form posts and non-browser clients are unaffected."""
    if request.method != "POST":
        return None
    sfs = request.headers.get("Sec-Fetch-Site")
    if sfs and sfs not in ("same-origin", "same-site", "none"):
        return "Cross-site POST blocked", 403
    origin = request.headers.get("Origin")
    if origin:
        # "Origin: null" comes from sandboxed iframes / opaque origins — never
        # from a legitimate same-origin form post. Treat it as cross-site.
        from urllib.parse import urlparse
        if origin == "null" or urlparse(origin).netloc != request.host:
            return "Cross-site POST blocked", 403
    return None


_init_db_lock = threading.Lock()


@app.before_request
def ensure_db():
    # Double-checked lock: under gunicorn's threaded worker, concurrent first
    # requests must not run init_db (and its FTS rebuild) twice in parallel.
    if not getattr(app, '_db_initialized', False):
        with _init_db_lock:
            if not getattr(app, '_db_initialized', False):
                run_async(init_db())
                # Safe here: no collection worker can be running yet in this
                # process, so any mission still in an in-flight state is one
                # whose thread died with a previous process.
                stale = run_async(reconcile_interrupted_missions())
                if stale:
                    print(f"[STARTUP] marked {stale} interrupted mission(s) as failed",
                          file=sys.stderr, flush=True)
                # Started after the DB exists. Safe under the single gunicorn
                # worker; the Flask reloader would otherwise start two.
                if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
                    start_scheduler()
                app._db_initialized = True


@app.before_request
def require_login():
    """When a password is configured, everything except the login page and
    static assets requires a session. Registered after ensure_db so startup
    reconciliation still runs on the first request either way."""
    if not auth.enabled():
        return None
    if request.endpoint in ("login", "static"):
        return None
    if session.get("authed"):
        return None
    if request.path.startswith("/api/"):
        return {"error": "authentication required"}, 401
    nxt = request.full_path if request.method == "GET" else None
    return redirect(url_for("login", next=nxt))


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth.enabled() or session.get("authed"):
        return redirect(url_for("index"))

    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        locked, wait_s = auth.is_locked_out(ip)
        if locked:
            flash(f"Too many attempts — try again in {wait_s // 60 + 1} minute(s).", "error")
            return render_template("login.html"), 429

        if auth.verify_password(request.form.get("password", "")):
            auth.clear_failures(ip)
            session.clear()  # fresh session id state on privilege change
            session["authed"] = True
            session.permanent = True
            nxt = request.form.get("next", "")
            # Only ever redirect within this app (no open redirect).
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("index")
            return redirect(nxt)

        auth.record_failure(ip)
        flash("Wrong password.", "error")
        return render_template("login.html"), 401

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("login") if auth.enabled() else url_for("index"))


@app.route("/")
def index():
    try:
        stats = {
            "docs": run_async(count_documents()),
            "searches": run_async(count_searches()),
            "extractions": run_async(count_extractions()),
            "domains": run_async(count_domains()),
        }
    except Exception:
        stats = None
    return render_template("index.html", stats=stats, agents=run_async(list_agents()))


@app.route("/search", methods=["POST"])
def search():
    query = request.form.get("query", "").strip()[:500]
    try:
        max_results = max(1, min(20, int(request.form.get("max_results", 5))))
    except (TypeError, ValueError):
        max_results = 5
    extract = request.form.get("extract") == "on"
    extract_prompt = request.form.get("extract_prompt", "").strip()[:5000]

    if not query:
        flash("Please enter a search query.", "error")
        return redirect(url_for("index"))

    try:
        job_id = create_job(query, max_results, extract, extract_prompt)
    except JobLimitReached as e:
        flash(f"Busy: {e}.", "error")
        return redirect(url_for("index"))
    run_job_in_background(job_id)
    return redirect(url_for("crawl_view", job_id=job_id))


@app.route("/crawl/<job_id>")
def crawl_view(job_id):
    job = get_job(job_id)
    if not job:
        flash("Job not found.", "error")
        return redirect(url_for("index"))
    return render_template("crawl.html", job=job)


@app.route("/api/job/<job_id>")
def api_job(job_id):
    state = job_state(job_id)
    if not state:
        return {"error": "not found"}, 404
    return state


@app.route("/api/job/<job_id>/stream")
def api_job_stream(job_id):
    def gen():
        last_hash = None
        # Cap the stream at ~10 minutes to bound resource use
        for _ in range(2400):
            state = job_state(job_id)
            if not state:
                yield 'event: error\ndata: {"error":"not found"}\n\n'
                return
            payload = json.dumps(state)
            h = hashlib.md5(payload.encode()).hexdigest()
            if h != last_hash:
                yield f"data: {payload}\n\n"
                last_hash = h
            if state.get("done"):
                return
            time.sleep(0.25)

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/results/<job_id>")
def results_view(job_id):
    job = get_job(job_id)
    if not job:
        flash("Job not found.", "error")
        return redirect(url_for("index"))

    documents = []
    for doc_id in job.document_ids:
        d = run_async(get_document(doc_id))
        if d:
            documents.append(d)

    ext_ids = run_async(get_doc_ids_with_extractions())
    elapsed_s = int(time.time() - job.started_at)
    total_words = sum((d.word_count or 0) for d in documents)

    return render_template(
        "index.html",
        query=job.query,
        max_results=job.max_results,
        extract=job.extract,
        extract_prompt=job.extract_prompt,
        documents=documents,
        ext_ids=ext_ids,
        agents=run_async(list_agents()),
        active_page="results",
        job_meta={
            "elapsed": elapsed_s,
            "extract_done": job.extract_done,
            "total_words": total_words,
            "crawl_total": job.crawl_total,
        },
    )


@app.route("/document/<doc_id>")
def document_view(doc_id):
    doc = run_async(get_document(doc_id))
    if not doc:
        flash("Document not found.", "error")
        return redirect(url_for("index"))

    extractions = run_async(get_extractions_for_document(doc_id))

    metadata = None
    if doc.metadata_json:
        try:
            metadata = json.dumps(json.loads(doc.metadata_json), indent=2)
        except json.JSONDecodeError:
            metadata = doc.metadata_json

    parsed_extractions = []
    for ext in extractions:
        parsed = None
        try:
            parsed = json.loads(ext.data_json) if ext.data_json else None
        except json.JSONDecodeError:
            parsed = None
        parsed_extractions.append({"ext": ext, "data": parsed})

    reading_min = max(1, round((doc.word_count or 0) / 220))

    related = run_async(get_related_documents(
        doc.id, doc.search_query or "", doc.domain or "", limit=3
    ))

    content_html = render_markdown(doc.content_markdown or "")
    content_fit_html = render_markdown(doc.content_fit) if doc.content_fit else ""
    content_plain = to_plain_text(doc.content_markdown or "")

    return render_template(
        "document.html",
        doc=doc,
        extractions=extractions,
        parsed_extractions=parsed_extractions,
        metadata=metadata,
        reading_min=reading_min,
        related=related,
        content_html=content_html,
        content_fit_html=content_fit_html,
        content_plain=content_plain,
    )


@app.route("/extract/<doc_id>", methods=["GET", "POST"])
def extract_document(doc_id):
    doc = run_async(get_document(doc_id))
    if not doc:
        flash("Document not found.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        prompt = request.form.get("prompt", "").strip()[:5000]
        try:
            from extractor import extract_from_document
            extraction = extract_from_document(doc, prompt)
            if extraction:
                run_async(insert_extraction(extraction))
                flash("Extraction completed successfully.", "success")
            else:
                flash("Extraction returned no results.", "info")
        except Exception as e:
            flash(f"Extraction error: {str(e)}", "error")

    return redirect(url_for("document_view", doc_id=doc_id))


@app.route("/history")
def history():
    searches = run_async(get_search_history_enriched())
    missions = run_async(get_missions_enriched())
    agents = {a.id: a.name for a in run_async(list_agents())}

    events = []
    for s in searches:
        events.append({"kind": "search", "ts": s["executed_at"], **s})
    for m in missions:
        events.append({"kind": "mission", "ts": m["created_at"],
                       "agent_name": agents.get(m["agent_id"], "agent"), **m})
    events.sort(key=lambda e: e["ts"] or "", reverse=True)

    groups = {}
    for e in events:
        day = (e["ts"] or "")[:10]
        groups.setdefault(day, []).append(e)
    grouped = [(day, items) for day, items in groups.items()]
    return render_template(
        "history.html",
        grouped=grouped,
        total=len(events),
        live_job_ids=get_in_memory_job_ids(),
    )


@app.route("/documents")
def documents_list():
    search_filter = request.args.get("search", "").strip()
    mission_filter = request.args.get("mission", "").strip()
    full_text = request.args.get("q", "").strip()[:200]

    mission_obj = None
    if mission_filter:
        # Mission filter takes precedence; full-text within a mission is handled
        # client-side by the title filter.
        mission_obj = run_async(get_mission(mission_filter))
        documents = run_async(get_mission_documents(mission_filter))
    elif full_text:
        documents = run_async(search_documents_fts(full_text, search_filter or None))
    elif search_filter:
        documents = run_async(get_documents_by_search(search_filter))
    else:
        documents = run_async(get_all_documents())

    ext_ids = run_async(get_doc_ids_with_extractions())
    domain_counts = {}
    for d in documents:
        domain_counts[d.domain] = domain_counts.get(d.domain, 0) + 1
    domain_counts = dict(sorted(domain_counts.items(), key=lambda x: -x[1])[:12])

    # Collection selector: one-shot searches + agentic missions.
    search_queries = run_async(get_distinct_search_queries())
    missions = run_async(list_missions())

    return render_template(
        "documents.html",
        documents=documents,
        ext_ids=ext_ids,
        domain_counts=domain_counts,
        search_filter=search_filter,
        mission_filter=mission_filter,
        mission_obj=mission_obj,
        full_text_query=full_text,
        search_queries=search_queries,
        missions=missions,
    )


# --- Agentic collection ---

@app.route("/agents")
def agents_list():
    agents = run_async(list_agents())
    next_runs = {j["agent_id"]: j["next_run"] for j in scheduled_jobs()}
    return render_template("agents.html", agents=agents,
                           records=run_async(get_agent_track_records()),
                           next_runs=next_runs, active_page="agents")


@app.route("/agents/new", methods=["GET", "POST"])
def agent_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:120]
        expertise = request.form.get("expertise", "").strip()[:500]
        if not name or not expertise:
            flash("Name and area of expertise are required.", "error")
            return redirect(url_for("agent_new"))

        def _clamp(field, default, lo, hi):
            try:
                return max(lo, min(hi, int(request.form.get(field, default))))
            except (TypeError, ValueError):
                return default

        max_passes = _clamp("max_passes", 4, 1, 10)
        max_sources = _clamp("max_sources", 30, 1, 100)
        per_req = _clamp("per_req_attempts", 3, 1, 6)
        custom_persona = request.form.get("persona_prompt", "").strip()[:5000]
        persona = custom_persona or build_persona(expertise)

        cron = request.form.get("schedule_cron", "").strip()[:120]
        ok, err = validate_cron(cron)
        if not ok:
            flash(f"That schedule isn't a valid cron expression: {err}", "error")
            return redirect(url_for("agent_new"))
        sched_q = request.form.get("schedule_question", "").strip()[:500]

        agent = Agent(
            id=str(uuid.uuid4()), name=name, expertise=expertise,
            persona_prompt=persona, default_max_passes=max_passes,
            default_max_sources=max_sources, default_per_req_attempts=per_req,
            schedule_cron=cron or None, schedule_question=sched_q or None,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        run_async(insert_agent(agent))
        sync_agent_jobs()
        flash(f"Agent “{name}” created.", "success")
        return redirect(url_for("agents_list"))

    return render_template("agent_form.html", active_page="agents")


@app.route("/agents/<agent_id>/edit", methods=["GET", "POST"])
def agent_edit(agent_id):
    agent = run_async(get_agent(agent_id))
    if not agent:
        flash("Agent not found.", "error")
        return redirect(url_for("agents_list"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()[:120]
        expertise = request.form.get("expertise", "").strip()[:500]
        if not name or not expertise:
            flash("Name and area of expertise are required.", "error")
            return redirect(url_for("agent_edit", agent_id=agent_id))

        def _clamp(field, default, lo, hi):
            try:
                return max(lo, min(hi, int(request.form.get(field, default))))
            except (TypeError, ValueError):
                return default

        # Blank persona regenerates from the (possibly changed) expertise.
        custom_persona = request.form.get("persona_prompt", "").strip()[:5000]
        persona = custom_persona or build_persona(expertise)

        cron = request.form.get("schedule_cron", "").strip()[:120]
        ok, err = validate_cron(cron)
        if not ok:
            flash(f"That schedule isn't a valid cron expression: {err}", "error")
            return redirect(url_for("agent_edit", agent_id=agent_id))
        sched_q = request.form.get("schedule_question", "").strip()[:500]

        run_async(update_agent(
            agent_id,
            name=name, expertise=expertise, persona_prompt=persona,
            schedule_cron=cron or None, schedule_question=sched_q or None,
            default_max_passes=_clamp("max_passes", agent.default_max_passes, 1, 10),
            default_max_sources=_clamp("max_sources", agent.default_max_sources, 1, 100),
            default_per_req_attempts=_clamp("per_req_attempts", agent.default_per_req_attempts, 1, 6),
        ))
        sync_agent_jobs()
        flash(f"Agent “{name}” updated.", "success")
        return redirect(url_for("agents_list"))

    return render_template("agent_form.html", agent=agent, active_page="agents")


@app.route("/agents/<agent_id>/run-scheduled", methods=["POST"])
def agent_run_scheduled(agent_id):
    """Fire an agent's scheduled run immediately — so a morning brief can be
    tested without waiting for its cron window. Same unattended path as the
    scheduler: auto-approves and collects."""
    agent = run_async(get_agent(agent_id))
    if not agent:
        flash("Agent not found.", "error")
        return redirect(url_for("agents_list"))
    if not (agent.schedule_question or "").strip():
        flash("Set a standing question before running the schedule.", "error")
        return redirect(url_for("agent_edit", agent_id=agent_id))
    run_scheduled_agent_now(agent_id)
    flash(f"Running {agent.name}'s scheduled question now — it approves its own plan.",
          "success")
    return redirect(url_for("missions_list"))


@app.route("/agents/<agent_id>/delete", methods=["POST"])
def agent_delete(agent_id):
    agent = run_async(get_agent(agent_id))
    if not agent:
        flash("Agent not found.", "error")
        return redirect(url_for("agents_list"))
    run_async(delete_agent(agent_id))
    sync_agent_jobs()
    flash(f"Agent “{agent.name}” deleted. Its past missions are kept under History.", "success")
    return redirect(url_for("agents_list"))


@app.route("/agents/<agent_id>/run", methods=["POST"])
def agent_run(agent_id):
    agent = run_async(get_agent(agent_id))
    if not agent:
        flash("Agent not found.", "error")
        return redirect(url_for("agents_list"))
    # Accept either "question" (agents page) or "query" (unified search bar).
    question = (request.form.get("question") or request.form.get("query") or "").strip()[:500]
    if not question:
        flash("Enter a question for the agent to research.", "error")
        return redirect(url_for("agents_list"))

    # Optional per-run budget overrides (from the unified bar); default to the
    # agent's saved values.
    def _clamp(field, default, lo, hi):
        try:
            return max(lo, min(hi, int(request.form.get(field, default))))
        except (TypeError, ValueError):
            return default

    max_sources = _clamp("max_sources", agent.default_max_sources, 1, 100)
    max_passes = _clamp("max_passes", agent.default_max_passes, 1, 10)
    per_req = _clamp("per_req_attempts", agent.default_per_req_attempts, 1, 6)
    # LLM extraction applies to the collected sources regardless of mode.
    extract = request.form.get("extract") == "on"
    extract_prompt = request.form.get("extract_prompt", "").strip()[:5000]

    try:
        job_id = create_mission_job(question, max_sources)
    except JobLimitReached as e:
        flash(f"Busy: {e}.", "error")
        return redirect(url_for("agents_list"))
    mission = Mission(
        id=str(uuid.uuid4()), agent_id=agent.id, question=question,
        status="planning", job_id=job_id,
        budget_json=json.dumps({
            "max_passes": max_passes,
            "max_sources": max_sources,
            "per_req_attempts": per_req,
            "extract": extract,
            "extract_prompt": extract_prompt,
        }),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    run_async(insert_mission(mission))
    start_planning(mission.id)
    return redirect(url_for("mission_view", mission_id=mission.id))


@app.route("/missions")
def missions_list():
    # Grouped, not one flat list: a mission awaiting approval is a call to
    # action and must not be buried under finished ones.
    missions = run_async(get_missions_enriched())
    agents = {a.id: a for a in run_async(list_agents())}
    groups = {"needs_you": [], "running": [], "finished": []}
    for m in missions:
        if m["status"] == "awaiting_approval":
            groups["needs_you"].append(m)
        elif m["status"] in ("planning", "collecting", "synthesizing"):
            groups["running"].append(m)
        else:
            groups["finished"].append(m)
    return render_template("missions.html", groups=groups, agents=agents,
                           total=len(missions), active_page="missions")


@app.route("/missions/<mission_id>")
def mission_view(mission_id):
    mission = run_async(get_mission(mission_id))
    if not mission:
        flash("Mission not found.", "error")
        return redirect(url_for("missions_list"))
    requirements = run_async(get_requirements_for_mission(mission_id))
    documents = run_async(get_mission_documents(mission_id))
    agent = run_async(get_agent(mission.agent_id))
    ext_ids = run_async(get_doc_ids_with_extractions())

    # Number the sources exactly as brief.py numbered them for the LLM, so the
    # [n] markers in the brief bind to the right rail entry.
    numbered = list(enumerate(ordered_sources(documents), 1))
    doc_number = {d.id: n for n, d in numbered}

    # Sanitize first (never bypassed), then turn [n] into citation controls.
    brief_html = ""
    if mission.brief_markdown:
        brief_html = linkify_citations(
            render_markdown(mission.brief_markdown), len(numbered))

    # Sources per requirement, carrying their citation number where they have
    # one, plus the queries the agent ran/will run (stored as JSON).
    req_sources, req_queries = {}, {}
    for r in requirements:
        req_sources[r.id] = [
            {"doc": d, "n": doc_number.get(d.id)}
            for d in run_async(get_requirement_documents(mission_id, r.id))
        ]
        try:
            req_queries[r.id] = json.loads(r.next_queries_json or "[]")
        except json.JSONDecodeError:
            req_queries[r.id] = []

    budget = {}
    try:
        budget = json.loads(mission.budget_json or "{}")
    except json.JSONDecodeError:
        budget = {}

    live_state = job_state(mission.job_id) if mission.job_id else None
    return render_template(
        "mission.html", mission=mission, agent=agent,
        requirements=requirements, documents=documents,
        numbered_sources=numbered, req_sources=req_sources,
        req_queries=req_queries,
        brief_html=brief_html, ext_ids=ext_ids, budget=budget,
        live_state=live_state,
        live=mission.job_id in get_in_memory_job_ids(),
        active_page="missions",
    )


@app.route("/missions/<mission_id>/approve", methods=["POST"])
def mission_approve(mission_id):
    mission = run_async(get_mission(mission_id))
    if not mission:
        flash("Mission not found.", "error")
        return redirect(url_for("missions_list"))
    if mission.status != "awaiting_approval":
        flash("This mission is not awaiting approval.", "info")
        return redirect(url_for("mission_view", mission_id=mission_id))

    # The gate lets the user reword requirements, cut them, and edit the seeded
    # queries. JS serializes that into plan_json; with JS off the field is empty
    # and we approve the plan as drafted.
    dropped = 0
    raw_plan = request.form.get("plan_json", "").strip()
    if raw_plan:
        try:
            edits = json.loads(raw_plan)
        except json.JSONDecodeError:
            edits = []
        existing = {r.id: r for r in run_async(get_requirements_for_mission(mission_id))}
        for item in edits if isinstance(edits, list) else []:
            if not isinstance(item, dict):
                continue
            rid = item.get("id")
            if item.get("dropped"):
                if rid in existing:
                    run_async(delete_requirement(rid))
                    dropped += 1
                continue
            title = (item.get("title") or "").strip()[:200]
            queries = [q.strip()[:300] for q in (item.get("queries") or [])
                       if isinstance(q, str) and q.strip()][:8]
            if rid in existing:
                fields = {}
                if title:
                    fields["title"] = title
                if queries:
                    fields["next_queries_json"] = json.dumps(queries)
                if fields:
                    run_async(update_requirement(rid, **fields))
            elif title:
                # A requirement the user added at the gate.
                run_async(insert_requirement(Requirement(
                    id=str(uuid.uuid4()), mission_id=mission_id, title=title,
                    description=(item.get("description") or "").strip()[:1000],
                    rationale="Added by you at the approval gate.",
                    status="pending", attempts=0,
                    next_queries_json=json.dumps(queries or [title]),
                )))

    remaining = run_async(get_requirements_for_mission(mission_id))
    if not remaining:
        flash("A plan needs at least one requirement — nothing was approved.", "error")
        return redirect(url_for("mission_view", mission_id=mission_id))

    # Flip status before spawning the worker so the redirect renders the
    # collecting view immediately (avoids briefly re-showing the approve card).
    run_async(update_mission(mission_id, status="collecting"))
    start_collection(mission_id)
    msg = f"Plan approved — collecting against {len(remaining)} requirements."
    if dropped:
        msg += f" {dropped} dropped."
    flash(msg, "success")
    return redirect(url_for("mission_view", mission_id=mission_id))


@app.route("/missions/<mission_id>/stop", methods=["POST"])
def mission_stop(mission_id):
    """Cooperative stop: the runner finishes the current pass, then synthesizes
    a brief from whatever was collected."""
    mission = run_async(get_mission(mission_id))
    if not mission:
        flash("Mission not found.", "error")
        return redirect(url_for("missions_list"))
    if mission.job_id and request_cancel(mission.job_id):
        flash("Stopping after the current pass — the brief will still be written.", "info")
    else:
        flash("This mission is not running.", "info")
    return redirect(url_for("mission_view", mission_id=mission_id))


@app.route("/missions/<mission_id>/delete", methods=["POST"])
def mission_delete(mission_id):
    mission = run_async(get_mission(mission_id))
    if not mission:
        flash("Mission not found.", "error")
        return redirect(url_for("missions_list"))
    # Deleting a mission out from under its worker would leave the thread
    # writing rows for a mission that no longer exists.
    if mission.status in ("planning", "collecting", "synthesizing") \
            and mission.job_id in get_in_memory_job_ids():
        flash("This mission is still running — stop it first, then delete.", "error")
        return redirect(url_for("mission_view", mission_id=mission_id))
    run_async(delete_mission(mission_id))
    flash("Mission deleted. Its collected sources are still in the Library.", "success")
    return redirect(url_for("missions_list"))


@app.route("/missions/<mission_id>/requirements/<req_id>/retask", methods=["POST"])
def requirement_retask(mission_id, req_id):
    """Reopen a requirement the agent gave up on, with a query the user
    supplies, and put the mission back to work on it."""
    mission = run_async(get_mission(mission_id))
    if not mission:
        flash("Mission not found.", "error")
        return redirect(url_for("missions_list"))
    if mission.status in ("planning", "collecting", "synthesizing"):
        flash("The agent is still working — wait for it to finish before re-tasking.", "info")
        return redirect(url_for("mission_view", mission_id=mission_id))

    req = next((r for r in run_async(get_requirements_for_mission(mission_id))
                if r.id == req_id), None)
    if not req:
        flash("Requirement not found.", "error")
        return redirect(url_for("mission_view", mission_id=mission_id))

    query = request.form.get("query", "").strip()[:300]
    if not query:
        flash("Enter a search query to re-task with.", "error")
        return redirect(url_for("mission_view", mission_id=mission_id))

    try:
        queries = json.loads(req.next_queries_json or "[]")
    except json.JSONDecodeError:
        queries = []
    if query not in queries:
        queries.append(query)

    # A fresh attempt budget — the user explicitly asked for another round.
    run_async(update_requirement(
        req_id, status="pending", attempts=0, accepted_by_user=0,
        next_queries_json=json.dumps(queries[-8:]),
        assessment_missing="", assessment_confidence="",
    ))

    # The old live trace is gone once the process restarts, so give the re-run
    # its own job and point the mission at it.
    budget = {}
    try:
        budget = json.loads(mission.budget_json or "{}")
    except json.JSONDecodeError:
        pass
    try:
        job_id = create_mission_job(mission.question, int(budget.get("max_sources", 30)))
    except JobLimitReached as e:
        flash(f"Busy: {e}.", "error")
        return redirect(url_for("mission_view", mission_id=mission_id))
    run_async(update_mission(mission_id, status="collecting", job_id=job_id, error=None))
    start_collection(mission_id)
    flash(f"Re-tasking “{req.title}” with a fresh attempt budget.", "success")
    return redirect(url_for("mission_view", mission_id=mission_id))


@app.route("/missions/<mission_id>/requirements/<req_id>/accept", methods=["POST"])
def requirement_accept(mission_id, req_id):
    """Override the assessor and accept a requirement's coverage as-is. Recorded
    as a user decision so the UI never implies the assessor was satisfied."""
    mission = run_async(get_mission(mission_id))
    if not mission:
        flash("Mission not found.", "error")
        return redirect(url_for("missions_list"))
    req = next((r for r in run_async(get_requirements_for_mission(mission_id))
                if r.id == req_id), None)
    if not req:
        flash("Requirement not found.", "error")
        return redirect(url_for("mission_view", mission_id=mission_id))
    run_async(update_requirement(req_id, status="satisfied", accepted_by_user=1))
    flash(f"“{req.title}” marked satisfied — recorded as your decision, not the assessor's.",
          "success")
    return redirect(url_for("mission_view", mission_id=mission_id))


@app.route("/missions/<mission_id>/brief.md")
def mission_brief_export(mission_id):
    mission = run_async(get_mission(mission_id))
    if not mission or not mission.brief_markdown:
        flash("No brief to export yet.", "info")
        return redirect(url_for("mission_view", mission_id=mission_id))
    slug = re.sub(r"[^a-z0-9]+", "-", (mission.question or "brief").lower()).strip("-")[:60]
    return Response(
        mission.brief_markdown,
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug or "brief"}.md"'},
    )


@app.route("/api/mission/<mission_id>")
def api_mission(mission_id):
    mission = run_async(get_mission(mission_id))
    if not mission:
        return {"error": "not found"}, 404
    requirements = run_async(get_requirements_for_mission(mission_id))
    state = {
        "id": mission.id,
        "status": mission.status,
        "question": mission.question,
        "error": mission.error,
        "has_brief": bool(mission.brief_markdown),
        "requirements": [
            {"id": r.id, "title": r.title, "status": r.status,
             "attempts": r.attempts,
             "missing": r.assessment_missing or "",
             "confidence": r.assessment_confidence or ""}
            for r in requirements
        ],
        "satisfied": sum(1 for r in requirements if r.status == "satisfied"),
        "unmet": sum(1 for r in requirements if r.status == "unmet"),
        "total": len(requirements),
        "done": mission.status in ("done", "error"),
    }
    if mission.job_id:
        js = job_state(mission.job_id)
        if js:
            state["trace"] = {
                "stage": js["stage"], "elapsed": js["elapsed"],
                "log": js["log"], "urls": js["urls"],
                "pass_num": js["pass_num"], "sources_used": js["sources_used"],
                "cancel_requested": js["cancel_requested"],
            }
    return state


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        vals = {
            "llm_provider": request.form.get("llm_provider", "").strip()[:200] or settings.llm_provider,
            # Fast tier may be intentionally blank (falls back to reasoning).
            "llm_provider_fast": request.form.get("llm_provider_fast", "").strip()[:200],
            "ollama_api_base": request.form.get("ollama_api_base", "").strip()[:300] or settings.ollama_api_base,
        }
        try:
            vals["search_max_results"] = max(1, min(20, int(request.form.get("search_max_results", settings.search_max_results))))
        except (TypeError, ValueError):
            vals["search_max_results"] = settings.search_max_results
        # Only overwrite the API key when a new one is supplied.
        key = request.form.get("llm_api_key", "").strip()
        if key:
            vals["llm_api_key"] = key
        save_overrides(vals)
        flash("Settings saved — applied immediately, no restart needed.", "success")
        return redirect(url_for("settings_page"))

    return render_template(
        "settings.html", s=settings, models=known_models(),
        has_key=bool(active_api_key()), active_page="settings",
    )


if __name__ == "__main__":
    app.run(
        host=settings.flask_host,
        port=settings.flask_port,
        debug=settings.flask_debug,
    )
