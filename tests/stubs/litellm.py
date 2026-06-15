"""Minimal stub so modules that `import litellm` are importable in unit tests.
Real LLM calls are monkeypatched in the tests; this is never invoked."""


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


def completion(**kwargs):
    return _Resp("{}")
