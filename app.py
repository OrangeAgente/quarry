import asyncio
import hashlib
import json
import secrets
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, Response, render_template, request, redirect, url_for, flash, stream_with_context

from config import settings
from search import web_search
from crawler import crawl_urls
from storage import (
    init_db, insert_document, insert_search, insert_extraction,
    get_document, get_documents_by_search, get_all_documents,
    get_extractions_for_document, get_search_history,
    count_documents, count_searches, count_extractions, count_domains,
    get_doc_ids_with_extractions, get_search_history_enriched,
    get_related_documents, search_documents_fts,
    insert_agent, get_agent, list_agents, delete_agent,
    insert_mission, update_mission, get_mission, list_missions,
    get_requirements_for_mission, get_mission_documents,
    get_missions_enriched, get_distinct_search_queries,
)
from jobs import (
    create_job, get_job, job_state, run_job_in_background,
    get_sidebar_jobs, get_in_memory_job_ids, create_mission_job,
)
from agent_runner import start_planning, start_collection
from markdown_render import render_markdown, to_plain_text
from models import SearchRecord, Agent, Mission
from prompt_templates import build_persona

app = Flask(__name__)
app.secret_key = settings.flask_secret_key or secrets.token_hex(32)


@app.context_processor
def inject_globals():
    try:
        doc_ct = run_async(count_documents())
        search_ct = run_async(count_searches())
        extract_ct = run_async(count_extractions())
        llm_provider = settings.llm_provider
        llm_model = llm_provider.split("/")[-1] if "/" in llm_provider else llm_provider
        llm_vendor = llm_provider.split("/")[0] if "/" in llm_provider else llm_provider
        sidebar = get_sidebar_jobs()
        return dict(
            doc_count=doc_ct,
            search_count=search_ct,
            extraction_count=extract_ct,
            llm_vendor=llm_vendor.title(),
            llm_model=llm_model,
            live_job=sidebar["live"],
            previous_jobs=sidebar["previous"],
        )
    except Exception:
        return dict(
            doc_count=0, search_count=0, extraction_count=0,
            llm_vendor="Cohere", llm_model="command-a-03-2025",
            live_job=None, previous_jobs=[],
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


@app.before_request
def ensure_db():
    if not getattr(app, '_db_initialized', False):
        run_async(init_db())
        app._db_initialized = True


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

    job_id = create_job(query, max_results, extract, extract_prompt)
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
    return render_template("agents.html", agents=agents, active_page="agents")


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

        agent = Agent(
            id=str(uuid.uuid4()), name=name, expertise=expertise,
            persona_prompt=persona, default_max_passes=max_passes,
            default_max_sources=max_sources, default_per_req_attempts=per_req,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        run_async(insert_agent(agent))
        flash(f"Agent “{name}” created.", "success")
        return redirect(url_for("agents_list"))

    return render_template("agent_form.html", active_page="agents")


@app.route("/agents/<agent_id>/delete", methods=["POST"])
def agent_delete(agent_id):
    agent = run_async(get_agent(agent_id))
    if not agent:
        flash("Agent not found.", "error")
        return redirect(url_for("agents_list"))
    run_async(delete_agent(agent_id))
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

    job_id = create_mission_job(question, max_sources)
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
    missions = run_async(list_missions())
    agents = {a.id: a for a in run_async(list_agents())}
    return render_template("missions.html", missions=missions, agents=agents,
                           active_page="missions")


@app.route("/missions/<mission_id>")
def mission_view(mission_id):
    mission = run_async(get_mission(mission_id))
    if not mission:
        flash("Mission not found.", "error")
        return redirect(url_for("missions_list"))
    requirements = run_async(get_requirements_for_mission(mission_id))
    documents = run_async(get_mission_documents(mission_id))
    agent = run_async(get_agent(mission.agent_id))
    brief_html = render_markdown(mission.brief_markdown) if mission.brief_markdown else ""
    ext_ids = run_async(get_doc_ids_with_extractions())
    return render_template(
        "mission.html", mission=mission, agent=agent,
        requirements=requirements, documents=documents,
        brief_html=brief_html, ext_ids=ext_ids,
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
    # Flip status before spawning the worker so the redirect renders the
    # collecting view immediately (avoids briefly re-showing the approve card).
    run_async(update_mission(mission_id, status="collecting"))
    start_collection(mission_id)
    flash("Collection plan approved — the agent is now collecting.", "success")
    return redirect(url_for("mission_view", mission_id=mission_id))


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
            {"title": r.title, "status": r.status, "attempts": r.attempts}
            for r in requirements
        ],
        "done": mission.status in ("done", "error"),
    }
    if mission.job_id:
        js = job_state(mission.job_id)
        if js:
            state["trace"] = {
                "stage": js["stage"], "elapsed": js["elapsed"],
                "log": js["log"], "urls": js["urls"],
            }
    return state


if __name__ == "__main__":
    app.run(
        host=settings.flask_host,
        port=settings.flask_port,
        debug=settings.flask_debug,
    )
