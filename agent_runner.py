"""Orchestration for agentic collection.

Two background entry points, split by the human approval gate:

    start_planning(mission_id)   -> plan, then status=awaiting_approval
    start_collection(mission_id) -> collect/assess/re-task loop, then brief

Both run in daemon threads (one asyncio loop each) and stream progress into the
in-memory job store so the existing live-log / SSE machinery works unchanged.
Mission status, requirements, and the brief are the durable source of truth in
SQLite; the job store only carries the live trace.
"""
import asyncio
import json
import threading
import traceback
from datetime import datetime, timezone
from html import escape as _esc

import jobs
from jobs import JobUrl
from search import web_search
from crawler import crawl_urls_with_progress
from agent_planner import build_collection_plan
from agent_assessor import assess_requirement
from brief import synthesize_brief
from extractor import extract_from_document
from storage import (
    get_agent, get_mission, update_mission,
    insert_requirement, update_requirement, get_requirements_for_mission,
    upsert_document, link_mission_document, insert_extraction,
    get_requirement_documents, get_mission_documents,
    get_latest_finished_mission, get_prior_mission_urls,
)

PER_QUERY_RESULTS = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Thread launchers ---

def start_planning(mission_id: str) -> None:
    threading.Thread(target=_thread, args=(_run_planning, mission_id), daemon=True).start()


def start_collection(mission_id: str) -> None:
    threading.Thread(target=_thread, args=(_run_collection, mission_id), daemon=True).start()


def _thread(coro_fn, mission_id: str) -> None:
    try:
        asyncio.run(coro_fn(mission_id))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        try:
            asyncio.run(update_mission(mission_id, status="error", error=str(e), finished_at=_now()))
        except Exception:
            pass


# --- Stage 1: planning ---

async def _run_planning(mission_id: str) -> None:
    mission = await get_mission(mission_id)
    if not mission:
        return
    agent = await get_agent(mission.agent_id)
    job_id = mission.job_id

    await update_mission(mission_id, status="planning", started_at=_now())
    if job_id:
        jobs.update_job(job_id, stage="planning")
        jobs.add_log(job_id, "info", f'planning collection for <em>"{_esc(mission.question)}"</em>')

    try:
        requirements = await asyncio.to_thread(
            build_collection_plan, agent, mission_id, mission.question
        )
    except Exception as e:  # noqa: BLE001
        await update_mission(mission_id, status="error", error=str(e), finished_at=_now())
        if job_id:
            jobs.add_log(job_id, "err", f"planning failed: {_esc(str(e))}")
            jobs.update_job(job_id, stage="error", done=True, error=str(e))
        return

    for req in requirements:
        await insert_requirement(req)

    plan_summary = [{"title": r.title, "description": r.description} for r in requirements]
    await update_mission(
        mission_id, status="awaiting_approval", plan_json=json.dumps(plan_summary)
    )
    if job_id:
        jobs.add_log(job_id, "ok",
                     f"plan ready: <em>{len(requirements)}</em> requirements — awaiting approval")
        jobs.update_job(job_id, stage="awaiting_approval")


# --- Stage 2: collection loop ---

