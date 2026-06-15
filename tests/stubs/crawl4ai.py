"""Stub for crawl4ai, for import-wiring tests (no real browser)."""


class AsyncWebCrawler:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def arun(self, *a, **k):
        raise NotImplementedError

    async def arun_many(self, *a, **k):
        return []


class BrowserConfig:
    def __init__(self, *a, **k):
        pass


class CrawlerRunConfig:
    def __init__(self, *a, **k):
        pass


class CacheMode:
    BYPASS = "bypass"
