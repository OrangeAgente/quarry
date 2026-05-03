import asyncio
import json
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from models import Document, SearchResult
from config import settings


async def crawl_urls(search_results: list[SearchResult], search_query: str) -> list[Document]:
    browser_cfg = BrowserConfig(
        headless=True,
        browser_type="chromium",
    )
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        word_count_threshold=50,
        page_timeout=settings.crawl_timeout,
    )

    documents = []
    urls = [sr.url for sr in search_results]

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        results = await crawler.arun_many(urls=urls, config=run_cfg)

        for result in results:
            if not result.success:
                print(f"Failed to crawl {result.url}: {result.error_message}")
                continue

            parsed = urlparse(result.url)
            markdown_content = ""
            fit_content = ""

            if result.markdown:
                markdown_content = result.markdown.raw_markdown or ""
                fit_content = result.markdown.fit_markdown or ""

            internal_links = len(result.links.get("internal", [])) if result.links else 0
            external_links = len(result.links.get("external", [])) if result.links else 0

            metadata = {}
            if result.metadata:
                metadata = result.metadata if isinstance(result.metadata, dict) else {}

            doc = Document(
                id=str(uuid.uuid4()),
                url=result.url,
                domain=parsed.netloc,
                title=metadata.get("title", parsed.netloc),
                search_query=search_query,
                crawled_at=datetime.now(timezone.utc).isoformat(),
                content_markdown=markdown_content,
                content_fit=fit_content,
                word_count=len(markdown_content.split()) if markdown_content else 0,
                links_internal=internal_links,
                links_external=external_links,
                metadata_json=json.dumps(metadata),
            )
            documents.append(doc)

    return documents


async def crawl_urls_with_progress(search_results: list[SearchResult], search_query: str, job_id: str) -> list[Document]:
    from jobs import update_url, add_log, inc_counter

    browser_cfg = BrowserConfig(headless=True, browser_type="chromium")
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        word_count_threshold=50,
        page_timeout=settings.crawl_timeout,
    )

    documents: list[Document] = []
    semaphore = asyncio.Semaphore(4)

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        async def crawl_one(sr: SearchResult) -> Document | None:
            async with semaphore:
                update_url(job_id, sr.url, status="fetching")
                add_log(job_id, "info", f"fetching <code>{sr.url}</code>")
                try:
                    result = await crawler.arun(url=sr.url, config=run_cfg)
                    if not result.success:
                        msg = (result.error_message or "unknown error")[:140]
                        update_url(job_id, sr.url, status="error", error=msg)
                        inc_counter(job_id, "crawl_done")
                        add_log(job_id, "err", f"failed <code>{sr.url}</code>: {msg}")
                        return None

                    parsed = urlparse(result.url)
                    markdown_content = result.markdown.raw_markdown if result.markdown else ""
                    fit_content = result.markdown.fit_markdown if result.markdown else ""
                    internal_links = len(result.links.get("internal", [])) if result.links else 0
                    external_links = len(result.links.get("external", [])) if result.links else 0
                    metadata = result.metadata if isinstance(result.metadata, dict) else {}
                    title = metadata.get("title") or sr.title or parsed.netloc
                    word_count = len(markdown_content.split()) if markdown_content else 0

                    doc = Document(
                        id=str(uuid.uuid4()),
                        url=result.url,
                        domain=parsed.netloc,
                        title=title,
                        search_query=search_query,
                        crawled_at=datetime.now(timezone.utc).isoformat(),
                        content_markdown=markdown_content,
                        content_fit=fit_content,
                        word_count=word_count,
                        links_internal=internal_links,
                        links_external=external_links,
                        metadata_json=json.dumps(metadata),
                    )

                    update_url(
                        job_id, sr.url,
                        status="done", title=title, words=word_count,
                        links_internal=internal_links, links_external=external_links,
                    )
                    inc_counter(job_id, "crawl_done")
                    add_log(job_id, "ok", f"<em>{title[:80]}</em> · {word_count:,}w")
                    return doc
                except Exception as e:
                    update_url(job_id, sr.url, status="error", error=str(e)[:140])
                    inc_counter(job_id, "crawl_done")
                    add_log(job_id, "err", f"exception on <code>{sr.url}</code>: {str(e)[:120]}")
                    return None

        results = await asyncio.gather(*[crawl_one(sr) for sr in search_results])
        documents = [d for d in results if d is not None]

    return documents
