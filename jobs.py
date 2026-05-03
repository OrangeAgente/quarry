import asyncio
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse


@dataclass
class JobUrl:
    url: str
    title: str = ""
    status: str = "pending"
    words: int = 0
    links_internal: int = 0
    links_external: int = 0
    error: Optional[str] = None


@dataclass
class JobLog:
    t: float
    level: str
    msg: str


@dataclass
class Job:
    id: str
    query: str
    max_results: int
    extract: bool
    extract_prompt: str
    started_at: float
    stage: str = "search"
    search_total: int = 0
    crawl_total: int = 0
    crawl_done: int = 0
    extract_total: int = 0
    extract_done: int = 0
    urls: list = field(default_factory=list)
    log: list = field(default_factory=list)
    document_ids: list = field(default_factory=list)
    error: Optional[str] = None
    done: bool = False


_store: dict[str, Job] = {}
_lock = threading.Lock()


def create_job(query: str, max_results: int, extract: bool, extract_prompt: str) -> str:
    job = Job(
        id=str(uuid.uuid4()),
        query=query,
        max_results=max_results,
        extract=extract,
        extract_prompt=extract_prompt,
        started_at=time.time(),
    )
    with _lock:
        _store[job.id] = job
    return job.id


def get_job(job_id: str) -> Optional[Job]:
    with _lock:
        return _store.get(job_id)


def update_job(job_id: str, **kwargs) -> None:
    with _lock:
        j = _store.get(job_id)
        if not j:
            return
        for k, v in kwargs.items():
            setattr(j, k, v)


def inc_counter(job_id: str, field_name: str, by: int = 1) -> None:
    with _lock:
        j = _store.get(job_id)
        if not j:
            return
        setattr(j, field_name, getattr(j, field_name) + by)


def add_log(job_id: str, level: str, msg: str) -> None:
    with _lock:
        j = _store.get(job_id)
        if not j:
            return
        j.log.append(JobLog(round(time.time() - j.started_at, 2), level, msg))


def add_urls(job_id: str, urls: list[JobUrl]) -> None:
    with _lock:
        j = _store.get(job_id)
        if not j:
            return
        j.urls.extend(urls)


def update_url(job_id: str, url: str, **kwargs) -> None:
    with _lock:
        j = _store.get(job_id)
        if not j:
            return
        for u in j.urls:
            if u.url == url:
                for k, v in kwargs.items():
                    setattr(u, k, v)
                return


def set_document_ids(job_id: str, ids: list[str]) -> None:
    with _lock:
        j = _store.get(job_id)
        if not j:
            return
        j.document_ids = ids


def job_state(job_id: str) -> Optional[dict]:
    with _lock:
        j = _store.get(job_id)
        if not j:
            return None
        return {
            "id": j.id,
            "query": j.query,
            "stage": j.stage,
            "elapsed": round(time.time() - j.started_at, 1),
            "search_total": j.search_total,
            "crawl_total": j.crawl_total,
            "crawl_done": j.crawl_done,
            "extract": j.extract,
            "extract_total": j.extract_total,
            "extract_done": j.extract_done,
            "urls": [asdict(u) for u in j.urls],
            "log": [asdict(l) for l in j.log[-200:]],
            "done": j.done,
            "error": j.error,
            "document_ids": list(j.document_ids),
        }


def run_job_in_background(job_id: str) -> None:
    t = threading.Thread(target=_run_thread, args=(job_id,), daemon=True)
    t.start()


def _run_thread(job_id: str) -> None:
    try:
        asyncio.run(_run_job(job_id))
    except Exception as e:
        traceback.print_exc()
        update_job(job_id, stage="error", done=True, error=str(e))
        add_log(job_id, "err", f"worker crashed: {e}")


async def _run_job(job_id: str) -> None:
    from search import web_search
    from crawler import crawl_urls_with_progress
    from storage import insert_document, insert_search, insert_extraction
    from models import SearchRecord
    from extractor import extract_from_document

    job = get_job(job_id)
    if not job:
        return

    try:
        update_job(job_id, stage="search")
        add_log(job_id, "info", f'searching duckduckgo for <em>"{job.query}"</em>')
        search_results = web_search(job.query, max_results=job.max_results)
        update_job(job_id, search_total=len(search_results))

        if not search_results:
            update_job(job_id, stage="done", done=True, error="no search results")
            add_log(job_id, "warn", "no results")
            return

        domain_set = {urlparse(r.url).netloc for r in search_results}
        add_log(job_id, "ok", f"got <em>{len(search_results)}</em> results across <em>{len(domain_set)}</em> domains")

        add_urls(job_id, [JobUrl(url=sr.url, title=sr.title or sr.url) for sr in search_results])
        update_job(job_id, stage="crawl", crawl_total=len(search_results))
        add_log(job_id, "info", f"opening headless chromium · concurrency <em>4</em>")

        documents = await crawl_urls_with_progress(search_results, job.query, job_id)

        for doc in documents:
            await insert_document(doc)
        set_document_ids(job_id, [d.id for d in documents])
        add_log(job_id, "ok", f"stored <em>{len(documents)}</em> documents")

        search_record = SearchRecord(
            id=str(uuid.uuid4()),
            query=job.query,
            executed_at=datetime.now(timezone.utc).isoformat(),
            result_count=len(documents),
        )
        await insert_search(search_record)

        if job.extract and documents:
            update_job(job_id, stage="extract", extract_total=len(documents))
            add_log(job_id, "info", f"running llm extraction on <em>{len(documents)}</em> documents")
            for doc in documents:
                add_log(job_id, "info", f"extracting <em>{(doc.title or doc.domain)[:70]}</em>")
                extraction = extract_from_document(doc, job.extract_prompt)
                if extraction:
                    await insert_extraction(extraction)
                    add_log(job_id, "ok", f"extracted <code>{doc.domain}</code>")
                else:
                    add_log(job_id, "warn", f"no extraction for <code>{doc.domain}</code>")
                inc_counter(job_id, "extract_done")

        update_job(job_id, stage="done", done=True)
        add_log(job_id, "ok", "agent finished")
    except Exception as e:
        traceback.print_exc()
        update_job(job_id, stage="error", done=True, error=str(e))
        add_log(job_id, "err", f"job failed: {e}")