async def _run_collection(mission_id: str) -> None:
    mission = await get_mission(mission_id)
    if not mission:
        return
    agent = await get_agent(mission.agent_id)
    job_id = mission.job_id
    budget = json.loads(mission.budget_json or "{}")
    max_passes = int(budget.get("max_passes", agent.default_max_passes if agent else 4))
    max_sources = int(budget.get("max_sources", agent.default_max_sources if agent else 30))
    per_req_attempts = int(budget.get("per_req_attempts", agent.default_per_req_attempts if agent else 3))

    await update_mission(mission_id, status="collecting")
    if job_id:
        jobs.update_job(job_id, stage="collecting")
        jobs.add_log(job_id, "info",
                     f"collecting · budget {max_sources} sources / {max_passes} passes")

    collected: dict[str, str] = {}   # url -> doc_id (this mission)
    job_urls: set[str] = set()       # urls already shown in the live trace

    # Fair share of the source budget per requirement, so one greedy
    # requirement can't starve the rest within a pass. The global max_sources
    # remains the hard ceiling.
    all_reqs = await get_requirements_for_mission(mission_id)
    per_req_cap = max(1, max_sources // max(1, len(all_reqs)))

    for pass_num in range(1, max_passes + 1):
        reqs = await get_requirements_for_mission(mission_id)
        pending = [r for r in reqs if r.status == "pending"]
        if not pending:
            break
        if job_id:
            jobs.add_log(job_id, "info",
                         f"pass <em>{pass_num}</em> · {len(pending)} open requirements")

        for req in pending:
            if len(collected) >= max_sources:
                break
            if req.attempts >= per_req_attempts:
                await update_requirement(req.id, status="unmet")
                if job_id:
                    jobs.add_log(job_id, "warn", f"unmet (capped): <em>{_esc(req.title)}</em>")
                continue

            queries = json.loads(req.next_queries_json or "[]") or [mission.question]
            if job_id:
                jobs.add_log(job_id, "info", f"collecting: <em>{_esc(req.title)}</em>")

            # Search across this requirement's queries.
            results = []
            seen_q_urls: set[str] = set()
            for q in queries:
                for sr in await asyncio.to_thread(web_search, q, PER_QUERY_RESULTS):
                    if sr.url not in seen_q_urls:
                        seen_q_urls.add(sr.url)
                        results.append(sr)

            # New URLs to crawl, bounded by this requirement's fair share and
            # the remaining global source budget.
            remaining = max_sources - len(collected)
            cap = max(0, min(per_req_cap, remaining))
            to_crawl = [sr for sr in results if sr.url not in collected][:cap]

            if to_crawl:
                fresh_for_trace = [JobUrl(url=sr.url, title=sr.title or sr.url)
                                   for sr in to_crawl if sr.url not in job_urls]
                if job_id and fresh_for_trace:
                    jobs.add_urls(job_id, fresh_for_trace)
                    job_urls.update(u.url for u in fresh_for_trace)
                docs = await crawl_urls_with_progress(to_crawl, mission.question, job_id or "")
                for doc in docs:
                    doc_id = await upsert_document(doc)
                    collected[doc.url] = doc_id
                    await link_mission_document(mission_id, req.id, doc_id)

            # Link any already-collected URLs that resurfaced for this requirement,
            # so assessment sees the full picture.
            for sr in results:
                if sr.url in collected:
                    await link_mission_document(mission_id, req.id, collected[sr.url])

            req_docs = await get_requirement_documents(mission_id, req.id)
            assessment = await asyncio.to_thread(assess_requirement, req, req_docs)
            attempts = req.attempts + 1

            if assessment.satisfied:
                await update_requirement(
                    req.id, status="satisfied", attempts=attempts,
                    satisfied_doc_ids_json=json.dumps([d.id for d in req_docs]),
                )
                if job_id:
                    jobs.add_log(job_id, "ok",
                                 f"satisfied: <em>{_esc(req.title)}</em> · {len(req_docs)} sources")
            else:
                next_q = assessment.next_queries or queries
                status = "unmet" if attempts >= per_req_attempts else "pending"
                await update_requirement(
                    req.id, status=status, attempts=attempts,
                    next_queries_json=json.dumps(next_q),
                )
                if job_id:
                    label = "unmet (capped)" if status == "unmet" else "gap remains"
                    jobs.add_log(job_id, "warn",
                                 f"{label} ({assessment.confidence}): <em>{_esc(req.title)}</em>")

        if len(collected) >= max_sources:
            if job_id:
                jobs.add_log(job_id, "warn", "source budget reached")
            break

    # Anything still pending after the pass budget is an unmet gap.
    for r in await get_requirements_for_mission(mission_id):
        if r.status == "pending":
            await update_requirement(r.id, status="unmet")

    # Optional LLM extraction over the collected sources (applies regardless of
    # how they were gathered).
    if budget.get("extract"):
        await _extract_sources(mission_id, budget.get("extract_prompt", ""), job_id)

    await _synthesize(mission_id, agent, job_id)


async def _extract_sources(mission_id: str, prompt: str, job_id) -> None:
    docs = await get_mission_documents(mission_id)
    if job_id:
        jobs.add_log(job_id, "info",
                     f"extracting structured data from <em>{len(docs)}</em> sources")
    done = 0
    for doc in docs:
        ext = await asyncio.to_thread(extract_from_document, doc, prompt)
        if ext:
            await insert_extraction(ext)
            done += 1
    if job_id:
        jobs.add_log(job_id, "ok", f"extraction complete · <em>{done}/{len(docs)}</em>")


async def _synthesize(mission_id: str, agent, job_id) -> None:
    await update_mission(mission_id, status="synthesizing")
    if job_id:
        jobs.update_job(job_id, stage="synthesizing")
        jobs.add_log(job_id, "info", "synthesizing brief")

    mission = await get_mission(mission_id)
    requirements = await get_requirements_for_mission(mission_id)
    docs = await get_mission_documents(mission_id)

    # Delta: only when there is a prior finished run for the same agent+question.
    new_urls: set[str] = set()
    prior = await get_latest_finished_mission(mission.agent_id, mission.question, mission_id)
    if prior:
        prior_urls = await get_prior_mission_urls(mission_id)
        new_urls = {d.url for d in docs} - prior_urls

    brief_md = await asyncio.to_thread(synthesize_brief, mission, requirements, docs, new_urls)

    n_sat = sum(1 for r in requirements if r.status == "satisfied")
    await update_mission(
        mission_id, status="done", brief_markdown=brief_md, finished_at=_now()
    )
    if job_id:
        jobs.add_log(job_id, "ok",
                     f"done · {n_sat}/{len(requirements)} requirements satisfied · "
                     f"{len(docs)} sources")
        jobs.update_job(job_id, stage="done", done=True)
