"""Mirrors the real ddgs exception surface used by search.py."""


class DDGSException(Exception):
    pass


class RatelimitException(DDGSException):
    pass
