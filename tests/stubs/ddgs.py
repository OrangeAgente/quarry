"""Stub for the ddgs search package, for import-wiring tests."""


class DDGS:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def text(self, query, max_results=5):
        return iter([])
