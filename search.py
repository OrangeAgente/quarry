from ddgs import DDGS
from models import SearchResult


def web_search(query: str, max_results: int = 5) -> list[SearchResult]:
    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append(SearchResult(
                url=r.get("href") or r.get("url") or "",
                title=r.get("title", ""),
                snippet=r.get("body", ""),
            ))
    return [r for r in results if r.url]
