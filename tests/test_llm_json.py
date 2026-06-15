from llm import _extract_json


def test_plain_json():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_fenced_json():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_with_prose():
    assert _extract_json('Sure, here: {"a": 1} — done') == {"a": 1}


def test_no_json():
    assert _extract_json("no json at all") is None
